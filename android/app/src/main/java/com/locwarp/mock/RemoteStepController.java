package com.locwarp.mock;

import android.content.Context;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;

import org.json.JSONObject;

import java.util.Random;

final class RemoteStepController implements LocalSimulationEngine.Listener {
    private static final double WALKING_SPEED_LIMIT_KMH = 15d;
    private static volatile boolean remoteJobActive;
    private static volatile boolean remoteFollowControlling;

    private final Context context;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final Random random = new Random();
    private final LocalSimulationEngine engine = LocalSimulationEngine.get();
    private final Runnable emitter = this::emitNextBatch;

    private String mode = "none";
    private String state = "idle";
    private int targetSteps;
    private int injectedSteps;
    private long startedElapsedMs;
    private long accumulatedPausedMs;
    private long pauseStartedElapsedMs;
    private long finishedElapsedMs;
    private boolean remoteFollowEnabled;
    private boolean followEligible;
    private double followSpeedKmh;

    RemoteStepController(Context context) {
        this.context = context.getApplicationContext();
        engine.attach(this.context, this);
    }

    static boolean isRemoteJobActive() {
        return remoteJobActive;
    }

    static boolean isRemoteFollowControlling() {
        return remoteFollowControlling;
    }

    synchronized boolean start(int steps) {
        if (!canStartManualJob()) return false;
        OverlaySettings.setDefitStepCount(
            context,
            RemoteCommandLimits.requireStepCount(steps)
        );
        beginJob("single", steps);
        return true;
    }

    synchronized boolean startLoop() {
        if (!canStartManualJob()) return false;
        beginJob("loop", 0);
        return true;
    }

    synchronized boolean setFollowEnabled(boolean enabled) {
        if (enabled
            && (isActive() || StepHookController.isManualJobActive())) {
            return false;
        }
        remoteFollowEnabled = enabled;
        remoteFollowControlling = enabled;
        OverlaySettings.setDefitWalkingStepsEnabled(context, enabled);
        if (!enabled) {
            if ("follow".equals(mode) && isActive()) {
                finishActiveJob("stopped");
            } else {
                state = "idle";
                mode = "follow";
            }
            followEligible = false;
            return true;
        }
        mode = "follow";
        state = "waiting";
        targetSteps = 0;
        injectedSteps = 0;
        finishedElapsedMs = 0L;
        reconcileFollow(engine.snapshot());
        return true;
    }

    synchronized boolean togglePause() {
        if (!isManualActive()) return false;
        long now = SystemClock.elapsedRealtime();
        if ("paused".equals(state)) {
            accumulatedPausedMs += Math.max(0L, now - pauseStartedElapsedMs);
            pauseStartedElapsedMs = 0L;
            state = "running";
            scheduleNextBatch();
        } else {
            state = "paused";
            pauseStartedElapsedMs = now;
            mainHandler.removeCallbacks(emitter);
        }
        return true;
    }

    synchronized boolean stop() {
        if ("follow".equals(mode) && remoteFollowEnabled) {
            remoteFollowEnabled = false;
            remoteFollowControlling = false;
            OverlaySettings.setDefitWalkingStepsEnabled(context, false);
            if (isActive()) finishActiveJob("stopped");
            else state = "stopped";
            followEligible = false;
            return true;
        }
        if (!isManualActive()) return false;
        finishActiveJob("stopped");
        return true;
    }

    synchronized JSONObject status() {
        reconcileFollow(engine.snapshot());
        JSONObject out = new JSONObject();
        try {
            out.put("mode", mode);
            out.put("state", state);
            out.put("running", isActive());
            out.put("paused", "paused".equals(state));
            out.put("target", targetSteps);
            out.put("injected", injectedSteps);
            out.put("elapsedMs", elapsedMs());
            out.put("followEnabled", remoteFollowEnabled);
            out.put("followEligible", followEligible);
            out.put("speedKmh", followSpeedKmh);
            out.put("movementRunning", engine.snapshot().running);
            out.put(
                "anotherJobActive",
                !remoteJobActive && StepHookController.isManualJobActive()
            );
        } catch (Exception ignored) {
        }
        return out;
    }

    @Override
    public void onSimulationUpdate(LocalSimulationEngine.Snapshot snapshot) {
        synchronized (this) {
            reconcileFollow(snapshot);
        }
    }

    private boolean canStartManualJob() {
        if (isActive() || StepHookController.isManualJobActive()) return false;
        remoteFollowEnabled = false;
        remoteFollowControlling = false;
        OverlaySettings.setDefitWalkingStepsEnabled(context, false);
        return true;
    }

    private void beginJob(String nextMode, int steps) {
        mode = nextMode;
        state = "running";
        targetSteps = Math.max(0, steps);
        injectedSteps = 0;
        startedElapsedMs = SystemClock.elapsedRealtime();
        accumulatedPausedMs = 0L;
        pauseStartedElapsedMs = 0L;
        finishedElapsedMs = 0L;
        setRemoteJobActive(true);
        scheduleNextBatch();
    }

    private void reconcileFollow(LocalSimulationEngine.Snapshot snapshot) {
        if (remoteFollowEnabled
            && !OverlaySettings.isDefitWalkingStepsEnabled(context)) {
            remoteFollowEnabled = false;
            remoteFollowControlling = false;
        }
        followSpeedKmh = snapshot == null ? 0d : snapshot.speedKmh;
        followEligible = remoteFollowEnabled
            && snapshot != null
            && snapshot.running
            && !snapshot.paused
            && snapshot.speedKmh <= WALKING_SPEED_LIMIT_KMH;

        if (!remoteFollowEnabled) {
            if ("follow".equals(mode) && isActive()) {
                finishActiveJob("idle");
            }
            return;
        }
        if (!"follow".equals(mode) && isActive()) return;
        if (!followEligible) {
            if ("follow".equals(mode) && isActive()) {
                finishActiveJob("waiting");
            } else {
                mode = "follow";
                state = "waiting";
            }
            return;
        }
        if (isActive()) return;
        if (StepHookController.isManualJobActive()) {
            mode = "follow";
            state = "waiting";
            return;
        }
        beginJob("follow", 0);
    }

    private synchronized void emitNextBatch() {
        if (!"running".equals(state)) return;
        if ("follow".equals(mode)) {
            LocalSimulationEngine.Snapshot snapshot = engine.snapshot();
            boolean eligible = remoteFollowEnabled
                && snapshot != null
                && snapshot.running
                && !snapshot.paused
                && snapshot.speedKmh <= WALKING_SPEED_LIMIT_KMH;
            if (!eligible) {
                reconcileFollow(snapshot);
                return;
            }
        }
        int batch = StepInjectionPolicy.randomBatchSteps(random);
        if ("single".equals(mode)) {
            int remaining = Math.max(0, targetSteps - injectedSteps);
            batch = Math.min(batch, remaining);
            if (batch <= 0) {
                completeSingle();
                return;
            }
        }
        StepHookController.injectBatch(context, batch);
        injectedSteps = saturatedAdd(injectedSteps, batch);
        if ("single".equals(mode) && injectedSteps >= targetSteps) {
            completeSingle();
            return;
        }
        scheduleNextBatch();
    }

    private void scheduleNextBatch() {
        mainHandler.removeCallbacks(emitter);
        if (!"running".equals(state)) return;
        mainHandler.postDelayed(
            emitter,
            StepInjectionPolicy.randomStepIntervalMillis(random)
        );
    }

    private void completeSingle() {
        finishActiveJob("completed");
        OperationHistoryStore.record(
            context,
            "step",
            "遠端指定步數完成：" + injectedSteps + " 步",
            true
        );
    }

    private void finishActiveJob(String nextState) {
        finishedElapsedMs = elapsedMs();
        state = nextState;
        mainHandler.removeCallbacks(emitter);
        setRemoteJobActive(false);
    }

    private void setRemoteJobActive(boolean active) {
        remoteJobActive = active;
        StepHookController.setManualJobActive(context, active);
    }

    private boolean isActive() {
        return "running".equals(state) || "paused".equals(state);
    }

    private boolean isManualActive() {
        return isActive()
            && ("single".equals(mode) || "loop".equals(mode));
    }

    private long elapsedMs() {
        if (startedElapsedMs <= 0L) return 0L;
        if (!isActive()) return Math.max(0L, finishedElapsedMs);
        long now = SystemClock.elapsedRealtime();
        long currentPause = "paused".equals(state)
            ? Math.max(0L, now - pauseStartedElapsedMs)
            : 0L;
        return Math.max(
            0L,
            now - startedElapsedMs - accumulatedPausedMs - currentPause
        );
    }

    private int saturatedAdd(int current, int delta) {
        return (int) Math.min(Integer.MAX_VALUE, (long) current + delta);
    }
}
