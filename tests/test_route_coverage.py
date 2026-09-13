from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from dataclasses import asdict, replace
from pathlib import Path

from bloompilot.geo_recognition import GeoRecognitionWaypoint, validate_geo_waypoints
from bloompilot.route_coverage import (
    coverage_km_to_radius,
    coverage_radii_from_km,
    grid_step_km,
    radius_to_coverage_km,
    viewport_half_km,
)


def _custom_step_waypoint() -> GeoRecognitionWaypoint:
    horizontal = grid_step_km("horizontal")
    vertical = grid_step_km("vertical")
    return GeoRecognitionWaypoint(
        id="calibrated",
        name="Calibrated settlement",
        lat=60.0,
        lng=25.0,
        left_radius=0,
        right_radius=0,
        up_radius=0,
        down_radius=0,
        left_km=2 * horizontal,
        right_km=3 * horizontal,
        up_km=4 * vertical,
        down_km=vertical,
        horizontal_step_km=horizontal / 2,
        vertical_step_km=vertical / 2,
        scan_spans=((0, -4, 6), (8, 0, 0), (-2, 0, 0)),
    )


class RouteCoverageTests(unittest.TestCase):
    def test_km_conversion_always_covers_the_requested_boundary(self) -> None:
        for axis in ("horizontal", "vertical"):
            for step in (None, grid_step_km(axis) / 2, grid_step_km(axis) * 2):
                for distance in (0.0, 0.5, 1.0, 7.3, 20.147326, 74.491):
                    with self.subTest(axis=axis, step=step, distance=distance):
                        radius = coverage_km_to_radius(distance, axis, step)
                        covered = radius_to_coverage_km(radius, axis, step)
                        self.assertGreaterEqual(covered + 1e-12, distance)
                        if radius:
                            self.assertLess(
                                radius_to_coverage_km(radius - 1, axis, step), distance
                            )

    def test_reference_scale_uses_the_current_phone_dimensions(self) -> None:
        self.assertAlmostEqual(viewport_half_km("horizontal"), 0.31395, places=6)
        self.assertAlmostEqual(grid_step_km("horizontal"), 0.413948889, places=9)
        self.assertAlmostEqual(grid_step_km("vertical"), 0.897664444, places=9)

    def test_step_scales_the_initial_viewport_as_well_as_each_swipe(self) -> None:
        for axis in ("horizontal", "vertical"):
            default_step = grid_step_km(axis)
            default_half = viewport_half_km(axis)
            self.assertEqual(viewport_half_km(axis, default_step), default_half)
            self.assertEqual(
                coverage_km_to_radius(7.3, axis, default_step),
                coverage_km_to_radius(7.3, axis),
            )
            for scale in (0.5, 2.0):
                with self.subTest(axis=axis, scale=scale):
                    step = default_step * scale
                    half = default_half * scale
                    self.assertEqual(grid_step_km(axis, step), step)
                    self.assertEqual(viewport_half_km(axis, step), half)
                    self.assertEqual(radius_to_coverage_km(0, axis, step), half)
                    self.assertEqual(coverage_km_to_radius(half, axis, step), 0)
                    self.assertEqual(coverage_km_to_radius(half + step / 10, axis, step), 1)

    def test_halved_steps_double_radii_and_keep_the_larger_mask_valid(self) -> None:
        calibrated = _custom_step_waypoint()
        default = replace(calibrated, horizontal_step_km=None, vertical_step_km=None)
        self.assertEqual(default.resolved_radii, (2, 3, 4, 1))
        self.assertEqual(calibrated.resolved_radii, (4, 6, 8, 2))
        self.assertEqual(validate_geo_waypoints([calibrated]), 13)
        with self.assertRaisesRegex(ValueError, "有效格帶超出"):
            validate_geo_waypoints([default])
        default = replace(default, scan_spans=((0, -2, 3), (4, 0, 0), (-1, 0, 0)))
        self.assertEqual(validate_geo_waypoints([default]), 8)
        self.assertNotEqual(default.scan_spans, calibrated.scan_spans)

    def test_each_axis_can_use_a_custom_step_independently(self) -> None:
        waypoint = _custom_step_waypoint()
        distances = (waypoint.left_km, waypoint.right_km, waypoint.up_km, waypoint.down_km)
        for horizontal, vertical, expected in (
            (waypoint.horizontal_step_km, None, (4, 6, 4, 1)),
            (None, waypoint.vertical_step_km, (2, 3, 8, 2)),
        ):
            with self.subTest(horizontal=horizontal, vertical=vertical):
                self.assertEqual(
                    coverage_radii_from_km(
                        *distances, horizontal_step_km=horizontal, vertical_step_km=vertical
                    ),
                    expected,
                )
                self.assertEqual(
                    replace(waypoint, horizontal_step_km=horizontal, vertical_step_km=vertical).resolved_radii,
                    expected,
                )

    def test_radius_only_waypoints_keep_their_grid_counts(self) -> None:
        waypoint = replace(
            _custom_step_waypoint(),
            left_km=None, right_km=None, up_km=None, down_km=None,
            left_radius=1, right_radius=2, up_radius=3, down_radius=4,
            scan_spans=None,
        )
        self.assertEqual(waypoint.resolved_radii, (1, 2, 3, 4))

    def test_custom_steps_must_be_positive_and_finite(self) -> None:
        for axis in ("horizontal", "vertical"):
            for step in (0.0, -1.0, float("nan"), float("inf"), float("-inf")):
                for function, args in (
                    (grid_step_km, (axis,)),
                    (viewport_half_km, (axis,)),
                    (coverage_km_to_radius, (0.0, axis)),
                    (radius_to_coverage_km, (0, axis)),
                ):
                    with self.subTest(axis=axis, step=step, function=function.__name__):
                        with self.assertRaises(ValueError):
                            function(*args, step_km=step)
                with self.subTest(axis=axis, step=step, function="resolved_radii"):
                    with self.assertRaises(ValueError):
                        replace(_custom_step_waypoint(), **{f"{axis}_step_km": step}).resolved_radii

    def test_frontend_parser_and_radius_edits_preserve_calibration(self) -> None:
        node = os.environ.get("NODE_BINARY") or shutil.which("node")
        if not node:
            self.skipTest("Node with stripTypeScriptTypes is required; set NODE_BINARY")
        # Execute the actual parser and edit callbacks without mounting the UI.
        script = r"""
const assert = require('node:assert/strict')
const { readFileSync } = require('node:fs')
const { stripTypeScriptTypes } = require('node:module')
const { runInNewContext } = require('node:vm')
const fixture = JSON.parse(readFileSync(0, 'utf8'))
const read = (path) => readFileSync(`frontend/src/${path}`, 'utf8')
const panel = read('components/GeoPanel.tsx')
const helpers = panel.slice(panel.indexOf('const id ='), panel.indexOf('\nfunction storedSequences'))
const updater = panel.slice(panel.indexOf('  const updateWaypoint ='), panel.indexOf('  const moveWaypoint ='))
const source = [read('projectDefaults.ts'), read('routeCoverage.ts'), read('utils/sequenceEditor.ts'), helpers, updater]
  .join('\n').replace(/^import .*$/gm, '').replace(/^export /gm, '')
const checks = `
const radii = (item) => [item.left_radius, item.right_radius, item.up_radius, item.down_radius]
const parse = (item) => normalizeSequence({ waypoints: [item] }).waypoints[0]
const original = parse(fixture)
assert.deepEqual(Array.from(radii(original)), [4, 6, 8, 2])
assert.equal(JSON.stringify(original.scan_spans), JSON.stringify(fixture.scan_spans))
assert.equal(waypointPointCount(original), 13)
assert.deepEqual(parse(original), original)
assert.throws(() => parse({ ...fixture, horizontal_step_km: undefined, vertical_step_km: undefined }))
const withoutMask = { ...fixture, scan_spans: undefined }
assert.deepEqual(radii(parse({ ...withoutMask, horizontal_step_km: undefined, vertical_step_km: undefined })), [2, 3, 4, 1])
assert.deepEqual(radii(parse({ ...withoutMask, vertical_step_km: undefined })), [4, 6, 4, 1])
assert.deepEqual(radii(parse({ ...withoutMask, horizontal_step_km: undefined })), [2, 3, 8, 2])
const radiusOnly = parse({ ...original, left_km: undefined, right_km: undefined, up_km: undefined, down_km: undefined })
assert.deepEqual(radii(radiusOnly), radii(original))
assert.deepEqual(radiusOnly.scan_spans, original.scan_spans)
for (const [direction, axis] of [['left', 'horizontal'], ['right', 'horizontal'], ['up', 'vertical'], ['down', 'vertical']]) {
  const step = original[axis + '_step_km']
  assert.equal(radiusOnly[direction + '_km'], radiusToCoverageKm(original[direction + '_radius'], axis, step))
}
for (const axis of ['horizontal', 'vertical']) {
  const step = gridStepKm(axis) / 2
  const half = viewportHalfKm(axis) / 2
  assert.equal(viewportHalfKm(axis, step), half)
  assert.equal(coverageKmToRadius(half, axis, step), 0)
  assert.equal(coverageKmToRadius(half + step / 10, axis, step), 1)
  for (const invalid of [0, -1, NaN, Infinity, -Infinity]) {
    assert.ok(Number.isNaN(gridStepKm(axis, invalid)))
    assert.ok(Number.isNaN(viewportHalfKm(axis, invalid)))
    assert.ok(Number.isNaN(coverageKmToRadius(0, axis, invalid)))
    assert.ok(Number.isNaN(radiusToCoverageKm(0, axis, invalid)))
    assert.throws(() => parse({ ...withoutMask, [axis + '_step_km']: invalid }))
  }
}
let sequence = { waypoints: [original] }
const edited = { current: false }
const originals = { current: new Map() }
const setSequenceDirty = () => {}
const setSequence = (update) => { sequence = update(sequence) }
for (const [direction, axis] of [['left', 'horizontal'], ['right', 'horizontal'], ['up', 'vertical'], ['down', 'vertical']]) {
  sequence = { waypoints: [original] }
  const item = sequence.waypoints[0]
  const index = 0
  const pattern = new RegExp('onValue=\\\\{(value => updateWaypoint\\\\(index, \\\\{ ' + direction + '_km: value, ' + direction + '_radius: .*? \\\\}\\\\))\\\\}')
  const match = panel.match(pattern)
  assert.ok(match, direction + ' edit callback missing')
  const edit = eval(match[1])
  edit(gridStepKm(axis) * 5)
  assert.equal(sequence.waypoints[0][direction + '_radius'], 10)
  assert.equal(sequence.waypoints[0].scan_spans, undefined)
  assert.equal(parse(sequence.waypoints[0])[direction + '_radius'], 10)
}
sequence = { waypoints: [original] }
updateWaypoint(0, { name: 'Renamed' })
assert.deepEqual(sequence.waypoints[0].scan_spans, original.scan_spans)
for (const patch of [{ lat: 61 }, { lng: 26 }, { horizontal_step_km: gridStepKm('horizontal') }, { vertical_step_km: gridStepKm('vertical') }]) {
  sequence = { waypoints: [original] }
  updateWaypoint(0, patch)
  assert.equal(sequence.waypoints[0].scan_spans, undefined)
}
`
// JSON keeps assertion values in the same realm as the executed TypeScript.
runInNewContext(stripTypeScriptTypes(source) + '\nconst fixture = JSON.parse(fixtureJson)\n' + checks,
  { assert, panel, fixtureJson: JSON.stringify(fixture) })
"""
        result = subprocess.run(
            [node, "--disable-warning=ExperimentalWarning", "-e", script],
            cwd=Path(__file__).resolve().parents[1],
            input=json.dumps(asdict(_custom_step_waypoint())),
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_waypoint_recomputes_stale_grid_fields_from_km(self) -> None:
        waypoint = GeoRecognitionWaypoint(
            id="berlin",
            name="Berlin",
            lat=52.52437,
            lng=13.41053,
            left_radius=0,
            right_radius=0,
            up_radius=0,
            down_radius=0,
            left_km=20.147326,
            right_km=23.852674,
            up_km=19.64932,
            down_km=8.35068,
        )
        self.assertEqual(waypoint.resolved_radii, (48, 57, 22, 9))


if __name__ == "__main__":
    unittest.main()
