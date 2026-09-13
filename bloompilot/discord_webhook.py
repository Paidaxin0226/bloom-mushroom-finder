"""Background Discord notifications, readable embeds and cropped image uploads."""

from __future__ import annotations

import base64
import binascii
import copy
import json
import queue
import re
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


DISCORD_WEBHOOK_SECTION = "discord_webhook"
_DISCORD_WEBHOOK_HOSTS = {
    "discord.com",
    "ptb.discord.com",
    "canary.discord.com",
    "discordapp.com",
}
_DISCORD_WEBHOOK_PATH = re.compile(
    r"^/api(?:/v\d+)?/webhooks/[0-9]{5,30}/[A-Za-z0-9._-]{20,200}/?$"
)


class DiscordWebhookError(RuntimeError):
    """A safe, user-facing Discord delivery error."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class DiscordAttachment:
    filename: str
    content_type: str
    data: bytes = field(repr=False)


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def validate_discord_webhook_url(value: object) -> str:
    """Return a trimmed official Discord webhook URL or raise ValueError."""
    url = str(value or "").strip()
    if not url:
        raise ValueError("請輸入 Discord Webhook 網址。")
    if len(url) > 2048:
        raise ValueError("Discord Webhook 網址過長。")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Discord Webhook 網址格式錯誤。") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _DISCORD_WEBHOOK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
        or not _DISCORD_WEBHOOK_PATH.fullmatch(parsed.path)
    ):
        raise ValueError("請貼上 Discord 官方產生的 HTTPS Webhook 網址。")
    query_keys = {key for key, _value in parse_qsl(parsed.query, keep_blank_values=True)}
    if not query_keys.issubset({"thread_id"}):
        raise ValueError("Discord Webhook 網址包含不支援的查詢參數。")
    return url


def normalize_discord_webhook_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Validate and copy the persisted Discord settings without mutating the input."""
    normalized = dict(settings)
    raw = settings.get(DISCORD_WEBHOOK_SECTION)
    if raw is None:
        return normalized
    if not isinstance(raw, Mapping):
        raise ValueError("Discord 播報設定格式錯誤。")
    enabled = raw.get("enabled") is True
    mode = raw.get("mode", "webhook")
    if mode not in {"webhook", "bot"}:
        raise ValueError("請選擇 Webhook 或 Discord 機器人播報。")
    webhook_url = str(raw.get("webhook_url") or "").strip()
    if webhook_url and mode == "webhook":
        webhook_url = validate_discord_webhook_url(webhook_url)
    elif not webhook_url and enabled and mode == "webhook":
        raise ValueError("啟用 Discord 播報前，請先輸入 Webhook 網址。")
    from .discord_bot import validate_bot_token, validate_channel_id

    bot_token = str(raw.get("bot_token") or "").strip()
    channel_id = str(raw.get("channel_id") or "").strip()
    if bot_token and mode == "bot":
        bot_token = validate_bot_token(bot_token)
    elif not bot_token and enabled and mode == "bot":
        raise ValueError("啟用 Discord 機器人播報前，請先輸入 Bot Token。")
    if channel_id and mode == "bot":
        channel_id = validate_channel_id(channel_id)
    normalized[DISCORD_WEBHOOK_SECTION] = {
        "enabled": enabled,
        "mode": mode,
        "webhook_url": webhook_url,
        "bot_token": bot_token,
        "channel_id": channel_id,
        "channel_label": str(raw.get("channel_label") or "")[:300],
    }
    return normalized


def _record(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _finite_number(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _coordinate(
    location: object,
    detection: Mapping[str, Any],
) -> tuple[float, float] | None:
    if isinstance(detection.get("coordinate_error"), str):
        return None
    candidates = [
        _record(detection.get("precise_coordinate")),
        _record(detection.get("estimated_coordinate")),
        _record(_record(location).get("point")),
        _record(_record(location).get("center")),
        _record(location),
    ]
    for candidate in candidates:
        lat = _finite_number(candidate.get("lat"))
        lng = _finite_number(candidate.get("lng"))
        if lat is not None and lng is not None and -90 <= lat <= 90 and -180 <= lng <= 180:
            return lat, lng
    return None


def _source_label(location: object) -> str:
    value = _record(location)
    point = _record(value.get("point")) or value
    parts: list[str] = []
    waypoint = value.get("waypoint_name")
    if isinstance(waypoint, str) and waypoint.strip():
        parts.append(waypoint.strip()[:120])
    grid_x = _finite_number(point.get("grid_x"))
    grid_y = _finite_number(point.get("grid_y"))
    if grid_x is not None and grid_y is not None:
        parts.append(f"格位 x={int(grid_x):+d}, y={int(grid_y):+d}")
    return " · ".join(parts)


_MODE_LABELS = {
    "screen_recognition": "自動搜尋辨識",
    "geo_sequence_recognition": "座標序列辨識",
    "live_recognition": "即時畫面辨識",
}
_TARGET_COLORS = {
    "red": ("紅色", 0xEF5350), "blue": ("藍色", 0x42A5F5),
    "yellow": ("黃色", 0xFBC02D), "purple": ("紫色", 0xAB47BC),
    "white": ("白色", 0xECEFF1), "gray": ("灰色", 0x90A4AE),
    "pink": ("粉紅色", 0xEC407A), "brown": ("棕色", 0xA1887F),
    "water": ("水", 0x29B6F6), "fire": ("火", 0xFF7043),
    "crystal": ("水晶", 0x80CBC4), "electric": ("電", 0xFFCA28),
}
_HITS_PER_MESSAGE = 5  # Bounds both the 6000-character embed budget and attachment memory.
_MAX_CROP_BYTES = 512 * 1024


def _target_style(name: str) -> tuple[str, int]:
    parts = name.lower().split("_")
    sizes = {"large": "大型", "normal": "普通", "small": "小型", "giant": "巨大"}
    if len(parts) == 2 and parts[0] in sizes and parts[1] in _TARGET_COLORS:
        label, color = _TARGET_COLORS[parts[1]]
        return f"{sizes[parts[0]]}{label}蘑菇", color
    return name.replace("_", " "), 0x768BFF


def build_discord_hit_payload(
    job_kind: str,
    location: object,
    detections: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build one bounded message; the notifier splits larger events into batches."""
    visible = list(detections[:_HITS_PER_MESSAGE])
    source = _source_label(location)
    embeds: list[dict[str, Any]] = []
    for detection in visible:
        name = str(detection.get("class_name") or "未命名目標")[:100]
        label, color = _target_style(name)
        confidence = _finite_number(detection.get("confidence"))
        participant_count = _finite_number(detection.get("participant_count"))
        fields = [
            {"name": "👥 參與人數", "value": f"**{int(participant_count)} 人**" if participant_count is not None and 0 <= participant_count <= 9999 else "未辨識", "inline": True},
            {"name": "🎯 辨識信心", "value": f"**{confidence:.0%}**" if confidence is not None and 0 <= confidence <= 1 else "未提供", "inline": True},
        ]
        coordinate = _coordinate(location, detection)
        if coordinate is not None:
            lat, lng = coordinate
            maps_url = f"https://www.google.com/maps?q={lat:.7f},{lng:.7f}"
            precise = _record(detection.get("precise_coordinate"))
            estimated = _record(detection.get("estimated_coordinate"))
            if (_finite_number(precise.get("lat")), _finite_number(precise.get("lng"))) == coordinate:
                coordinate_label = "📍 精確座標"
            elif (_finite_number(estimated.get("lat")), _finite_number(estimated.get("lng"))) == coordinate:
                coordinate_label = "📍 估算座標"
            else:
                coordinate_label = "📍 掃描中心（約略位置）"
            description = f"{coordinate_label}\n`{lat:.7f}, {lng:.7f}`\n[🗺️ 開啟 Google 地圖]({maps_url})"
        else:
            description = "📍 **尚未取得目標座標**"
        embeds.append({
            "title": f"🍄 {label}",
            "description": description,
            "fields": fields,
            "color": color,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": f"BloomPilot · {_MODE_LABELS.get(job_kind, '目標辨識')}" + (f" · {source[:100]}" if source else "")},
        })
    count = len(detections)
    suffix = f"（顯示前 {len(visible)} 筆）" if count > len(visible) else ""
    return {
        "username": "BloomPilot",
        "content": f"🎯 BloomPilot 即時命中：{count} 筆{suffix}",
        "embeds": embeds,
        "allowed_mentions": {"parse": []},
    }


def build_discord_test_payload() -> dict[str, Any]:
    payload = build_discord_hit_payload("screen_recognition", {}, [{"class_name": "測試播報", "confidence": 0.98, "participant_count": 0}])
    payload["content"] = "✅ BloomPilot Discord 即時命中播報測試成功。"
    payload["embeds"][0]["title"] = "✅ Discord 播報已連線"
    payload["embeds"][0]["description"] = "這是測試訊息，並非實際命中。\n實際命中會附上目標裁切圖片與可用的座標連結。"
    return payload


def _crop_attachment(value: object, index: int) -> DiscordAttachment | None:
    if not isinstance(value, str) or len(value) > _MAX_CROP_BYTES * 4 // 3 + 100:
        return None
    for mime, extension in (("image/jpeg", "jpg"), ("image/png", "png")):
        prefix = f"data:{mime};base64,"
        if not value.startswith(prefix):
            continue
        try:
            data = base64.b64decode(value[len(prefix):], validate=True)
        except (ValueError, binascii.Error):
            return None
        signature = b"\xff\xd8\xff" if extension == "jpg" else b"\x89PNG\r\n\x1a\n"
        if not data.startswith(signature) or len(data) > _MAX_CROP_BYTES:
            return None
        return DiscordAttachment(f"target-{index + 1}.{extension}", mime, data)
    return None


def build_discord_hit_messages(job_kind: str, location: object, detections: Sequence[Mapping[str, Any]]):
    for offset in range(0, len(detections), _HITS_PER_MESSAGE):
        batch = detections[offset:offset + _HITS_PER_MESSAGE]
        payload = build_discord_hit_payload(job_kind, location, batch)
        files = []
        for index, (detection, embed) in enumerate(zip(batch, payload["embeds"])):
            attachment = _crop_attachment(detection.get("crop_data_url"), index)
            if attachment is None:
                embed["fields"].append({"name": "🖼️ 目標圖片", "value": "裁切圖未取得或無法使用", "inline": False})
                continue
            files.append(attachment)
            embed["image"] = {"url": f"attachment://{attachment.filename}"}
        if len(detections) > _HITS_PER_MESSAGE:
            payload["content"] = f"🎯 BloomPilot 即時命中：共 {len(detections)} 筆（第 {offset + 1}–{offset + len(batch)} 筆）"
        yield payload, files


def _request_body(payload: Mapping[str, Any], files: Sequence[DiscordAttachment]) -> tuple[bytes, str]:
    message = dict(payload)
    if files:
        message["attachments"] = [{"id": index, "filename": item.filename} for index, item in enumerate(files)]
    encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if not files:
        return encoded, "application/json"
    boundary = f"BloomPilot{uuid.uuid4().hex}"
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="payload_json"\r\nContent-Type: application/json\r\n\r\n'.encode(), encoded, b"\r\n"]
    for index, item in enumerate(files):
        parts.extend([
            f'--{boundary}\r\nContent-Disposition: form-data; name="files[{index}]"; filename="{item.filename}"\r\nContent-Type: {item.content_type}\r\n\r\n'.encode(),
            item.data, b"\r\n",
        ])
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def discord_request(
    url: str,
    payload: Mapping[str, Any] | None = None,
    *,
    bot_token: str = "",
    files: Sequence[DiscordAttachment] = (),
    timeout: float = 8.0,
) -> Any:
    body, content_type = _request_body(payload, files) if payload is not None else (None, "application/json")
    headers = {"Content-Type": content_type, "User-Agent": "BloomPilot/0.2"}
    if bot_token:
        headers["Authorization"] = f"Bot {bot_token}"
    request = Request(url, data=body, method="GET" if payload is None else "POST", headers=headers)
    for attempt in range(3):
        try:
            with build_opener(_NoRedirectHandler()).open(request, timeout=timeout) as response:
                status = int(getattr(response, "status", 0))
                if not 200 <= status < 300:
                    raise DiscordWebhookError(f"Discord 回傳 HTTP {status}。", status=status)
                raw = response.read()
                return json.loads(raw) if raw else None
        except HTTPError as exc:
            if exc.code == 429 and attempt < 2:
                try:
                    delay = float(json.loads(exc.read()).get("retry_after", 1))
                except (ValueError, TypeError, AttributeError):
                    delay = 1.0
                if 0 <= delay <= 10:
                    time.sleep(max(0.05, delay))
                    continue
            messages = {
                401: "Discord 驗證失敗，請確認 Bot Token 或 Webhook 網址。",
                403: "Discord 權限不足，請允許檢視頻道、傳送訊息、嵌入連結及附加檔案。",
                404: "Discord 頻道或 Webhook 不存在，請重新選擇或更新設定。",
                413: "Discord 圖片附件過大。",
                429: "Discord 發送頻率受限，請稍後再試。",
            }
            raise DiscordWebhookError(messages.get(exc.code, f"Discord 回傳 HTTP {exc.code}。"), status=exc.code) from None
        except (URLError, TimeoutError, OSError):
            # Network exceptions may contain a credential-bearing URL. Never echo them.
            raise DiscordWebhookError("無法連線至 Discord，請確認網路連線後重試。") from None
        except (ValueError, UnicodeError):
            raise DiscordWebhookError("Discord 回應格式錯誤，請稍後重試。") from None


def post_discord_webhook(
    webhook_url: object,
    payload: Mapping[str, Any],
    *,
    files: Sequence[DiscordAttachment] = (),
    timeout: float = 8.0,
) -> None:
    """Post a message and optional image attachments without following redirects."""
    url = validate_discord_webhook_url(webhook_url)
    discord_request(url, payload, files=files, timeout=timeout)


class DiscordWebhookNotifier:
    """Serialize webhook delivery on a daemon thread so recognition never blocks."""

    def __init__(
        self,
        settings_provider: Callable[[], Mapping[str, Any]],
        *,
        sender: Callable[..., None] = post_discord_webhook,
        bot_sender: Callable[..., None] | None = None,
        error_callback: Callable[[str, str], None] | None = None,
        queue_size: int = 64,
    ) -> None:
        self._settings_provider = settings_provider
        self._sender = sender
        self._bot_sender = bot_sender
        self._error_callback = error_callback
        self._queue: queue.Queue[tuple[str, object, list[Mapping[str, Any]]] | None] = queue.Queue(queue_size)
        self._closed = threading.Event()
        self._thread_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def _ensure_thread(self) -> None:
        with self._thread_lock:
            if self._thread is not None or self._closed.is_set():
                return
            self._thread = threading.Thread(
                target=self._run,
                name="BloomPilotDiscord",
                daemon=True,
            )
            self._thread.start()

    def notify(self, job_kind: str, location: object, detections: Sequence[Mapping[str, Any]]) -> bool:
        if self._closed.is_set() or not detections:
            return False
        raw = self._settings_provider().get(DISCORD_WEBHOOK_SECTION)
        if not isinstance(raw, Mapping) or raw.get("enabled") is not True:
            return False
        try:
            normalize_discord_webhook_settings({DISCORD_WEBHOOK_SECTION: raw})
            self._ensure_thread()
            self._queue.put_nowait((job_kind, copy.deepcopy(location), copy.deepcopy(list(detections))))
            return True
        except (ValueError, queue.Full) as exc:
            message = "Discord 播報佇列已滿，已略過本次命中。" if isinstance(exc, queue.Full) else str(exc)
            if self._error_callback is not None:
                self._error_callback(job_kind, message)
            return False

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                job_kind, location, detections = item
                try:
                    from .discord_bot import post_discord_bot, resolve_bot_channel

                    for payload, files in build_discord_hit_messages(job_kind, location, detections):
                        if self._closed.is_set():
                            break
                        # Read saved settings for every delivery, including queued batches.
                        raw = self._settings_provider().get(DISCORD_WEBHOOK_SECTION)
                        if not isinstance(raw, Mapping) or raw.get("enabled") is not True:
                            break
                        destination = normalize_discord_webhook_settings({DISCORD_WEBHOOK_SECTION: raw})[DISCORD_WEBHOOK_SECTION]
                        is_bot = destination["mode"] == "bot"
                        channel = resolve_bot_channel(destination["bot_token"], destination["channel_id"]) if is_bot else ""
                        if is_bot:
                            (self._bot_sender or post_discord_bot)(destination["bot_token"], channel, payload, files=files)
                        elif files:
                            self._sender(destination["webhook_url"], payload, files=files)
                        else:
                            self._sender(destination["webhook_url"], payload)
                except Exception as exc:
                    if self._error_callback is not None:
                        self._error_callback(job_kind, str(exc))
            finally:
                self._queue.task_done()

    def close(self, timeout: float = 1.0) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        thread = self._thread
        if thread is None:
            return
        while True:
            try:
                self._queue.put_nowait(None)
                break
            except queue.Full:
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                except queue.Empty:
                    continue
        thread.join(timeout=max(0.0, timeout))
