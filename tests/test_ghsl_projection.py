from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from scripts.ghsl_settlement_bounds import ShapeSource, mollweide_lat_lng, mollweide_xy


# Copied from the bundled GHS_WUP_DEGURBA_*_UC_V1_0.prj; keep independent
# of the implementation's CRS check.
GHSL_PRJ = (
    'PROJCS["World_Mollweide",GEOGCS["WGS 84",DATUM["WGS_1984",'
    'SPHEROID["WGS 84",6378137,298.257223563,AUTHORITY["EPSG","7030"]],'
    'AUTHORITY["EPSG","6326"]],PRIMEM["Greenwich",0],'
    'UNIT["Degree",0.0174532925199433]],PROJECTION["Mollweide"],'
    'PARAMETER["central_meridian",0],PARAMETER["false_easting",0],'
    'PARAMETER["false_northing",0],UNIT["metre",1,AUTHORITY["EPSG","9001"]],'
    'AXIS["Easting",EAST],AXIS["Northing",NORTH],AUTHORITY["ESRI","54009"]]'
)

# Generated independently with .route-tools/pyproj 3.7.2 / PROJ 9.5.1:
# crs = CRS.from_wkt(the bundled UC .prj contents)
# forward = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
# inverse = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
# forward.transform(lng, lat) -> (x, y); inverse.transform(x, y) -> (lng, lat).
# Tests require only the standard library, not pyproj or the GHSL download.
FORWARD_CASES = (
    ("Origin", 0.0, 0.0, 0.0, 0.0),
    ("Equator east", 0.0, 90.0, 9020047.848073646, 0.0),
    ("Equator west", 0.0, -90.0, -9020047.848073646, 0.0),
    ("Taipei", 25.05306, 121.52639, 11457606.403991388, 3059641.8122749943),
    ("Tokyo", 35.6895, 139.69171, 12305264.169902466, 4302129.440933382),
    ("Berlin", 52.52437, 13.41053, 985248.6888653646, 6135208.941149432),
    ("Paris", 48.8566, 2.3522, 181565.5635387241, 5753213.524446035),
    ("Karimnagar", 18.43915, 79.12856, 7676433.5934275035, 2264780.8956900802),
    ("Malacca", 2.196, 102.2405, 10242181.715899182, 271498.6549835251),
    ("Auckland", -36.84853, 174.76349, 15252746.659370713, -4434189.592226147),
    ("Honolulu", 21.30694, -157.85833, -15143601.289893007, 2611131.867977874),
    ("Anchorage", 61.21806, -149.90028, -9490532.525993405, 6992341.007164092),
)

# Inputs are actual PWCentroidX/Y values from UC DBF records, not values
# produced by mollweide_xy. Expected latitude/longitude comes from PROJ.
CENTROID_CASES = (
    ("Paris UC_2994", 181901.141720, 5752375.309283, 48.8486645886658, 2.3563120295815385),
    ("Taipei UC_12128", 11453498.420592, 3055946.671827, 25.022028482063494, 121.46375832951094),
    ("Tokyo UC_6030", 12304137.510543, 4299553.301426, 35.666954166215135, 139.65430418931533),
    ("Berlin UC_5576", 984783.063532, 6132904.359585, 52.50191554485676, 13.399860260685037),
    ("Karimnagar UC_10073", 7676217.196441, 2264998.739952, 18.44094771219857, 79.12684151404098),
    ("Malacca UC_2289", 10242169.253782, 276202.861885, 2.2340570132583526, 102.24199595036563),
    ("Auckland UC_1005", 15247210.053629, -4440319.838776, -36.90249088591487, 174.77712381747642),
    ("Honolulu UC_1", -15144429.035631, 2614668.983354, 21.336332296690493, -157.88653503183173),
    ("Anchorage UC_5571", -9489422.153850, 6990656.527035, 61.20018725978564, -149.82840565613367),
)


class GhslProjectionTests(unittest.TestCase):
    def test_forward_matches_independent_proj_coordinates(self) -> None:
        for name, lat, lng, expected_x, expected_y in FORWARD_CASES:
            with self.subTest(city=name):
                x, y = mollweide_xy(lat, lng)
                self.assertAlmostEqual(x, expected_x, delta=0.001)
                self.assertAlmostEqual(y, expected_y, delta=0.001)

    def test_inverse_matches_independent_proj_dbf_centroids(self) -> None:
        for name, x, y, expected_lat, expected_lng in CENTROID_CASES:
            with self.subTest(settlement=name):
                lat, lng = mollweide_lat_lng(x, y)
                self.assertAlmostEqual(lat, expected_lat, delta=1e-8)
                self.assertAlmostEqual(lng, expected_lng, delta=1e-8)

    def test_source_accepts_bundled_crs_with_formatting_whitespace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settlement.shp"
            # Empty SHP/DBF pair isolates validation from settlement selection.
            shp_header = bytearray(100)
            struct.pack_into(">I", shp_header, 0, 9994)
            struct.pack_into(">I", shp_header, 24, 50)
            struct.pack_into("<II", shp_header, 28, 1000, 5)
            path.write_bytes(shp_header)
            dbf_header = bytearray(33)
            dbf_header[0] = 3
            struct.pack_into("<IHH", dbf_header, 4, 0, 33, 1)
            dbf_header[32] = 0x0D
            path.with_suffix(".dbf").write_bytes(dbf_header)
            for wkt in (GHSL_PRJ, "\n  " + GHSL_PRJ.replace(",", ",\n  ") + "\n"):
                with self.subTest(formatted="\n" in wkt):
                    path.with_suffix(".prj").write_text(wkt, encoding="utf-8")
                    self.assertEqual(ShapeSource("UC", path, 0).records, [])

    def test_source_rejects_incompatible_crs_before_reading_geometry(self) -> None:
        changes = (
            ("6378137", "6371007.181"),
            ('PROJECTION["Mollweide"]', 'PROJECTION["Mercator"]'),
            ('PARAMETER["central_meridian",0]', 'PARAMETER["central_meridian",10]'),
            ('PARAMETER["false_easting",0]', 'PARAMETER["false_easting",1000]'),
            ('PARAMETER["false_northing",0]', 'PARAMETER["false_northing",1000]'),
            ('UNIT["metre",1,', 'UNIT["foot",0.3048,'),
            ('PRIMEM["Greenwich",0]', 'PRIMEM["Paris",2.33722917]'),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settlement.shp"
            for old, new in changes:
                with self.subTest(change=new):
                    path.with_suffix(".prj").write_text(GHSL_PRJ.replace(old, new), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "ESRI:54009"):
                        ShapeSource("UC", path, 0)

    def test_source_requires_projection_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settlement.shp"
            with self.assertRaises(FileNotFoundError) as error:
                ShapeSource("UC", path, 0)
            self.assertEqual(Path(error.exception.filename), path.with_suffix(".prj"))


if __name__ == "__main__":
    unittest.main()
