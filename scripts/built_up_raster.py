"""Bounded ESA WorldCover 2021 v200 built-surface reads for city scans.

Requires numpy, rasterio and pyproj in the caller's environment. No installation
or sys.path changes happen here. Example (coordinates are latitude, longitude)::

    with BuiltUpRaster(Path("data/reference/worldcover"), summary_mode=True) as reader:
        grid = reader.read(48.8, 2.5, radius_m=3000)
        mask = grid.scan_mask(0.20) & grid.within_radius(3000)
        latitudes, longitudes = grid.lat_lng()
        points = list(zip(latitudes[mask], longitudes[mask]))

Rows run north to south in local WGS84 AEQD. Every native pixel center in the
output square is assigned to exactly one output cell. ``fraction`` is the exact
count of class-50 pixels / count of valid pixels in that cell, NOT a density
inferred from nearest-neighbour samples or categorical overviews. Cell boundary
membership is quantized at native pixel centers; this is not polygon-clipped
area integration. Nominal 10 m geographic pixels are narrower at high latitude.
For large city batches, opt into ``summary_mode=True``: exact 12x12-pixel built
and valid counts are area-averaged from the geographic ~100 m grid into AEQD.
These reprojected fractions are approximate (sub-summary spatial detail is lost)
and count arrays are None. Missing input contributes zero valid coverage, rather
than being silently omitted from the coverage average. Both modes share caches.

The cache contains independent, atomically published 0.1-degree chunks (about
11 km north/south). Each holds 0.001-degree, 12x12-native-pixel count summaries
AND packed native built/valid masks. The latter preserve original detail when
different cities request different AEQD grids. Interrupted runs reuse completed
chunks. No whole COG, global grid, or permanent cache of HTTP failures is made.
Use one reader for a serial city batch to reuse its bounded COG handle LRU.

Source: https://esa-worldcover.org/en/data-access
Naming, class definitions: https://esa-worldcover.s3.eu-central-1.amazonaws.com/
v200/2021/docs/WorldCover_PUM_V2.0.pdf
Class 50 includes roads/industry/greenhouses, and does not prove habitation.
Attribution: (c) ESA WorldCover project 2021 / Contains modified Copernicus
Sentinel data (2021) processed by ESA WorldCover consortium (CC BY 4.0).
"""

from __future__ import annotations

import math
import os
import re
import tempfile
import time
import zipfile
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import rasterio
from pyproj import CRS, Geod, Transformer
from rasterio.errors import RasterioIOError
from rasterio.warp import Resampling, reproject
from rasterio.windows import Window


BASE_URL = "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map"
DEFAULT_CACHE = Path(__file__).resolve().parents[1] / "data/reference/worldcover"
PIXELS_PER_DEGREE = 12_000
CHUNK_PIXELS = 1_200
SUMMARY_FACTOR = 12
CLASS_BUILT_UP = 50
VALID_CLASSES = (10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 100)
CACHE_VERSION = "worldcover2021-v200-native1200-v1"
_GEOD = Geod(ellps="WGS84")


def tile_name(lat: float, lng: float) -> str:
    """Name the 3-degree tile's southwest corner (floor, including negatives)."""
    if not (math.isfinite(lat) and math.isfinite(lng) and -90 <= lat < 90):
        raise ValueError("Tile latitude must be in [-90, 90); longitude must be finite")
    south = 3 * math.floor(lat / 3)
    west = 3 * math.floor((((lng + 180) % 360) - 180) / 3)
    return f"{'N' if south >= 0 else 'S'}{abs(south):02d}{'E' if west >= 0 else 'W'}{abs(west):03d}"


def tile_url(lat: float, lng: float) -> str:
    return f"{BASE_URL}/ESA_WorldCover_10m_2021_v200_{tile_name(lat, lng)}_Map.tif"


def _projection(lat: float, lng: float) -> CRS:
    return CRS.from_proj4(f"+proj=aeqd +lat_0={lat:.15g} +lon_0={lng:.15g} +datum=WGS84 +units=m")


@dataclass
class ReadStats:
    """Per-read work counters; source_pixels includes reusable chunk read-ahead.

    HTTP byte totals are unavailable through Rasterio's public API. They are
    deliberately not estimated from uncompressed source pixels. Set http_debug
    on the reader to inspect GDAL's actual HTTP Content-Length/Range responses.
    """

    cache_hits: int = 0
    cache_misses: int = 0
    cache_bytes_written: int = 0
    source_pixels: int = 0
    pixels_aggregated: int = 0
    tiles_opened: int = 0
    missing_tiles: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0


@dataclass
class BuiltUpGrid:
    origin_lat: float
    origin_lng: float
    west_m: float
    north_m: float
    resolution_m: float
    fraction: np.ndarray
    valid: np.ndarray
    valid_fraction: np.ndarray
    built_count: np.ndarray | None = None
    valid_count: np.ndarray | None = None
    total_count: np.ndarray | None = None
    stats: ReadStats = field(default_factory=ReadStats)
    aggregation: str = "native_pixel_counts"

    @property
    def x_m(self) -> np.ndarray:
        """One-dimensional eastings at column centers."""
        return self.west_m + (np.arange(self.fraction.shape[1]) + 0.5) * self.resolution_m

    @property
    def y_m(self) -> np.ndarray:
        """One-dimensional northings at row centers (descending)."""
        return self.north_m - (np.arange(self.fraction.shape[0]) + 0.5) * self.resolution_m

    @property
    def missing(self) -> np.ndarray:
        """Any missing native data, including partial coverage and absent tiles."""
        return self.valid_fraction < 1

    def lat_lng(self) -> tuple[np.ndarray, np.ndarray]:
        """Two 2-D arrays matching fraction: (latitude, longitude), cell centers."""
        inverse = Transformer.from_crs(_projection(self.origin_lat, self.origin_lng), 4326, always_xy=True)
        lng, lat = inverse.transform(*np.meshgrid(self.x_m, self.y_m))
        return lat, lng

    def within_radius(self, radius_m: float) -> np.ndarray:
        """A center-based circle about the origin; read() itself returns a square."""
        if not math.isfinite(radius_m) or radius_m < 0:
            raise ValueError("radius_m must be finite and nonnegative")
        return self.y_m[:, None] ** 2 + self.x_m[None, :] ** 2 <= radius_m**2

    def scan_mask(self, min_fraction: float = 0.20) -> np.ndarray:
        if not math.isfinite(min_fraction) or not 0 <= min_fraction <= 1:
            raise ValueError("min_fraction must be in [0, 1]")
        return self.valid & np.isfinite(self.fraction) & (self.fraction >= min_fraction)


def _summary(bits: np.ndarray) -> np.ndarray:
    n = CHUNK_PIXELS // SUMMARY_FACTOR
    return bits.reshape(n, SUMMARY_FACTOR, n, SUMMARY_FACTOR).sum(axis=(1, 3), dtype=np.uint16)


def _cell_limits(low: float, high: float) -> tuple[int, int]:
    """Native global pixel indexes whose centers lie inside [low, high]."""
    return math.ceil(low * PIXELS_PER_DEGREE - 0.5), math.floor(high * PIXELS_PER_DEGREE - 0.5) + 1


class BuiltUpRaster:
    """Serial, bounded reader; close it or use a context manager for batch work.

    Defaults cap an output at 256x256 cells, requests at 64 native chunks,
    each source read at 1.44M pixels, and open remote tiles at four. Temporary
    reprojection arrays cover only 64 native rows. GDAL's decoded block cache
    is capped at 16 MiB during reads; its HTTP range cache at 8 MiB. Returned
    grids use about 22 bytes/cell plus arrays explicitly requested by callers.
    """

    def __init__(
        self,
        cache_dir: Path = DEFAULT_CACHE,
        *,
        min_valid_fraction: float = 0.95,
        max_output_cells: int = 256 * 256,
        max_chunks: int = 64,
        max_open_tiles: int = 4,
        http_debug: bool = False,
        summary_mode: bool = False,
    ) -> None:
        if not math.isfinite(min_valid_fraction) or not 0 < min_valid_fraction <= 1:
            raise ValueError("min_valid_fraction must be in (0, 1]")
        for name, value in (("max_output_cells", max_output_cells), ("max_chunks", max_chunks), ("max_open_tiles", max_open_tiles)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self.cache_dir = Path(cache_dir) / CACHE_VERSION
        self.min_valid_fraction = min_valid_fraction
        self.max_output_cells = max_output_cells
        self.max_chunks = max_chunks
        self.max_open_tiles = max_open_tiles
        self.http_debug = http_debug
        self.summary_mode = summary_mode
        self._datasets: OrderedDict[str, object] = OrderedDict()
        self._closed = False

    def __enter__(self) -> BuiltUpRaster:
        if self._closed:
            raise RuntimeError("Reader is closed")
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        while self._datasets:
            self._datasets.popitem()[1].close()
        self._closed = True

    def _dataset(self, south_chunk: int, west_chunk: int, stats: ReadStats):
        south, west = 3 * (south_chunk // 30), 3 * (west_chunk // 30)
        name = tile_name(south, west)
        if name in stats.missing_tiles:
            return None
        if name in self._datasets:
            self._datasets.move_to_end(name)
            return self._datasets[name]
        # Evict before opening so the configured descriptor bound is respected.
        if len(self._datasets) >= self.max_open_tiles:
            self._datasets.popitem(last=False)[1].close()
        try:
            ds = rasterio.open(tile_url(south, west), sharing=False)
        except RasterioIOError as exc:
            # Only a definite absent object is nodata. Authentication, timeouts,
            # range failures and malformed TIFFs must not become empty cities.
            if re.search(r"\bHTTP response code:\s*(404|410)\b", str(exc)):
                stats.missing_tiles.append(name)
                return None
            raise RuntimeError(f"WorldCover tile {name} could not be read: {exc}") from exc
        expected = rasterio.Affine(1 / PIXELS_PER_DEGREE, 0, west, 0, -1 / PIXELS_PER_DEGREE, south + 3)
        if not (
            ds.crs == rasterio.crs.CRS.from_epsg(4326)
            and ds.count == 1 and ds.width == 36000 and ds.height == 36000
            and ds.dtypes == ("uint8",) and ds.nodata == 0
            and np.allclose(tuple(ds.transform), tuple(expected), rtol=0, atol=1e-12)
        ):
            ds.close()
            raise ValueError(f"Unexpected WorldCover native raster layout: {name}")
        self._datasets[name] = ds
        stats.tiles_opened += 1
        return ds

    def _cache_path(self, south_chunk: int, west_chunk: int) -> Path:
        return self.cache_dir / tile_name(south_chunk / 10, west_chunk / 10) / f"{south_chunk}_{west_chunk}.npz"

    def _cached_chunk(self, path: Path) -> tuple[np.ndarray, np.ndarray] | None:
        try:
            with np.load(path, allow_pickle=False) as saved:
                size = CHUNK_PIXELS * CHUNK_PIXELS // 8
                arrays = []
                for key in ("built", "valid"):
                    packed = saved[f"{key}_bits"]
                    if packed.dtype != np.uint8 or packed.shape != (size,):
                        return None
                    bits = np.unpackbits(packed).reshape(CHUNK_PIXELS, CHUNK_PIXELS).view(np.bool_)
                    counts = saved[f"{key}_count"]
                    if counts.dtype != np.uint16 or not np.array_equal(counts, _summary(bits)):
                        return None
                    arrays.append(bits)
                if np.any(arrays[0] & ~arrays[1]):
                    return None
                return arrays[0], arrays[1]
        except (OSError, ValueError, KeyError, EOFError, zipfile.BadZipFile):
            return None

    def _save_chunk(self, path: Path, built: np.ndarray, valid: np.ndarray) -> int:
        """Publish an immutable chunk, accepting a valid concurrent winner.

        Windows can deny replacing a destination held open by another reader.
        Revalidate after that race; retry only Windows access/sharing/lock errors
        when there is no readable valid winner. Persistent failures still raise.
        The return value counts only bytes published by this invocation.
        """
        if self._cached_chunk(path) is not None:
            return 0
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.stem + ".", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                np.savez_compressed(
                    stream, built_bits=np.packbits(built), valid_bits=np.packbits(valid),
                    built_count=_summary(built), valid_count=_summary(valid),
                )
                stream.flush()
                os.fsync(stream.fileno())
                written_bytes = stream.tell()
            for attempt in range(6):
                if self._cached_chunk(path) is not None:
                    return 0
                try:
                    os.replace(temporary, path)
                    return written_bytes
                except PermissionError as exc:
                    if self._cached_chunk(path) is not None:
                        return 0
                    if getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == 5:
                        raise
                    time.sleep(0.05 * 2**attempt)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _chunk(self, south_chunk: int, west_chunk: int, stats: ReadStats) -> tuple[np.ndarray, np.ndarray]:
        path = self._cache_path(south_chunk, west_chunk)
        cached = self._cached_chunk(path)
        if cached is not None:
            stats.cache_hits += 1
            return cached
        stats.cache_misses += 1
        ds = self._dataset(south_chunk, west_chunk, stats)
        if ds is None:
            # Do not persist missing objects; retries can discover recovered data.
            return np.zeros((CHUNK_PIXELS, CHUNK_PIXELS), dtype=bool), np.zeros((CHUNK_PIXELS, CHUNK_PIXELS), dtype=bool)
        window = Window((west_chunk % 30) * CHUNK_PIXELS, (29 - south_chunk % 30) * CHUNK_PIXELS, CHUNK_PIXELS, CHUNK_PIXELS)
        # No out_shape/resampling: full-resolution native pixels, never overviews.
        raw = ds.read(1, window=window, masked=True)
        stats.source_pixels += raw.size
        valid = ~np.ma.getmaskarray(raw) & np.isin(raw.data, VALID_CLASSES)
        built = (raw.data == CLASS_BUILT_UP) & valid
        del raw
        stats.cache_bytes_written += self._save_chunk(path, built, valid)
        return built, valid

    def _summary_chunk(self, south_chunk: int, west_chunk: int, stats: ReadStats) -> tuple[np.ndarray, np.ndarray]:
        """Warm reads inflate only the two small counts, not the native masks."""
        path = self._cache_path(south_chunk, west_chunk)
        try:
            with np.load(path, allow_pickle=False) as saved:
                built, valid = saved["built_count"], saved["valid_count"]
                shape = (CHUNK_PIXELS // SUMMARY_FACTOR,) * 2
                if (built.shape == valid.shape == shape and built.dtype == valid.dtype == np.uint16
                        and np.all(built <= valid) and np.all(valid <= SUMMARY_FACTOR**2)):
                    stats.cache_hits += 1
                    return built, valid
        except (OSError, ValueError, KeyError, EOFError, zipfile.BadZipFile):
            pass
        built, valid = self._chunk(south_chunk, west_chunk, stats)
        return _summary(built), _summary(valid)

    def _read_summaries(self, lat, lng, cells, extent, resolution_m, projection, bounds, stats):
        s0, s1, w0, w1 = bounds
        size = CHUNK_PIXELS // SUMMARY_FACTOR
        # One bounded 2-band summary mosaic avoids cross-chunk warp seams and
        # averages built and valid coverage with identical spatial weights.
        source = np.zeros((2, (s1 - s0) * size, (w1 - w0) * size), dtype=np.float32)
        for sc in range(s1 - 1, s0 - 1, -1):
            for wc in range(w0, w1):
                built, valid = self._summary_chunk(sc, (wc + 1800) % 3600 - 1800, stats)
                rr, cc = (s1 - 1 - sc) * size, (wc - w0) * size
                source[0, rr:rr + size, cc:cc + size] = built / SUMMARY_FACTOR**2
                source[1, rr:rr + size, cc:cc + size] = valid / SUMMARY_FACTOR**2
        destination = np.zeros((2, cells, cells), dtype=np.float32)
        reproject(
            source, destination,
            src_transform=rasterio.Affine(0.001, 0, w0 / 10, 0, -0.001, s1 / 10),
            src_crs="EPSG:4326", src_nodata=None,
            dst_transform=rasterio.Affine(resolution_m, 0, -extent, 0, -resolution_m, extent),
            # A real zero is meaningful coverage. GDAL may nudge zeros to a
            # subnormal if zero is also destination nodata, making 0/0 look 1.
            dst_crs=projection, dst_nodata=-1,
            resampling=Resampling.average, num_threads=1, warp_mem_limit=16,
        )
        valid_fraction = np.clip(destination[1], 0, 1)
        fraction = np.full((cells, cells), np.nan, dtype=np.float32)
        np.divide(destination[0], valid_fraction, out=fraction, where=valid_fraction > 0)
        np.clip(fraction, 0, 1, out=fraction)
        sampled = (valid_fraction > 0) & (valid_fraction >= self.min_valid_fraction)
        return BuiltUpGrid(
            lat, lng, -extent, extent, resolution_m, fraction, sampled, valid_fraction,
            stats=stats, aggregation="summary_average",
        )

    def read(self, lat: float, lng: float, radius_m: float, resolution_m: float = 100) -> BuiltUpGrid:
        """Read a local AEQD square; radius is its half-side, rounded up to cells.

        ``valid`` means >= min_valid_fraction (default 95%) of input coverage
        has data. In native mode ``fraction`` divides by valid_count, so completely
        missing cells are NaN, valid non-built cells are 0, and partially missing
        cells retain their measured fraction with coverage reported separately.
        Apply within_radius() if a circular search upper bound is desired.

        Oversized/polar requests fail before opening a tile. HTTP failures raise;
        definite HTTP 404/410 tiles produce missing cells and stats.missing_tiles.
        """
        if self._closed:
            raise RuntimeError("Reader is closed")
        if not all(math.isfinite(v) for v in (lat, lng, radius_m, resolution_m)):
            raise ValueError("Coordinates, radius and resolution must be finite")
        if not -90 < lat < 90 or not -180 <= lng <= 180:
            raise ValueError("Coordinates must be WGS84 latitude/longitude degrees")
        if radius_m <= 0 or resolution_m < 10:
            raise ValueError("radius_m must be positive; resolution_m must be at least 10")
        cells = 2 * math.ceil(radius_m / resolution_m)
        if cells * cells > self.max_output_cells:
            raise ValueError("Output exceeds max_output_cells; request a smaller city subset")
        extent = cells * resolution_m / 2
        pole_distance = _GEOD.inv(lng, lat, lng, math.copysign(90, lat))[2]
        if extent * math.sqrt(2) + 20 >= pole_distance:
            raise ValueError("City square must not reach a pole")
        started = time.monotonic()
        projection = _projection(lat, lng)
        forward = Transformer.from_crs(4326, projection, always_xy=True)
        inverse = Transformer.from_crs(projection, 4326, always_xy=True)
        # Densify all four square edges, then pad a native pixel. Unwrapped
        # longitudes make dateline-adjacent cities small contiguous requests.
        edge = np.linspace(-extent, extent, 65)
        bx = np.concatenate((edge, edge, np.full(65, -extent), np.full(65, extent)))
        by = np.concatenate((np.full(65, -extent), np.full(65, extent), edge, edge))
        blng, blat = inverse.transform(bx, by)
        blng = lng + (blng - lng + 180) % 360 - 180
        pad = 1 / PIXELS_PER_DEGREE
        west, east = float(blng.min() - pad), float(blng.max() + pad)
        south, north = float(blat.min() - pad), float(blat.max() + pad)
        if not all(map(math.isfinite, (west, east, south, north))) or east - west >= 180:
            raise ValueError("Request is too broad for a local city grid")
        s0, s1 = max(-900, math.floor(south * 10)), min(900, math.ceil(north * 10))
        w0, w1 = math.floor(west * 10), math.ceil(east * 10)
        if (s1 - s0) * (w1 - w0) > self.max_chunks:
            raise ValueError("Request exceeds max_chunks; request a smaller city subset")
        stats = ReadStats()
        built_count = np.zeros((cells, cells), dtype=np.uint32)
        valid_count = np.zeros_like(built_count)
        total_count = np.zeros_like(built_count)
        env = dict(
            GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
            GDAL_HTTP_TIMEOUT="30", GDAL_HTTP_CONNECTTIMEOUT="10", GDAL_HTTP_MAX_RETRY="2",
            GDAL_HTTP_RETRY_DELAY="1", GDAL_CACHEMAX=16 * 1024 * 1024,
            CPL_VSIL_CURL_CACHE_SIZE=8 * 1024 * 1024, VSI_CACHE=False,
            GDAL_NUM_THREADS="1", CPL_CURL_VERBOSE=self.http_debug,
        )
        lon0, lon1 = _cell_limits(west, east)
        lat0, lat1 = _cell_limits(south, north)
        with rasterio.Env(**env):
            if self.summary_mode:
                grid = self._read_summaries(
                    lat, lng, cells, extent, resolution_m, projection,
                    (s0, s1, w0, w1), stats,
                )
                stats.elapsed_seconds = time.monotonic() - started
                return grid
            for sc in range(s1 - 1, s0 - 1, -1):
                for wc in range(w0, w1):
                    normalized_wc = (wc + 1800) % 3600 - 1800
                    built, valid = self._chunk(sc, normalized_wc, stats)
                    c0 = max(0, lon0 - wc * CHUNK_PIXELS)
                    c1 = min(CHUNK_PIXELS, lon1 - wc * CHUNK_PIXELS)
                    r0 = max(0, (sc + 1) * CHUNK_PIXELS - lat1)
                    r1 = min(CHUNK_PIXELS, (sc + 1) * CHUNK_PIXELS - lat0)
                    if c1 <= c0 or r1 <= r0:
                        continue
                    native_lng = (wc * CHUNK_PIXELS + np.arange(c0, c1) + 0.5) / PIXELS_PER_DEGREE
                    for row in range(r0, r1, 64):
                        stop = min(row + 64, r1)
                        native_lat = ((sc + 1) * CHUNK_PIXELS - np.arange(row, stop) - 0.5) / PIXELS_PER_DEGREE
                        x, y = forward.transform(*np.meshgrid(native_lng, native_lat))
                        cols = np.floor((x + extent) / resolution_m).astype(np.int64)
                        rows = np.floor((extent - y) / resolution_m).astype(np.int64)
                        inside = (cols >= 0) & (cols < cells) & (rows >= 0) & (rows < cells)
                        indexes = (rows[inside] * cells + cols[inside])
                        known = valid[row:stop, c0:c1][inside]
                        is_built = built[row:stop, c0:c1][inside]
                        for target, selected in ((total_count, indexes), (valid_count, indexes[known]), (built_count, indexes[is_built])):
                            target += np.bincount(selected, minlength=cells * cells).reshape(cells, cells).astype(np.uint32)
                        stats.pixels_aggregated += indexes.size
        fraction = np.full((cells, cells), np.nan, dtype=np.float32)
        valid_fraction = np.zeros((cells, cells), dtype=np.float32)
        np.divide(built_count, valid_count, out=fraction, where=valid_count > 0)
        np.divide(valid_count, total_count, out=valid_fraction, where=total_count > 0)
        sampled = (valid_count > 0) & (valid_fraction >= self.min_valid_fraction)
        stats.elapsed_seconds = time.monotonic() - started
        return BuiltUpGrid(lat, lng, -extent, extent, resolution_m, fraction, sampled, valid_fraction, built_count, valid_count, total_count, stats)
