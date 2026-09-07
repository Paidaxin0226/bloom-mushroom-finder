package com.locwarp.mock;

import android.content.Context;
import android.content.Intent;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.location.Address;
import android.location.Geocoder;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.util.Base64;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.io.ByteArrayOutputStream;

final class RemoteCommandExecutor {
    interface ResultCallback {
        void onResult(JSONObject result);
    }

    private final Context context;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final AsyncTaskGate routeTasks = new AsyncTaskGate("LocWarpRemoteRoute");
    private final AsyncTaskGate searchTasks = new AsyncTaskGate("LocWarpRemoteSearch");
    private final LocalSimulationEngine engine = LocalSimulationEngine.get();
    private final LocalDataStore library;
    private final RemoteStepController stepController;
    private final RemoteFlowerController flowerController;

    RemoteCommandExecutor(Context context) {
        this.context = context.getApplicationContext();
        library = new LocalDataStore(this.context);
        stepController = new RemoteStepController(this.context);
        flowerController = RemoteFlowerController.get(this.context);
        mainHandler.post(XposedScopeRepository::initialize);
        engine.attach(this.context, null);
    }

    Context context() {
        return context;
    }

    void execute(String command, JSONObject payload, ResultCallback callback) {
        JSONObject safePayload = payload == null ? new JSONObject() : payload;
        if (isMovementCommand(command)) routeTasks.cancel();
        try {
            switch (command == null ? "" : command) {
                case "setEnabled":
                    setEnabled(safePayload, callback);
                    break;
                case "setSpeed":
                    setSpeed(safePayload, callback);
                    break;
                case "setStraightLine":
                    setStraightLine(safePayload, callback);
                    break;
                case "searchAddress":
                    searchAddress(safePayload, callback);
                    break;
                case "teleport":
                    teleport(safePayload, callback);
                    break;
                case "navigate":
                    navigate(safePayload, callback);
                    break;
                case "loop":
                    loop(safePayload, callback);
                    break;
                case "multiStop":
                    multiStop(safePayload, callback);
                    break;
                case "randomWalk":
                    randomWalk(safePayload, callback);
                    break;
                case "startJoystick":
                    startJoystick(safePayload, callback);
                    break;
                case "joystickInput":
                    joystickInput(safePayload, callback);
                    break;
                case "setMovementSettings":
                    setMovementSettings(safePayload, callback);
                    break;
                case "setMapSettings":
                    setMapSettings(safePayload, callback);
                    break;
                case "clearMapCache":
                    new MapTileCache(context).clear();
                    complete(callback, true, "Map tile cache cleared");
                    break;
                case "setOverlay":
                    setOverlay(safePayload, callback);
                    break;
                case "startFlower":
                    startFlower(safePayload, callback);
                    break;
                case "stopFlower":
                    flowerController.stop();
                    complete(callback, true, "Flower mode stopped");
                    break;
                case "addFlowerTargets":
                    flowerController.addTargets(requirePoints(safePayload));
                    complete(callback, true, "Flower targets added");
                    break;
                case "deleteFlowerTarget":
                    flowerController.deleteTarget(safePayload.optString("id", ""));
                    complete(callback, true, "Flower target deleted");
                    break;
                case "clearFlowerTargets":
                    flowerController.clearTargets();
                    complete(callback, true, "Flower targets cleared");
                    break;
                case "pause":
                    engine.pause();
                    complete(callback, true, "Paused");
                    break;
                case "resume":
                    engine.resume();
                    complete(callback, true, "Resumed");
                    break;
                case "togglePause":
                    togglePause(callback);
                    break;
                case "stop":
                    if (RemoteFlowerController.isRemoteControlling()) {
                        flowerController.stop();
                    } else {
                        engine.stop();
                    }
                    complete(callback, true, "Stopped");
                    break;
                case "startSteps":
                    startSteps(safePayload, callback);
                    break;
                case "startStepLoop":
                    startStepLoop(callback);
                    break;
                case "setStepFollow":
                    setStepFollow(safePayload, callback);
                    break;
                case "toggleStepsPause":
                    toggleStepsPause(callback);
                    break;
                case "stopSteps":
                    stopSteps(callback);
                    break;
                case "setStepSettings":
                    setStepSettings(safePayload, callback);
                    break;
                case "restore":
                    flowerController.stop();
                    engine.restore();
                    MockLocationKeeper.setMockEnabled(context, false);
                    complete(callback, true, "Restored real location");
                    break;
                case "addBookmark":
                    addBookmark(safePayload, callback);
                    break;
                case "deleteBookmark":
                    deleteBookmark(safePayload, callback);
                    break;
                case "addRoute":
                    addRoute(safePayload, callback);
                    break;
                case "deleteRoute":
                    deleteRoute(safePayload, callback);
                    break;
                case "listHookApps":
                    completeData(callback, true, "Hook apps", "apps", hookAppsJson(true));
                    break;
                case "addHookApp":
                    addHookApp(safePayload, callback);
                    break;
                case "removeHookApp":
                    removeHookApp(safePayload, callback);
                    break;
                case "setHook":
                    setHook(safePayload, callback);
                    break;
                case "diagnostics":
                    completeData(callback, true, "Diagnostics ready", "text", DiagnosticsReport.create(context));
                    break;
                case "history":
                    completeData(callback, true, "History ready", "entries", historyJson());
                    break;
                case "historyCsv":
                    completeData(callback, true, "History CSV ready", "text", OperationHistoryStore.toCsv(context));
                    break;
                case "clearHistory":
                    OperationHistoryStore.clear(context);
                    complete(callback, true, "History cleared");
                    break;
                case "exportBackup":
                    completeData(callback, true, "Backup exported", "backup", LocWarpBackupCodec.exportJson(context));
                    break;
                case "importBackup":
                    importBackup(safePayload, callback);
                    break;
                case "startRelay":
                    startRelay(safePayload, callback);
                    break;
                case "stopRelay":
                    stopRelay(callback);
                    break;
                case "rotateRelay":
                    RemoteRelayClient.get(context).rotateSession();
                    complete(callback, true, "Relay session rotated");
                    break;
                case "beginCoordinateCapture":
                    beginCoordinateCapture(safePayload, callback);
                    break;
                case "coordinateCaptureStatus":
                    coordinateCaptureStatus(safePayload, callback);
                    break;
                case "endCoordinateCapture":
                    endCoordinateCapture(safePayload, callback);
                    break;
                case "relayQr":
                    relayQr(callback);
                    break;
                case "relayViewerRole":
                    relayViewerRole(safePayload, callback);
                    break;
                case "disconnectRelayViewer":
                    RemoteRelayClient.get(context).disconnectViewer(safePayload.optString("id", ""));
                    complete(callback, true, "Relay viewer disconnected");
                    break;
                case "decideRelayViewer":
                    decideRelayViewer(safePayload, callback);
                    break;
                case "stopUsbControl":
                    complete(callback, true, "USB control stopping");
                    mainHandler.postDelayed(
                        () -> context.stopService(new Intent(context, UsbControlService.class)),
                        350L
                    );
                    break;
                case "status":
                    complete(callback, true, "Status");
                    break;
                case "ping":
                    completeWithoutStatus(callback, true, "USB control alive");
                    break;
                default:
                    complete(callback, false, "Unknown command: " + command);
                    break;
            }
        } catch (Exception error) {
            complete(callback, false, error.getMessage() == null
                ? error.getClass().getSimpleName()
                : error.getMessage());
        }
    }

    private boolean isMovementCommand(String command) {
        return "teleport".equals(command)
            || "navigate".equals(command)
            || "loop".equals(command)
            || "multiStop".equals(command)
            || "randomWalk".equals(command)
            || "startJoystick".equals(command)
            || "startFlower".equals(command)
            || "stopFlower".equals(command)
            || "stop".equals(command)
            || "restore".equals(command)
            || "setEnabled".equals(command);
    }

    JSONObject status() {
        JSONObject out = new JSONObject();
        try {
            MockLocationKeeper.Snapshot saved = MockLocationKeeper.snapshot(context);
            LocalSimulationEngine.Snapshot snapshot = engine.snapshot();
            out.put("mockEnabled", MockLocationKeeper.isMockEnabled(context));
            out.put("active", saved.active);
            out.put("state", snapshot.state);
            out.put("running", snapshot.running);
            out.put("paused", snapshot.paused);
            out.put("speedKmh", snapshot.speedKmh);
            out.put("lap", snapshot.lap);
            out.put("routeIndex", snapshot.routeIndex);
            out.put("straightLine", OverlaySettings.isStraightLineMovement(context));
            out.put("updatedAtMs", saved.updatedAtMs);
            out.put("source", saved.source);
            putPoint(out, "current", snapshot.current);
            putPoint(out, "target", snapshot.target);
            out.put("route", pointsJson(snapshot.route));
            out.put("bookmarks", bookmarksJson());
            out.put("routes", routesJson());
            out.put("steps", stepController.status());
            out.put("settings", settingsJson());
            out.put("flower", flowerController.status());
            out.put("hookApps", hookAppsJson(false));
            out.put("hookScope", hookScopeJson());
            out.put("relay", relayJson());
            out.put("coordinateCapture", CoordinateCaptureStore.snapshot(context, ""));
        } catch (Exception ignored) {
        }
        return out;
    }

    private void beginCoordinateCapture(JSONObject payload, ResultCallback callback) {
        String session = payload.optString("session", "");
        long timeoutMs = payload.optLong("timeoutMs", 60_000L);
        CoordinateCaptureStore.begin(context, session, timeoutMs);
        completeDataWithoutStatus(
            callback,
            true,
            "Coordinate capture started; movement is disabled for map intents",
            "capture",
            CoordinateCaptureStore.snapshot(context, session)
        );
    }

    private void coordinateCaptureStatus(JSONObject payload, ResultCallback callback) {
        String session = payload.optString("session", "");
        completeDataWithoutStatus(
            callback,
            true,
            "Coordinate capture status",
            "capture",
            CoordinateCaptureStore.snapshot(context, session)
        );
    }

    private void endCoordinateCapture(JSONObject payload, ResultCallback callback) {
        String session = payload.optString("session", "");
        CoordinateCaptureStore.end(context, session);
        completeDataWithoutStatus(
            callback,
            true,
            "Coordinate capture ended",
            "capture",
            CoordinateCaptureStore.snapshot(context, session)
        );
    }

    private void setEnabled(JSONObject payload, ResultCallback callback) {
        boolean enabled = payload.optBoolean("enabled", true);
        if (enabled) {
            MockLocationKeeper.setMockEnabled(context, true);
            engine.attach(context, null);
            complete(callback, true, "Mock location enabled");
            return;
        }
        flowerController.stop();
        engine.restore();
        MockLocationKeeper.setMockEnabled(context, false);
        complete(callback, true, "Mock location disabled");
    }

    private void setSpeed(JSONObject payload, ResultCallback callback) {
        double fixed = positiveDouble(payload, "speedKmh", engine.snapshot().speedKmh);
        Double min = optionalPositiveDouble(payload, "minKmh");
        Double max = optionalPositiveDouble(payload, "maxKmh");
        engine.configureSpeed(fixed, min, max);
        complete(callback, true, "Speed updated");
    }

    private void setStraightLine(JSONObject payload, ResultCallback callback) {
        OverlaySettings.setStraightLineMovement(
            context,
            payload.optBoolean("enabled", false)
        );
        complete(callback, true, "Movement mode updated");
    }

    private void searchAddress(JSONObject payload, ResultCallback callback) {
        String query = RemoteCommandLimits.requireSearchQuery(
            payload.optString("query", "")
        );
        if (!Geocoder.isPresent()) {
            completeSearch(callback, false, "Address search is unavailable", null);
            return;
        }
        searchTasks.submit(
            () -> new Geocoder(context, Locale.getDefault())
                .getFromLocationName(query, 5),
            mainHandler,
            results -> completeSearch(
                callback,
                true,
                results == null || results.isEmpty()
                    ? "No search results"
                    : "Search completed",
                results
            ),
            error -> completeSearch(
                callback,
                false,
                "Address search is unavailable",
                null
            )
        );
    }

    private void teleport(JSONObject payload, ResultCallback callback) {
        ensureEnabled();
        applySpeedIfPresent(payload);
        LocalSimulationEngine.Point point = requirePoint(payload);
        if (!engine.teleport(point.lat, point.lng)) {
            complete(callback, false, "Android mock-location permission is not ready");
            return;
        }
        complete(callback, true, "Teleported to " + LocalSimulationEngine.format(point));
    }

    private void navigate(JSONObject payload, ResultCallback callback) {
        ensureEnabled();
        applySpeedIfPresent(payload);
        LocalSimulationEngine.Point point = requirePoint(payload);
        boolean usePath = payload.optBoolean(
            "usePath",
            !OverlaySettings.isStraightLineMovement(context)
        );
        if (!usePath) {
            complete(callback, engine.navigate(point), "Navigation started");
            return;
        }
        LocalSimulationEngine.Snapshot snapshot = engine.snapshot();
        if (snapshot.current == null) {
            complete(callback, false, "Set a current location before navigation");
            return;
        }
        LocalSimulationEngine.Point start = snapshot.current;
        routeTasks.submit(
            WalkingRoutePlanner.planTask(start, point),
            mainHandler,
            route -> complete(
                callback,
                engine.startPathRoute(new ArrayList<>(route)),
                "Path navigation started"
            ),
            error -> complete(callback, false, "Route planning failed")
        );
    }

    private void loop(JSONObject payload, ResultCallback callback) {
        ensureEnabled();
        applySpeedIfPresent(payload);
        List<LocalSimulationEngine.Point> points = requirePoints(payload);
        if (points.size() < 2) {
            complete(callback, false, "Loop requires at least 2 points");
            return;
        }
        int laps = boundedNonNegativeInt(
            payload,
            "laps",
            RemoteCommandLimits.MAX_LAPS
        );
        boolean pauseEnabled = payload.optBoolean("pauseEnabled", false);
        double pauseMin = boundedPositiveDouble(
            payload, "pauseMin", 5d, RemoteCommandLimits.MAX_PAUSE_SECONDS
        );
        double pauseMax = boundedPositiveDouble(
            payload, "pauseMax", 20d, RemoteCommandLimits.MAX_PAUSE_SECONDS
        );
        boolean usePath = payload.optBoolean(
            "usePath",
            !OverlaySettings.isStraightLineMovement(context)
        );
        if (!usePath) {
            complete(
                callback,
                engine.startLoop(points, laps, pauseEnabled, pauseMin, pauseMax),
                "Loop started"
            );
            return;
        }
        LocalSimulationEngine.Snapshot snapshot = engine.snapshot();
        if (snapshot.current == null) {
            complete(callback, false, "Set a current location before loop movement");
            return;
        }
        LocalSimulationEngine.Point start = snapshot.current;
        routeTasks.submit(
            WalkingRoutePlanner.planThroughTask(start, points, true),
            mainHandler,
            route -> complete(
                callback,
                engine.startLoopRoute(
                    route.points,
                    route.stopIndices,
                    laps,
                    pauseEnabled,
                    pauseMin,
                    pauseMax
                ),
                "Loop route started"
            ),
            error -> complete(callback, false, "Loop route planning failed")
        );
    }

    private void multiStop(JSONObject payload, ResultCallback callback) {
        ensureEnabled();
        applySpeedIfPresent(payload);
        List<LocalSimulationEngine.Point> points = requirePoints(payload);
        if (points.size() < 2) {
            complete(callback, false, "Multi-stop requires at least 2 points");
            return;
        }
        int stopSeconds = boundedNonNegativeInt(
            payload,
            "stopSeconds",
            RemoteCommandLimits.MAX_STOP_SECONDS
        );
        boolean routeLoops = payload.optBoolean("loop", false);
        boolean pauseEnabled = payload.optBoolean("pauseEnabled", false);
        double pauseMin = boundedPositiveDouble(
            payload, "pauseMin", 5d, RemoteCommandLimits.MAX_PAUSE_SECONDS
        );
        double pauseMax = boundedPositiveDouble(
            payload, "pauseMax", 20d, RemoteCommandLimits.MAX_PAUSE_SECONDS
        );
        boolean usePath = payload.optBoolean(
            "usePath",
            !OverlaySettings.isStraightLineMovement(context)
        );
        if (!usePath) {
            complete(
                callback,
                engine.startMultiStop(
                    points,
                    stopSeconds,
                    routeLoops,
                    pauseEnabled,
                    pauseMin,
                    pauseMax
                ),
                "Multi-stop started"
            );
            return;
        }
        LocalSimulationEngine.Snapshot snapshot = engine.snapshot();
        if (snapshot.current == null) {
            complete(callback, false, "Set a current location before multi-stop movement");
            return;
        }
        LocalSimulationEngine.Point start = snapshot.current;
        routeTasks.submit(
            WalkingRoutePlanner.planThroughTask(start, points, routeLoops),
            mainHandler,
            route -> complete(
                callback,
                engine.startMultiStopRoute(
                    route.points,
                    route.stopIndices,
                    stopSeconds,
                    routeLoops,
                    pauseEnabled,
                    pauseMin,
                    pauseMax
                ),
                "Multi-stop route started"
            ),
            error -> complete(callback, false, "Multi-stop route planning failed")
        );
    }

    private void randomWalk(JSONObject payload, ResultCallback callback) {
        ensureEnabled();
        applySpeedIfPresent(payload);
        LocalSimulationEngine.Point center = pointFromPayload(payload, false);
        double radiusM = boundedPositiveDouble(
            payload,
            "radiusM",
            500d,
            RemoteCommandLimits.MAX_RANDOM_RADIUS_M
        );
        boolean pauseEnabled = payload.optBoolean("pauseEnabled", true);
        double pauseMin = boundedPositiveDouble(
            payload, "pauseMin", 5d, RemoteCommandLimits.MAX_PAUSE_SECONDS
        );
        double pauseMax = boundedPositiveDouble(
            payload, "pauseMax", 20d, RemoteCommandLimits.MAX_PAUSE_SECONDS
        );
        boolean usePath = payload.optBoolean(
            "usePath",
            !OverlaySettings.isStraightLineMovement(context)
        );
        complete(
            callback,
            engine.startRandomWalk(center, radiusM, pauseEnabled, pauseMin, pauseMax, usePath),
            "Random walk started"
        );
    }

    private void togglePause(ResultCallback callback) {
        LocalSimulationEngine.Snapshot snapshot = engine.snapshot();
        if (!snapshot.running) {
            complete(callback, false, "No active movement");
            return;
        }
        if (snapshot.paused) {
            engine.resume();
            complete(callback, true, "Resumed");
        } else {
            engine.pause();
            complete(callback, true, "Paused");
        }
    }

    private void startSteps(JSONObject payload, ResultCallback callback) {
        int steps = RemoteCommandLimits.requireStepCount(
            payload.optInt("steps", 0)
        );
        if (!stepController.start(steps)) {
            complete(callback, false, "已有其他步數工作執行中");
            return;
        }
        complete(callback, true, "已開始注入 " + steps + " 步");
    }

    private void startStepLoop(ResultCallback callback) {
        if (!stepController.startLoop()) {
            complete(callback, false, "已有其他步數工作執行中");
            return;
        }
        complete(callback, true, "已開始循環新增步數");
    }

    private void setStepFollow(JSONObject payload, ResultCallback callback) {
        boolean enabled = payload.optBoolean("enabled", false);
        if (!stepController.setFollowEnabled(enabled)) {
            complete(callback, false, "其他步數工作執行中，無法啟用自動跟隨");
            return;
        }
        complete(
            callback,
            true,
            enabled ? "已啟用自動跟隨" : "已關閉自動跟隨"
        );
    }

    private void toggleStepsPause(ResultCallback callback) {
        JSONObject before = stepController.status();
        if (!stepController.togglePause()) {
            complete(callback, false, "目前沒有可暫停的步數工作");
            return;
        }
        complete(
            callback,
            true,
            before.optBoolean("paused", false) ? "已繼續注入步數" : "已暫停注入步數"
        );
    }

    private void stopSteps(ResultCallback callback) {
        if (!stepController.stop()) {
            complete(callback, false, "目前沒有執行中的步數工作");
            return;
        }
        complete(callback, true, "已停止注入步數");
    }

    private void addBookmark(JSONObject payload, ResultCallback callback) {
        LocalSimulationEngine.Point point = requirePoint(payload);
        String name = boundedName(
            payload.optString("name", "Remote bookmark"),
            "Remote bookmark"
        );
        library.addBookmark(name.isEmpty() ? "Remote bookmark" : name, point);
        complete(callback, true, "Bookmark added");
    }

    private void deleteBookmark(JSONObject payload, ResultCallback callback) {
        String id = payload.optString("id", "");
        if (id.isEmpty()) {
            complete(callback, false, "Bookmark id is required");
            return;
        }
        library.deleteBookmark(id);
        complete(callback, true, "Bookmark deleted");
    }

    private void addRoute(JSONObject payload, ResultCallback callback) {
        List<LocalSimulationEngine.Point> points = requirePoints(payload);
        if (points.size() < 2) {
            complete(callback, false, "Route requires at least 2 points");
            return;
        }
        String name = boundedName(
            payload.optString("name", "Remote route"),
            "Remote route"
        );
        library.addRoute(name.isEmpty() ? "Remote route" : name, points);
        complete(callback, true, "Route saved");
    }

    private void deleteRoute(JSONObject payload, ResultCallback callback) {
        String id = payload.optString("id", "");
        if (id.isEmpty()) {
            complete(callback, false, "Route id is required");
            return;
        }
        library.deleteRoute(id);
        complete(callback, true, "Route deleted");
    }

    private void startJoystick(JSONObject payload, ResultCallback callback) {
        ensureEnabled();
        applySpeedIfPresent(payload);
        LocalSimulationEngine.Point point = pointFromPayload(payload, false);
        if (point != null && engine.snapshot().current == null) {
            if (!engine.teleport(point.lat, point.lng)) {
                complete(callback, false, "Unable to set joystick start point");
                return;
            }
        }
        if (!engine.startJoystick()) {
            complete(callback, false, "Set a current location before starting joystick mode");
            return;
        }
        complete(callback, true, "Joystick mode started");
    }

    private void joystickInput(JSONObject payload, ResultCallback callback) {
        double x = Math.max(-1d, Math.min(1d, payload.optDouble("x", 0d)));
        double y = Math.max(-1d, Math.min(1d, payload.optDouble("y", 0d)));
        engine.joystickInput(x, y);
        complete(callback, true, "Joystick updated");
    }

    private void setMovementSettings(JSONObject payload, ResultCallback callback) {
        applySpeedIfPresent(payload);
        if (payload.has("straightLine")) {
            OverlaySettings.setStraightLineMovement(
                context,
                payload.optBoolean("straightLine", false)
            );
        }
        OverlaySettings.setMovementSafety(
            context,
            payload.has("autoStopMinutes")
                ? payload.optInt("autoStopMinutes", 0)
                : OverlaySettings.safetyAutoStopMinutes(context),
            payload.has("restoreOnFinish")
                ? payload.optBoolean("restoreOnFinish", false)
                : OverlaySettings.restoreOnMovementFinish(context),
            payload.has("resumeAfterRestart")
                ? payload.optBoolean("resumeAfterRestart", true)
                : OverlaySettings.resumeMovementAfterRestart(context)
        );
        complete(callback, true, "Movement settings updated");
    }

    private void setMapSettings(JSONObject payload, ResultCallback callback) {
        String template = payload.has("template")
            ? payload.optString("template", "")
            : OverlaySettings.mapTileTemplate(context);
        String mode = payload.has("mode")
            ? payload.optString("mode", OverlaySettings.MAP_TILE_MODE_ONLINE_CACHE)
            : OverlaySettings.mapTileMode(context);
        String attribution = payload.has("attribution")
            ? payload.optString("attribution", "")
            : OverlaySettings.mapTileAttribution(context);
        if (!OverlaySettings.validMapTileTemplate(template)) {
            complete(callback, false, "Map tile URL must be HTTPS and contain {z}, {x}, and {y}");
            return;
        }
        OverlaySettings.setMapTileSettings(context, mode, template, attribution);
        if (payload.has("interceptMapLinks")) {
            OverlaySettings.setMapIntentInterceptEnabled(
                context,
                payload.optBoolean("interceptMapLinks", false)
            );
        }
        complete(callback, true, "Map settings updated");
    }

    private void setOverlay(JSONObject payload, ResultCallback callback) {
        String kind = payload.optString("kind", "");
        boolean visible = payload.optBoolean("visible", false);
        if ("joystick".equals(kind)) {
            if (payload.has("sizeDp")) {
                OverlaySettings.setJoystickSizeDp(context, payload.optInt("sizeDp"));
            }
            if (visible) {
                if (!Settings.canDrawOverlays(context)) {
                    complete(callback, false, "Android overlay permission is missing");
                    return;
                }
                if (!engine.startJoystick()) {
                    complete(callback, false, "Set a current location before opening the joystick");
                    return;
                }
                context.startForegroundService(new Intent(context, JoystickOverlayService.class)
                    .setAction(JoystickOverlayService.ACTION_SHOW));
            } else {
                OverlaySettings.setJoystickVisible(context, false);
                engine.joystickInput(0d, 0d);
                context.stopService(new Intent(context, JoystickOverlayService.class));
            }
        } else if ("map".equals(kind)) {
            if (payload.has("sizeDp")) {
                OverlaySettings.setMapHintSizeDp(context, payload.optInt("sizeDp"));
            }
            if (visible) {
                if (!Settings.canDrawOverlays(context)) {
                    complete(callback, false, "Android overlay permission is missing");
                    return;
                }
                context.startForegroundService(new Intent(context, MapHintOverlayService.class)
                    .setAction(MapHintOverlayService.ACTION_SHOW));
            } else {
                OverlaySettings.setMapHintVisible(context, false);
                context.stopService(new Intent(context, MapHintOverlayService.class));
            }
        } else {
            complete(callback, false, "Overlay kind must be joystick or map");
            return;
        }
        complete(callback, true, "Overlay updated");
    }

    private void startFlower(JSONObject payload, ResultCallback callback) {
        ensureEnabled();
        applySpeedIfPresent(payload);
        double radius = payload.has("radiusMeters")
            ? payload.optDouble("radiusMeters", 30d)
            : OverlaySettings.flowerRadiusMeters(context);
        double spacing = payload.has("spacingMeters")
            ? payload.optDouble("spacingMeters", 7d)
            : OverlaySettings.flowerSpacingMeters(context);
        flowerController.start(radius, spacing);
        complete(callback, true, "Flower mode started");
    }

    private void setStepSettings(JSONObject payload, ResultCallback callback) {
        if (payload.has("defaultSteps")) {
            OverlaySettings.setDefitStepCount(
                context,
                RemoteCommandLimits.requireStepCount(payload.optInt("defaultSteps", 0))
            );
        }
        if (payload.has("walkingMultiplier")) {
            OverlaySettings.setWalkingStepMultiplier(
                context,
                payload.optInt("walkingMultiplier", 1)
            );
        }
        complete(callback, true, "Step settings updated");
    }

    private void addHookApp(JSONObject payload, ResultCallback callback) {
        String packageName = payload.optString("packageName", "").trim();
        if (!HookScopeSettings.isValidPackageName(packageName)) {
            complete(callback, false, "Package name is invalid");
            return;
        }
        HookScopeSettings.rememberManualPackage(context, packageName);
        StepHookController.sendConfig(context, packageName);
        complete(callback, true, "Hook app added");
    }

    private void removeHookApp(JSONObject payload, ResultCallback callback) {
        String packageName = payload.optString("packageName", "").trim();
        HookScopeSettings.forgetManualPackage(context, packageName);
        complete(callback, true, "Hook app removed");
    }

    private void setHook(JSONObject payload, ResultCallback callback) {
        String packageName = payload.optString("packageName", "").trim();
        if (!HookScopeSettings.isValidPackageName(packageName)) {
            complete(callback, false, "Package name is invalid");
            return;
        }
        if (payload.has("locationEnabled")) {
            HookScopeSettings.setLocationEnabled(
                context,
                packageName,
                payload.optBoolean("locationEnabled", true)
            );
        }
        if (payload.has("stepEnabled")) {
            HookScopeSettings.setStepEnabled(
                context,
                packageName,
                payload.optBoolean("stepEnabled", true)
            );
        }
        StepHookController.sendConfig(context, packageName);
        complete(callback, true, "Hook settings updated");
    }

    private void importBackup(JSONObject payload, ResultCallback callback) throws Exception {
        String raw = payload.optString("backup", "");
        LocWarpBackupCodec.inspect(raw);
        RemoteRelayClient.get(context).stop();
        flowerController.stop();
        engine.restore();
        LocWarpBackupCodec.restore(context, raw);
        complete(callback, true, "Backup restored");
    }

    private void startRelay(JSONObject payload, ResultCallback callback) {
        String baseUrl = payload.optString("baseUrl", "").trim();
        if (baseUrl.isEmpty()) {
            complete(callback, false, "Relay URL is required");
            return;
        }
        Intent intent = new Intent(context, RemoteRelayService.class)
            .setAction(RemoteRelayService.ACTION_START)
            .putExtra(RemoteRelayService.EXTRA_BASE_URL, baseUrl);
        context.startForegroundService(intent);
        complete(callback, true, "Relay starting");
    }

    private void stopRelay(ResultCallback callback) {
        RemoteRelayClient.get(context).stop();
        context.stopService(new Intent(context, RemoteRelayService.class));
        complete(callback, true, "Relay stopped");
    }

    private void relayViewerRole(JSONObject payload, ResultCallback callback) {
        String id = payload.optString("id", "");
        String role = payload.optString("role", "readonly");
        RemoteRelayClient.get(context).changeViewerRole(id, role);
        complete(callback, true, "Relay viewer role updated");
    }

    private void decideRelayViewer(JSONObject payload, ResultCallback callback) {
        String id = payload.optString("id", "");
        boolean approved = payload.optBoolean("approved", false);
        String role = payload.optString("role", "readonly");
        RemoteRelayClient.get(context).approveViewer(id, approved, role);
        complete(callback, true, approved ? "Relay viewer approved" : "Relay viewer rejected");
    }

    private void relayQr(ResultCallback callback) throws Exception {
        String url = RemoteRelayClient.get(context).publicUrl();
        if (url.isEmpty()) {
            complete(callback, false, "Relay URL is not ready");
            return;
        }
        Bitmap bitmap = QrCodeBitmap.render(url, 260);
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        if (!bitmap.compress(Bitmap.CompressFormat.PNG, 100, output)) {
            complete(callback, false, "Unable to render Relay QR code");
            return;
        }
        String dataUrl = "data:image/png;base64," + Base64.encodeToString(
            output.toByteArray(),
            Base64.NO_WRAP
        );
        completeData(callback, true, "Relay QR ready", "dataUrl", dataUrl);
    }

    private void ensureEnabled() {
        if (!MockLocationKeeper.isMockEnabled(context)) {
            MockLocationKeeper.setMockEnabled(context, true);
        }
        engine.attach(context, null);
    }

    private void applySpeedIfPresent(JSONObject payload) {
        if (!payload.has("speedKmh") && !payload.has("minKmh") && !payload.has("maxKmh")) {
            return;
        }
        double fixed = positiveDouble(payload, "speedKmh", engine.snapshot().speedKmh);
        Double min = optionalPositiveDouble(payload, "minKmh");
        Double max = optionalPositiveDouble(payload, "maxKmh");
        engine.configureSpeed(fixed, min, max);
    }

    private LocalSimulationEngine.Point requirePoint(JSONObject payload) {
        LocalSimulationEngine.Point point = pointFromPayload(payload, true);
        if (point == null) throw new IllegalArgumentException("Coordinate is required");
        return point;
    }

    private LocalSimulationEngine.Point pointFromPayload(JSONObject payload, boolean required) {
        if (payload.has("lat") && payload.has("lng")) {
            double lat = payload.optDouble("lat", Double.NaN);
            double lng = payload.optDouble("lng", Double.NaN);
            if (validCoordinate(lat, lng)) return new LocalSimulationEngine.Point(lat, lng);
        }
        JSONObject point = payload.optJSONObject("point");
        if (point != null) {
            double lat = point.optDouble("lat", Double.NaN);
            double lng = point.optDouble("lng", Double.NaN);
            if (validCoordinate(lat, lng)) return new LocalSimulationEngine.Point(lat, lng);
        }
        LocalSimulationEngine.Point parsed = CoordinateParser.parse(payload.optString("text", ""));
        if (parsed != null) return parsed;
        parsed = CoordinateParser.parse(payload.optString("coordinate", ""));
        if (parsed != null) return parsed;
        if (required) throw new IllegalArgumentException("Coordinate is invalid");
        return null;
    }

    private List<LocalSimulationEngine.Point> requirePoints(JSONObject payload) {
        List<LocalSimulationEngine.Point> points = new ArrayList<>();
        JSONArray raw = payload.optJSONArray("points");
        if (raw != null) {
            RemoteCommandLimits.requireRouteSize(raw.length());
            for (int i = 0; i < raw.length(); i++) {
                LocalSimulationEngine.Point point = pointFromJson(raw.opt(i));
                if (point != null) points.add(point);
            }
        }
        if (points.isEmpty()) {
            points.addAll(CoordinateParser.parseMany(payload.optString("text", "")));
        }
        if (points.isEmpty()) {
            points.addAll(CoordinateParser.parseMany(payload.optString("routeText", "")));
        }
        if (points.isEmpty()) throw new IllegalArgumentException("Route points are required");
        RemoteCommandLimits.requireRouteSize(points.size());
        return points;
    }

    private LocalSimulationEngine.Point pointFromJson(Object value) {
        if (value instanceof JSONObject) {
            JSONObject item = (JSONObject) value;
            double lat = item.optDouble("lat", Double.NaN);
            double lng = item.optDouble("lng", Double.NaN);
            if (validCoordinate(lat, lng)) return new LocalSimulationEngine.Point(lat, lng);
            return CoordinateParser.parse(item.optString("text", ""));
        }
        if (value instanceof JSONArray) {
            JSONArray item = (JSONArray) value;
            double lat = item.optDouble(0, Double.NaN);
            double lng = item.optDouble(1, Double.NaN);
            if (validCoordinate(lat, lng)) return new LocalSimulationEngine.Point(lat, lng);
        }
        return CoordinateParser.parse(String.valueOf(value));
    }

    private void complete(ResultCallback callback, boolean ok, String message) {
        JSONObject out = new JSONObject();
        try {
            out.put("ok", ok);
            out.put("message", message == null ? "" : message);
            out.put("status", status());
        } catch (Exception ignored) {
        }
        callback.onResult(out);
    }

    private void completeWithoutStatus(ResultCallback callback, boolean ok, String message) {
        JSONObject out = new JSONObject();
        try {
            out.put("ok", ok);
            out.put("message", message == null ? "" : message);
        } catch (Exception ignored) {
        }
        callback.onResult(out);
    }

    private void completeData(
        ResultCallback callback,
        boolean ok,
        String message,
        String key,
        Object value
    ) {
        JSONObject out = new JSONObject();
        try {
            out.put("ok", ok);
            out.put("message", message == null ? "" : message);
            out.put(key, value == null ? JSONObject.NULL : value);
            out.put("status", status());
        } catch (Exception ignored) {
        }
        callback.onResult(out);
    }

    private void completeDataWithoutStatus(
        ResultCallback callback,
        boolean ok,
        String message,
        String key,
        Object value
    ) {
        JSONObject out = new JSONObject();
        try {
            out.put("ok", ok);
            out.put("message", message == null ? "" : message);
            out.put(key, value == null ? JSONObject.NULL : value);
        } catch (Exception ignored) {
        }
        callback.onResult(out);
    }

    private void completeSearch(
        ResultCallback callback,
        boolean ok,
        String message,
        List<Address> results
    ) {
        JSONObject out = new JSONObject();
        JSONArray items = new JSONArray();
        try {
            if (results != null) {
                for (Address address : results) {
                    if (address == null) continue;
                    if (!address.hasLatitude() || !address.hasLongitude()) continue;
                    double lat = address.getLatitude();
                    double lng = address.getLongitude();
                    if (!validCoordinate(lat, lng)) continue;
                    String label = address.getAddressLine(0);
                    if (label == null || label.trim().isEmpty()) {
                        label = address.getFeatureName();
                    }
                    JSONObject item = new JSONObject();
                    item.put(
                        "label",
                        label == null || label.trim().isEmpty()
                            ? LocalSimulationEngine.format(
                                new LocalSimulationEngine.Point(lat, lng)
                            )
                            : label.trim()
                    );
                    item.put("lat", lat);
                    item.put("lng", lng);
                    items.put(item);
                }
            }
            out.put("ok", ok);
            out.put("message", message == null ? "" : message);
            out.put("results", items);
            out.put("status", status());
        } catch (Exception ignored) {
        }
        callback.onResult(out);
    }

    private void putPoint(JSONObject target, String key, LocalSimulationEngine.Point point) {
        try {
            if (point == null) {
                target.put(key, JSONObject.NULL);
                return;
            }
            JSONObject raw = new JSONObject();
            raw.put("lat", point.lat);
            raw.put("lng", point.lng);
            target.put(key, raw);
        } catch (Exception ignored) {
        }
    }

    private JSONArray pointsJson(List<LocalSimulationEngine.Point> points) {
        JSONArray array = new JSONArray();
        try {
            for (LocalSimulationEngine.Point point : points) {
                JSONObject raw = new JSONObject();
                raw.put("lat", point.lat);
                raw.put("lng", point.lng);
                array.put(raw);
            }
        } catch (Exception ignored) {
        }
        return array;
    }

    private JSONArray bookmarksJson() {
        JSONArray array = new JSONArray();
        try {
            for (LocalDataStore.Bookmark bookmark : library.bookmarks()) {
                JSONObject item = new JSONObject();
                item.put("id", bookmark.id);
                item.put("name", bookmark.name);
                putPoint(item, "point", bookmark.point);
                array.put(item);
            }
        } catch (Exception ignored) {
        }
        return array;
    }

    private JSONArray routesJson() {
        JSONArray array = new JSONArray();
        try {
            for (LocalDataStore.Route route : library.routes()) {
                JSONObject item = new JSONObject();
                item.put("id", route.id);
                item.put("name", route.name);
                item.put("points", pointsJson(route.points));
                array.put(item);
            }
        } catch (Exception ignored) {
        }
        return array;
    }

    private JSONObject settingsJson() {
        JSONObject out = new JSONObject();
        try {
            JSONObject movement = new JSONObject();
            movement.put("speedKmh", OverlaySettings.speedKmh(context));
            movement.put("straightLine", OverlaySettings.isStraightLineMovement(context));
            movement.put("autoStopMinutes", OverlaySettings.safetyAutoStopMinutes(context));
            movement.put("restoreOnFinish", OverlaySettings.restoreOnMovementFinish(context));
            movement.put("resumeAfterRestart", OverlaySettings.resumeMovementAfterRestart(context));
            out.put("movement", movement);

            JSONObject overlays = new JSONObject();
            overlays.put("permissionGranted", Settings.canDrawOverlays(context));
            overlays.put("joystickVisible", OverlaySettings.isJoystickVisible(context));
            overlays.put("joystickSizeDp", OverlaySettings.joystickSizeDp(context));
            overlays.put("mapVisible", OverlaySettings.isMapHintVisible(context));
            overlays.put("mapSizeDp", OverlaySettings.mapHintSizeDp(context));
            out.put("overlays", overlays);

            MapTileCache.CacheStats cache = new MapTileCache(context).stats();
            JSONObject map = new JSONObject();
            map.put("mode", OverlaySettings.mapTileMode(context));
            map.put("template", OverlaySettings.mapTileTemplate(context));
            map.put("attribution", OverlaySettings.mapTileAttribution(context));
            map.put("interceptMapLinks", OverlaySettings.isMapIntentInterceptEnabled(context));
            map.put("cachedTiles", cache.tileCount);
            map.put("cachedBytes", cache.bytes);
            out.put("map", map);

            JSONObject steps = new JSONObject();
            steps.put("defaultSteps", OverlaySettings.defitStepCount(context));
            steps.put("followEnabled", OverlaySettings.isDefitWalkingStepsEnabled(context));
            steps.put("walkingMultiplier", OverlaySettings.walkingStepMultiplier(context));
            out.put("steps", steps);
        } catch (Exception ignored) {
        }
        return out;
    }

    private JSONArray hookAppsJson(boolean includeInstalled) {
        JSONArray out = new JSONArray();
        try {
            Set<String> known = XposedScopeRepository.isServiceConnected()
                ? new HashSet<>(XposedScopeRepository.getScope())
                : HookScopeSettings.fallbackPackages(context);
            known.remove(context.getPackageName());
            Set<String> manual = HookScopeSettings.manualPackages(context);
            Set<String> packages = new HashSet<>(known);
            if (includeInstalled && !XposedScopeRepository.isServiceConnected()) {
                PackageManager manager = context.getPackageManager();
                Intent launcher = new Intent(Intent.ACTION_MAIN)
                    .addCategory(Intent.CATEGORY_LAUNCHER);
                manager.queryIntentActivities(launcher, 0).forEach(info -> {
                    if (info.activityInfo != null
                        && HookScopeSettings.isValidPackageName(info.activityInfo.packageName)
                        && !context.getPackageName().equals(info.activityInfo.packageName)) {
                        packages.add(info.activityInfo.packageName);
                    }
                });
            }
            List<String> sorted = new ArrayList<>(packages);
            Collections.sort(sorted, String.CASE_INSENSITIVE_ORDER);
            PackageManager manager = context.getPackageManager();
            for (String packageName : sorted) {
                JSONObject item = new JSONObject();
                item.put("packageName", packageName);
                item.put("label", applicationLabel(manager, packageName));
                item.put("known", known.contains(packageName));
                item.put("manual", manual.contains(packageName));
                item.put("locationEnabled", HookScopeSettings.isLocationEnabled(context, packageName));
                item.put("stepEnabled", HookScopeSettings.isStepEnabled(context, packageName));
                item.put("lastSeenAt", HookScopeSettings.lastSeenAt(context, packageName));
                out.put(item);
            }
        } catch (Exception ignored) {
        }
        return out;
    }

    private JSONObject hookScopeJson() {
        JSONObject out = new JSONObject();
        try {
            boolean connected = XposedScopeRepository.isServiceConnected();
            out.put("official", connected);
            out.put("framework", connected ? XposedScopeRepository.frameworkLabel() : "");
            Throwable error = XposedScopeRepository.initializationError();
            out.put("error", error == null ? "" : error.getClass().getSimpleName());
        } catch (Exception ignored) {
        }
        return out;
    }

    private String applicationLabel(PackageManager manager, String packageName) {
        try {
            ApplicationInfo info = manager.getApplicationInfo(packageName, 0);
            CharSequence label = manager.getApplicationLabel(info);
            return label == null || label.toString().trim().isEmpty()
                ? packageName
                : label.toString();
        } catch (Exception ignored) {
            return packageName;
        }
    }

    private JSONObject relayJson() {
        JSONObject out = new JSONObject();
        try {
            RemoteRelayClient relay = RemoteRelayClient.get(context);
            out.put("running", relay.isRunning());
            out.put("connected", relay.isConnected());
            out.put("baseUrl", OverlaySettings.relayBaseUrl(context));
            out.put("publicUrl", relay.publicUrl());
            out.put("viewerCount", relay.viewerCount());
            out.put("admissionKeyId", relay.admissionKeyId());
            out.put("admissionFingerprint", relay.admissionFingerprint());
            out.put("admissionPublicKeyEntry", relay.admissionPublicKeyEntry());
            out.put("usbRunning", UsbControlService.isRunning());
            out.put("usbError", UsbControlService.serverError());
            out.put("viewers", viewersJson(relay.viewers()));
            out.put("pendingViewers", viewersJson(relay.pendingViewers()));
        } catch (Exception ignored) {
        }
        return out;
    }

    private JSONArray viewersJson(List<RemoteRelayClient.Viewer> viewers) {
        JSONArray out = new JSONArray();
        try {
            for (RemoteRelayClient.Viewer viewer : viewers) {
                JSONObject item = new JSONObject();
                item.put("id", viewer.id);
                item.put("name", viewer.name);
                item.put("deviceId", viewer.deviceId);
                item.put("accessRole", viewer.accessRole);
                item.put("connectedAt", viewer.connectedAt);
                item.put("pairingCode", viewer.pairingCode);
                out.put(item);
            }
        } catch (Exception ignored) {
        }
        return out;
    }

    private JSONArray historyJson() {
        JSONArray out = new JSONArray();
        try {
            for (OperationHistoryStore.Entry entry : OperationHistoryStore.entries(context)) {
                JSONObject item = new JSONObject();
                item.put("id", entry.id);
                item.put("timestampMs", entry.timestampMs);
                item.put("type", entry.type);
                item.put("message", entry.message);
                item.put("success", entry.success);
                out.put(item);
            }
        } catch (Exception ignored) {
        }
        return out;
    }

    private double positiveDouble(JSONObject payload, String key, double fallback) {
        return boundedPositiveDouble(
            payload,
            key,
            fallback,
            RemoteCommandLimits.MAX_SPEED_KMH
        );
    }

    private Double optionalPositiveDouble(JSONObject payload, String key) {
        if (!payload.has(key)) return null;
        double value = payload.optDouble(key, Double.NaN);
        return RemoteCommandLimits.optionalPositive(
            value,
            RemoteCommandLimits.MAX_SPEED_KMH
        );
    }

    private double boundedPositiveDouble(
        JSONObject payload,
        String key,
        double fallback,
        double maximum
    ) {
        return RemoteCommandLimits.boundedPositive(
            payload.optDouble(key, fallback),
            fallback,
            maximum
        );
    }

    private int boundedNonNegativeInt(
        JSONObject payload,
        String key,
        int maximum
    ) {
        return RemoteCommandLimits.boundedNonNegative(
            payload.optInt(key, 0),
            maximum
        );
    }

    private String boundedName(String value, String fallback) {
        return RemoteCommandLimits.boundedName(value, fallback);
    }

    private boolean validCoordinate(double lat, double lng) {
        return RemoteCommandLimits.validCoordinate(lat, lng);
    }
}
