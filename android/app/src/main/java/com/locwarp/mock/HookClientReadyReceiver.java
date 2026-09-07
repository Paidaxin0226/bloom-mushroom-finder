package com.locwarp.mock;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

public final class HookClientReadyReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        if (intent == null
            || !StepHookController.ACTION_CLIENT_READY.equals(intent.getAction())) {
            return;
        }
        String packageName = intent.getStringExtra(
            StepHookController.EXTRA_TARGET_PACKAGE
        );
        if (!HookScopeSettings.isValidPackageName(packageName)
            || context.getPackageName().equals(packageName)) {
            return;
        }
        try {
            context.getPackageManager().getApplicationInfo(packageName, 0);
        } catch (Exception notInstalled) {
            return;
        }
        HookScopeSettings.rememberObservedPackage(context, packageName);
        StepHookController.sendConfig(context, packageName);
    }
}
