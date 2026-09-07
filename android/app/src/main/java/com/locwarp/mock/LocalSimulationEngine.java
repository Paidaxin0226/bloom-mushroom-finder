package com.locwarp.mock;

import android.annotation.SuppressLint;
import android.content.Context;
import android.content.Intent;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.Looper;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Locale;
import java.util.Random;

final class LocalSimulationEngine {
    interface Listener {
        void onSimulationUpdate(Snapshot snapshot);
    }

    static final class Point {
        final double lat;
        final double lng;

        Point(double lat, double lng) {
            this.lat = lat;
            this.lng = lng;
        }
    }

    static final class Snapshot {
        final String state;
        final boolean running;
        final boolean paused;
        final Point current;
        final Point target;
        final List<Point> route;
        final double speedKmh;
        final int routeIndex;
        final int lap;
        final long routeRevision;

        Snapshot(
            String state,
            boolean running,
            boolean paused,
            Point current,
            Point target,
            List<Point> route,
            double speedKmh,
            int routeIndex,
            int lap,
            long routeRevision
        ) {
            this.state = state;
            this.running = running;
            this.paused = paused;
            this.current = current;
            this.target = target;
            this.route = route;
            this.speedKmh = speedKmh;
            this.routeIndex = routeIndex;
            this.lap = lap;
            this.routeRevision = routeRevision;
        }
    }

    private static final long TICK_MS = 400L;
    private static final long SESSION_PERSIST_INTERVAL_MS = 5_000L;
    private static final double MAX_SPEED_KMH = 200d;
    // This singleton stores only context.getApplicationContext(), never an Activity.
    @SuppressLint("StaticFieldLeak")
    private static final LocalSimulationEngine INSTANCE = new LocalSimulationEngine();

    static LocalSimulationEngine get() {
        return INSTANCE;
    }

    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final Random random = new Random();
    private final LatestValueQueue<Snapshot> listenerUpdates =
        new LatestValueQueue<>();
    private HandlerThread workerThread;
    private Handler worker;
    private Context context;
    private final List<Listener> listeners = new ArrayList<>();

    private String state = "idle";
    private boolean running;
    private boolean paused;
    private Point current;
    private Point target;
    private final List<Point> route = new ArrayList<>();
    private final List<Integer> routePausePointIndices = new ArrayList<>();
    private long routeRevision;
    private int routeIndex;
    private boolean routeLoops;
    private int lap;
    private int lapLimit;
    private long pauseUntilMs;
    private int stopDurationSeconds;
    private boolean routePauseEnabled;
    private double routePauseMin;
    private double routePauseMax;
    private double speedKmh = 10.8d;
    private Double speedMinKmh;
    private Double speedMaxKmh;
    private Point randomCenter;
    private boolean foregroundStarted;
    private double randomRadiusM = 500d;
    private boolean randomPauseEnabled = true;
    private double randomPauseMin = 5d;
    private double randomPauseMax = 20d;
    private boolean randomUsePathRoutes;
    private boolean randomRoutePlanning;
    private AsyncTaskGate.CancelableTask<List<Point>> randomRouteTask;
    private double joystickDirection;
    private double joystickIntensity;
    private int safetyAutoStopMinutes;
    private boolean restoreOnFinish;
    private long safetyDeadlineEpochMs;
    private long lastSessionPersistMs;
    private boolean sessionRestoreAttempted;

    private final Runnable tick = new Runnable() {
        @Override
        public void run() {
            synchronized (LocalSimulationEngine.this) {
                if (!running) return;
                if (safetyDeadlineEpochMs > 0L
                    && System.currentTimeMillis() >= safetyDeadlineEpochMs) {
                    finishRouteLocked("safety timeout");
                    return;
                }
                if (paused || System.currentTimeMillis() < pauseUntilMs) {
                    scheduleTickLocked();
                    persistSessionLocked(false);
                    notifyListenerLocked();
                    return;
                }
                if ("joystick".equals(state)) {
                    tickJoystickLocked();
                } else if ("random_walk".equals(state)) {
                    tickRandomWalkLocked();
                } else {
                    tickRouteLocked();
                }
                persistSessionLocked(false);
                if (running) scheduleTickLocked();
            }
        }
    };

    private LocalSimulationEngine() {
    }

    synchronized void attach(Context nextContext, Listener nextListener) {
        context = nextContext.getApplicationContext();
        safetyAutoStopMinutes = OverlaySettings.safetyAutoStopMinutes(context);
        restoreOnFinish = OverlaySettings.restoreOnMovementFinish(context);
        if (nextListener != null && !listeners.contains(nextListener)) {
            listeners.add(nextListener);
        }
        MockLocationKeeper.Snapshot saved = MockLocationKeeper.snapshot(context);
        if (current == null && saved.active) {
            current = new Point(saved.lat, saved.lng);
        }
        restoreSessionLocked(saved.active);
        ensureWorkerLocked();
        notifyListenerLocked();
    }

    synchronized void detach(Listener target) {
        listeners.remove(target);
    }

    synchronized Snapshot snapshot() {
        return snapshotLocked();
    }

    synchronized void seedCurrent(Point point) {
        if (point == null || running || current != null) return;
        current = point;
        transitionLifecycleLocked(
            SimulationStateMachine.Event.STOP,
            null,
            false
        );
        notifyListenerLocked();
    }

    synchronized boolean teleport(double lat, double lng) {
        stopLocked(false);
        transitionLifecycleLocked(
            SimulationStateMachine.Event.START,
            "teleporting",
            false
        );
        current = new Point(lat, lng);
        target = null;
        route.clear();
        routeRevision++;
        boolean applied = pushLocked(0f, Float.NaN);
        transitionLifecycleLocked(
            applied
                ? SimulationStateMachine.Event.FINISH
                : SimulationStateMachine.Event.FAIL,
            null,
            false
        );
        OperationHistoryStore.record(
            context,
            "teleport",
            format(current),
            applied
        );
        notifyListenerLocked();
        return applied;
    }

    synchronized boolean navigate(Point destination) {
        if (!requireCurrentLocked()) return false;
        List<Point> points = new ArrayList<>();
        points.add(current);
        points.add(destination);
        return startRouteLocked(
            "navigating",
            points,
            false,
            0,
            0,
            false,
            0d,
            0d
        );
    }

    synchronized boolean startPathRoute(List<Point> points) {
        if (!requireCurrentLocked() || points == null || points.size() < 2) return false;
        List<Point> normalized = normalizeRouteLocked(points);
        return startRouteLocked(
            "path_navigation",
            normalized,
            false,
            0,
            0,
            false,
            0d,
            0d
        );
    }

    synchronized boolean startFlowerPlantingRoute(List<Point> points) {
        if (!requireCurrentLocked() || points == null || points.size() < 2) return false;
        List<Point> normalized = normalizeRouteLocked(points);
        return startRouteLocked(
            "flower_planting",
            normalized,
            false,
            0,
            0,
            false,
            0d,
            0d
        );
    }

    synchronized boolean startLoop(
        List<Point> points,
        int laps,
        boolean pauseEnabled,
        double pauseMin,
        double pauseMax
    ) {
        if (!requireCurrentLocked() || points == null || points.size() < 2) return false;
        List<Point> normalized = normalizeRouteLocked(points);
        return startRouteLocked(
            "looping",
            normalized,
            true,
            0,
            Math.max(0, laps),
            pauseEnabled,
            pauseMin,
            pauseMax
        );
    }

    synchronized boolean startLoopRoute(
        List<Point> routePoints,
        List<Integer> pausePointIndices,
        int laps,
        boolean pauseEnabled,
        double pauseMin,
        double pauseMax
    ) {
        if (!requireCurrentLocked() || routePoints == null || routePoints.size() < 2) return false;
        List<Point> normalized = normalizeRouteLocked(routePoints);
        return startRouteLocked(
            "looping",
            normalized,
            true,
            0,
            Math.max(0, laps),
            pauseEnabled,
            pauseMin,
            pauseMax,
            normalizePauseIndices(routePoints, normalized, pausePointIndices)
        );
    }

    synchronized boolean startMultiStop(
        List<Point> points,
        int stopSeconds,
        boolean loop,
        boolean pauseEnabled,
        double pauseMin,
        double pauseMax
    ) {
        if (!requireCurrentLocked() || points == null || points.size() < 2) return false;
        List<Point> normalized = normalizeRouteLocked(points);
        return startRouteLocked(
            "multi_stop",
            normalized,
            loop,
            Math.max(0, stopSeconds),
            0,
            pauseEnabled,
            pauseMin,
            pauseMax
        );
    }

    synchronized boolean startMultiStopRoute(
        List<Point> routePoints,
        List<Integer> stopPointIndices,
        int stopSeconds,
        boolean loop,
        boolean pauseEnabled,
        double pauseMin,
        double pauseMax
    ) {
        if (!requireCurrentLocked() || routePoints == null || routePoints.size() < 2) return false;
        List<Point> normalized = normalizeRouteLocked(routePoints);
        return startRouteLocked(
            "multi_stop",
            normalized,
            loop,
            Math.max(0, stopSeconds),
            0,
            pauseEnabled,
            pauseMin,
            pauseMax,
            normalizePauseIndices(routePoints, normalized, stopPointIndices)
        );
    }

    synchronized boolean startRandomWalk(
        Point center,
        double radiusM,
        boolean pauseEnabled,
        double pauseMin,
        double pauseMax
    ) {
        return startRandomWalk(center, radiusM, pauseEnabled, pauseMin, pauseMax, false);
    }

    synchronized boolean startRandomWalk(
        Point center,
        double radiusM,
        boolean pauseEnabled,
        double pauseMin,
        double pauseMax,
        boolean usePathRoutes
    ) {
        if (!requireCurrentLocked()) return false;
        stopLocked(false);
        transitionLifecycleLocked(
            SimulationStateMachine.Event.START,
            "random_walk",
            false
        );
        randomCenter = center == null ? current : center;
        randomRadiusM = Math.max(10d, radiusM);
        randomPauseEnabled = pauseEnabled;
        randomPauseMin = Math.max(0d, Math.min(pauseMin, pauseMax));
        randomPauseMax = Math.max(randomPauseMin, Math.max(pauseMin, pauseMax));
        randomUsePathRoutes = usePathRoutes;
        randomRoutePlanning = false;
        target = randomPoint(randomCenter, randomRadiusM);
        route.clear();
        routeIndex = 0;
        if (randomUsePathRoutes) {
            route.add(current);
            startRandomRoutePlanningLocked(current, target);
        } else {
            route.add(current);
            route.add(target);
        }
        routeRevision++;
        beginMovementLocked();
        OperationHistoryStore.record(
            context,
            "movement",
            "開始隨機漫步，半徑 " + Math.round(randomRadiusM) + " m",
            true
        );
        scheduleTickLocked();
        notifyListenerLocked();
        return true;
    }

    synchronized boolean startJoystick() {
        if (!requireCurrentLocked()) return false;
        stopLocked(false);
        transitionLifecycleLocked(
            SimulationStateMachine.Event.START,
            "joystick",
            false
        );
        target = null;
        route.clear();
        routeRevision++;
        joystickIntensity = 0d;
        beginMovementLocked();
        OperationHistoryStore.record(
            context,
            "movement",
            "開始浮動搖桿控制",
            true
        );
        scheduleTickLocked();
        notifyListenerLocked();
        return true;
    }

    synchronized void joystickInput(double direction, double intensity) {
        joystickDirection = normalizeBearing(direction);
        joystickIntensity = clamp(intensity, 0d, 1d);
        if ("joystick".equals(state) && running) {
            scheduleTickLocked();
        }
    }

    synchronized void configureSpeed(
        double fixedKmh,
        Double minKmh,
        Double maxKmh
    ) {
        speedKmh = clamp(fixedKmh, 0.1d, MAX_SPEED_KMH);
        if (minKmh != null && maxKmh != null) {
            speedMinKmh = clamp(
                Math.min(minKmh, maxKmh),
                0.1d,
                MAX_SPEED_KMH
            );
            speedMaxKmh = clamp(
                Math.max(minKmh, maxKmh),
                speedMinKmh,
                MAX_SPEED_KMH
            );
        } else {
            speedMinKmh = null;
            speedMaxKmh = null;
        }
        notifyListenerLocked();
    }

    synchronized void configureSafety(
        int autoStopMinutes,
        boolean shouldRestoreOnFinish
    ) {
        safetyAutoStopMinutes = Math.max(0, Math.min(24 * 60, autoStopMinutes));
        restoreOnFinish = shouldRestoreOnFinish;
        if (running) {
            safetyDeadlineEpochMs = safetyAutoStopMinutes <= 0
                ? 0L
                : System.currentTimeMillis() + safetyAutoStopMinutes * 60_000L;
            persistSessionLocked(true);
        }
    }

    synchronized void pause() {
        if (!running) return;
        transitionLifecycleLocked(
            SimulationStateMachine.Event.PAUSE,
            null,
            false
        );
        persistSessionLocked(true);
        notifyListenerLocked();
    }

    synchronized void resume() {
        if (!running) return;
        transitionLifecycleLocked(
            SimulationStateMachine.Event.RESUME,
            null,
            false
        );
        pauseUntilMs = 0L;
        persistSessionLocked(true);
        scheduleTickLocked();
        notifyListenerLocked();
    }

    synchronized void stop() {
        if (running) {
            OperationHistoryStore.record(
                context,
                "movement",
                "手動停止 " + state,
                true
            );
        }
        stopLocked(false);
        notifyListenerLocked();
    }

    synchronized void restore() {
        stopLocked(true);
        current = null;
        if (context != null) {
            MockLocationKeeper.clearLocal(context);
            Intent intent = new Intent(context, LocalMockService.class)
                .setAction(LocalMockService.ACTION_CLEAR);
            context.startService(intent);
        }
        foregroundStarted = false;
        OperationHistoryStore.record(
            context,
            "movement",
            "已恢復真實定位",
            true
        );
        notifyListenerLocked();
    }

    private boolean startRouteLocked(
        String nextState,
        List<Point> points,
        boolean loops,
        int stopSeconds,
        int laps,
        boolean pauseEnabled,
        double pauseMin,
        double pauseMax
    ) {
        return startRouteLocked(
            nextState,
            points,
            loops,
            stopSeconds,
            laps,
            pauseEnabled,
            pauseMin,
            pauseMax,
            null
        );
    }

    private boolean startRouteLocked(
        String nextState,
        List<Point> points,
        boolean loops,
        int stopSeconds,
        int laps,
        boolean pauseEnabled,
        double pauseMin,
        double pauseMax,
        List<Integer> pausePointIndices
    ) {
        stopLocked(false);
        route.clear();
        route.addAll(points);
        routeRevision++;
        routePausePointIndices.clear();
        if (pausePointIndices != null) {
            routePausePointIndices.addAll(pausePointIndices);
        }
        routeIndex = route.size() > 1 ? 1 : 0;
        routeLoops = loops;
        lap = 0;
        lapLimit = laps;
        stopDurationSeconds = stopSeconds;
        routePauseEnabled = pauseEnabled;
        routePauseMin = Math.max(0d, Math.min(pauseMin, pauseMax));
        routePauseMax = Math.max(routePauseMin, Math.max(pauseMin, pauseMax));
        transitionLifecycleLocked(
            SimulationStateMachine.Event.START,
            nextState,
            false
        );
        target = route.get(routeIndex);
        beginMovementLocked();
        OperationHistoryStore.record(
            context,
            "movement",
            "開始 " + nextState + "，共 " + route.size() + " 個路徑點",
            true
        );
        scheduleTickLocked();
        notifyListenerLocked();
        return true;
    }

    private List<Point> normalizeRouteLocked(List<Point> points) {
        List<Point> normalized = new ArrayList<>();
        if (points.isEmpty() || distanceMeters(current, points.get(0)) > 2d) {
            normalized.add(current);
        }
        normalized.addAll(points);
        return normalized;
    }

    private List<Integer> normalizePauseIndices(
        List<Point> original,
        List<Point> normalized,
        List<Integer> indices
    ) {
        List<Integer> out = new ArrayList<>();
        if (indices == null || indices.isEmpty()) return out;
        int offset = normalized.size() > original.size() ? 1 : 0;
        for (Integer index : indices) {
            if (index == null) continue;
            int next = index + offset;
            if (next >= 0 && next < normalized.size()) {
                out.add(next);
            }
        }
        return out;
    }

    private boolean shouldPauseAtRouteIndex(int index) {
        return routePausePointIndices.isEmpty() || routePausePointIndices.contains(index);
    }

    private void tickRouteLocked() {
        if (target == null || current == null) {
            finishRouteLocked();
            return;
        }
        boolean reached = advanceTowardLocked(target, currentSpeedKmhLocked());
        if (!reached) return;

        int reachedIndex = routeIndex;
        routeIndex++;
        if (routeIndex < route.size()) {
            target = route.get(routeIndex);
            boolean shouldPause = shouldPauseAtRouteIndex(reachedIndex);
            double pauseSeconds =
                "multi_stop".equals(state) && shouldPause ? stopDurationSeconds : 0d;
            if (routePauseEnabled && routePauseMax > 0d && shouldPause) {
                pauseSeconds += routePauseMin
                    + random.nextDouble() * Math.max(0d, routePauseMax - routePauseMin);
            }
            if (pauseSeconds > 0d) {
                pauseUntilMs = System.currentTimeMillis() + (long) (pauseSeconds * 1000d);
            }
            notifyListenerLocked();
            return;
        }

        if (routeLoops) {
            lap++;
            if (lapLimit > 0 && lap >= lapLimit) {
                finishRouteLocked();
                return;
            }
            routeIndex = 0;
            target = route.get(routeIndex);
            notifyListenerLocked();
            return;
        }
        finishRouteLocked();
    }

    private void tickRandomWalkLocked() {
        if (current == null || randomCenter == null) {
            finishRouteLocked();
            return;
        }
        if (randomUsePathRoutes) {
            tickRoutedRandomWalkLocked();
            return;
        }
        if (target == null) {
            target = randomPoint(randomCenter, randomRadiusM);
            route.clear();
            route.add(current);
            route.add(target);
            routeRevision++;
            notifyListenerLocked();
        }
        if (advanceTowardLocked(target, currentSpeedKmhLocked())) {
            target = null;
            if (randomPauseEnabled && randomPauseMax > 0d) {
                double seconds = randomPauseMin
                    + random.nextDouble() * Math.max(0d, randomPauseMax - randomPauseMin);
                pauseUntilMs = System.currentTimeMillis() + (long) (seconds * 1000d);
            }
            notifyListenerLocked();
        }
    }

    private void tickRoutedRandomWalkLocked() {
        if (target == null) {
            target = randomPoint(randomCenter, randomRadiusM);
            route.clear();
            route.add(current);
            routeRevision++;
            routeIndex = 0;
            startRandomRoutePlanningLocked(current, target);
            notifyListenerLocked();
            return;
        }

        if (randomRoutePlanning) {
            notifyListenerLocked();
            return;
        }

        if (route.size() < 2 || routeIndex <= 0 || routeIndex >= route.size()) {
            route.clear();
            route.add(current);
            routeRevision++;
            routeIndex = 0;
            startRandomRoutePlanningLocked(current, target);
            notifyListenerLocked();
            return;
        }

        if (advanceTowardLocked(route.get(routeIndex), currentSpeedKmhLocked())) {
            routeIndex++;
            if (routeIndex >= route.size()) {
                target = null;
                routeIndex = 0;
                if (randomPauseEnabled && randomPauseMax > 0d) {
                    double seconds = randomPauseMin
                        + random.nextDouble() * Math.max(0d, randomPauseMax - randomPauseMin);
                    pauseUntilMs = System.currentTimeMillis() + (long) (seconds * 1000d);
                }
                notifyListenerLocked();
            } else {
                notifyListenerLocked();
            }
        }
    }

    private void startRandomRoutePlanningLocked(Point start, Point destination) {
        if (randomRoutePlanning || start == null || destination == null) return;
        randomRoutePlanning = true;
        Point plannedTarget = destination;
        Point plannedStart = start;
        AsyncTaskGate.CancelableTask<List<Point>> task =
            WalkingRoutePlanner.planTask(plannedStart, plannedTarget);
        randomRouteTask = task;
        new Thread(() -> {
            List<Point> planned;
            try {
                planned = task.call();
            } catch (Exception error) {
                planned = new ArrayList<>();
                planned.add(plannedStart);
                planned.add(plannedTarget);
            }
            synchronized (LocalSimulationEngine.this) {
                if (randomRouteTask == task) randomRouteTask = null;
                if (!running || !"random_walk".equals(state)) {
                    return;
                }
                if (target == null || distanceMeters(target, plannedTarget) > 1d) {
                    randomRoutePlanning = false;
                    return;
                }
                route.clear();
                route.addAll(planned);
                routeRevision++;
                routeIndex = route.size() > 1 ? 1 : 0;
                randomRoutePlanning = false;
                scheduleTickLocked();
                notifyListenerLocked();
            }
        }, "LocWarpRandomRoutePlanner").start();
    }

    private void tickJoystickLocked() {
        if (current == null) {
            finishRouteLocked();
            return;
        }
        if (joystickIntensity <= 0d) {
            if (!pushLocked(0f, (float) joystickDirection)) {
                failProviderLocked();
            }
            return;
        }
        double speed = currentSpeedKmhLocked() / 3.6d * joystickIntensity;
        double distance = speed * TICK_MS / 1000d;
        current = movePoint(current, joystickDirection, distance);
        if (!pushLocked((float) speed, (float) joystickDirection)) {
            failProviderLocked();
            return;
        }
        notifyListenerLocked();
    }

    private boolean advanceTowardLocked(Point destination, double speedKmhNow) {
        double remaining = distanceMeters(current, destination);
        double speedMps = Math.max(0.03d, speedKmhNow / 3.6d);
        double step = speedMps * TICK_MS / 1000d;
        double bearing = bearingDegrees(current, destination);
        if (remaining <= Math.max(step, 0.8d)) {
            current = destination;
            if (!pushLocked((float) speedMps, (float) bearing)) {
                failProviderLocked();
                return false;
            }
            notifyListenerLocked();
            return true;
        }
        current = movePoint(current, bearing, step);
        if (!pushLocked((float) speedMps, (float) bearing)) {
            failProviderLocked();
            return false;
        }
        notifyListenerLocked();
        return false;
    }

    private double currentSpeedKmhLocked() {
        if (speedMinKmh == null || speedMaxKmh == null) return speedKmh;
        return speedMinKmh + random.nextDouble() * (speedMaxKmh - speedMinKmh);
    }

    private boolean pushLocked(float speedMps, float bearing) {
        if (context == null || current == null) return false;
        boolean applied = MockLocationKeeper.startLocal(
            context,
            current.lat,
            current.lng,
            speedMps,
            bearing
        );
        if (applied) ensureForegroundLocked();
        return applied;
    }

    private boolean requireCurrentLocked() {
        if (current != null) return true;
        if (context == null) return false;
        MockLocationKeeper.Snapshot saved = MockLocationKeeper.snapshot(context);
        if (!saved.active) return false;
        current = new Point(saved.lat, saved.lng);
        return true;
    }

    private void finishRouteLocked() {
        finishRouteLocked("completed");
    }

    private void finishRouteLocked(String reason) {
        String finishedState = state;
        transitionLifecycleLocked(
            SimulationStateMachine.Event.FINISH,
            null,
            false
        );
        target = null;
        routeIndex = 0;
        routePausePointIndices.clear();
        pauseUntilMs = 0L;
        joystickIntensity = 0d;
        randomRoutePlanning = false;
        cancelRandomRouteTaskLocked();
        safetyDeadlineEpochMs = 0L;
        SimulationSessionStore.clear(context);
        MockLocationKeeper.persistNow(context);
        OperationHistoryStore.record(
            context,
            "movement",
            finishedState + " · " + reason,
            true
        );
        if (restoreOnFinish) restoreMockLocked();
        notifyListenerLocked();
    }

    private void failProviderLocked() {
        transitionLifecycleLocked(
            SimulationStateMachine.Event.FAIL,
            null,
            false
        );
        target = null;
        routeIndex = 0;
        pauseUntilMs = 0L;
        joystickIntensity = 0d;
        randomRoutePlanning = false;
        cancelRandomRouteTaskLocked();
        safetyDeadlineEpochMs = 0L;
        SimulationSessionStore.clear(context);
        OperationHistoryStore.record(
            context,
            "movement",
            "模擬位置 provider 失效",
            false
        );
        if (worker != null) worker.removeCallbacks(tick);
        notifyListenerLocked();
    }

    private void stopLocked(boolean clearRoute) {
        transitionLifecycleLocked(
            SimulationStateMachine.Event.STOP,
            null,
            false
        );
        target = null;
        routeIndex = 0;
        routeLoops = false;
        routePausePointIndices.clear();
        pauseUntilMs = 0L;
        joystickIntensity = 0d;
        randomRoutePlanning = false;
        cancelRandomRouteTaskLocked();
        randomUsePathRoutes = false;
        safetyDeadlineEpochMs = 0L;
        SimulationSessionStore.clear(context);
        MockLocationKeeper.persistNow(context);
        if (worker != null) worker.removeCallbacks(tick);
        if (clearRoute) {
            route.clear();
            routeRevision++;
        }
    }

    private void cancelRandomRouteTaskLocked() {
        AsyncTaskGate.CancelableTask<List<Point>> task = randomRouteTask;
        randomRouteTask = null;
        if (task != null) task.cancel();
    }

    private void beginMovementLocked() {
        safetyDeadlineEpochMs = safetyAutoStopMinutes <= 0
            ? 0L
            : System.currentTimeMillis() + safetyAutoStopMinutes * 60_000L;
        persistSessionLocked(true);
    }

    private void persistSessionLocked(boolean force) {
        if (context == null || !running) return;
        long now = System.currentTimeMillis();
        if (!force
            && lastSessionPersistMs > 0L
            && now - lastSessionPersistMs < SESSION_PERSIST_INTERVAL_MS) {
            return;
        }
        SimulationSessionStore.Session session =
            new SimulationSessionStore.Session();
        session.state = state;
        session.paused = paused;
        session.route.addAll(route);
        session.pauseIndices.addAll(routePausePointIndices);
        session.routeIndex = routeIndex;
        session.routeLoops = routeLoops;
        session.lap = lap;
        session.lapLimit = lapLimit;
        session.stopSeconds = stopDurationSeconds;
        session.pauseEnabled = routePauseEnabled;
        session.pauseMin = routePauseMin;
        session.pauseMax = routePauseMax;
        session.speedKmh = speedKmh;
        session.speedMinKmh = speedMinKmh;
        session.speedMaxKmh = speedMaxKmh;
        session.target = target;
        session.randomCenter = randomCenter;
        session.randomRadiusM = randomRadiusM;
        session.randomPauseEnabled = randomPauseEnabled;
        session.randomPauseMin = randomPauseMin;
        session.randomPauseMax = randomPauseMax;
        session.randomUsePath = randomUsePathRoutes;
        session.safetyDeadlineEpochMs = safetyDeadlineEpochMs;
        session.restoreOnFinish = restoreOnFinish;
        SimulationSessionStore.save(context, session);
        lastSessionPersistMs = now;
    }

    private void restoreSessionLocked(boolean mockActive) {
        if (sessionRestoreAttempted || running || context == null) return;
        sessionRestoreAttempted = true;
        if (!mockActive || !OverlaySettings.resumeMovementAfterRestart(context)) {
            SimulationSessionStore.clear(context);
            return;
        }
        SimulationSessionStore.Session session =
            SimulationSessionStore.load(context);
        if (session == null || session.state == null) return;
        boolean randomSession = "random_walk".equals(session.state);
        boolean joystickSession = "joystick".equals(session.state);
        if (!randomSession && !joystickSession && session.route.size() < 2) {
            SimulationSessionStore.clear(context);
            return;
        }
        try {
            transitionLifecycleLocked(
                SimulationStateMachine.Event.RESTORE,
                session.state,
                session.paused || joystickSession
            );
        } catch (IllegalArgumentException invalidState) {
            SimulationSessionStore.clear(context);
            return;
        }
        route.clear();
        route.addAll(session.route);
        routeRevision++;
        routePausePointIndices.clear();
        routePausePointIndices.addAll(session.pauseIndices);
        routeIndex = route.isEmpty()
            ? 0
            : Math.max(0, Math.min(session.routeIndex, route.size() - 1));
        routeLoops = session.routeLoops;
        lap = Math.max(0, session.lap);
        lapLimit = Math.max(0, Math.min(10_000, session.lapLimit));
        stopDurationSeconds = Math.max(0, Math.min(86_400, session.stopSeconds));
        routePauseEnabled = session.pauseEnabled;
        routePauseMin = clamp(session.pauseMin, 0d, 86_400d);
        routePauseMax = clamp(session.pauseMax, routePauseMin, 86_400d);
        speedKmh = clamp(session.speedKmh, 0.1d, MAX_SPEED_KMH);
        speedMinKmh = session.speedMinKmh == null
            ? null
            : clamp(session.speedMinKmh, 0.1d, MAX_SPEED_KMH);
        speedMaxKmh = session.speedMaxKmh == null
            ? null
            : clamp(session.speedMaxKmh, 0.1d, MAX_SPEED_KMH);
        if (speedMinKmh != null && speedMaxKmh != null
            && speedMinKmh > speedMaxKmh) {
            double swap = speedMinKmh;
            speedMinKmh = speedMaxKmh;
            speedMaxKmh = swap;
        }
        target = session.target;
        if (target == null && !route.isEmpty()) target = route.get(routeIndex);
        randomCenter = session.randomCenter == null ? current : session.randomCenter;
        randomRadiusM = clamp(session.randomRadiusM, 10d, 100_000d);
        randomPauseEnabled = session.randomPauseEnabled;
        randomPauseMin = clamp(session.randomPauseMin, 0d, 86_400d);
        randomPauseMax = clamp(
            session.randomPauseMax,
            randomPauseMin,
            86_400d
        );
        randomUsePathRoutes = session.randomUsePath;
        safetyDeadlineEpochMs = session.safetyDeadlineEpochMs;
        restoreOnFinish = session.restoreOnFinish;
        if (safetyDeadlineEpochMs > 0L
            && System.currentTimeMillis() >= safetyDeadlineEpochMs) {
            finishRouteLocked("expired during restart");
            return;
        }
        ensureForegroundLocked();
        scheduleTickLocked();
        OperationHistoryStore.record(
            context,
            "movement",
            "程序重啟後恢復 " + state,
            true
        );
    }

    private void restoreMockLocked() {
        if (context == null) return;
        current = null;
        MockLocationKeeper.clearLocal(context);
        Intent intent = new Intent(context, LocalMockService.class)
            .setAction(LocalMockService.ACTION_CLEAR);
        context.startService(intent);
        foregroundStarted = false;
    }

    private void scheduleTickLocked() {
        ensureWorkerLocked();
        worker.removeCallbacks(tick);
        worker.postDelayed(tick, TICK_MS);
    }

    private void ensureWorkerLocked() {
        if (workerThread != null && workerThread.isAlive() && worker != null) return;
        workerThread = new HandlerThread(
            "LocWarpLocalSimulation",
            android.os.Process.THREAD_PRIORITY_BACKGROUND
        );
        workerThread.start();
        worker = new Handler(workerThread.getLooper());
    }

    private void ensureForegroundLocked() {
        if (context == null || foregroundStarted) return;
        Intent intent = new Intent(context, LocalMockService.class)
            .setAction(LocalMockService.ACTION_START);
        context.startForegroundService(intent);
        foregroundStarted = true;
    }

    private void transitionLifecycleLocked(
        SimulationStateMachine.Event event,
        String requestedState,
        boolean restoredPaused
    ) {
        SimulationStateMachine.State next = SimulationStateMachine.transition(
            new SimulationStateMachine.State(state, running, paused),
            event,
            requestedState,
            restoredPaused
        );
        state = next.name;
        running = next.running;
        paused = next.paused;
    }

    private void notifyListenerLocked() {
        if (listeners.isEmpty()) return;
        if (listenerUpdates.offer(snapshotLocked())) {
            mainHandler.post(this::dispatchLatestListenerUpdate);
        }
    }

    private void dispatchLatestListenerUpdate() {
        Snapshot next = listenerUpdates.take();
        if (next == null) return;
        List<Listener> targets;
        synchronized (this) {
            targets = new ArrayList<>(listeners);
        }
        for (Listener targetListener : targets) {
            targetListener.onSimulationUpdate(next);
        }
    }

    private Snapshot snapshotLocked() {
        List<Point> copiedRoute = Collections.unmodifiableList(new ArrayList<>(route));
        return new Snapshot(
            state,
            running,
            paused,
            current,
            target,
            copiedRoute,
            speedKmh,
            routeIndex,
            lap,
            routeRevision
        );
    }

    private Point randomPoint(Point center, double radiusM) {
        double distance = radiusM * Math.sqrt(random.nextDouble());
        double bearing = random.nextDouble() * 360d;
        return movePoint(center, bearing, distance);
    }

    static Point movePoint(Point start, double bearingDegrees, double distanceM) {
        double[] moved = GeoMath.move(start.lat, start.lng, bearingDegrees, distanceM);
        return new Point(moved[0], moved[1]);
    }

    static double distanceMeters(Point a, Point b) {
        if (a == null || b == null) return Double.POSITIVE_INFINITY;
        return GeoMath.distanceMeters(a.lat, a.lng, b.lat, b.lng);
    }

    private static double bearingDegrees(Point a, Point b) {
        double lat1 = Math.toRadians(a.lat);
        double lat2 = Math.toRadians(b.lat);
        double dLng = Math.toRadians(b.lng - a.lng);
        double y = Math.sin(dLng) * Math.cos(lat2);
        double x = Math.cos(lat1) * Math.sin(lat2)
            - Math.sin(lat1) * Math.cos(lat2) * Math.cos(dLng);
        return normalizeBearing(Math.toDegrees(Math.atan2(y, x)));
    }

    private static double normalizeBearing(double value) {
        return ((value % 360d) + 360d) % 360d;
    }

    private static double clamp(double value, double min, double max) {
        return Math.max(min, Math.min(max, value));
    }

    static String format(Point point) {
        if (point == null) return "—";
        return String.format(Locale.US, "%.6f, %.6f", point.lat, point.lng);
    }
}
