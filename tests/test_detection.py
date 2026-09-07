from __future__ import annotations

import tempfile
import threading
import unittest
import os
from pathlib import Path
from unittest.mock import patch

import numpy as np

from bloompilot.detection import detections_with_crops, draw_detections, newest_trained_model
from bloompilot.phone_stream import LatestSlot


class FakeTensor:
    def __init__(self, values: object) -> None:
        self.values = np.asarray(values)

    def detach(self) -> "FakeTensor":
        return self

    def cpu(self) -> "FakeTensor":
        return self

    def numpy(self) -> np.ndarray:
        return self.values


class FakeBoxes:
    def __init__(self) -> None:
        self.xyxy = FakeTensor([[10, 12, 48, 55]])
        self.conf = FakeTensor([0.91])
        self.cls = FakeTensor([7])

    def __len__(self) -> int:
        return 1


class FakeResult:
    boxes = FakeBoxes()
    names = {7: "Large_Red"}


class PhoneYoloTests(unittest.TestCase):
    def test_latest_slot_never_queues_old_frames(self) -> None:
        slot: LatestSlot[str] = LatestSlot()
        self.assertEqual(slot.put("old"), 1)
        self.assertEqual(slot.put("new"), 2)
        sequence, value = slot.get_new(0)
        self.assertEqual((sequence, value), (2, "new"))
        self.assertEqual(slot.wait_new(sequence, threading.Event(), timeout=0), (sequence, None))

    def test_newest_trained_model_uses_modification_time(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            old = root / "old" / "weights" / "best.pt"
            new = root / "new" / "weights" / "best.pt"
            old.parent.mkdir(parents=True)
            new.parent.mkdir(parents=True)
            old.write_bytes(b"old")
            new.write_bytes(b"new")
            old.touch()
            new.touch()
            old_time = old.stat().st_mtime - 10
            os.utime(old, (old_time, old_time))
            self.assertEqual(newest_trained_model(root), new)

    def test_newest_trained_model_ignores_legacy_pointer(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary:
            root = Path(temporary)
            selected = root / "selected" / "weights" / "best.pt"
            newer = root / "newer" / "weights" / "best.pt"
            selected.parent.mkdir(parents=True)
            newer.parent.mkdir(parents=True)
            selected.write_bytes(b"selected")
            newer.write_bytes(b"newer")
            old_time = selected.stat().st_mtime - 10
            os.utime(selected, (old_time, old_time))
            (root / "LATEST_MODEL.txt").write_text(
                "selected/weights/best.pt\n", encoding="utf-8"
            )
            self.assertEqual(newest_trained_model(root), newer)

    def test_draw_detections_returns_annotated_copy(self) -> None:
        source = np.zeros((80, 80, 3), dtype=np.uint8)
        with patch(
            "bloompilot.detection.cv2.putText", wraps=__import__("cv2").putText
        ) as put_text:
            rendered, count = draw_detections(FakeResult(), source)
        self.assertEqual(count, 1)
        self.assertEqual(rendered.shape, source.shape)
        self.assertFalse(np.shares_memory(rendered, source))
        self.assertGreater(int(rendered.sum()), 0)
        self.assertEqual(put_text.call_args.args[1], "Large_Red 91%")

    def test_live_hit_crop_contains_only_clamped_detection_bounds(self) -> None:
        source = np.zeros((100, 120, 3), dtype=np.uint8)
        source[20:60, 10:70] = (20, 120, 240)
        detections = [{
            "class_id": 7,
            "class_name": "Large_Red",
            "confidence": 0.91,
            "xyxy": [10.2, 20.4, 69.7, 59.2],
        }]
        hits = detections_with_crops(source, detections)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["crop_bounds"], [10, 20, 70, 60])
        self.assertEqual((hits[0]["crop_width"], hits[0]["crop_height"]), (60, 40))
        self.assertTrue(str(hits[0]["crop_data_url"]).startswith("data:image/jpeg;base64,"))
        self.assertNotIn("crop_data_url", detections[0])

    def test_invalid_or_offscreen_boxes_are_not_sent_as_live_hits(self) -> None:
        source = np.zeros((100, 120, 3), dtype=np.uint8)
        hits = detections_with_crops(source, [
            {"xyxy": [130, 10, 150, 20]},
            {"xyxy": [10, 10, float("nan"), 20]},
        ])
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
