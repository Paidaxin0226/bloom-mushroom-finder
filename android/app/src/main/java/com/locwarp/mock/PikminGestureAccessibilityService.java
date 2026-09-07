package com.locwarp.mock;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.graphics.Path;
import android.os.Handler;
import android.os.Looper;
import android.util.DisplayMetrics;
import android.view.WindowManager;
import android.view.accessibility.AccessibilityEvent;

import java.lang.ref.WeakReference;

public final class PikminGestureAccessibilityService extends AccessibilityService {
    interface GestureResultCallback {
        void onResult(boolean ok, String message);
    }

    private static final Handler MAIN = new Handler(Looper.getMainLooper());
    private static volatile WeakReference<PikminGestureAccessibilityService> active =
        new WeakReference<>(null);

    static void pinchZoomOut(int repetitions, GestureResultCallback callback) {
        PikminGestureAccessibilityService service = active.get();
        if (service == null) {
            callback.onResult(false, "ADB gesture accessibility service is not connected");
            return;
        }
        int count = Math.max(1, Math.min(8, repetitions));
        MAIN.post(() -> service.dispatchPinch(count, callback));
    }

    @Override
    protected void onServiceConnected() {
        super.onServiceConnected();
        active = new WeakReference<>(this);
    }

    @Override
    public void onDestroy() {
        PikminGestureAccessibilityService current = active.get();
        if (current == this) active.clear();
        super.onDestroy();
    }

    @Override
    public void onAccessibilityEvent(AccessibilityEvent event) {
        // Gesture injection is invoked explicitly over ADB; events need no processing.
    }

    @Override
    public void onInterrupt() {
    }

    private void dispatchPinch(int remaining, GestureResultCallback callback) {
        if (remaining <= 0) {
            callback.onResult(true, "Map pinch zoom-out completed");
            return;
        }
        DisplayMetrics metrics = new DisplayMetrics();
        WindowManager manager = getSystemService(WindowManager.class);
        if (manager == null) {
            callback.onResult(false, "Android window manager is unavailable");
            return;
        }
        manager.getDefaultDisplay().getRealMetrics(metrics);
        float width = metrics.widthPixels;
        float height = metrics.heightPixels;
        float centerY = height * 0.46f;

        // Pinching inward zooms the Pikmin 2D map out. Both paths start together.
        Path left = new Path();
        left.moveTo(width * 0.18f, centerY);
        left.lineTo(width * 0.44f, centerY);
        Path right = new Path();
        right.moveTo(width * 0.82f, centerY);
        right.lineTo(width * 0.56f, centerY);

        GestureDescription gesture = new GestureDescription.Builder()
            .addStroke(new GestureDescription.StrokeDescription(left, 0, 420))
            .addStroke(new GestureDescription.StrokeDescription(right, 0, 420))
            .build();
        boolean accepted = dispatchGesture(
            gesture,
            new GestureResultCallbackAdapter(remaining, callback),
            MAIN
        );
        if (!accepted) callback.onResult(false, "Android rejected the multi-touch gesture");
    }

    private final class GestureResultCallbackAdapter
        extends AccessibilityService.GestureResultCallback {
        private final int remaining;
        private final GestureResultCallback callback;

        GestureResultCallbackAdapter(int remaining, GestureResultCallback callback) {
            this.remaining = remaining;
            this.callback = callback;
        }

        @Override
        public void onCompleted(GestureDescription gestureDescription) {
            super.onCompleted(gestureDescription);
            MAIN.postDelayed(() -> dispatchPinch(remaining - 1, callback), 140);
        }

        @Override
        public void onCancelled(GestureDescription gestureDescription) {
            super.onCancelled(gestureDescription);
            callback.onResult(false, "Android cancelled the multi-touch gesture");
        }
    }
}
