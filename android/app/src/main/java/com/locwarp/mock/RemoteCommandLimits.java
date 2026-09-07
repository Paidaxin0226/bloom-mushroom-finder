package com.locwarp.mock;

final class RemoteCommandLimits {
    static final double MAX_SPEED_KMH = 200d;
    static final double MAX_RANDOM_RADIUS_M = 100_000d;
    static final double MAX_PAUSE_SECONDS = 86_400d;
    static final int MAX_STOP_SECONDS = 86_400;
    static final int MAX_LAPS = 10_000;
    static final int MAX_ROUTE_POINTS = 500;
    static final int MAX_NAME_LENGTH = 100;
    static final int MAX_SEARCH_QUERY_LENGTH = 200;
    static final int MAX_STEP_COUNT = 200_000;

    private RemoteCommandLimits() {}

    static double boundedPositive(double value, double fallback, double maximum) {
        if (!(value > 0d) || !Double.isFinite(value)) return fallback;
        return Math.min(maximum, value);
    }

    static Double optionalPositive(double value, double maximum) {
        return value > 0d && Double.isFinite(value)
            ? Math.min(maximum, value)
            : null;
    }

    static int boundedNonNegative(int value, int maximum) {
        return Math.min(maximum, Math.max(0, value));
    }

    static String boundedName(String value, String fallback) {
        String out = value == null ? "" : value.trim();
        if (out.isEmpty()) out = fallback;
        return out.length() > MAX_NAME_LENGTH
            ? out.substring(0, MAX_NAME_LENGTH)
            : out;
    }

    static boolean validCoordinate(double lat, double lng) {
        return Double.isFinite(lat) && Double.isFinite(lng)
            && lat >= -90d && lat <= 90d
            && lng >= -180d && lng <= 180d;
    }

    static void requireRouteSize(int size) {
        if (size > MAX_ROUTE_POINTS) {
            throw new IllegalArgumentException(
                "Route cannot exceed " + MAX_ROUTE_POINTS + " points"
            );
        }
    }

    static int requireStepCount(int steps) {
        if (steps < 1 || steps > MAX_STEP_COUNT) {
            throw new IllegalArgumentException(
                "Step count must be between 1 and " + MAX_STEP_COUNT
            );
        }
        return steps;
    }

    static String requireSearchQuery(String query) {
        String safe = query == null ? "" : query.trim();
        if (safe.isEmpty()) {
            throw new IllegalArgumentException("Search query is required");
        }
        if (safe.length() > MAX_SEARCH_QUERY_LENGTH) {
            throw new IllegalArgumentException(
                "Search query cannot exceed " + MAX_SEARCH_QUERY_LENGTH + " characters"
            );
        }
        return safe;
    }

}
