package com.locwarp.mock;

import android.app.Activity;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

public final class GestureCommandReceiver extends BroadcastReceiver {
    static final String ACTION_PINCH_ZOOM_OUT = "com.locwarp.mock.PINCH_ZOOM_OUT";

    @Override
    public void onReceive(Context context, Intent intent) {
        if (intent == null || !ACTION_PINCH_ZOOM_OUT.equals(intent.getAction())) {
            setResultCode(Activity.RESULT_CANCELED);
            setResultData("Unsupported gesture command");
            return;
        }
        int repetitions = Math.max(1, Math.min(8, intent.getIntExtra("repetitions", 8)));
        PendingResult pending = goAsync();
        PikminGestureAccessibilityService.pinchZoomOut(repetitions, (ok, message) -> {
            pending.setResultCode(ok ? Activity.RESULT_OK : Activity.RESULT_CANCELED);
            pending.setResultData(message);
            pending.finish();
        });
    }
}
