package com.locwarp.mock;

import android.net.Uri;

import java.util.ArrayList;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

final class CoordinateParser {
    private static final Pattern COORDINATE_PATTERN = Pattern.compile(
        "([-+]?\\d{1,2}(?:\\.\\d+)?)\\s*(?:,|\\s)\\s*([-+]?\\d{1,3}(?:\\.\\d+)?)"
    );
    private static final Pattern GOOGLE_AT_PATTERN = Pattern.compile(
        "/@([-+]?\\d{1,2}(?:\\.\\d+)?),([-+]?\\d{1,3}(?:\\.\\d+)?)"
    );
    private static final Pattern GOOGLE_DATA_PATTERN = Pattern.compile(
        "!3d([-+]?\\d{1,2}(?:\\.\\d+)?)!4d([-+]?\\d{1,3}(?:\\.\\d+)?)"
    );

    private CoordinateParser() {
    }

    static LocalSimulationEngine.Point parse(String value) {
        if (value == null) return null;
        String raw = value.trim();
        if (raw.isEmpty()) return null;
        String decoded = Uri.decode(raw);

        Matcher at = GOOGLE_AT_PATTERN.matcher(decoded);
        if (at.find()) return pointFrom(at.group(1), at.group(2));

        Matcher data = GOOGLE_DATA_PATTERN.matcher(decoded);
        if (data.find()) return pointFrom(data.group(1), data.group(2));

        LocalSimulationEngine.Point query = queryParameterCoordinate(raw, "query");
        if (query != null) return query;
        LocalSimulationEngine.Point q = queryParameterCoordinate(raw, "q");
        if (q != null) return q;
        LocalSimulationEngine.Point ll = queryParameterCoordinate(raw, "ll");
        if (ll != null) return ll;

        return firstCoordinate(decoded);
    }

    static List<LocalSimulationEngine.Point> parseMany(String value) {
        List<LocalSimulationEngine.Point> points = new ArrayList<>();
        if (value == null) return points;
        String[] lines = value.split("\\r?\\n");
        for (String line : lines) {
            LocalSimulationEngine.Point point = parse(line);
            if (point != null) points.add(point);
        }
        return points;
    }

    private static LocalSimulationEngine.Point queryParameterCoordinate(String raw, String key) {
        try {
            Uri uri = Uri.parse(raw);
            String value = uri.getQueryParameter(key);
            return firstCoordinate(value);
        } catch (Exception ignored) {
            return null;
        }
    }

    private static LocalSimulationEngine.Point firstCoordinate(String value) {
        if (value == null) return null;
        Matcher matcher = COORDINATE_PATTERN.matcher(value);
        if (!matcher.find()) return null;
        return pointFrom(matcher.group(1), matcher.group(2));
    }

    private static LocalSimulationEngine.Point pointFrom(String rawLat, String rawLng) {
        try {
            double lat = Double.parseDouble(rawLat);
            double lng = Double.parseDouble(rawLng);
            if (!validCoordinate(lat, lng)) return null;
            return new LocalSimulationEngine.Point(lat, lng);
        } catch (Exception ignored) {
            return null;
        }
    }

    private static boolean validCoordinate(double lat, double lng) {
        return Double.isFinite(lat) && Double.isFinite(lng)
            && lat >= -90d && lat <= 90d
            && lng >= -180d && lng <= 180d;
    }
}
