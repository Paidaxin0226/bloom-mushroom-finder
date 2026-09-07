from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


PAUSE_REQUEST = "pause.request"
STOP_REQUEST = "stop.request"
STOP_ACCEPTED = "stop.accepted"
PAUSE_ACCEPTED = "pause.accepted"
STATUS_FILE = "status.json"
STATUS_RETRY_DELAYS = (0.01, 0.02, 0.04, 0.08, 0.16, 0.25, 0.25)
LOGGER = logging.getLogger(__name__)


@dataclass
class TrainingControlSignal(Exception):
    """Raised only after Ultralytics has safely written the current checkpoint."""

    action: str
    completed_epoch: int

    def __str__(self) -> str:
        return f"{self.action} requested after epoch {self.completed_epoch}"


class TrainingControl:
    def __init__(self, directory: Path) -> None:
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._status_warning_reported = False

    @property
    def status_path(self) -> Path:
        return self.directory / STATUS_FILE

    def clear_requests(self) -> None:
        for filename in (PAUSE_REQUEST, STOP_REQUEST, STOP_ACCEPTED, PAUSE_ACCEPTED):
            path = self.directory / filename
            if path.is_file():
                path.unlink()

    def request(self, action: str) -> Path:
        if action not in {"pause", "stop"}:
            raise ValueError(f"不支援的訓練控制：{action}")
        filename = PAUSE_REQUEST if action == "pause" else STOP_REQUEST
        path = self.directory / filename
        # Publish only a closed file so the worker can atomically claim it on Windows.
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory, suffix=".request.tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(datetime.now().isoformat(timespec="seconds"))
        try:
            temporary.replace(path)
            if action == "stop":
                # An explicit stop supersedes any earlier pause; withdrawing the
                # stop must not leave a hidden pause that still ends the run.
                (self.directory / PAUSE_REQUEST).unlink(missing_ok=True)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    def requested_action(self) -> str | None:
        # 安全停止優先於暫停，避免兩個按鈕先後被按下時誤判。
        if (self.directory / STOP_REQUEST).is_file() or self.stop_accepted:
            return "stop"
        if (self.directory / PAUSE_REQUEST).is_file() or (self.directory / PAUSE_ACCEPTED).is_file():
            return "pause"
        return None

    @property
    def stop_accepted(self) -> bool:
        return (self.directory / STOP_ACCEPTED).is_file()

    @property
    def request_accepted(self) -> bool:
        return self.stop_accepted or (self.directory / PAUSE_ACCEPTED).is_file()

    def cancel_stop(self) -> None:
        """Unlink competes atomically with the worker's checkpoint-time rename."""
        if self.request_accepted:
            raise RuntimeError("安全停止已生效，請等待停止完成後再繼續訓練。")
        try:
            (self.directory / STOP_REQUEST).unlink()
        except FileNotFoundError:
            # No file also occurs when cancelled before the runner sends it.
            if self.request_accepted:
                raise RuntimeError("安全停止已生效，請等待停止完成後再繼續訓練。")

    def write_status(self, state: str, *, required: bool = True, **details: Any) -> bool:
        """Atomically publish status, retrying transient Windows file locks.

        Progress is advisory (required=False); completion/checkpoint states remain
        required so a failed durable write cannot masquerade as a safe pause.
        """
        payload = {
            "state": state,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "pid": os.getpid(),
            **details,
        }
        serialized = json.dumps(payload, ensure_ascii=False, indent=2)
        temporary: Path | None = None
        try:
            # A unique path avoids collisions between writers in the same process.
            # Close the handle before replace, which Windows otherwise rejects.
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.directory,
                prefix=f".{STATUS_FILE}.{os.getpid()}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(serialized)
            for attempt in range(len(STATUS_RETRY_DELAYS) + 1):
                try:
                    temporary.replace(self.status_path)
                    self._status_warning_reported = False
                    return True
                except OSError as exc:
                    locked = isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {5, 32, 33}
                    if not locked or attempt == len(STATUS_RETRY_DELAYS):
                        raise
                    time.sleep(STATUS_RETRY_DELAYS[attempt])
        except OSError as exc:
            if required:
                raise
            if not self._status_warning_reported:
                LOGGER.warning(
                    "無法更新訓練狀態檔 %s；保留先前狀態，本次訊息仍會顯示在程序輸出。原因：%s",
                    self.status_path,
                    exc,
                )
                self._status_warning_reported = True
            return False
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    # Cleanup must not hide the original persistence failure.
                    pass
        return False

    def read_status(self) -> dict[str, Any]:
        try:
            return json.loads(self.status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def checkpoint_callback(self, trainer: Any) -> None:
        """Stop by exception after save_model() has closed last.pt.

        Letting Ultralytics finish its normal stop path would strip optimizer state
        from last.pt. Raising here deliberately skips final weight stripping, so the
        saved checkpoint remains resumable.
        """
        action = self.requested_action()
        if action is None:
            return
        completed_epoch = int(getattr(trainer, "epoch", -1)) + 1
        total_epochs = int(getattr(trainer, "epochs", 0))
        # 最後一輪已經完成時讓 Ultralytics 正常收尾，不再把「暫停」誤報成未完成。
        if total_epochs and completed_epoch >= total_epochs:
            return
        request_file = STOP_REQUEST if action == "stop" else PAUSE_REQUEST
        accepted_file = STOP_ACCEPTED if action == "stop" else PAUSE_ACCEPTED
        try:
            (self.directory / request_file).replace(self.directory / accepted_file)
        except FileNotFoundError:
            # Cancellation or a superseding request won before this checkpoint.
            return
        final_state = "paused" if action == "pause" else "stopped"
        self.write_status(
            final_state,
            completed_epoch=completed_epoch,
            total_epochs=total_epochs,
            checkpoint=str(Path(getattr(trainer, "last", "")).resolve()),
            message=(
                "本輪 checkpoint 已保存，可安全繼續訓練。"
                if action == "pause"
                else "本輪 checkpoint 已保存，訓練已安全停止。"
            ),
        )
        raise TrainingControlSignal(action, completed_epoch)
