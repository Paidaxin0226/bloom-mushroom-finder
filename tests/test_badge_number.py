from __future__ import annotations

import unittest

import cv2
import numpy as np

from bloompilot.badge_number import (
    enrich_detections_with_badge_numbers,
    filter_detections_by_participant_count,
    normalize_participant_count_filter,
    recognize_badge_number,
)


def badge_frame(digit: str = "6") -> np.ndarray:
    hsv = np.zeros((180, 240, 3), dtype=np.uint8)
    hsv[:, :] = (62, 145, 170)
    frame = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
    pill_color = cv2.cvtColor(
        np.uint8([[[70, 137, 125]]]), cv2.COLOR_HSV2BGR
    )[0, 0].tolist()
    cv2.rectangle(frame, (58, 28), (186, 76), pill_color, -1, cv2.LINE_AA)
    cv2.circle(frame, (84, 42), 7, (255, 255, 255), -1, cv2.LINE_AA)
    cv2.ellipse(frame, (84, 61), (13, 12), 0, 180, 360, (255, 255, 255), -1, cv2.LINE_AA)
    cv2.rectangle(frame, (77, 47), (91, 62), (255, 255, 255), -1, cv2.LINE_AA)
    cv2.putText(
        frame,
        digit,
        (112, 68),
        cv2.FONT_HERSHEY_DUPLEX,
        1.0,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )
    return frame


class BadgeNumberTests(unittest.TestCase):
    def test_reads_digit_to_right_of_person_icon(self) -> None:
        result = recognize_badge_number(badge_frame("6"), [40, 18, 205, 160])
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.value, 6)
        self.assertGreaterEqual(result.confidence, 0.54)
        self.assertGreaterEqual(result.bounds[0], 100)

    def test_enrichment_keeps_detection_payload_small(self) -> None:
        detections = [{
            "class_name": "Large_Electric",
            "confidence": 0.92,
            "xyxy": [40, 18, 205, 160],
        }]
        enrich_detections_with_badge_numbers(badge_frame("5"), detections)
        self.assertEqual(detections[0]["participant_count"], 5)
        self.assertIn("participant_count_confidence", detections[0])
        self.assertNotIn("participant_crop", detections[0])

    def test_map_lines_without_badge_are_not_treated_as_digits(self) -> None:
        frame = badge_frame("3")
        frame[:, :] = (70, 170, 75)
        cv2.line(frame, (20, 20), (200, 150), (240, 240, 230), 5)
        self.assertIsNone(recognize_badge_number(frame, [0, 0, 220, 170]))

    def test_absent_number_is_enriched_as_zero(self) -> None:
        frame = np.full((120, 160, 3), (70, 170, 75), dtype=np.uint8)
        detections = [{"class_name": "Large_Yellow", "xyxy": [10, 10, 140, 110]}]
        enrich_detections_with_badge_numbers(frame, detections)
        self.assertEqual(detections[0]["participant_count"], 0)
        self.assertEqual(detections[0]["participant_count_method"], "number_absent")
        self.assertNotIn("participant_count_bounds", detections[0])

    def test_exact_count_filter_includes_zero_and_rejects_invalid_values(self) -> None:
        detections = [
            {"class_name": "Large_Blue", "participant_count": 0},
            {"class_name": "Large_Red", "participant_count": 5},
        ]
        self.assertEqual(
            filter_detections_by_participant_count(detections, 0),
            [detections[0]],
        )
        self.assertEqual(filter_detections_by_participant_count(detections, None), detections)
        self.assertEqual(normalize_participant_count_filter("5"), 5)
        with self.assertRaisesRegex(ValueError, "整數"):
            normalize_participant_count_filter(1.5)
        with self.assertRaisesRegex(ValueError, "整數"):
            normalize_participant_count_filter(-1)


if __name__ == "__main__":
    unittest.main()
