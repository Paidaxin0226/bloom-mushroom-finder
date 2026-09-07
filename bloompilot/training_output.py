"""Plain-text training output and GPU telemetry parsing, independent of UI."""

from __future__ import annotations

import re


ANSI_ESCAPE_RE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"
    r"|\x1b[@-_]"
)


def sanitize_training_output(text: str) -> str:
    cleaned = ANSI_ESCAPE_RE.sub("", text).replace("\x00", "")
    # CRLF is a newline, not a progress overwrite. Keep the newest version of
    # each line while preserving multiline errors and tracebacks.
    cleaned = cleaned.replace("\r\n", "\n")
    return "\n".join(line.rsplit("\r", 1)[-1] for line in cleaned.split("\n")).rstrip()


def parse_nvidia_smi_output(text: str) -> dict[str, float]:
    fields = [field.strip() for field in text.splitlines()[0].split(",")]
    if len(fields) != 5:
        raise ValueError("nvidia-smi 輸出欄位數量不正確")
    return {
        "utilization": float(fields[0]),
        "memory_used": float(fields[1]),
        "memory_total": float(fields[2]),
        "temperature": float(fields[3]),
        "power": float(fields[4]) if fields[4] not in {"[N/A]", "N/A"} else 0.0,
    }
