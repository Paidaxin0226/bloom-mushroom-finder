package com.locwarp.mock;

import android.content.Context;
import android.os.Handler;
import android.os.Looper;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;

/** Keeps the phone's flower sweep workflow alive when it is controlled by USB. */
final class RemoteFlowerController implements LocalSimulationEngine.Listener {
    private static final int REQUIRED_PASSES = 2;
    private static final long COOLDOWN_MS = 5L * 60L * 1000L;
    private static volatile RemoteFlowerController instance;
    private static volatile boolean remoteControlling;

    static boolean isRemoteControlling() {
        return remoteControlling;
    }

    static RemoteFlowerController get(Context context) {
        if (instance == null) {
            synchronized (RemoteFlowerController.class) {
                if (instance == null) {
                    instance = new RemoteFlowerController(context);
                }
            }
        }
        return instance;
    }

    private final Context context;
    private final LocalSimulationEngine engine = LocalSimulationEngine.get();
    private final LocalDataStore library;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final Runnable cooldown = this::onCooldownFinished;

    private LocalDataStore.FlowerTarget activeTarget;
    private boolean routeRunning;
    private boolean routeSeenRunning;
    private boolean waiting;

    private RemoteFlowerController(Context context) {
        this.context = context.getApplicationContext();
        library = new LocalDataStore(this.context);
        engine.attach(this.context, this);
        if (OverlaySettings.isFlowerModeActive(this.context)
            && OverlaySettings.isFlowerRemoteControlled(this.context)) {
            remoteControlling = true;
            mainHandler.post(this::resume);
        }
    }

    synchronized boolean start(double radiusMeters, double spacingMeters) {
        remoteControlling = true;
        OverlaySettings.setFlowerRemoteControlled(context, true);
        OverlaySettings.setFlowerGeometry(context, radiusMeters, spacingMeters);
        OverlaySettings.setFlowerModeActive(context, true);
        resume();
        return true;
    }

    synchronized void stop() {
        remoteControlling = false;
        OverlaySettings.setFlowerRemoteControlled(context, false);
        mainHandler.removeCallbacks(cooldown);
        OverlaySettings.setFlowerModeActive(context, false);
        OverlaySettings.setFlowerCooldownUntilMs(context, 0L);
        activeTarget = null;
        waiting = false;
        routeRunning = false;
        routeSeenRunning = false;
        LocalSimulationEngine.Snapshot snapshot = engine.snapshot();
        if (snapshot.running && "flower_planting".equals(snapshot.state)) {
            engine.stop();
        }
    }

    synchronized void addTargets(List<LocalSimulationEngine.Point> points) {
        library.addFlowerTargets(points);
        if (OverlaySettings.isFlowerModeActive(context)
            && activeTarget == null && !waiting && !routeRunning) {
            mainHandler.post(this::startNext);
        }
    }

    synchronized void deleteTarget(String id) {
        if (activeTarget != null && activeTarget.id.equals(id)) {
            LocalSimulationEngine.Snapshot snapshot = engine.snapshot();
            if (snapshot.running && "flower_planting".equals(snapshot.state)) {
                engine.stop();
            }
            activeTarget = null;
            routeRunning = false;
            routeSeenRunning = false;
        }
        library.deleteFlowerTarget(id == null ? "" : id);
        mainHandler.post(this::startNext);
    }

    synchronized void clearTargets() {
        library.clearFlowerTargets();
        if (activeTarget != null || routeRunning) {
            LocalSimulationEngine.Snapshot snapshot = engine.snapshot();
            if (snapshot.running && "flower_planting".equals(snapshot.state)) {
                engine.stop();
            }
        }
        activeTarget = null;
        routeRunning = false;
        routeSeenRunning = false;
        waiting = false;
        mainHandler.removeCallbacks(cooldown);
        OverlaySettings.setFlowerCooldownUntilMs(context, 0L);
    }

    synchronized JSONObject status() {
        JSONObject out = new JSONObject();
        try {
            out.put("active", OverlaySettings.isFlowerModeActive(context));
            out.put("waiting", waiting);
            out.put("routeRunning", routeRunning);
            out.put("cooldownUntilMs", OverlaySettings.flowerCooldownUntilMs(context));
            out.put("radiusMeters", OverlaySettings.flowerRadiusMeters(context));
            out.put("spacingMeters", OverlaySettings.flowerSpacingMeters(context));
            out.put("activeTargetId", activeTarget == null ? JSONObject.NULL : activeTarget.id);
            JSONArray targets = new JSONArray();
            for (LocalDataStore.FlowerTarget target : library.flowerTargets()) {
                JSONObject item = new JSONObject();
                item.put("id", target.id);
                item.put("lat", target.center.lat);
                item.put("lng", target.center.lng);
                item.put("passesCompleted", target.passesCompleted);
                item.put("createdAtMs", target.createdAtMs);
                targets.put(item);
            }
            out.put("targets", targets);
        } catch (Exception ignored) {
        }
        return out;
    }

    @Override
    public void onSimulationUpdate(LocalSimulationEngine.Snapshot snapshot) {
        synchronized (this) {
            if (!OverlaySettings.isFlowerModeActive(context) || activeTarget == null) {
                return;
            }
            if (snapshot.running && "flower_planting".equals(snapshot.state)) {
                routeSeenRunning = true;
                return;
            }
            if (!routeRunning || !routeSeenRunning || snapshot.running) return;
            completePass();
        }
    }

    private synchronized void resume() {
        if (!OverlaySettings.isFlowerModeActive(context)) return;
        if (activeTarget != null || routeRunning || waiting) return;
        startNext();
    }

    private synchronized void startNext() {
        if (!OverlaySettings.isFlowerModeActive(context)) return;
        List<LocalDataStore.FlowerTarget> targets = library.flowerTargets();
        if (targets.isEmpty()) {
            activeTarget = null;
            waiting = false;
            return;
        }
        activeTarget = targets.get(0);
        long until = OverlaySettings.flowerCooldownUntilMs(context);
        if (activeTarget.passesCompleted > 0 && until > System.currentTimeMillis()) {
            waiting = true;
            mainHandler.removeCallbacks(cooldown);
            mainHandler.postDelayed(cooldown, Math.max(0L, until - System.currentTimeMillis()));
            return;
        }
        OverlaySettings.setFlowerCooldownUntilMs(context, 0L);
        startPass();
    }

    private synchronized void startPass() {
        if (activeTarget == null || !OverlaySettings.isFlowerModeActive(context)) return;
        waiting = false;
        LocalSimulationEngine.Point center = activeTarget.center;
        List<LocalSimulationEngine.Point> route = sweepRoute(
            center,
            OverlaySettings.flowerRadiusMeters(context),
            OverlaySettings.flowerSpacingMeters(context)
        );
        if (route.size() < 2 || !engine.teleport(center.lat, center.lng)) {
            stop();
            return;
        }
        routeRunning = engine.startFlowerPlantingRoute(route);
        routeSeenRunning = false;
        if (!routeRunning) stop();
    }

    private synchronized void completePass() {
        if (activeTarget == null) return;
        routeRunning = false;
        routeSeenRunning = false;
        int completed = activeTarget.passesCompleted + 1;
        if (completed >= REQUIRED_PASSES) {
            library.deleteFlowerTarget(activeTarget.id);
            activeTarget = null;
            mainHandler.postDelayed(this::startNext, 600L);
            return;
        }
        library.updateFlowerTargetPasses(activeTarget.id, completed);
        activeTarget = new LocalDataStore.FlowerTarget(
            activeTarget.id,
            activeTarget.center,
            completed,
            activeTarget.createdAtMs
        );
        waiting = true;
        long until = System.currentTimeMillis() + COOLDOWN_MS;
        OverlaySettings.setFlowerCooldownUntilMs(context, until);
        mainHandler.removeCallbacks(cooldown);
        mainHandler.postDelayed(cooldown, COOLDOWN_MS);
    }

    private synchronized void onCooldownFinished() {
        waiting = false;
        OverlaySettings.setFlowerCooldownUntilMs(context, 0L);
        startPass();
    }

    private List<LocalSimulationEngine.Point> sweepRoute(
        LocalSimulationEngine.Point center,
        double radiusMeters,
        double spacingMeters
    ) {
        List<LocalSimulationEngine.Point> points = new ArrayList<>();
        boolean reverse = false;
        for (double north = -radiusMeters;
             north <= radiusMeters + 0.01d;
             north += spacingMeters) {
            double half = Math.sqrt(Math.max(
                0d,
                radiusMeters * radiusMeters - north * north
            ));
            if (half < 2d) continue;
            double inset = Math.min(2d, half * 0.4d);
            double west = -half + inset;
            double east = half - inset;
            points.add(offset(center, reverse ? east : west, north));
            points.add(offset(center, reverse ? west : east, north));
            reverse = !reverse;
        }
        return points;
    }

    private LocalSimulationEngine.Point offset(
        LocalSimulationEngine.Point center,
        double eastMeters,
        double northMeters
    ) {
        double metersPerDegreeLat = 111320d;
        double metersPerDegreeLng = Math.max(
            1d,
            metersPerDegreeLat * Math.cos(Math.toRadians(center.lat))
        );
        return new LocalSimulationEngine.Point(
            center.lat + northMeters / metersPerDegreeLat,
            center.lng + eastMeters / metersPerDegreeLng
        );
    }
}
