package com.locwarp.mock;

import android.content.Context;
import android.content.SharedPreferences;

import org.json.JSONObject;

/**
 * Short-lived, USB-controlled map-intent capture lease.
 *
 * While a lease is active MapIntentProxyActivity must consume map intents and
 * expose their coordinate to BloomPilot without changing the simulated
 * location. The expiry is deliberately persisted so a disconnected desktop
 * process cannot leave interception in read-only mode forever.
 */
final class CoordinateCaptureStore {
    private static final String PREFS = "coordinate_capture_v1";
    private static final String KEY_SESSION = "session";
    private static final String KEY_EXPIRES_AT = "expiresAtMs";
    private static final String KEY_CAPTURED_SESSION = "capturedSession";
    private static final String KEY_CAPTURED_AT = "capturedAtMs";
    private static final String KEY_LAT = "lat";
    private static final String KEY_LNG = "lng";
    private static final String KEY_URI = "uri";
    private static final long MIN_TIMEOUT_MS = 5_000L;
    private static final long MAX_TIMEOUT_MS = 180_000L;

    private CoordinateCaptureStore() {
    }

    static synchronized void begin(Context context, String session, long timeoutMs) {
        String normalized = requireSession(session);
        long boundedTimeout = Math.max(MIN_TIMEOUT_MS, Math.min(MAX_TIMEOUT_MS, timeoutMs));
        preferences(context).edit()
            .putString(KEY_SESSION, normalized)
            .putLong(KEY_EXPIRES_AT, System.currentTimeMillis() + boundedTimeout)
            .remove(KEY_CAPTURED_SESSION)
            .remove(KEY_CAPTURED_AT)
            .remove(KEY_LAT)
            .remove(KEY_LNG)
            .remove(KEY_URI)
            .apply();
    }

    static synchronized boolean isActive(Context context) {
        SharedPreferences preferences = preferences(context);
        if (activeSession(preferences) != null) return true;
        clearExpired(preferences);
        return false;
    }

    static synchronized boolean capture(
        Context context,
        LocalSimulationEngine.Point point,
        String rawUri
    ) {
        if (point == null) return false;
        SharedPreferences preferences = preferences(context);
        String session = activeSession(preferences);
        if (session == null) {
            clearExpired(preferences);
            return false;
        }
        preferences.edit()
            .putString(KEY_CAPTURED_SESSION, session)
            .putLong(KEY_CAPTURED_AT, System.currentTimeMillis())
            .putLong(KEY_LAT, Double.doubleToRawLongBits(point.lat))
            .putLong(KEY_LNG, Double.doubleToRawLongBits(point.lng))
            .putString(KEY_URI, rawUri == null ? "" : rawUri)
            .apply();
        return true;
    }

    static synchronized void end(Context context, String session) {
        SharedPreferences preferences = preferences(context);
        String active = preferences.getString(KEY_SESSION, "");
        if (session != null && !session.trim().isEmpty() && !session.equals(active)) return;
        preferences.edit().clear().apply();
    }

    static synchronized JSONObject snapshot(Context context, String requestedSession) {
        SharedPreferences preferences = preferences(context);
        String active = activeSession(preferences);
        if (active == null) clearExpired(preferences);
        String capturedSession = preferences.getString(KEY_CAPTURED_SESSION, "");
        boolean requestedMatches = requestedSession == null
            || requestedSession.trim().isEmpty()
            || requestedSession.equals(active)
            || requestedSession.equals(capturedSession);
        JSONObject out = new JSONObject();
        try {
            out.put("active", active != null && requestedMatches);
            out.put("session", requestedMatches && active != null ? active : "");
            out.put("expiresAtMs", requestedMatches && active != null
                ? preferences.getLong(KEY_EXPIRES_AT, 0L)
                : 0L);
            boolean captured = requestedMatches
                && !capturedSession.isEmpty()
                && capturedSession.equals(requestedSession == null || requestedSession.isEmpty()
                    ? capturedSession
                    : requestedSession)
                && preferences.contains(KEY_LAT)
                && preferences.contains(KEY_LNG);
            out.put("captured", captured);
            if (captured) {
                out.put("lat", Double.longBitsToDouble(preferences.getLong(KEY_LAT, 0L)));
                out.put("lng", Double.longBitsToDouble(preferences.getLong(KEY_LNG, 0L)));
                out.put("capturedAtMs", preferences.getLong(KEY_CAPTURED_AT, 0L));
                out.put("uri", preferences.getString(KEY_URI, ""));
            }
        } catch (Exception ignored) {
        }
        return out;
    }

    private static SharedPreferences preferences(Context context) {
        return context.getApplicationContext().getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    private static String activeSession(SharedPreferences preferences) {
        String session = preferences.getString(KEY_SESSION, "");
        long expiresAt = preferences.getLong(KEY_EXPIRES_AT, 0L);
        return session == null || session.isEmpty() || expiresAt < System.currentTimeMillis()
            ? null
            : session;
    }

    private static void clearExpired(SharedPreferences preferences) {
        if (!preferences.getString(KEY_SESSION, "").isEmpty()) {
            preferences.edit().clear().apply();
        }
    }

    private static String requireSession(String value) {
        String session = value == null ? "" : value.trim();
        if (!session.matches("[A-Za-z0-9_-]{1,80}")) {
            throw new IllegalArgumentException("Coordinate capture session is invalid");
        }
        return session;
    }
}
