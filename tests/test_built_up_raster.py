"""Isolated reader tests: synthetic native rasters and temporary caches only.

Run with the optional GIS target on PYTHONPATH, e.g. python -m unittest
discover -s tests -p test_built_up_raster.py. No tests contact ESA or install
dependencies. A missing optional GIS stack skips this module's test cases.
"""

from __future__ import annotations

import math
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import numpy as np
    import rasterio
    from pyproj import CRS, Geod, Transformer
    from rasterio.errors import RasterioIOError
    from scripts import built_up_raster as raster
except ImportError:
    raster = None


class NativeTile:
    """Generate native values on demand, without allocating a 36,000² tile."""

    def __init__(self, name, value):
        self.name = name
        self.south = int(name[1:3]) * (-1 if name[0] == "S" else 1)
        self.west = int(name[4:7]) * (-1 if name[3] == "W" else 1)
        self.crs = rasterio.crs.CRS.from_epsg(4326)
        self.transform = rasterio.Affine(1 / 12000, 0, self.west, 0, -1 / 12000, self.south + 3)
        self.count = 1
        self.width = self.height = 36000
        self.dtypes = ("uint8",)
        self.nodata = 0
        self.value = value
        self.windows = []
        self.closed = False

    def close(self):
        self.closed = True

    def read(self, band, *, window, masked):
        # Signature intentionally rejects out_shape and resampling. The reader
        # must read every native pixel, not decimate the categorical map.
        assert not self.closed and band == 1 and masked
        assert window.width == window.height == 1200
        assert 0 <= window.row_off <= 34800 and 0 <= window.col_off <= 34800
        self.windows.append(window)
        rows = np.arange(int(window.row_off), int(window.row_off + window.height))[:, None]
        cols = np.arange(int(window.col_off), int(window.col_off + window.width))[None, :]
        if callable(self.value):
            data = np.broadcast_to(self.value(rows, cols), (1200, 1200)).astype(np.uint8).copy()
        else:
            data = np.full((1200, 1200), self.value, dtype=np.uint8)
        return np.ma.array(data, mask=data == 0)


@unittest.skipIf(raster is None, "Optional numpy/rasterio/pyproj dependencies unavailable")
class BuiltUpRasterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cache = Path(self.temporary.name) / "cache"
        self.tiles = []

    def opener(self, values):
        def open_tile(url, *, sharing):
            self.assertFalse(sharing)
            name = re.search(r"v200_([NS]\d{2}[EW]\d{3})_Map\.tif$", url).group(1)
            value = values.get(name, 0) if isinstance(values, dict) else values
            if isinstance(value, Exception):
                raise value
            tile = NativeTile(name, value)
            self.tiles.append(tile)
            return tile
        return mock.patch.object(raster.rasterio, "open", side_effect=open_tile)

    def test_official_tile_names_and_negative_floor(self):
        for lat, lng, expected in [
            (48.8, 2.5, "N48E000"), (-0.001, -0.001, "S03W003"),
            (-3, -3, "S03W003"), (-3.001, -3.001, "S06W006"),
            (0, 0, "N00E000"), (-48, 36, "S48E036"),
            (10, 180, "N09W180"), (10, 179.999, "N09E177"),
        ]:
            with self.subTest(lat=lat, lng=lng):
                self.assertEqual(raster.tile_name(lat, lng), expected)
        self.assertEqual(raster.tile_url(48.8, 2.5),
                         raster.BASE_URL + "/ESA_WorldCover_10m_2021_v200_N48E000_Map.tif")

    def test_every_native_pixel_matches_independent_scalar_projection(self):
        # One-column-wide built strips would disappear under many overview or
        # nearest-sample strategies. Missing and ordinary farmland are mixed in.
        pattern = lambda rows, cols: np.where(cols % 12 == 1, 50, np.where((rows + cols) % 7 == 0, 0, 40))
        lat, lng = 48.85, 2.35
        with self.opener(pattern), raster.BuiltUpRaster(self.cache) as reader:
            grid = reader.read(lat, lng, 200)
        self.assertEqual(grid.fraction.shape, (4, 4))
        expected = np.zeros((3, 4, 4), dtype=np.uint32)
        # Independent scalar oracle scans a deliberately oversized neighborhood
        # using the tile's affine coordinates, not the reader's chunk/bbox code.
        projector = Transformer.from_crs(4326, CRS.from_proj4(
            f"+proj=aeqd +lat_0={lat} +lon_0={lng} +datum=WGS84 +units=m"), always_xy=True)
        for row in range(25740, 25860):
            for col in range(28140, 28260):
                native_lat = 51 - (row + 0.5) / 12000
                native_lng = (col + 0.5) / 12000
                x, y = projector.transform(native_lng, native_lat)
                rr, cc = math.floor((200 - y) / 100), math.floor((x + 200) / 100)
                if 0 <= rr < 4 and 0 <= cc < 4:
                    value = 50 if col % 12 == 1 else (0 if (row + col) % 7 == 0 else 40)
                    expected[0, rr, cc] += 1
                    expected[1, rr, cc] += value != 0
                    expected[2, rr, cc] += value == 50
        np.testing.assert_array_equal(grid.total_count, expected[0])
        np.testing.assert_array_equal(grid.valid_count, expected[1])
        np.testing.assert_array_equal(grid.built_count, expected[2])
        np.testing.assert_allclose(grid.fraction, expected[2] / expected[1], rtol=1e-6)
        self.assertTrue(np.all((grid.fraction > 0) & (grid.fraction < 0.3)))
        self.assertTrue(grid.missing.all())
        self.assertFalse(grid.valid.any())
        self.assertFalse(grid.scan_mask(0).any())
        self.assertEqual(grid.stats.source_pixels, 1_440_000)
        self.assertEqual(grid.stats.pixels_aggregated, int(grid.total_count.sum()))

    def test_four_tile_seam_orientation_and_nodata_are_distinct_from_zero(self):
        values = {"N00W003": 50, "N00E000": 40, "S03W003": 80, "S03E000": 0}
        with self.opener(values), raster.BuiltUpRaster(self.cache) as reader:
            grid = reader.read(0, 0, 200)
        np.testing.assert_array_equal(grid.fraction[:2, :2], 1)
        np.testing.assert_array_equal(grid.fraction[:2, 2:], 0)
        np.testing.assert_array_equal(grid.fraction[2:, :2], 0)
        self.assertTrue(np.isnan(grid.fraction[2:, 2:]).all())
        self.assertTrue(grid.valid[:2].all())
        self.assertFalse(grid.valid[2:, 2:].any())
        self.assertTrue(grid.missing[2:, 2:].all())
        self.assertTrue((grid.total_count > 0).all())
        self.assertEqual(grid.stats.tiles_opened, 4)
        self.assertEqual({tile.name for tile in self.tiles}, set(values))
        self.assertTrue(all(tile.closed for tile in self.tiles))
        for tile in self.tiles:
            window = tile.windows[0]
            self.assertEqual(window.col_off, 34800 if tile.west < 0 else 0)
            self.assertEqual(window.row_off, 0 if tile.south < 0 else 34800)

    def test_partial_coverage_threshold_is_configurable(self):
        # ~2% missing: normally accepted; strict coverage mode excludes it.
        pattern = lambda rows, cols: np.where((rows * 17 + cols * 7) % 50 == 0, 0, 50)
        with self.opener(pattern), raster.BuiltUpRaster(self.cache) as reader:
            grid = reader.read(48.85, 2.35, 300)
        self.assertTrue(grid.valid.all())
        self.assertTrue(grid.missing.all())
        np.testing.assert_array_equal(grid.fraction, 1)
        with mock.patch.object(raster.rasterio, "open", side_effect=AssertionError("cache must be offline")):
            with raster.BuiltUpRaster(self.cache, min_valid_fraction=1) as reader:
                strict = reader.read(48.85, 2.35, 300)
        self.assertFalse(strict.valid.any())
        np.testing.assert_array_equal(strict.fraction, grid.fraction)

    def test_aeqd_centers_distances_and_scan_mask(self):
        with self.opener(50), raster.BuiltUpRaster(self.cache) as reader:
            grid = reader.read(48.8, 2.5, 250)
        self.assertEqual((grid.west_m, grid.north_m, grid.resolution_m), (-300, 300, 100))
        np.testing.assert_array_equal(grid.x_m, [-250, -150, -50, 50, 150, 250])
        np.testing.assert_array_equal(grid.y_m, [250, 150, 50, -50, -150, -250])
        latitudes, longitudes = grid.lat_lng()
        self.assertGreater(latitudes[0, 0], latitudes[-1, 0])
        self.assertGreater(longitudes[0, -1], longitudes[0, 0])
        _, _, distances = Geod(ellps="WGS84").inv(
            np.full(grid.fraction.shape, 2.5), np.full(grid.fraction.shape, 48.8), longitudes, latitudes)
        np.testing.assert_allclose(distances, np.hypot(grid.x_m[None, :], grid.y_m[:, None]), atol=1e-6)
        np.testing.assert_array_equal(grid.within_radius(250), distances <= 250)
        self.assertTrue(grid.scan_mask(1).all())

    def test_dateline_wrap_uses_two_neighboring_tiles(self):
        with self.opener({"N09E177": 50, "N09W180": 40}), raster.BuiltUpRaster(self.cache) as reader:
            grid = reader.read(10.05, 179.9999, 200)
        self.assertEqual({tile.name for tile in self.tiles}, {"N09E177", "N09W180"})
        self.assertTrue(grid.valid.all())
        self.assertTrue((grid.fraction[:, 0] == 1).all())
        self.assertTrue((grid.fraction[:, -1] == 0).all())
        _, longitudes = grid.lat_lng()
        self.assertTrue((longitudes[:, 0] > 179).all())
        self.assertTrue((longitudes[:, -1] < -179).all())

    def test_same_and_shifted_city_reuse_native_cache_without_network(self):
        pattern = lambda rows, cols: np.where((rows + cols) % 3 == 0, 50, 40)
        with self.opener(pattern), raster.BuiltUpRaster(self.cache) as reader:
            original = reader.read(48.85, 2.35, 300)
        with mock.patch.object(raster.rasterio, "open", side_effect=AssertionError("unexpected network")):
            with raster.BuiltUpRaster(self.cache) as reader:
                warm = reader.read(48.85, 2.35, 300)
                shifted = reader.read(48.8505, 2.3505, 400)
        np.testing.assert_array_equal(original.built_count, warm.built_count)
        self.assertEqual(warm.stats.cache_hits, 1)
        self.assertEqual(warm.stats.source_pixels, 0)
        self.assertEqual(shifted.stats.cache_hits, 1)
        self.assertEqual(shifted.stats.tiles_opened, 0)
        self.assertTrue((shifted.fraction > 0).all())
        files = list(self.cache.rglob("*.npz"))
        self.assertEqual(len(files), 1)
        with np.load(files[0], allow_pickle=False) as saved:
            self.assertEqual(saved["built_count"].shape, (100, 100))
            self.assertTrue((saved["built_count"] == 48).all())
            self.assertTrue((saved["valid_count"] == 144).all())
            self.assertEqual(saved["built_bits"].size, 180000)

    def test_summary_mode_preserves_fraction_and_missing_coverage(self):
        # Each exact 12x12 summary has 36 built, 36 ordinary, 72 missing.
        # Averaging only nonmissing pixels for coverage would incorrectly say 1.
        pattern = lambda rows, cols: np.where(cols % 4 == 0, 50, np.where(cols % 4 == 1, 40, 0))
        with self.opener(pattern), raster.BuiltUpRaster(self.cache, summary_mode=True) as reader:
            grid = reader.read(48.85, 2.35, 300)
        np.testing.assert_allclose(grid.fraction, 0.5, atol=1e-6)
        np.testing.assert_allclose(grid.valid_fraction, 0.5, atol=1e-6)
        self.assertFalse(grid.valid.any())
        self.assertTrue(grid.missing.all())
        self.assertIsNone(grid.built_count)
        self.assertEqual(grid.aggregation, "summary_average")
        with mock.patch.object(raster.rasterio, "open", side_effect=AssertionError("network")):
            with raster.BuiltUpRaster(self.cache, summary_mode=True) as reader:
                # Prove the fast path doesn't inflate/project native masks.
                with mock.patch.object(reader, "_chunk", side_effect=AssertionError("native read")):
                    warm = reader.read(48.8505, 2.3505, 300)
        np.testing.assert_allclose(warm.fraction, 0.5, atol=1e-6)
        self.assertEqual(warm.stats.cache_hits, 1)
        self.assertEqual(warm.stats.source_pixels, 0)

    def test_summary_mode_cross_tile_and_dateline_coverage(self):
        with self.opener({"N00W003": 50, "N00E000": 40, "S03W003": 80, "S03E000": 0}):
            with raster.BuiltUpRaster(self.cache, summary_mode=True) as reader:
                grid = reader.read(0, 0, 300)
        self.assertEqual(grid.fraction[0, 0], 1)
        self.assertEqual(grid.fraction[0, -1], 0)
        self.assertEqual(grid.fraction[-1, 0], 0)
        self.assertTrue(np.isnan(grid.fraction[-1, -1]))
        self.assertFalse(grid.valid[-1, -1])
        with self.opener({"N09E177": 50, "N09W180": 40}):
            with raster.BuiltUpRaster(self.cache, summary_mode=True) as reader:
                wrapped = reader.read(10.05, 179.9999, 300)
        self.assertTrue(wrapped.valid.all())
        np.testing.assert_allclose(wrapped.fraction[:, 0], 1, atol=1e-6)
        np.testing.assert_allclose(wrapped.fraction[:, -1], 0, atol=1e-6)

    def test_failed_read_resumes_completed_chunks_and_reuses_open_tile(self):
        with self.opener(50), raster.BuiltUpRaster(self.cache) as reader:
            original_chunk = reader._chunk
            calls = 0
            def interrupted(*args):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise RuntimeError("simulated interruption")
                return original_chunk(*args)
            with mock.patch.object(reader, "_chunk", side_effect=interrupted):
                with self.assertRaisesRegex(RuntimeError, "interruption"):
                    reader.read(48.8, 2.5, 300)
            self.assertEqual(len(list(self.cache.rglob("*.npz"))), 1)
            grid = reader.read(48.8, 2.5, 300)
            self.assertEqual(grid.stats.cache_hits, 1)
            self.assertEqual(grid.stats.cache_misses, 3)
            self.assertEqual(len(self.tiles), 1)
            self.assertEqual(len(self.tiles[0].windows), 4)
        self.assertTrue(grid.valid.all())

    def test_corrupt_cache_rebuilt_and_atomic_failure_leaves_no_partial_entry(self):
        with self.opener(50), raster.BuiltUpRaster(self.cache) as reader:
            reader.read(48.85, 2.35, 100)
            path = next(self.cache.rglob("*.npz"))
            path.write_bytes(b"interrupted archive")
            recovered = reader.read(48.85, 2.35, 100)
            self.assertEqual(recovered.stats.cache_misses, 1)
            self.assertEqual(len(self.tiles[0].windows), 2)
            # A valid immutable destination is now accepted without replacing;
            # use an invalid entry to exercise a genuine publication failure.
            path.write_bytes(b"invalid destination to repair")
            previous = path.read_bytes()
            bits = np.zeros((1200, 1200), dtype=bool)
            with mock.patch.object(raster.os, "replace", side_effect=OSError("disk error")):
                with self.assertRaisesRegex(OSError, "disk error"):
                    reader._save_chunk(path, bits, bits)
            self.assertEqual(path.read_bytes(), previous)
            self.assertFalse(list(self.cache.rglob("*.tmp")))

    def test_cache_publication_accepts_valid_immutable_entry_without_replacing(self):
        bits = np.ones((1200, 1200), dtype=bool)
        with raster.BuiltUpRaster(self.cache) as reader:
            path = reader._cache_path(488, 23)
            reader._save_chunk(path, bits, bits)
            previous = path.read_bytes()
            with mock.patch.object(raster.os, "replace", side_effect=AssertionError("immutable cache")):
                self.assertEqual(reader._save_chunk(path, ~bits, bits), 0)
            self.assertEqual(path.read_bytes(), previous)
            self.assertFalse(list(self.cache.rglob("*.tmp")))

    def test_windows_publication_race_accepts_winner_and_removes_losing_temp(self):
        bits = np.ones((1200, 1200), dtype=bool)
        busy = PermissionError("destination held by concurrent reader")
        busy.winerror = 5
        original_replace = raster.os.replace
        with raster.BuiltUpRaster(self.cache) as reader:
            path = reader._cache_path(488, 23)
            winner = path.with_name("winner.npz")
            reader._save_chunk(winner, bits, bits)
            expected = winner.read_bytes()

            def publish_then_deny(_temporary, destination):
                # The winner appears after the final pre-publication check.
                original_replace(winner, destination)
                raise busy

            with mock.patch.object(raster.os, "replace", side_effect=publish_then_deny) as replace:
                with mock.patch.object(raster.time, "sleep") as sleep:
                    self.assertEqual(reader._save_chunk(path, ~bits, bits), 0)
            replace.assert_called_once()
            sleep.assert_not_called()
            self.assertEqual(path.read_bytes(), expected)
            self.assertIsNotNone(reader._cached_chunk(path))
            self.assertFalse(list(self.cache.rglob("*.tmp")))

    def test_windows_busy_invalid_destination_retries_then_publishes(self):
        bits = np.ones((1200, 1200), dtype=bool)
        busy = PermissionError("sharing violation")
        busy.winerror = 32
        original_replace = raster.os.replace
        with raster.BuiltUpRaster(self.cache) as reader:
            path = reader._cache_path(488, 23)
            path.parent.mkdir(parents=True)
            path.write_bytes(b"invalid cache")
            attempts = 0

            def temporarily_busy(temporary, destination):
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    raise busy
                return original_replace(temporary, destination)

            with mock.patch.object(raster.os, "replace", side_effect=temporarily_busy):
                with mock.patch.object(raster.time, "sleep") as sleep:
                    self.assertEqual(reader._save_chunk(path, bits, bits), path.stat().st_size)
            self.assertEqual(sleep.call_args_list, [mock.call(0.05), mock.call(0.1)])
            self.assertEqual(attempts, 3)
            self.assertIsNotNone(reader._cached_chunk(path))
            self.assertFalse(list(self.cache.rglob("*.tmp")))

    def test_persistent_windows_permission_failure_is_not_suppressed(self):
        bits = np.ones((1200, 1200), dtype=bool)
        busy = PermissionError("access denied")
        busy.winerror = 5
        with raster.BuiltUpRaster(self.cache) as reader:
            path = reader._cache_path(488, 23)
            with mock.patch.object(raster.os, "replace", side_effect=busy) as replace:
                with mock.patch.object(raster.time, "sleep") as sleep:
                    with self.assertRaises(PermissionError) as failure:
                        reader._save_chunk(path, bits, bits)
            self.assertIs(failure.exception, busy)
            self.assertEqual(replace.call_count, 6)
            self.assertEqual(sleep.call_args_list, [mock.call(delay) for delay in (0.05, 0.1, 0.2, 0.4, 0.8)])
            self.assertFalse(path.exists())
            self.assertFalse(list(self.cache.rglob("*.tmp")))

    def test_missing_http_tile_is_explicit_nodata_and_is_retried(self):
        absent = RasterioIOError("HTTP response code: 404")
        with self.opener({"N48E000": absent}), raster.BuiltUpRaster(self.cache) as reader:
            grid = reader.read(48.8, 2.5, 100)
        self.assertEqual(grid.stats.missing_tiles, ["N48E000"])
        self.assertFalse(grid.valid.any())
        self.assertTrue(np.isnan(grid.fraction).all())
        self.assertTrue((grid.valid_fraction == 0).all())
        self.assertFalse(list(self.cache.rglob("*.npz")))
        with self.opener(50), raster.BuiltUpRaster(self.cache) as reader:
            recovered = reader.read(48.8, 2.5, 100)
        self.assertTrue(recovered.valid.all())

    def test_http_errors_are_not_cached_as_empty_land(self):
        for message in ("HTTP response code: 403", "HTTP response code: 500", "Operation timed out", "Range downloading not supported"):
            with self.subTest(message=message), self.opener(RasterioIOError(message)):
                with raster.BuiltUpRaster(self.cache) as reader:
                    with self.assertRaisesRegex(RuntimeError, "N48E000"):
                        reader.read(48.85, 2.35, 100)
                self.assertFalse(list(self.cache.rglob("*.npz")))

    def test_open_handle_lru_closes_evicted_tiles(self):
        with self.opener(50), raster.BuiltUpRaster(self.cache, max_open_tiles=1) as reader:
            grid = reader.read(0, 0, 100)
            self.assertEqual(len(reader._datasets), 1)
            self.assertEqual(sum(not tile.closed for tile in self.tiles), 1)
        self.assertTrue(grid.valid.all())
        self.assertTrue(all(tile.closed for tile in self.tiles))

    def test_invalid_inputs_and_limits_fail_before_cache_or_network(self):
        with mock.patch.object(raster.rasterio, "open", side_effect=AssertionError("network")):
            with raster.BuiltUpRaster(self.cache) as reader:
                for args in [(90, 0, 100), (0, 181, 100), (0, 0, 0), (0, 0, -1),
                             (float("nan"), 0, 100), (0, 0, 100, 0), (0, 0, 100, float("nan")),
                             (0, 0, 1_000_000), (89.999, 0, 1000), (0, 0, 100, 9)]:
                    with self.subTest(args=args), self.assertRaises(ValueError):
                        reader.read(*args)
            with raster.BuiltUpRaster(self.cache, max_chunks=1) as reader:
                with self.assertRaisesRegex(ValueError, "max_chunks"):
                    reader.read(48.8, 2.5, 300)
        self.assertFalse(self.cache.exists())
        with self.assertRaisesRegex(RuntimeError, "closed"):
            reader.read(48.8, 2.5, 100)

    def test_real_sparse_geotiff_reads_native_window(self):
        source = Path(self.temporary.name) / "native.tif"
        original_open = rasterio.open
        # Sparse TIFF metadata matches the real product, but only one native
        # chunk is written. This exercises real GDAL window and nodata behavior.
        with original_open(source, "w", driver="GTiff", width=36000, height=36000,
                           count=1, dtype="uint8", nodata=0, crs="EPSG:4326",
                           transform=rasterio.Affine(1/12000, 0, 0, 0, -1/12000, 51),
                           tiled=True, blockxsize=256, blockysize=256, compress="deflate", SPARSE_OK=True) as ds:
            raw = np.full((1200, 1200), 40, dtype=np.uint8)
            raw[:, ::2] = 50
            ds.write(raw, 1, window=rasterio.windows.Window(27600, 25200, 1200, 1200))
        with mock.patch.object(raster.rasterio, "open", side_effect=lambda *_args, **_kwargs: original_open(source)):
            with raster.BuiltUpRaster(self.cache) as reader:
                grid = reader.read(48.85, 2.35, 100)
        self.assertTrue(grid.valid.all())
        np.testing.assert_allclose(grid.fraction, 0.5, atol=0.04)
        self.assertEqual(grid.stats.source_pixels, 1_440_000)


if __name__ == "__main__":
    unittest.main()
