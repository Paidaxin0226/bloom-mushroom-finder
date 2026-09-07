package com.locwarp.mock;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

final class RouteFileCodec {
    private static final int MAX_INPUT_CHARS = 2 * 1024 * 1024;
    private static final int MAX_POINTS = 10_000;
    private static final Pattern GPX_POINT = Pattern.compile(
        "<(?:trkpt|rtept|wpt)\\b[^>]*>",
        Pattern.CASE_INSENSITIVE
    );
    private static final Pattern LAT_ATTRIBUTE = Pattern.compile(
        "\\blat\\s*=\\s*[\"']([^\"']+)[\"']",
        Pattern.CASE_INSENSITIVE
    );
    private static final Pattern LON_ATTRIBUTE = Pattern.compile(
        "\\blon\\s*=\\s*[\"']([^\"']+)[\"']",
        Pattern.CASE_INSENSITIVE
    );
    private static final Pattern KML_COORDINATES = Pattern.compile(
        "<coordinates\\b[^>]*>(.*?)</coordinates>",
        Pattern.CASE_INSENSITIVE | Pattern.DOTALL
    );

    private RouteFileCodec() {}

    static List<LocalSimulationEngine.Point> parse(String raw) {
        String text = raw == null ? "" : raw.trim();
        if (text.isEmpty()) return new ArrayList<>();
        if (text.length() > MAX_INPUT_CHARS) {
            return new ArrayList<>();
        }
        List<LocalSimulationEngine.Point> points;
        if (text.startsWith("{") || text.startsWith("[")) {
            points = parseGeoJson(text);
            if (!points.isEmpty()) return points;
        }
        if (text.contains("<gpx") || text.contains("<trkpt")
            || text.contains("<rtept") || text.contains("<wpt")) {
            points = parseGpx(text);
            if (!points.isEmpty()) return points;
        }
        if (text.contains("<kml") || text.contains("<coordinates")) {
            points = parseKml(text);
            if (!points.isEmpty()) return points;
        }
        return limited(CoordinateParser.parseMany(text));
    }

    static String toGpx(String name, List<LocalSimulationEngine.Point> points) {
        StringBuilder out = new StringBuilder();
        out.append("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n")
            .append("<gpx version=\"1.1\" creator=\"LocWarp\" ")
            .append("xmlns=\"http://www.topografix.com/GPX/1/1\">\n")
            .append("  <trk><name>").append(xml(name)).append("</name><trkseg>\n");
        for (LocalSimulationEngine.Point point : safe(points)) {
            out.append(String.format(
                Locale.US,
                "    <trkpt lat=\"%.7f\" lon=\"%.7f\" />\n",
                point.lat,
                point.lng
            ));
        }
        return out.append("  </trkseg></trk>\n</gpx>\n").toString();
    }

    static String toKml(String name, List<LocalSimulationEngine.Point> points) {
        StringBuilder out = new StringBuilder();
        out.append("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n")
            .append("<kml xmlns=\"http://www.opengis.net/kml/2.2\"><Document><Placemark>")
            .append("<name>").append(xml(name)).append("</name><LineString><coordinates>\n");
        for (LocalSimulationEngine.Point point : safe(points)) {
            out.append(String.format(Locale.US, "%.7f,%.7f,0\n", point.lng, point.lat));
        }
        return out.append("</coordinates></LineString></Placemark></Document></kml>\n")
            .toString();
    }

    static String toGeoJson(String name, List<LocalSimulationEngine.Point> points) {
        JSONArray coordinates = new JSONArray();
        JSONObject geometry = new JSONObject();
        JSONObject feature = new JSONObject();
        JSONObject properties = new JSONObject();
        try {
            for (LocalSimulationEngine.Point point : safe(points)) {
                JSONArray coordinate = new JSONArray();
                coordinate.put(point.lng);
                coordinate.put(point.lat);
                coordinates.put(coordinate);
            }
            geometry.put("type", "LineString");
            geometry.put("coordinates", coordinates);
            properties.put("name", name == null ? "LocWarp route" : name);
            feature.put("type", "Feature");
            feature.put("properties", properties);
            feature.put("geometry", geometry);
        } catch (Exception ignored) {
        }
        return feature.toString();
    }

    private static List<LocalSimulationEngine.Point> parseGpx(String text) {
        List<LocalSimulationEngine.Point> points = new ArrayList<>();
        Matcher matcher = GPX_POINT.matcher(text);
        while (matcher.find() && points.size() < MAX_POINTS) {
            Matcher lat = LAT_ATTRIBUTE.matcher(matcher.group());
            Matcher lon = LON_ATTRIBUTE.matcher(matcher.group());
            if (lat.find() && lon.find()) add(points, lat.group(1), lon.group(1));
        }
        return points;
    }

    private static List<LocalSimulationEngine.Point> parseKml(String text) {
        List<LocalSimulationEngine.Point> points = new ArrayList<>();
        Matcher blocks = KML_COORDINATES.matcher(text);
        while (blocks.find() && points.size() < MAX_POINTS) {
            String[] coordinates = blocks.group(1).trim().split("\\s+");
            for (String coordinate : coordinates) {
                if (points.size() >= MAX_POINTS) break;
                String[] values = coordinate.split(",");
                if (values.length >= 2) add(points, values[1], values[0]);
            }
        }
        return points;
    }

    private static List<LocalSimulationEngine.Point> parseGeoJson(String text) {
        List<LocalSimulationEngine.Point> points = new ArrayList<>();
        try {
            Object root = text.startsWith("[") ? new JSONArray(text) : new JSONObject(text);
            collectGeoJson(root, points);
        } catch (Exception ignored) {
        }
        return points;
    }

    private static void collectGeoJson(
        Object value,
        List<LocalSimulationEngine.Point> points
    ) {
        if (value instanceof JSONObject) {
            JSONObject object = (JSONObject) value;
            if ("FeatureCollection".equals(object.optString("type"))) {
                JSONArray features = object.optJSONArray("features");
                if (features != null) {
                    for (int i = 0; i < features.length(); i++) {
                        collectGeoJson(features.opt(i), points);
                    }
                }
                return;
            }
            if ("Feature".equals(object.optString("type"))) {
                collectGeoJson(object.opt("geometry"), points);
                return;
            }
            String type = object.optString("type");
            JSONArray coordinates = object.optJSONArray("coordinates");
            if ("LineString".equals(type)) {
                collectCoordinateArray(coordinates, points);
            } else if ("MultiLineString".equals(type) && coordinates != null) {
                for (int i = 0; i < coordinates.length(); i++) {
                    collectCoordinateArray(coordinates.optJSONArray(i), points);
                }
            } else if ("Point".equals(type)) {
                addCoordinate(points, coordinates);
            }
            return;
        }
        if (value instanceof JSONArray) {
            collectCoordinateArray((JSONArray) value, points);
        }
    }

    private static void collectCoordinateArray(
        JSONArray coordinates,
        List<LocalSimulationEngine.Point> points
    ) {
        if (coordinates == null) return;
        for (int i = 0;
            i < coordinates.length() && points.size() < MAX_POINTS;
            i++) {
            Object value = coordinates.opt(i);
            if (value instanceof JSONArray) addCoordinate(points, (JSONArray) value);
        }
    }

    private static void addCoordinate(
        List<LocalSimulationEngine.Point> points,
        JSONArray coordinate
    ) {
        if (coordinate == null || coordinate.length() < 2) return;
        if (points.size() >= MAX_POINTS) return;
        double lng = coordinate.optDouble(0, Double.NaN);
        double lat = coordinate.optDouble(1, Double.NaN);
        if (valid(lat, lng)) points.add(new LocalSimulationEngine.Point(lat, lng));
    }

    private static void add(
        List<LocalSimulationEngine.Point> points,
        String rawLat,
        String rawLng
    ) {
        try {
            if (points.size() >= MAX_POINTS) return;
            double lat = Double.parseDouble(rawLat.trim());
            double lng = Double.parseDouble(rawLng.trim());
            if (valid(lat, lng)) points.add(new LocalSimulationEngine.Point(lat, lng));
        } catch (Exception ignored) {
        }
    }

    private static boolean valid(double lat, double lng) {
        return Double.isFinite(lat) && Double.isFinite(lng)
            && lat >= -90d && lat <= 90d
            && lng >= -180d && lng <= 180d;
    }

    private static List<LocalSimulationEngine.Point> safe(
        List<LocalSimulationEngine.Point> points
    ) {
        return points == null ? new ArrayList<>() : points;
    }

    private static List<LocalSimulationEngine.Point> limited(
        List<LocalSimulationEngine.Point> points
    ) {
        if (points.size() <= MAX_POINTS) return points;
        return new ArrayList<>(points.subList(0, MAX_POINTS));
    }

    private static String xml(String value) {
        String text = value == null ? "LocWarp route" : value;
        return text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\"", "&quot;")
            .replace("'", "&apos;");
    }
}
