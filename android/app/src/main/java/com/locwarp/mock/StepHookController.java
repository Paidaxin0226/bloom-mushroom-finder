package com.locwarp.mock;

import android.content.Context;
import android.content.Intent;

final class StepHookController {
    static final String MODULE_PACKAGE = "com.locwarp.mock";
    static final String ACTION_COMMAND = "com.locwarp.mock.STEP_COMMAND";
    static final String ACTION_CONFIG = "com.locwarp.mock.HOOK_CONFIG";
    static final String ACTION_CLIENT_READY = "com.locwarp.mock.HOOK_CLIENT_READY";
    static final String COMMAND_PERMISSION =
        "com.locwarp.mock.permission.CONTROL_STEP_HOOK";
    static final String EXTRA_COMMAND = "COMMAND_TYPE";
    static final String EXTRA_STEPS = "STEPS";
    static final String EXTRA_TARGET_PACKAGE = "TARGET_PACKAGE";
    static final String EXTRA_LOCATION_ENABLED = "LOCATION_ENABLED";
    static final String EXTRA_STEP_ENABLED = "STEP_ENABLED";
    static final String EXTRA_WALKING_MULTIPLIER = "WALKING_MULTIPLIER";
    static final String EXTRA_MANUAL_JOB_ACTIVE = "MANUAL_JOB_ACTIVE";
    static final String COMMAND_INJECT_BATCH = "INJECT_BATCH";

    private static volatile boolean manualJobActive;

    private StepHookController() {}

    static void injectBatch(Context context, int steps) {
        if (steps <= 0) return;
        Intent intent = new Intent(ACTION_COMMAND)
            .putExtra(EXTRA_COMMAND, COMMAND_INJECT_BATCH)
            .putExtra(
                EXTRA_STEPS,
                Math.max(
                    StepInjectionPolicy.MIN_RANDOM_BATCH_STEPS,
                    Math.min(StepInjectionPolicy.MAX_RANDOM_BATCH_STEPS, steps)
                )
            );
        context.getApplicationContext().sendBroadcast(intent);
    }

    static void setManualJobActive(Context context, boolean active) {
        manualJobActive = active;
        Intent intent = new Intent(ACTION_CONFIG)
            .putExtra(EXTRA_MANUAL_JOB_ACTIVE, active);
        context.getApplicationContext().sendBroadcast(intent);
    }

    static boolean isManualJobActive() {
        return manualJobActive;
    }

    static void sendConfig(Context context, String packageName) {
        if (!HookScopeSettings.isValidPackageName(packageName)) return;
        Intent intent = new Intent(ACTION_CONFIG)
            .setPackage(packageName)
            .putExtra(
                EXTRA_LOCATION_ENABLED,
                HookScopeSettings.isLocationEnabled(context, packageName)
            )
            .putExtra(
                EXTRA_STEP_ENABLED,
                HookScopeSettings.isStepEnabled(context, packageName)
            )
            .putExtra(
                EXTRA_WALKING_MULTIPLIER,
                OverlaySettings.walkingStepMultiplier(context)
            )
            .putExtra(
                EXTRA_MANUAL_JOB_ACTIVE,
                manualJobActive
            );
        context.getApplicationContext().sendBroadcast(intent);
    }

    static void broadcastWalkingMultiplier(Context context, int multiplier) {
        Intent intent = new Intent(ACTION_CONFIG)
            .putExtra(
                EXTRA_WALKING_MULTIPLIER,
                StepInjectionPolicy.clampWalkingMultiplier(multiplier)
            );
        context.getApplicationContext().sendBroadcast(intent);
    }

    static void announceClientReady(Context context, String packageName) {
        if (!HookScopeSettings.isValidPackageName(packageName)) return;
        Intent intent = new Intent(ACTION_CLIENT_READY)
            .setClassName(MODULE_PACKAGE, HookClientReadyReceiver.class.getName())
            .putExtra(EXTRA_TARGET_PACKAGE, packageName);
        context.getApplicationContext().sendBroadcast(intent);
    }

}
