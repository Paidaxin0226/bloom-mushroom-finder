package com.locwarp.mock;

import android.annotation.SuppressLint;
import android.app.AndroidAppHelper;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.hardware.Sensor;
import android.hardware.SensorEvent;
import android.hardware.SensorEventListener;
import android.os.Build;
import android.os.Handler;
import android.os.SystemClock;
import android.util.Log;

import java.util.Timer;
import java.util.TimerTask;
import java.util.Random;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.atomic.AtomicInteger;

import de.robv.android.xposed.IXposedHookLoadPackage;
import de.robv.android.xposed.XC_MethodHook;
import de.robv.android.xposed.XposedHelpers;
import de.robv.android.xposed.callbacks.XC_LoadPackage;

public final class LocWarpXposedHook implements IXposedHookLoadPackage {
    private static final String TAG = "LocWarpXposed";
    private static final int MAX_COMMAND_STEPS = 200_000;

    private final CopyOnWriteArrayList<ListenerRegistration> activeListeners =
        new CopyOnWriteArrayList<>();
    private final ConcurrentHashMap<SensorEventListener, SensorEventListener>
        listenerProxyMap = new ConcurrentHashMap<>();
    private final AtomicInteger stepOffset = new AtomicInteger();
    private final AtomicInteger pendingMultiplierSteps = new AtomicInteger();
    private final Object receiverLock = new Object();
    private final Object timerLock = new Object();
    private final Object multiplierLock = new Object();
    private final Object emissionLock = new Object();
    private final Random random = new Random();

    private volatile String targetPackageName;
    private volatile float lastKnownRealSteps = -1f;
    private volatile float lastMultiplierRealSteps = -1f;
    private volatile long lastDetectorTimestamp = Long.MIN_VALUE;
    private volatile boolean receiverRegistered;
    private volatile boolean clientAnnounced;
    private volatile boolean locationHookEnabled = true;
    private volatile boolean stepHookEnabled = true;
    private volatile int walkingMultiplier = 1;
    private volatile boolean manualJobActive;
    private BroadcastReceiver commandReceiver;
    private Timer emitterTimer;

    private static final class ListenerRegistration {
        final SensorEventListener listener;
        final Sensor sensor;

        ListenerRegistration(SensorEventListener listener, Sensor sensor) {
            this.listener = listener;
            this.sensor = sensor;
        }
    }

    @Override
    public void handleLoadPackage(XC_LoadPackage.LoadPackageParam loadPackageParam) {
        if (StepHookController.MODULE_PACKAGE.equals(loadPackageParam.packageName)) {
            return;
        }
        targetPackageName = loadPackageParam.packageName;
        hookApplicationAttach(loadPackageParam);
        hookStepSensors(loadPackageParam);
        hookMockLocationFlags(loadPackageParam);
    }

    private void hookApplicationAttach(
        XC_LoadPackage.LoadPackageParam loadPackageParam
    ) {
        try {
            XposedHelpers.findAndHookMethod(
                "android.app.Application",
                loadPackageParam.classLoader,
                "attach",
                Context.class,
                new XC_MethodHook() {
                    @Override
                    protected void afterHookedMethod(MethodHookParam param) {
                        Context context = (Context) param.args[0];
                        registerBroadcastReceiverOnce(
                            context,
                            loadPackageParam.packageName
                        );
                    }
                }
            );
        } catch (Throwable error) {
            Log.e(TAG, "Unable to install application attach hook", error);
        }
    }

    private void hookStepSensors(XC_LoadPackage.LoadPackageParam loadPackageParam) {
        try {
            Class<?> sensorManagerClass = XposedHelpers.findClass(
                "android.hardware.SystemSensorManager",
                loadPackageParam.classLoader
            );

            XposedHelpers.findAndHookMethod(
                sensorManagerClass,
                "registerListenerImpl",
                SensorEventListener.class,
                Sensor.class,
                Integer.TYPE,
                Handler.class,
                Integer.TYPE,
                Integer.TYPE,
                new XC_MethodHook() {
                    @Override
                    protected void beforeHookedMethod(MethodHookParam param) {
                        SensorEventListener original =
                            (SensorEventListener) param.args[0];
                        Sensor sensor = (Sensor) param.args[1];
                        if (original == null || !isStepSensor(sensor)) return;

                        SensorEventListener proxy = listenerProxyMap.get(original);
                        if (proxy == null) {
                            SensorEventListener candidate = createProxy(original);
                            SensorEventListener existing =
                                listenerProxyMap.putIfAbsent(original, candidate);
                            proxy = existing == null ? candidate : existing;
                        }
                        param.args[0] = proxy;

                        int sensorType = sensor.getType();
                        activeListeners.removeIf(item ->
                            item.listener == original
                                && item.sensor.getType() == sensorType
                        );
                        activeListeners.add(
                            new ListenerRegistration(original, sensor)
                        );
                        registerBroadcastReceiverOnce();
                    }
                }
            );

            XposedHelpers.findAndHookMethod(
                sensorManagerClass,
                "unregisterListenerImpl",
                SensorEventListener.class,
                Sensor.class,
                new XC_MethodHook() {
                    @Override
                    protected void beforeHookedMethod(MethodHookParam param) {
                        SensorEventListener original =
                            (SensorEventListener) param.args[0];
                        Sensor sensor = (Sensor) param.args[1];
                        if (original == null) return;
                        if (sensor != null && !isStepSensor(sensor)) return;

                        SensorEventListener proxy = listenerProxyMap.get(original);
                        if (proxy == null) return;
                        param.args[0] = proxy;

                        if (sensor == null) {
                            activeListeners.removeIf(item -> item.listener == original);
                            listenerProxyMap.remove(original, proxy);
                            return;
                        }

                        int sensorType = sensor.getType();
                        activeListeners.removeIf(item ->
                            item.listener == original
                                && item.sensor.getType() == sensorType
                        );
                        boolean stillActive = false;
                        for (ListenerRegistration item : activeListeners) {
                            if (item.listener == original) {
                                stillActive = true;
                                break;
                            }
                        }
                        if (!stillActive) listenerProxyMap.remove(original, proxy);
                    }
                }
            );
        } catch (Throwable error) {
            Log.e(TAG, "Unable to install step sensor hooks", error);
        }
    }

    private SensorEventListener createProxy(SensorEventListener original) {
        return new SensorEventListener() {
            @Override
            public void onSensorChanged(SensorEvent event) {
                if (event == null || event.sensor == null || event.values.length == 0) {
                    return;
                }
                int sensorType = event.sensor.getType();
                if (sensorType == Sensor.TYPE_STEP_COUNTER) {
                    float realSteps = event.values[0];
                    handleRealCounterEvent(realSteps);
                    if (stepHookEnabled) {
                        event.values[0] = realSteps + stepOffset.get();
                    }
                } else if (sensorType == Sensor.TYPE_STEP_DETECTOR) {
                    handleRealDetectorEvent(event.timestamp);
                }
                original.onSensorChanged(event);
                registerBroadcastReceiverOnce();
            }

            @Override
            public void onAccuracyChanged(Sensor sensor, int accuracy) {
                original.onAccuracyChanged(sensor, accuracy);
            }
        };
    }

    private void handleRealCounterEvent(float realSteps) {
        lastKnownRealSteps = realSteps;
        int realDelta = 0;
        synchronized (multiplierLock) {
            float previous = lastMultiplierRealSteps;
            lastMultiplierRealSteps = realSteps;
            if (stepHookEnabled
                && walkingMultiplier > 1
                && previous >= 0f
                && realSteps > previous) {
                realDelta = Math.max(0, (int) Math.floor(realSteps - previous));
            }
        }
        enqueueMultiplierStepsAfterRealEvent(
            StepInjectionPolicy.extraStepsForRealDelta(
                realDelta,
                walkingMultiplier
            )
        );
    }

    private void handleRealDetectorEvent(long timestamp) {
        if (!stepHookEnabled
            || walkingMultiplier <= 1
            || hasActiveCounterListener()) {
            return;
        }
        synchronized (multiplierLock) {
            if (timestamp == lastDetectorTimestamp) return;
            lastDetectorTimestamp = timestamp;
        }
        enqueueMultiplierStepsAfterRealEvent(walkingMultiplier - 1);
    }

    private boolean hasActiveCounterListener() {
        for (ListenerRegistration item : activeListeners) {
            if (item.sensor.getType() == Sensor.TYPE_STEP_COUNTER) return true;
        }
        return false;
    }

    private void hookMockLocationFlags(
        XC_LoadPackage.LoadPackageParam loadPackageParam
    ) {
        try {
            Class<?> locationClass = XposedHelpers.findClass(
                "android.location.Location",
                loadPackageParam.classLoader
            );
            hookBooleanFalse(locationClass, "isFromMockProvider");
            try {
                hookBooleanFalse(locationClass, "isMock");
            } catch (Throwable unavailableOnOldAndroid) {
                Log.d(TAG, "Location.isMock is unavailable on this Android version");
            }
        } catch (Throwable error) {
            Log.e(TAG, "Unable to install mock-location hooks", error);
        }
    }

    private void hookBooleanFalse(Class<?> targetClass, String methodName) {
        XposedHelpers.findAndHookMethod(
            targetClass,
            methodName,
            new XC_MethodHook() {
                @Override
                protected void afterHookedMethod(MethodHookParam param) {
                    if (locationHookEnabled) param.setResult(false);
                }
            }
        );
    }

    private void registerBroadcastReceiverOnce() {
        Context context = AndroidAppHelper.currentApplication();
        if (context != null) {
            registerBroadcastReceiverOnce(context, targetPackageName);
        }
    }

    @SuppressLint("UnspecifiedRegisterReceiverFlag")
    private void registerBroadcastReceiverOnce(
        Context context,
        String packageName
    ) {
        if (context == null) return;
        if (!receiverRegistered) {
            synchronized (receiverLock) {
                if (!receiverRegistered) {
                    BroadcastReceiver receiver = new BroadcastReceiver() {
                        @Override
                        public void onReceive(
                            Context receiverContext,
                            Intent intent
                        ) {
                            handleCommand(intent);
                        }
                    };
                    IntentFilter filter = new IntentFilter();
                    filter.addAction(StepHookController.ACTION_COMMAND);
                    filter.addAction(StepHookController.ACTION_CONFIG);
                    Context appContext = context.getApplicationContext();
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                        appContext.registerReceiver(
                            receiver,
                            filter,
                            StepHookController.COMMAND_PERMISSION,
                            null,
                            Context.RECEIVER_EXPORTED
                        );
                    } else {
                        appContext.registerReceiver(
                            receiver,
                            filter,
                            StepHookController.COMMAND_PERMISSION,
                            null
                        );
                    }
                    commandReceiver = receiver;
                    receiverRegistered = true;
                }
            }
        }
        if (!clientAnnounced && HookScopeSettings.isValidPackageName(packageName)) {
            synchronized (receiverLock) {
                if (!clientAnnounced) {
                    clientAnnounced = true;
                    StepHookController.announceClientReady(context, packageName);
                }
            }
        }
    }

    private void handleCommand(Intent intent) {
        if (intent == null) return;
        if (StepHookController.ACTION_CONFIG.equals(intent.getAction())) {
            applyConfig(intent);
            return;
        }
        if (!StepHookController.ACTION_COMMAND.equals(intent.getAction())
            || !stepHookEnabled) {
            return;
        }
        String command = intent.getStringExtra(StepHookController.EXTRA_COMMAND);
        int steps = boundedSteps(
            intent.getIntExtra(StepHookController.EXTRA_STEPS, 0)
        );
        if (StepHookController.COMMAND_INJECT_BATCH.equals(command)) {
            injectBatch(steps);
        }
    }

    private void applyConfig(Intent intent) {
        if (intent.hasExtra(StepHookController.EXTRA_LOCATION_ENABLED)) {
            locationHookEnabled = intent.getBooleanExtra(
                StepHookController.EXTRA_LOCATION_ENABLED,
                true
            );
        }
        if (intent.hasExtra(StepHookController.EXTRA_WALKING_MULTIPLIER)) {
            walkingMultiplier = StepInjectionPolicy.clampWalkingMultiplier(
                intent.getIntExtra(
                    StepHookController.EXTRA_WALKING_MULTIPLIER,
                    1
                )
            );
            synchronized (multiplierLock) {
                lastMultiplierRealSteps = lastKnownRealSteps;
                lastDetectorTimestamp = Long.MIN_VALUE;
            }
        }
        if (intent.hasExtra(StepHookController.EXTRA_STEP_ENABLED)) {
            boolean enabled = intent.getBooleanExtra(
                StepHookController.EXTRA_STEP_ENABLED,
                true
            );
            stepHookEnabled = enabled;
            synchronized (multiplierLock) {
                lastMultiplierRealSteps = lastKnownRealSteps;
                lastDetectorTimestamp = Long.MIN_VALUE;
            }
            if (!enabled) {
                pendingMultiplierSteps.set(0);
            }
        }
        if (intent.hasExtra(StepHookController.EXTRA_MANUAL_JOB_ACTIVE)) {
            manualJobActive = intent.getBooleanExtra(
                StepHookController.EXTRA_MANUAL_JOB_ACTIVE,
                false
            );
            if (manualJobActive) pendingMultiplierSteps.set(0);
        }
    }

    private void injectBatch(int steps) {
        int bounded = boundedSteps(steps);
        if (bounded <= 0 || !stepHookEnabled) return;
        synchronized (emissionLock) {
            incrementStepOffset(bounded);
        }
        dispatchSensorEvents(bounded);
    }

    private void enqueueMultiplierStepsAfterRealEvent(int steps) {
        int bounded = boundedSteps(steps);
        if (bounded <= 0 || !stepHookEnabled || manualJobActive) return;
        while (true) {
            int current = pendingMultiplierSteps.get();
            int next = (int) Math.min(
                MAX_COMMAND_STEPS,
                (long) current + bounded
            );
            if (pendingMultiplierSteps.compareAndSet(current, next)) break;
        }
        ensureEmitterRunning();
    }

    private void ensureEmitterRunning() {
        synchronized (timerLock) {
            if (emitterTimer != null) return;
            Timer timer = new Timer("LocWarp-step-emitter", true);
            emitterTimer = timer;
            scheduleNextEmitterLocked(timer);
        }
    }

    private void scheduleNextEmitterLocked(Timer timer) {
        if (timer != emitterTimer) return;
        boolean multiplierPending = !manualJobActive
            && pendingMultiplierSteps.get() > 0;
        if (!multiplierPending) {
            timer.cancel();
            emitterTimer = null;
            return;
        }
        timer.schedule(new TimerTask() {
            @Override
            public void run() {
                emitRandomSyntheticBatch(timer);
                synchronized (timerLock) {
                    scheduleNextEmitterLocked(timer);
                }
            }
        }, StepInjectionPolicy.randomStepIntervalMillis(random));
    }

    private void emitRandomSyntheticBatch(Timer timer) {
        if (timer != emitterTimer || !stepHookEnabled || manualJobActive) {
            return;
        }
        int emitted;
        synchronized (emissionLock) {
            emitted = takeRandomBatch(pendingMultiplierSteps);
            if (emitted <= 0) return;
            incrementStepOffset(emitted);
        }
        dispatchSensorEvents(emitted);
    }

    private int takeRandomBatch(AtomicInteger queue) {
        int requested = StepInjectionPolicy.randomBatchSteps(random);
        while (true) {
            int current = queue.get();
            if (current <= 0) return 0;
            int emitted = Math.min(current, requested);
            if (queue.compareAndSet(current, current - emitted)) return emitted;
        }
    }

    private void incrementStepOffset(int steps) {
        if (steps <= 0) return;
        while (true) {
            int current = stepOffset.get();
            if (current == Integer.MAX_VALUE) return;
            int next = saturatedAdd(current, steps);
            if (stepOffset.compareAndSet(current, next)) return;
        }
    }

    private void dispatchSensorEvents(int emittedSteps) {
        if (!stepHookEnabled) return;
        for (ListenerRegistration item : activeListeners) {
            try {
                if (item.sensor.getType() == Sensor.TYPE_STEP_COUNTER) {
                    if (lastKnownRealSteps < 0f) continue;
                    dispatchSensorEvent(
                        item.listener,
                        item.sensor,
                        lastKnownRealSteps + stepOffset.get()
                    );
                } else if (item.sensor.getType() == Sensor.TYPE_STEP_DETECTOR) {
                    for (int index = 0; index < emittedSteps; index++) {
                        dispatchSensorEvent(item.listener, item.sensor, 1f);
                    }
                }
            } catch (Throwable error) {
                Log.e(TAG, "Unable to dispatch synthetic step event", error);
            }
        }
    }

    private static int saturatedAdd(int current, int delta) {
        return (int) Math.min(Integer.MAX_VALUE, (long) current + delta);
    }

    private void dispatchSensorEvent(
        SensorEventListener listener,
        Sensor sensor,
        float value
    ) {
        SensorEvent event = (SensorEvent) XposedHelpers.newInstance(
            SensorEvent.class,
            1
        );
        XposedHelpers.setObjectField(event, "sensor", sensor);
        XposedHelpers.setLongField(
            event,
            "timestamp",
            SystemClock.elapsedRealtimeNanos()
        );
        XposedHelpers.setIntField(event, "accuracy", 3);
        float[] values = (float[]) XposedHelpers.getObjectField(event, "values");
        values[0] = value;
        listener.onSensorChanged(event);
    }

    private static boolean isStepSensor(Sensor sensor) {
        if (sensor == null) return false;
        return sensor.getType() == Sensor.TYPE_STEP_COUNTER
            || sensor.getType() == Sensor.TYPE_STEP_DETECTOR;
    }

    private static int boundedSteps(int steps) {
        return Math.max(0, Math.min(MAX_COMMAND_STEPS, steps));
    }
}
