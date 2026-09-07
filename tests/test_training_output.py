from __future__ import annotations

import json
import unittest
from pathlib import Path

from bloompilot.training_output import parse_nvidia_smi_output, sanitize_training_output


class TrainingOutputSanitizerTests(unittest.TestCase):
    def test_shared_terminal_output_cases(self) -> None:
        fixtures = Path(__file__).parent / "fixtures" / "terminal_output.json"
        for case in json.loads(fixtures.read_text(encoding="utf-8")):
            with self.subTest(case["name"]):
                self.assertEqual(sanitize_training_output(case["raw"]), case["expected"])

    def test_removes_ansi_erase_and_color_sequences(self) -> None:
        raw = "\x1b[K\x1b[32m10/100 epochs\x1b[0m"
        self.assertEqual(sanitize_training_output(raw), "10/100 epochs")

    def test_keeps_latest_carriage_return_progress(self) -> None:
        raw = "batch 1/10\rbatch 2/10\x1b[K"
        self.assertEqual(sanitize_training_output(raw), "batch 2/10")

    def test_does_not_change_normal_error_text(self) -> None:
        text = "錯誤：CUDA out of memory"
        self.assertEqual(sanitize_training_output(text), text)

    def test_parses_gpu_monitor_output(self) -> None:
        stats = parse_nvidia_smi_output("91, 7168, 8188, 72, 98.5\n")
        self.assertEqual(stats["utilization"], 91.0)
        self.assertEqual(stats["memory_total"], 8188.0)
        self.assertEqual(stats["power"], 98.5)


if __name__ == "__main__":
    unittest.main()
