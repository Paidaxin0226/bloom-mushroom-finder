package com.locwarp.mock;

import android.content.Context;
import android.content.SharedPreferences;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;
import java.util.UUID;

final class LocalDataStore {
    private static final String TAG = "LocWarpDataStore";
    static final class Bookmark {
        final String id;
        final String name;
        final LocalSimulationEngine.Point point;

        Bookmark(String id, String name, LocalSimulationEngine.Point point) {
            this.id = id;
            this.name = name;
            this.point = point;
        }
    }

    static final class Route {
        final String id;
        final String name;
        final List<LocalSimulationEngine.Point> points;

        Route(String id, String name, List<LocalSimulationEngine.Point> points) {
            this.id = id;
            this.name = name;
            this.points = points;
        }
    }

    static final class FlowerTarget {
        final String id;
        final LocalSimulationEngine.Point center;
        final int passesCompleted;
        final long createdAtMs;

        FlowerTarget(
            String id,
            LocalSimulationEngine.Point center,
            int passesCompleted,
            long createdAtMs
        ) {
            this.id = id;
            this.center = center;
            this.passesCompleted = passesCompleted;
            this.createdAtMs = createdAtMs;
        }
    }

    private static final String PREFS = "locwarp_local_library";
    private static final String BOOKMARKS = "bookmarks";
    private static final String ROUTES = "routes";
    private static final String FLOWER_TARGETS = "flower_targets";

    private final SharedPreferences preferences;

    LocalDataStore(Context context) {
        preferences = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    List<Bookmark> bookmarks() {
        List<Bookmark> result = new ArrayList<>();
        String raw = preferences.getString(BOOKMARKS, "[]");
        try {
            JSONArray array = new JSONArray(raw);
            for (int i = 0; i < array.length(); i++) {
                JSONObject item = array.getJSONObject(i);
                result.add(new Bookmark(
                    item.getString("id"),
                    item.getString("name"),
                    new LocalSimulationEngine.Point(
                        item.getDouble("lat"),
                        item.getDouble("lng")
                    )
                ));
            }
        } catch (Exception error) {
            backupCorrupt(BOOKMARKS, raw, error);
        }
        return result;
    }

    void addBookmark(String name, LocalSimulationEngine.Point point) {
        List<Bookmark> items = bookmarks();
        items.add(new Bookmark(UUID.randomUUID().toString(), name, point));
        saveBookmarks(items);
    }

    void deleteBookmark(String id) {
        List<Bookmark> items = bookmarks();
        items.removeIf(item -> item.id.equals(id));
        saveBookmarks(items);
    }

    List<Route> routes() {
        List<Route> result = new ArrayList<>();
        String raw = preferences.getString(ROUTES, "[]");
        try {
            JSONArray array = new JSONArray(raw);
            for (int i = 0; i < array.length(); i++) {
                JSONObject item = array.getJSONObject(i);
                JSONArray rawPoints = item.getJSONArray("points");
                List<LocalSimulationEngine.Point> points = new ArrayList<>();
                for (int j = 0; j < rawPoints.length(); j++) {
                    JSONObject point = rawPoints.getJSONObject(j);
                    points.add(new LocalSimulationEngine.Point(
                        point.getDouble("lat"),
                        point.getDouble("lng")
                    ));
                }
                result.add(new Route(
                    item.getString("id"),
                    item.getString("name"),
                    points
                ));
            }
        } catch (Exception error) {
            backupCorrupt(ROUTES, raw, error);
        }
        return result;
    }

    void addRoute(String name, List<LocalSimulationEngine.Point> points) {
        List<Route> items = routes();
        items.add(new Route(
            UUID.randomUUID().toString(),
            name,
            new ArrayList<>(points)
        ));
        saveRoutes(items);
    }

    void deleteRoute(String id) {
        List<Route> items = routes();
        items.removeIf(item -> item.id.equals(id));
        saveRoutes(items);
    }

    List<FlowerTarget> flowerTargets() {
        List<FlowerTarget> result = new ArrayList<>();
        String raw = preferences.getString(FLOWER_TARGETS, "[]");
        try {
            JSONArray array = new JSONArray(raw);
            for (int i = 0; i < array.length(); i++) {
                JSONObject item = array.getJSONObject(i);
                result.add(new FlowerTarget(
                    item.getString("id"),
                    new LocalSimulationEngine.Point(
                        item.getDouble("lat"),
                        item.getDouble("lng")
                    ),
                    Math.max(0, item.optInt("passesCompleted", 0)),
                    item.optLong("createdAtMs", 0L)
                ));
            }
        } catch (Exception error) {
            backupCorrupt(FLOWER_TARGETS, raw, error);
        }
        return result;
    }

    void addFlowerTarget(LocalSimulationEngine.Point center) {
        List<LocalSimulationEngine.Point> centers = new ArrayList<>();
        centers.add(center);
        addFlowerTargets(centers);
    }

    void addFlowerTargets(List<LocalSimulationEngine.Point> centers) {
        List<FlowerTarget> items = flowerTargets();
        long now = System.currentTimeMillis();
        for (LocalSimulationEngine.Point center : centers) {
            if (center == null) continue;
            items.add(new FlowerTarget(
                UUID.randomUUID().toString(),
                center,
                0,
                now
            ));
        }
        saveFlowerTargets(items);
    }

    void updateFlowerTargetPasses(String id, int passesCompleted) {
        List<FlowerTarget> items = flowerTargets();
        List<FlowerTarget> updated = new ArrayList<>();
        for (FlowerTarget item : items) {
            if (item.id.equals(id)) {
                updated.add(new FlowerTarget(
                    item.id,
                    item.center,
                    Math.max(0, passesCompleted),
                    item.createdAtMs
                ));
            } else {
                updated.add(item);
            }
        }
        saveFlowerTargets(updated);
    }

    void deleteFlowerTarget(String id) {
        List<FlowerTarget> items = flowerTargets();
        items.removeIf(item -> item.id.equals(id));
        saveFlowerTargets(items);
    }

    void clearFlowerTargets() {
        saveFlowerTargets(new ArrayList<>());
    }

    private void saveBookmarks(List<Bookmark> items) {
        JSONArray array = new JSONArray();
        try {
            for (Bookmark item : items) {
                JSONObject object = new JSONObject();
                object.put("id", item.id);
                object.put("name", item.name);
                object.put("lat", item.point.lat);
                object.put("lng", item.point.lng);
                array.put(object);
            }
        } catch (Exception error) {
            Log.e(TAG, "Unable to encode bookmarks", error);
        }
        preferences.edit().putString(BOOKMARKS, array.toString()).apply();
    }

    private void saveRoutes(List<Route> items) {
        JSONArray array = new JSONArray();
        try {
            for (Route item : items) {
                JSONObject object = new JSONObject();
                object.put("id", item.id);
                object.put("name", item.name);
                JSONArray points = new JSONArray();
                for (LocalSimulationEngine.Point point : item.points) {
                    JSONObject rawPoint = new JSONObject();
                    rawPoint.put("lat", point.lat);
                    rawPoint.put("lng", point.lng);
                    points.put(rawPoint);
                }
                object.put("points", points);
                array.put(object);
            }
        } catch (Exception ignored) {
        }
        preferences.edit().putString(ROUTES, array.toString()).apply();
    }

    private void saveFlowerTargets(List<FlowerTarget> items) {
        JSONArray array = new JSONArray();
        try {
            for (FlowerTarget item : items) {
                JSONObject object = new JSONObject();
                object.put("id", item.id);
                object.put("lat", item.center.lat);
                object.put("lng", item.center.lng);
                object.put("passesCompleted", item.passesCompleted);
                object.put("createdAtMs", item.createdAtMs);
                array.put(object);
            }
        } catch (Exception error) {
            Log.e(TAG, "Unable to encode flower targets", error);
        }
        preferences.edit().putString(FLOWER_TARGETS, array.toString()).apply();
    }

    private void backupCorrupt(String key, String raw, Exception error) {
        if (raw == null || raw.isEmpty() || "[]".equals(raw)) return;
        String backupKey = key + "_corrupt_backup";
        if (!preferences.contains(backupKey)) {
            preferences.edit().putString(backupKey, raw).apply();
        }
        Log.e(TAG, "Unable to parse " + key + "; original data was backed up", error);
    }
}
