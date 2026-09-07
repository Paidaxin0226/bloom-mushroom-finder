from __future__ import annotations

import argparse
import csv
import json
import sys
import traceback
from copy import deepcopy
from pathlib import Path

from .artifacts import ArtifactManager
from .dataset_builder import load_config
from .paths import CONFIG_PATH, TRAINING_CONTROL_DIR
from .train import describe_training_plan, generated_dataset_summary, resolve_dataset, train
from .training_control import TrainingControl, TrainingControlSignal


DEFAULT_CONTROL_DIR = TRAINING_CONTROL_DIR


def is_dataloader_worker_failure(error: BaseException) -> bool:
    message = f"{type(error).__name__}: {error}".casefold()
    return "dataloader worker" in message and (
        "exited unexpectedly" in message or "killed" in message
    )


def completed_epochs(run_dir: Path) -> int:
    results_path = run_dir / "results.csv"
    if not results_path.is_file():
        return 0
    try:
        with results_path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        return max(
            (int(float(row["epoch"])) for row in rows if row.get("epoch")),
            default=0,
        )
    except (OSError, ValueError, KeyError, csv.Error):
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="可由 GUI 安全暫停的 YOLO 訓練程序")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--control-dir", type=Path, default=DEFAULT_CONTROL_DIR)
    parser.add_argument("--model", type=Path, help="這一階段使用的起始模型")
    parser.add_argument("--run-name", help="這一階段的訓練 run 名稱")
    parser.add_argument("--no-resume", action="store_true", help="強制建立新的訓練 run")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    control = TrainingControl(args.control_dir)
    try:
        control.write_status("starting", required=False, message="正在讀取訓練設定…")
        print("訓練程序已啟動，正在讀取設定…", flush=True)
        config = load_config(args.config)
        requested_model = getattr(args, "model", None)
        run_name = getattr(args, "run_name", None)
        no_resume = bool(getattr(args, "no_resume", False))
        if requested_model is not None:
            model_path = requested_model.resolve()
            if not model_path.is_file():
                raise FileNotFoundError(f"找不到起始模型：{model_path}")
            config["training"] = dict(config["training"])
            config["training"]["model"] = str(model_path)
        dataset_dir = resolve_dataset(config, args.dataset)
        print(f"使用專案生成資料集，略過圖片與標註檢查：{dataset_dir}", flush=True)
        summary = generated_dataset_summary(dataset_dir)
        plan = describe_training_plan(config, dataset_dir, allow_resume=not no_resume)
        manager = ArtifactManager(config)
        if plan["resume_checkpoint"]:
            manager.register_training_run(
                Path(str(plan["resume_checkpoint"])).resolve().parent.parent,
                dataset_dir,
                status="running",
                completed_epochs=int(plan["completed_epochs"]),
                total_epochs=int(plan["total_epochs"]),
            )
        control.write_status(
            "running",
            required=False,
            dataset=str(dataset_dir),
            completed_epoch=int(plan["completed_epochs"]),
            total_epochs=int(plan["total_epochs"]),
            checkpoint=plan["resume_checkpoint"],
            class_count=int(summary["class_count"]),
            message=(
                f"正在從第 {int(plan['completed_epochs']) + 1} 輪繼續訓練。"
                if plan["resume_checkpoint"]
                else "正在開始新的訓練。"
            ),
        )
        print("資料集生成摘要：", flush=True)
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
        print(
            "暫停採安全 checkpoint 模式：按下暫停後，會完成目前 epoch 並保存 last.pt。",
            flush=True,
        )
        print("正在載入 PyTorch／YOLO 並初始化訓練裝置…", flush=True)
        try:
            save_dir = train(
                config,
                dataset_dir,
                allow_resume=not no_resume,
                run_name=run_name,
                callbacks={"on_model_save": control.checkpoint_callback},
            )
        except Exception as exc:
            if int(plan["workers"]) <= 0 or not is_dataloader_worker_failure(exc):
                raise
            fallback_config = deepcopy(config)
            fallback_config["training"]["workers"] = 0
            message = (
                "DataLoader 子程序異常結束；正在從最新 checkpoint "
                "自動改用 workers=0 繼續。"
            )
            print(message, flush=True)
            control.write_status(
                "running",
                required=False,
                dataset=str(dataset_dir),
                completed_epoch=int(plan["completed_epochs"]),
                total_epochs=int(plan["total_epochs"]),
                checkpoint=plan["resume_checkpoint"],
                message=message,
            )
            save_dir = train(
                fallback_config,
                dataset_dir,
                # The first attempt already wrote last.pt. Resume that exact run
                # with workers=0 instead of creating a colliding named run.
                allow_resume=True,
                run_name=None,
                callbacks={"on_model_save": control.checkpoint_callback},
            )
        finished_epochs = completed_epochs(save_dir)
        control.write_status(
            "completed",
            dataset=str(dataset_dir),
            completed_epoch=finished_epochs,
            total_epochs=int(plan["total_epochs"]),
            checkpoint=str(save_dir / "weights" / "best.pt"),
            message=(
                "訓練已全部完成。"
                if finished_epochs >= int(plan["total_epochs"])
                else f"訓練已在第 {finished_epochs} 輪依 early stopping 規則完成。"
            ),
        )
        return 0
    except TrainingControlSignal as signal:
        # checkpoint_callback 已先以原子方式寫入 paused/stopped 狀態。
        status = control.read_status()
        checkpoint = Path(str(status.get("checkpoint", "")))
        if checkpoint.is_file():
            manager.register_training_run(
                checkpoint.resolve().parent.parent,
                dataset_dir,
                status=str(status.get("state", "paused")),
                completed_epochs=signal.completed_epoch,
                total_epochs=int(status.get("total_epochs", 0) or 0),
            )
            manager.refresh_catalog()
        control.clear_requests()
        label = "暫停" if signal.action == "pause" else "安全停止"
        print(f"訓練已{label}於第 {signal.completed_epoch} 個 epoch。", flush=True)
        return 0
    except Exception as exc:
        control.write_status(
            "error",
            required=False,
            message=str(exc),
            traceback=traceback.format_exc(),
        )
        print(f"錯誤：{exc}", file=sys.stderr, flush=True)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
