"""Read only the daily-lookback title and navigation controls with local OCR."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

from .phone_tools import _process_kwargs


@lru_cache(maxsize=1)
def _review_button_templates() -> tuple[np.ndarray, ...]:
    # Ultralytics replaces cv2.imread with a wrapper that returns H x W x 1
    # for grayscale images. Decode directly so templates stay two-dimensional
    # both before and after a YOLO model has been loaded in this process.
    return tuple(cv2.imdecode(np.frombuffer(path.read_bytes(), dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
                 for path in sorted(Path(__file__).with_name("templates").glob("daily_review_*.png")))


def _review_button_center(image: np.ndarray) -> tuple[int, int] | None:
    """Match the recorded confirm/done glyphs, excluding the share icon."""
    h, w = image.shape[:2]
    scale = 1080 / w
    normalized = cv2.resize(image, (1080, round(h * scale)))
    nh = normalized.shape[0]
    top, left = round(nh*.85), 216
    crop = normalized[top:round(nh*.965), left:864]
    glyphs = cv2.inRange(cv2.cvtColor(crop, cv2.COLOR_BGR2HSV), (0, 0, 180), (179, 95, 255))
    matches = []
    for template in _review_button_templates():
        if template is None or min(template.shape) < 2 or any(a < b for a, b in zip(glyphs.shape, template.shape)):
            continue
        scores = cv2.matchTemplate(glyphs, template, cv2.TM_CCOEFF_NORMED)
        _, score, _, location = cv2.minMaxLoc(scores)
        if score >= .86:
            matches.append((score, location[0] + template.shape[1]/2, location[1] + template.shape[0]/2))
    if not matches:
        return None
    _, x, y = max(matches)
    return round((x+left)/scale), round((y+top)/scale)


def daily_review_mood_visible(image: np.ndarray) -> bool:
    """Recognize the three blue/neutral/yellow mood cards on the recap page."""
    if image is None or not image.size:
        return False
    h, w = image.shape[:2]
    image = cv2.resize(image, (360, max(1, round(h * 360 / w))))
    h, w = image.shape[:2]
    hue, saturation, value = cv2.split(cv2.cvtColor(image, cv2.COLOR_BGR2HSV))
    sky = (hue >= 90) & (hue <= 125) & (saturation >= 90) & (value >= 100)
    if float(np.mean(sky[round(h*.035):round(h*.20)])) < .65:
        return False
    masks = (
        ((hue >= 95) & (hue <= 115) & (saturation >= 100) & (value >= 160), .08, .23),
        ((hue >= 8) & (hue <= 30) & (saturation < 100) & (value >= 160), .42, .58),
        ((hue >= 15) & (hue <= 35) & (saturation >= 100) & (value >= 160), .78, .92),
    )
    return all(float(np.mean(mask[round(h*.66):round(h*.71), round(w*left):round(w*right)])) > .50
               for mask, left, right in masks)


def daily_review_summary_visible(image: np.ndarray) -> bool:
    """The step recap has a white framed map over blue sky and green ground."""
    if image is None or not image.size:
        return False
    h, w = image.shape[:2]
    if w > 360:
        image = cv2.resize(image, (360, round(h * 360 / w)))
        h, w = image.shape[:2]
    hue, saturation, value = cv2.split(cv2.cvtColor(image, cv2.COLOR_BGR2HSV))
    sky = (hue >= 90) & (hue <= 125) & (saturation >= 90) & (value >= 100)
    grass = (hue >= 30) & (hue <= 85) & (saturation >= 80) & (value >= 90)
    if (float(np.mean(sky[round(h*.035):round(h*.09)])) < .65
            or float(np.mean(grass[round(h*.60):round(h*.86)])) < .50):
        return False
    white = np.uint8((saturation <= 60) & (value >= 220)) * 255
    contours, _ = cv2.findContours(white, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in contours:
        x, y, width, height = cv2.boundingRect(contour)
        if (width >= w*.80 and 0 <= x <= w*.12
                and h*.075 <= y <= h*.20 and h*.40 <= y+height <= h*.62):
            # Require a frame enclosing a mostly non-white interior.
            # Its bottom border may connect to the bright horizon at both
            # edges, making the component as wide as the screenshot.
            inner = white[y+8:y+height-8, round(w*.12):round(w*.88)]
            left_edge = white[round(h*.15):round(h*.48), round(w*.062):round(w*.076)]
            if (inner.size and (
                    (float(np.mean(inner > 0)) < .30 and float(np.mean(left_edge > 0)) > .60)
                    or (y+height < h*.48 and float(np.mean(inner > 0)) > .65)
            )):
                return True
    return False


def daily_review_action(
    lines: list[dict[str, object]], *, continuing: bool = False,
) -> tuple[bool, tuple[int, int] | None]:
    """Require a lookback title before following a small navigation allowlist."""
    def normalized(value: object) -> str:
        text = re.sub(r"[\s’'!！。.：:]", "", str(value)).lower()
        # Windows OCR can render the single horizontal stroke as a hyphen.
        return text.replace("下-步", "下一步").replace("下—步", "下一步")

    titles = ("今日回顧", "今日回顾", "今天的回顧", "今天的回顾", "dailylookback", "todayslookback")
    visible = any(any(title in normalized(line.get("text", "")) for title in titles) for line in lines)
    if not visible and not continuing:
        return False, None
    actions = {"跳過", "略過", "跳过", "完成", "關閉", "关闭", "下一步", "繼續", "继续", "確定", "确定", "稍後", "稍后", "skip", "done", "finish", "close", "next", "continue", "ok", "later"}
    candidates = []
    for line in lines:
        label = normalized(line.get("text", ""))
        if label not in actions:
            continue
        try:
            x, y = float(line["x"]), float(line["y"])
            width, height = float(line["width"]), float(line["height"])
            screen_h = float(line["screen_height"])
        except (KeyError, TypeError, ValueError):
            continue
        if not all(np.isfinite(v) for v in (x, y, width, height, screen_h)):
            continue
        if width <= 0 or height <= 0 or screen_h <= 0 or y < screen_h * 0.55:
            continue
        # Prefer skipping/finishing to stepping through every recap page.
        priority = 1 if label in {"下一步", "繼續", "继续", "next", "continue"} else 0
        candidates.append((priority, round(x + width / 2), round(y + height / 2)))
    if not candidates:
        return visible or continuing, None
    _, x, y = min(candidates)
    return True, (x, y)


def read_daily_review(image: np.ndarray, *, continuing: bool = False) -> tuple[bool, tuple[int, int] | None]:
    """Windows OCR runs locally and only on non-map recovery frames."""
    if image is None or not image.size:
        return False, None
    if daily_review_mood_visible(image):
        # User-selected automation preference: choose the middle "normal" mood.
        h, w = image.shape[:2]
        return True, (round(w*.50), round(h*.69))
    summary = daily_review_summary_visible(image)
    if summary or continuing:
        action = _review_button_center(image)
        if action is not None:
            return True, action
    if os.name != "nt":
        return False, None
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    if not powershell.is_file():
        return False, None
    try:
        with tempfile.TemporaryDirectory(prefix="bloompilot-review-") as directory:
            path = Path(directory) / "frame.png"
            ok, encoded = cv2.imencode(".png", image)
            if not ok:
                return False, None
            path.write_bytes(encoded.tobytes())
            # Execute the trusted bundled code as an inline command. This does
            # not change the machine's PowerShell execution-policy setting.
            script = Path(__file__).with_name("screen_text.ps1").read_text(encoding="utf-8")
            command = "& {\n" + script + "\n} '" + str(path).replace("'", "''") + "'"
            result = subprocess.run(
                [str(powershell), "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True, text=True, encoding="utf-8", timeout=8,
                **_process_kwargs(),
            )
            if result.returncode != 0:
                return False, None
            lines = json.loads(result.stdout.lstrip("\ufeff"))
            observation = daily_review_action(
                lines if isinstance(lines, list) else [],
                continuing=continuing or summary,
            )
            if not observation[0] or observation[1] is not None:
                return observation
            # White text over the glowing green confirm button is missed by
            # full-colour OCR. Read its neutral glyphs as black on white while
            # translating the cropped coordinates back. Ignore the share button.
            h, w = image.shape[:2]
            hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
            glyphs = cv2.inRange(hsv, (0, 0, 180), (179, 95, 255))
            top, bottom, left, right = round(h*.907), round(h*.942), round(w*.33), round(w*.67)
            high_contrast = 255 - glyphs[top:bottom, left:right]
            path.write_bytes(cv2.imencode(".png", high_contrast)[1].tobytes())
            result = subprocess.run(
                [str(powershell), "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True, text=True, encoding="utf-8", timeout=8,
                **_process_kwargs(),
            )
            if result.returncode == 0:
                lines = json.loads(result.stdout.lstrip("\ufeff"))
                if isinstance(lines, list):
                    for line in lines:
                        line["x"] += left
                        line["y"] += top
                        line["screen_height"] = h
                return daily_review_action(lines if isinstance(lines, list) else [], continuing=True)
            return observation
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False, None
