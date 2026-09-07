from __future__ import annotations

import base64
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

from bloompilot.dataset_builder import discover_targets
from bloompilot.monthly_workflow import MonthlyWorkflowStore, validate_cycle
from bloompilot.monthly_route import (
    MONTHLY_BACKGROUND_ROUTE,
    build_monthly_background_route,
    ensure_monthly_background_route,
)
from bloompilot.route_library import RouteLibrary
from bloompilot.web.runtime import AppRuntime


def transparent_png(color: tuple[int, int, int] = (230, 80, 60)) -> bytes:
    image = Image.new("RGBA", (48, 48), (0, 0, 0, 0))
    ImageDraw.Draw(image).ellipse((5, 5, 43, 43), fill=(*color, 255))
    stream = io.BytesIO()
    image.save(stream, "PNG")
    return stream.getvalue()


class MonthlyWorkflowStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.root = Path(self.temporary.name)
        self.store = MonthlyWorkflowStore(
            project_root=self.root,
            workspace_root=self.root / "data/monthly_activity",
            state_path=self.root / "runtime/monthly_activity/state.json",
            resident_root=self.root / "models/resident",
        )
        self.targets = self.root / "data/source/targets"
        self.targets.mkdir(parents=True)
        (self.targets / "Large_Blue.png").write_bytes(transparent_png((40, 100, 240)))
        self.config = {
            "_project_root": str(self.root),
            "paths": {
                "backgrounds": "data/source/backgrounds",
                "targets": "data/source/targets",
                "generated_root": "data/generated",
            },
            "generation": {
                "excluded_target_directories": ["source"],
                "require_transparency": True,
                "alpha_threshold": 8,
                "trim_transparent_padding": False,
            },
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_cycle_and_background_paths_are_confined(self) -> None:
        self.assertEqual(validate_cycle("2026-09"), "2026-09")
        for value in ("2026-13", "26-09", "../2026-09"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_cycle(value)
        with self.assertRaises(ValueError):
            self.store.background_file("2026-09", "../secret.png")

    def test_review_is_invalidated_when_a_background_is_deleted(self) -> None:
        backgrounds = self.store.backgrounds_dir("2026-09") / "route-a"
        backgrounds.mkdir(parents=True)
        (backgrounds / "one.png").write_bytes(transparent_png())
        (backgrounds / "two.png").write_bytes(transparent_png((50, 220, 80)))

        self.store.approve_backgrounds("2026-09")
        self.assertTrue(self.store.review_is_current("2026-09"))
        result = self.store.delete_background("2026-09", "route-a/one.png")
        self.assertEqual(result["background_count"], 1)
        self.assertFalse(self.store.review_is_current("2026-09"))

    def test_activity_target_requires_transparency_and_mixes_with_permanent_targets(self) -> None:
        opaque = Image.new("RGB", (20, 20), (255, 0, 0))
        stream = io.BytesIO()
        opaque.save(stream, "PNG")
        with self.assertRaisesRegex(ValueError, "透明"):
            self.store.save_activity_target(
                "2026-09", "opaque.png", base64.b64encode(stream.getvalue()).decode("ascii")
            )

        saved = self.store.save_activity_target(
            "2026-09",
            "September Event.png",
            "data:image/png;base64," + base64.b64encode(transparent_png()).decode("ascii"),
        )
        self.assertEqual(saved["name"], "September_Event")
        bundle = self.store.prepare_mixed_targets(self.config, "2026-09")
        active = dict(self.config)
        active["paths"] = {"targets": str(bundle)}
        targets, skipped = discover_targets(active)
        self.assertFalse(skipped)
        self.assertEqual(
            sorted(item.relative_path.stem for item in targets),
            ["Large_Blue", "September_Event"],
        )

    def test_duplicate_activity_class_is_rejected_before_dataset_generation(self) -> None:
        self.store.save_activity_target(
            "2026-09", "Large_Blue.png", base64.b64encode(transparent_png()).decode("ascii")
        )
        with self.assertRaisesRegex(ValueError, "重複"):
            self.store.prepare_mixed_targets(self.config, "2026-09")

    def test_training_backgrounds_mix_clean_baseline_with_current_month(self) -> None:
        baseline = self.root / "data/source/backgrounds/taipei"
        baseline.mkdir(parents=True)
        (baseline / "clean.png").write_bytes(transparent_png())
        (baseline / "frame_target_Old_Event.png").write_bytes(transparent_png())
        monthly = self.store.backgrounds_dir("2026-09") / "fixed-route"
        monthly.mkdir(parents=True)
        (monthly / "current.png").write_bytes(transparent_png())
        (monthly / "current_target_Event.png").write_bytes(transparent_png())
        self.store.approve_backgrounds("2026-09")

        bundle = self.store.prepare_training_backgrounds(self.config, "2026-09")
        files = sorted(path.relative_to(bundle).as_posix() for path in bundle.rglob("*.png"))

        self.assertEqual(files, ["base_taipei/clean.png", "month_fixed-route/current.png"])
        status = self.store.status("2026-09")
        self.assertEqual(status["baseline_background_count"], 1)
        self.assertEqual(status["monthly_background_count"], 1)
        self.assertEqual(status["background_policy"], "clean_baseline_plus_current_month")

    def test_completed_cycle_cleanup_removes_only_rebuildable_and_old_month_data(self) -> None:
        current_background = self.store.backgrounds_dir("2026-09") / "route/current.png"
        current_background.parent.mkdir(parents=True)
        current_background.write_bytes(transparent_png())
        for name in ("training_backgrounds", "mixed_targets"):
            directory = self.store.cycle_dir("2026-09") / name
            directory.mkdir(parents=True)
            (directory / "temporary.png").write_bytes(transparent_png())
        old_cycle = self.store.cycle_dir("2026-08")
        old_cycle.mkdir(parents=True)
        (old_cycle / "old.png").write_bytes(transparent_png())
        datasets = []
        for name in ("resident", "event"):
            dataset = self.root / "data/generated" / name
            dataset.mkdir(parents=True)
            (dataset / "data.yaml").write_text("path: .\n", encoding="utf-8")
            datasets.append(dataset)
        (self.root / "data/generated/LATEST.txt").write_text("event\n", encoding="utf-8")

        result = self.store.cleanup_completed_cycle(self.config, "2026-09", datasets)

        self.assertEqual(len(result["datasets"]), 2)
        self.assertFalse(old_cycle.exists())
        self.assertTrue(current_background.exists())
        self.assertFalse((self.root / "data/generated/LATEST.txt").exists())
        self.assertIsNotNone(self.store.status("2026-09")["intermediates_cleaned_at"])

    def test_only_superseded_monthly_event_model_is_deleted(self) -> None:
        event = self.root / "outputs/training/old_event/weights/best.pt"
        resident = self.root / "outputs/training/old_resident/weights/best.pt"
        for path, role in ((event, "event"), (resident, "resident")):
            path.parent.mkdir(parents=True)
            path.write_bytes(role.encode("ascii"))
            (path.parent.parent / ".artifact.json").write_text(
                json.dumps({"monthly_workflow": {"cycle": "2026-08", "role": role}}),
                encoding="utf-8",
            )

        self.assertEqual(self.store.delete_superseded_monthly_model(str(event)), event.resolve())
        self.assertIsNone(self.store.delete_superseded_monthly_model(str(resident)))
        self.assertFalse(event.exists())
        self.assertTrue(resident.exists())

    def test_resident_model_is_copied_to_stable_protected_location(self) -> None:
        source = self.root / "outputs/training/base/weights/best.pt"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"resident-v1")
        destination = self.store.set_resident_model(
            source,
            source_label="existing model",
            class_names=["Large_Blue"],
        )
        source.unlink()
        self.assertEqual(destination.read_bytes(), b"resident-v1")
        self.assertEqual(self.store.resident_model(), destination.resolve())
        self.assertEqual(self.store.resident_metadata()["class_names"], ["Large_Blue"])

    def test_dataset_and_run_roles_are_recorded_per_cycle(self) -> None:
        dataset = self.root / "data/generated/dataset_test"
        dataset.mkdir(parents=True)
        (dataset / "metadata.json").write_text(json.dumps({"class_names": ["base"]}), encoding="utf-8")
        self.store.annotate_dataset(dataset, cycle="2026-09", role="resident")
        metadata = json.loads((dataset / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["monthly_workflow"], {"cycle": "2026-09", "role": "resident"})

    def test_private_background_route_is_extracted_from_current_dataset_towns(self) -> None:
        backgrounds = self.root / "data/source/backgrounds"
        for town in ("antalya", "bangkok"):
            directory = backgrounds / town
            directory.mkdir(parents=True)
            (directory / "background.png").write_bytes(b"png")

        document = build_monthly_background_route(backgrounds)
        self.assertEqual(document["metadata"]["towns"], ["antalya", "bangkok"])
        self.assertEqual(document["metadata"]["waypoint_count"], 2)
        self.assertEqual(document["metadata"]["total_search_points"], 149)

        routes = self.root / "routes"
        relative, _ = ensure_monthly_background_route(backgrounds, routes)
        library = RouteLibrary(routes)
        self.assertEqual(relative, MONTHLY_BACKGROUND_ROUTE)
        self.assertEqual(len(library.waypoints(relative)), 2)
        self.assertEqual(library.list(), [])

    def test_runtime_trains_resident_before_event_and_never_promotes_event_model(self) -> None:
        backgrounds = self.store.backgrounds_dir("2026-09") / "fixed-route"
        backgrounds.mkdir(parents=True)
        (backgrounds / "one.png").write_bytes(transparent_png())
        self.store.approve_backgrounds("2026-09")
        self.store.save_activity_target(
            "2026-09", "Event.png", base64.b64encode(transparent_png()).decode("ascii")
        )
        original = self.root / "outputs/training/original/weights/best.pt"
        original.parent.mkdir(parents=True)
        original.write_bytes(b"original-resident")
        self.store.set_resident_model(original, class_names=["Large_Blue"])

        datasets = []
        for name, classes in (
            ("resident_dataset", ["Large_Blue"]),
            ("event_dataset", ["Event", "Large_Blue"]),
        ):
            dataset = self.root / "data/generated" / name
            dataset.mkdir(parents=True)
            (dataset / "metadata.json").write_text(
                json.dumps({"class_names": classes}), encoding="utf-8"
            )
            datasets.append(dataset)
        resident_best = self.root / "outputs/training/resident_run/weights/best.pt"
        event_best = self.root / "outputs/training/event_run/weights/best.pt"
        resident_best.parent.mkdir(parents=True)
        event_best.parent.mkdir(parents=True)
        resident_best.write_bytes(b"strengthened-resident")
        event_best.write_bytes(b"monthly-event")

        runtime = AppRuntime()
        runtime.monthly_workflow = self.store
        context = SimpleNamespace(
            job_id="monthly-job",
            stop_event=threading.Event(),
            message=Mock(),
            check_cancelled=Mock(),
        )
        context.progress = Mock()
        training_results = [
            {"status": {"state": "completed", "checkpoint": str(resident_best)}},
            {"status": {"state": "completed", "checkpoint": str(event_best)}},
        ]
        with (
            patch.object(self.store, "dataset_config", return_value={}),
            patch.object(runtime, "config", return_value={}),
            patch.object(runtime.jobs, "start", side_effect=lambda _kind, runner: runner(context)),
            patch("bloompilot.web.runtime.generate_dataset", side_effect=datasets),
            patch.object(runtime, "_run_training_process", side_effect=training_results) as train_stage,
            patch.object(runtime, "set_default_recognition_model") as set_default,
            patch("bloompilot.web.runtime.ArtifactManager") as artifacts,
        ):
            result = runtime.start_monthly_training({"cycle": "2026-09"})

        self.assertEqual(train_stage.call_args_list[0].kwargs["phase"], "常駐模型強化")
        self.assertEqual(train_stage.call_args_list[1].kwargs["phase"], "本月活動菇模型")
        self.assertEqual(train_stage.call_args_list[1].kwargs["model"], self.store.resident_model())
        self.assertEqual(self.store.resident_model().read_bytes(), b"strengthened-resident")
        self.assertNotEqual(self.store.resident_model().read_bytes(), event_best.read_bytes())
        self.assertFalse(original.exists())
        self.assertEqual(result["event_model"], str(event_best))
        set_default.assert_called_once_with(str(event_best))
        artifacts.return_value.refresh_catalog.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
