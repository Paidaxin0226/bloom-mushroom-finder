package com.locwarp.mock;

import android.content.Context;
import android.content.SharedPreferences;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;

final class SimulationSessionStore {
    private static final int MAX_SESSION_POINTS = 10_000;
    static final class Session {
        String state;
        boolean paused;
        final List<LocalSimulationEngine.Point> route = new ArrayList<>();
        final List<Integer> pauseIndices = new ArrayList<>();
        int routeIndex;
        boolean routeLoops;
        int lap;
        int lapLimit;
        int stopSeconds;
        boolean pauseEnabled;
        double pauseMin;
        double pauseMax;
        double speedKmh;
        Double speedMinKmh;
        Double speedMaxKmh;
        LocalSimulationEngine.Point target;
        LocalSimulationEngine.Point randomCenter;
        double randomRadiusM;
        boolean randomPauseEnabled;
        double randomPauseMin;
        double randomPauseMax;
        boolean randomUsePath;
        long safetyDeadlineEpochMs;
        boolean restoreOnFinish;
    }

    private static final String PREFS = "locwarp_simulation_session";
    private static final String KEY_SESSION = "session";

    private SimulationSessionStore() {}

    static void save(Context context, Session session) {
        if (context == null || session == null || session.state == null) return;
        JSONObject raw = new JSONObject();
        try {
            raw.put("state", session.state);
            raw.put("paused", session.paused);
            raw.put("route", points(session.route));
            raw.put("pauseIndices", integers(session.pauseIndices));
            raw.put("routeIndex", session.routeIndex);
            raw.put("routeLoops", session.routeLoops);
            raw.put("lap", session.lap);
            raw.put("lapLimit", session.lapLimit);
            raw.put("stopSeconds", session.stopSeconds);
            raw.put("pauseEnabled", session.pauseEnabled);
            raw.put("pauseMin", session.pauseMin);
            raw.put("pauseMax", session.pauseMax);
            raw.put("speedKmh", session.speedKmh);
            if (session.speedMinKmh != null) raw.put("speedMinKmh", session.speedMinKmh);
            if (session.speedMaxKmh != null) raw.put("speedMaxKmh", session.speedMaxKmh);
            putPoint(raw, "target", session.target);
            putPoint(raw, "randomCenter", session.randomCenter);
            raw.put("randomRadiusM", session.randomRadiusM);
            raw.put("randomPauseEnabled", session.randomPauseEnabled);
            raw.put("randomPauseMin", session.randomPauseMin);
            raw.put("randomPauseMax", session.randomPauseMax);
            raw.put("randomUsePath", session.randomUsePath);
            raw.put("safetyDeadlineEpochMs", session.safetyDeadlineEpochMs);
            raw.put("restoreOnFinish", session.restoreOnFinish);
        } catch (Exception ignored) {
        }
        prefs(context).edit().putString(KEY_SESSION, raw.toString()).apply();
    }

    static Session load(Context context) {
        String text = prefs(context).getString(KEY_SESSION, "");
        if (text == null || text.isEmpty()) return null;
        try {
            JSONObject raw = new JSONObject(text);
            Session out = new Session();
            out.state = raw.optString("state", "");
            if (out.state.isEmpty()) return null;
            out.paused = raw.optBoolean("paused", false);
            readPoints(raw.optJSONArray("route"), out.route);
            readIntegers(raw.optJSONArray("pauseIndices"), out.pauseIndices);
            out.routeIndex = raw.optInt("routeIndex", 0);
            out.routeLoops = raw.optBoolean("routeLoops", false);
            out.lap = raw.optInt("lap", 0);
            out.lapLimit = raw.optInt("lapLimit", 0);
            out.stopSeconds = raw.optInt("stopSeconds", 0);
            out.pauseEnabled = raw.optBoolean("pauseEnabled", false);
            out.pauseMin = raw.optDouble("pauseMin", 0d);
            out.pauseMax = raw.optDouble("pauseMax", 0d);
            out.speedKmh = raw.optDouble("speedKmh", 10.8d);
            if (raw.has("speedMinKmh")) {
                out.speedMinKmh = raw.optDouble("speedMinKmh", out.speedKmh);
            }
            if (raw.has("speedMaxKmh")) {
                out.speedMaxKmh = raw.optDouble("speedMaxKmh", out.speedKmh);
            }
            out.target = point(raw.optJSONObject("target"));
            out.randomCenter = point(raw.optJSONObject("randomCenter"));
            out.randomRadiusM = raw.optDouble("randomRadiusM", 500d);
            out.randomPauseEnabled = raw.optBoolean("randomPauseEnabled", true);
            out.randomPauseMin = raw.optDouble("randomPauseMin", 5d);
            out.randomPauseMax = raw.optDouble("randomPauseMax", 20d);
            out.randomUsePath = raw.optBoolean("randomUsePath", false);
            out.safetyDeadlineEpochMs = raw.optLong("safetyDeadlineEpochMs", 0L);
            out.restoreOnFinish = raw.optBoolean("restoreOnFinish", false);
            return out;
        } catch (Exception error) {
            clear(context);
            return null;
        }
    }

    static void clear(Context context) {
        if (context == null) return;
        prefs(context).edit().remove(KEY_SESSION).apply();
    }

    private static JSONArray points(List<LocalSimulationEngine.Point> points) {
        JSONArray out = new JSONArray();
        try {
            for (int i = 0; i < points.size() && i < MAX_SESSION_POINTS; i++) {
                LocalSimulationEngine.Point point = points.get(i);
                JSONArray raw = new JSONArray();
                raw.put(point.lat);
                raw.put(point.lng);
                out.put(raw);
            }
        } catch (Exception ignored) {
        }
        return out;
    }

    private static JSONArray integers(List<Integer> values) {
        JSONArray out = new JSONArray();
        for (int i = 0; i < values.size() && i < MAX_SESSION_POINTS; i++) {
            Integer value = values.get(i);
            if (value != null) out.put(value);
        }
        return out;
    }

    private static void putPoint(
        JSONObject target,
        String key,
        LocalSimulationEngine.Point point
    ) throws Exception {
        if (point == null) return;
        JSONObject raw = new JSONObject();
        raw.put("lat", point.lat);
        raw.put("lng", point.lng);
        target.put(key, raw);
    }

    private static LocalSimulationEngine.Point point(JSONObject raw) {
        if (raw == null) return null;
        double lat = raw.optDouble("lat", Double.NaN);
        double lng = raw.optDouble("lng", Double.NaN);
        if (!RemoteCommandLimits.validCoordinate(lat, lng)) return null;
        return new LocalSimulationEngine.Point(lat, lng);
    }

    private static void readPoints(
        JSONArray raw,
        List<LocalSimulationEngine.Point> out
    ) {
        if (raw == null) return;
        for (int i = 0;
            i < raw.length() && out.size() < MAX_SESSION_POINTS;
            i++) {
            JSONArray item = raw.optJSONArray(i);
            if (item == null) continue;
            double lat = item.optDouble(0, Double.NaN);
            double lng = item.optDouble(1, Double.NaN);
            if (RemoteCommandLimits.validCoordinate(lat, lng)) {
                out.add(new LocalSimulationEngine.Point(lat, lng));
            }
        }
    }

    private static void readIntegers(JSONArray raw, List<Integer> out) {
        if (raw == null) return;
        for (int i = 0;
            i < raw.length() && out.size() < MAX_SESSION_POINTS;
            i++) {
            out.add(raw.optInt(i, 0));
        }
    }

    private static SharedPreferences prefs(Context context) {
        return context.getApplicationContext().getSharedPreferences(
            PREFS,
            Context.MODE_PRIVATE
        );
    }
}
