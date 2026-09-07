package com.locwarp.mock;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.PixelFormat;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.os.IBinder;
import android.provider.Settings;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;

public final class JoystickOverlayService extends Service {
    static final String ACTION_SHOW = "com.locwarp.mock.SHOW_JOYSTICK_OVERLAY";
    static final String ACTION_HIDE = "com.locwarp.mock.HIDE_JOYSTICK_OVERLAY";

    private static final String CHANNEL_ID = "locwarp_floating_joystick";
    private static final int NOTIFICATION_ID = 2604;

    private final LocalSimulationEngine engine = LocalSimulationEngine.get();
    private WindowManager windowManager;
    private WindowManager.LayoutParams windowParams;
    private FrameLayout rootView;
    private JoystickPadView padView;

    @Override
    public void onCreate() {
        super.onCreate();
        createNotificationChannel();
        windowManager = (WindowManager) getSystemService(WINDOW_SERVICE);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? ACTION_SHOW : intent.getAction();
        if (ACTION_HIDE.equals(action)) {
            closeOverlay(true);
            return START_NOT_STICKY;
        }
        if (!Settings.canDrawOverlays(this)) {
            stopSelf();
            return START_NOT_STICKY;
        }
        startForeground(NOTIFICATION_ID, buildNotification());
        if (rootView == null) showOverlay();
        else applySizeFromSettings();
        OverlaySettings.setJoystickVisible(this, true);
        return START_NOT_STICKY;
    }

    @Override
    public void onDestroy() {
        removeOverlay();
        engine.joystickInput(0d, 0d);
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private void showOverlay() {
        if (rootView != null || windowManager == null) return;

        rootView = new FrameLayout(this);
        rootView.setClipToPadding(false);
        rootView.setBackgroundColor(Color.TRANSPARENT);

        padView = new JoystickPadView(this);
        FrameLayout.LayoutParams padLp = new FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT,
            FrameLayout.LayoutParams.MATCH_PARENT,
            Gravity.CENTER
        );
        rootView.addView(padView, padLp);

        LinearLayout controls = new LinearLayout(this);
        controls.setOrientation(LinearLayout.HORIZONTAL);
        controls.setGravity(Gravity.CENTER_VERTICAL);
        controls.setPadding(dp(3), dp(1), dp(3), dp(1));
        controls.setBackground(roundRect("#8c10131c", "#202935", 999));
        FrameLayout.LayoutParams controlsLp = new FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.WRAP_CONTENT,
            dp(50),
            Gravity.TOP | Gravity.END
        );
        controlsLp.setMargins(0, dp(2), dp(2), 0);
        rootView.addView(controls, controlsLp);

        TextView drag = new DragHandleView(this);
        drag.setText("⋮⋮");
        drag.setTextColor(Color.parseColor("#d7dcef"));
        drag.setTextSize(14);
        drag.setTypeface(Typeface.DEFAULT_BOLD);
        drag.setGravity(Gravity.CENTER);
        drag.setContentDescription("拖曳浮動搖桿");
        controls.addView(drag, new LinearLayout.LayoutParams(dp(48), dp(48)));

        Button close = toolButton("×");
        close.setTextColor(Color.parseColor("#ff9aa2"));
        close.setContentDescription("關閉浮動搖桿");
        close.setOnClickListener(v -> closeOverlay(true));
        controls.addView(close, new LinearLayout.LayoutParams(dp(48), dp(48)));

        int size = OverlaySettings.joystickSizeDp(this);
        windowParams = new WindowManager.LayoutParams(
            dp(size),
            dp(size),
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
                | WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
            PixelFormat.TRANSLUCENT
        );
        windowParams.gravity = Gravity.END | Gravity.BOTTOM;
        windowParams.x = dp(14);
        windowParams.y = dp(110);

        View.OnTouchListener dragListener = new View.OnTouchListener() {
            private float startRawX;
            private float startRawY;
            private int startX;
            private int startY;

            @Override
            public boolean onTouch(View view, MotionEvent event) {
                if (event.getActionMasked() == MotionEvent.ACTION_DOWN) {
                    startRawX = event.getRawX();
                    startRawY = event.getRawY();
                    startX = windowParams.x;
                    startY = windowParams.y;
                    return true;
                }
                if (event.getActionMasked() == MotionEvent.ACTION_MOVE) {
                    windowParams.x = startX - Math.round(event.getRawX() - startRawX);
                    windowParams.y = startY - Math.round(event.getRawY() - startRawY);
                    clampWindowPosition();
                    windowManager.updateViewLayout(rootView, windowParams);
                    return true;
                }
                if (event.getActionMasked() == MotionEvent.ACTION_UP) {
                    view.performClick();
                    return true;
                }
                return event.getActionMasked() == MotionEvent.ACTION_CANCEL;
            }
        };
        drag.setOnTouchListener(dragListener);

        windowManager.addView(rootView, windowParams);
    }

    private void applySizeFromSettings() {
        if (rootView == null || windowManager == null || windowParams == null) return;
        int size = OverlaySettings.joystickSizeDp(this);
        windowParams.width = dp(size);
        windowParams.height = dp(size);
        clampWindowPosition();
        windowManager.updateViewLayout(rootView, windowParams);
        rootView.requestLayout();
    }

    private void clampWindowPosition() {
        if (windowParams == null) return;
        android.util.DisplayMetrics metrics = getResources().getDisplayMetrics();
        int maxX = Math.max(0, metrics.widthPixels - windowParams.width);
        int maxY = Math.max(0, metrics.heightPixels - windowParams.height);
        windowParams.x = Math.max(0, Math.min(maxX, windowParams.x));
        windowParams.y = Math.max(0, Math.min(maxY, windowParams.y));
    }

    private void closeOverlay(boolean stopJoystick) {
        if (stopJoystick) {
            engine.joystickInput(0d, 0d);
            engine.stop();
        }
        removeOverlay();
        stopForeground(STOP_FOREGROUND_REMOVE);
        stopSelf();
    }

    private void removeOverlay() {
        if (rootView == null || windowManager == null) return;
        try {
            windowManager.removeView(rootView);
        } catch (Exception ignored) {
        }
        rootView = null;
        padView = null;
        OverlaySettings.setJoystickVisible(this, false);
    }

    private void createNotificationChannel() {
        NotificationManager manager =
            (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (manager == null) return;
        NotificationChannel channel = new NotificationChannel(
            CHANNEL_ID,
            "LocWarp 浮動搖桿",
            NotificationManager.IMPORTANCE_LOW
        );
        channel.setDescription("顯示跨 App 的位置控制搖桿");
        manager.createNotificationChannel(channel);
    }

    private Notification buildNotification() {
        Intent closeIntent = new Intent(this, JoystickOverlayService.class)
            .setAction(ACTION_HIDE);
        PendingIntent closePending = PendingIntent.getService(
            this,
            0,
            closeIntent,
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
        Notification.Builder builder = new Notification.Builder(this, CHANNEL_ID);
        return builder
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentTitle("LocWarp 浮動搖桿")
            .setContentText("點擊通知可關閉浮動搖桿")
            .setContentIntent(closePending)
            .setOngoing(true)
            .build();
    }

    private Button toolButton(String text) {
        Button button = new Button(this);
        button.setAllCaps(false);
        button.setText(text);
        button.setTextSize(16);
        button.setTypeface(Typeface.DEFAULT_BOLD);
        button.setTextColor(Color.WHITE);
        button.setPadding(0, 0, 0, 0);
        button.setBackground(roundRect("#66343b50", "#40566a90", 999));
        return button;
    }

    private GradientDrawable roundRect(String fill, String stroke, int radiusDp) {
        GradientDrawable drawable = new GradientDrawable();
        drawable.setColor(Color.parseColor(fill));
        drawable.setCornerRadius(dp(radiusDp));
        drawable.setStroke(dp(1), Color.parseColor(stroke));
        return drawable;
    }

    private int dp(int value) {
        return (int) (value * getResources().getDisplayMetrics().density + 0.5f);
    }

    private static final class DragHandleView extends TextView {
        DragHandleView(Context context) {
            super(context);
        }

        @Override
        public boolean performClick() {
            super.performClick();
            return true;
        }
    }

    private final class JoystickPadView extends View {
        private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
        private float handleX;
        private float handleY;
        private boolean active;

        JoystickPadView(Context context) {
            super(context);
            setBackgroundColor(Color.TRANSPARENT);
            setContentDescription("無邊框浮動方向搖桿");
        }

        @Override
        protected void onDraw(Canvas canvas) {
            super.onDraw(canvas);
            float cx = getWidth() / 2f;
            float cy = getHeight() / 2f;
            float radius = Math.min(getWidth(), getHeight()) * 0.36f;

            paint.setStyle(Paint.Style.FILL);
            paint.setColor(Color.parseColor("#d411141d"));
            canvas.drawCircle(cx, cy, radius, paint);
            paint.setStyle(Paint.Style.STROKE);
            paint.setStrokeWidth(Math.max(2f, radius * 0.035f));
            paint.setColor(Color.parseColor("#d86c8cff"));
            canvas.drawCircle(cx, cy, radius, paint);
            paint.setStrokeWidth(Math.max(1f, radius * 0.012f));
            paint.setColor(Color.parseColor("#667b88ff"));
            canvas.drawCircle(cx, cy, radius * 0.62f, paint);

            paint.setStyle(Paint.Style.STROKE);
            paint.setStrokeWidth(Math.max(1f, radius * 0.012f));
            paint.setColor(Color.parseColor("#526a728c"));
            canvas.drawLine(cx, cy - radius * 0.75f, cx, cy + radius * 0.75f, paint);
            canvas.drawLine(cx - radius * 0.75f, cy, cx + radius * 0.75f, cy, paint);

            float labelRadius = radius * 0.78f;
            drawDirection(canvas, "北", cx, cy - labelRadius);
            drawDirection(canvas, "東", cx + labelRadius, cy);
            drawDirection(canvas, "南", cx, cy + labelRadius);
            drawDirection(canvas, "西", cx - labelRadius, cy);

            float x = active ? handleX : cx;
            float y = active ? handleY : cy;
            paint.setStyle(Paint.Style.FILL);
            paint.setColor(Color.parseColor("#ee6c8cff"));
            canvas.drawCircle(x, y, radius * 0.25f, paint);
            paint.setStyle(Paint.Style.STROKE);
            paint.setStrokeWidth(Math.max(2f, radius * 0.03f));
            paint.setColor(Color.WHITE);
            canvas.drawCircle(x, y, radius * 0.25f, paint);
        }

        private void drawDirection(Canvas canvas, String text, float x, float y) {
            paint.setStyle(Paint.Style.FILL);
            paint.setTypeface(Typeface.DEFAULT_BOLD);
            paint.setTextAlign(Paint.Align.CENTER);
            paint.setTextSize(Math.max(dp(12), getWidth() * 0.075f));
            paint.setColor(Color.WHITE);
            Paint.FontMetrics metrics = paint.getFontMetrics();
            float baseline = y - (metrics.ascent + metrics.descent) / 2f;
            canvas.drawText(text, x, baseline, paint);
            paint.setTypeface(Typeface.DEFAULT);
        }

        @Override
        public boolean onTouchEvent(MotionEvent event) {
            if (event.getActionMasked() == MotionEvent.ACTION_DOWN
                || event.getActionMasked() == MotionEvent.ACTION_MOVE) {
                updateJoystick(event.getX(), event.getY());
                return true;
            }
            if (event.getActionMasked() == MotionEvent.ACTION_UP
                || event.getActionMasked() == MotionEvent.ACTION_CANCEL) {
                active = false;
                engine.joystickInput(0d, 0d);
                invalidate();
                performClick();
                return true;
            }
            return true;
        }

        @Override
        public boolean performClick() {
            super.performClick();
            return true;
        }

        private void updateJoystick(float touchX, float touchY) {
            float cx = getWidth() / 2f;
            float cy = getHeight() / 2f;
            float radius = Math.min(getWidth(), getHeight()) * 0.34f;
            float dx = touchX - cx;
            float dy = touchY - cy;
            double distance = Math.sqrt(dx * dx + dy * dy);
            double intensity = Math.min(1d, distance / radius);
            if (distance > radius && distance > 0d) {
                dx = (float) (dx * radius / distance);
                dy = (float) (dy * radius / distance);
            }
            handleX = cx + dx;
            handleY = cy + dy;
            active = true;
            double direction = (Math.toDegrees(Math.atan2(dx, -dy)) + 360d) % 360d;
            engine.joystickInput(direction, intensity < 0.08d ? 0d : intensity);
            invalidate();
        }
    }
}
