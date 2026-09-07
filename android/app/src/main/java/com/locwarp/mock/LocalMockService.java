package com.locwarp.mock;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.os.IBinder;

import java.util.Locale;

public final class LocalMockService extends Service
    implements LocalSimulationEngine.Listener {
    static final String ACTION_START = "com.locwarp.mock.LOCAL_MOCK_START";
    static final String ACTION_CLEAR = "com.locwarp.mock.LOCAL_MOCK_CLEAR";
    static final String ACTION_TOGGLE_PAUSE =
        "com.locwarp.mock.LOCAL_MOCK_TOGGLE_PAUSE";
    static final String ACTION_SPEED_DOWN =
        "com.locwarp.mock.LOCAL_MOCK_SPEED_DOWN";
    static final String ACTION_SPEED_UP =
        "com.locwarp.mock.LOCAL_MOCK_SPEED_UP";
    static final String ACTION_RESTORE =
        "com.locwarp.mock.LOCAL_MOCK_RESTORE";

    private static final String CHANNEL_ID = "locwarp_local_mock";
    private static final int NOTIFICATION_ID = 2601;
    private static final double SPEED_STEP_KMH = 1.0d;

    private final LocalSimulationEngine engine = LocalSimulationEngine.get();

    @Override
    public void onCreate() {
        super.onCreate();
        ensureChannel();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent != null ? intent.getAction() : ACTION_START;
        if (ACTION_CLEAR.equals(action)) {
            MockLocationKeeper.clearLocal(this);
            stopForegroundCompat();
            stopSelf();
            return START_NOT_STICKY;
        }

        if (MockLocationKeeper.restoreIfEnabled(this)) {
            engine.attach(this, this);
            if (ACTION_RESTORE.equals(action)) {
                engine.restore();
                stopForegroundCompat();
                stopSelf();
                return START_NOT_STICKY;
            }
            if (ACTION_TOGGLE_PAUSE.equals(action)) {
                LocalSimulationEngine.Snapshot snapshot = engine.snapshot();
                if (snapshot.running) {
                    if (snapshot.paused) engine.resume();
                    else engine.pause();
                }
            } else if (ACTION_SPEED_DOWN.equals(action)) {
                adjustSpeed(-SPEED_STEP_KMH);
            } else if (ACTION_SPEED_UP.equals(action)) {
                adjustSpeed(SPEED_STEP_KMH);
            }
            startForeground(NOTIFICATION_ID, buildNotification(engine.snapshot()));
            return START_STICKY;
        }

        stopForegroundCompat();
        stopSelf();
        return START_NOT_STICKY;
    }

    @Override
    public void onTaskRemoved(Intent rootIntent) {
        if (!MockLocationKeeper.isActive(this)) {
            stopForegroundCompat();
            stopSelf();
        }
        super.onTaskRemoved(rootIntent);
    }

    @Override
    public void onTimeout(int startId, int fgsType) {
        MockLocationKeeper.clearLocal(this);
        stopForegroundCompat();
        stopSelf(startId);
    }

    @Override
    public void onDestroy() {
        engine.detach(this);
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onSimulationUpdate(LocalSimulationEngine.Snapshot snapshot) {
        if (!MockLocationKeeper.isActive(this)) return;
        NotificationManager manager = getSystemService(NotificationManager.class);
        if (manager != null) manager.notify(NOTIFICATION_ID, buildNotification(snapshot));
    }

    private void ensureChannel() {
        NotificationChannel channel = new NotificationChannel(
            CHANNEL_ID,
            "LocWarp local mock",
            NotificationManager.IMPORTANCE_LOW
        );
        channel.setDescription("Keeps local mock location active.");
        NotificationManager manager = getSystemService(NotificationManager.class);
        if (manager != null) manager.createNotificationChannel(channel);
    }

    private Notification buildNotification(LocalSimulationEngine.Snapshot snapshot) {
        int flags = PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT;
        PendingIntent open = PendingIntent.getActivity(
            this,
            2602,
            new Intent(this, MainActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP),
            flags
        );
        PendingIntent togglePause = serviceAction(ACTION_TOGGLE_PAUSE, 2604, flags);
        PendingIntent speedDown = serviceAction(ACTION_SPEED_DOWN, 2605, flags);
        PendingIntent speedUp = serviceAction(ACTION_SPEED_UP, 2606, flags);
        PendingIntent restore = serviceAction(ACTION_RESTORE, 2607, flags);

        String state;
        if (snapshot == null || !snapshot.running) {
            state = "位置固定中";
        } else if (snapshot.paused) {
            state = "已暫停";
        } else {
            state = stateLabel(snapshot.state);
        }
        double speed = snapshot == null
            ? OverlaySettings.speedKmh(this)
            : snapshot.speedKmh;
        String detail = String.format(
            Locale.TAIWAN,
            "%s · %.1f km/h",
            state,
            speed
        );

        Notification.Builder builder = new Notification.Builder(this, CHANNEL_ID);
        builder
            .setSmallIcon(android.R.drawable.ic_menu_mylocation)
            .setContentTitle("LocWarp 位置模擬執行中")
            .setContentText(detail)
            .setContentIntent(open)
            .setOnlyAlertOnce(true)
            .setOngoing(true)
            .addAction(
                android.R.drawable.ic_media_pause,
                snapshot != null && snapshot.paused ? "繼續" : "暫停",
                togglePause
            )
            .addAction(android.R.drawable.ic_media_rew, "減速", speedDown)
            .addAction(android.R.drawable.ic_media_ff, "加速", speedUp)
            .addAction(
                android.R.drawable.ic_menu_close_clear_cancel,
                "停止並還原",
                restore
            );
        return builder.build();
    }

    private PendingIntent serviceAction(String action, int requestCode, int flags) {
        return PendingIntent.getService(
            this,
            requestCode,
            new Intent(this, LocalMockService.class).setAction(action),
            flags
        );
    }

    private void adjustSpeed(double deltaKmh) {
        LocalSimulationEngine.Snapshot snapshot = engine.snapshot();
        double next = Math.max(0.1d, snapshot.speedKmh + deltaKmh);
        engine.configureSpeed(next, null, null);
        OverlaySettings.setSpeedKmh(this, next);
    }

    private String stateLabel(String state) {
        if ("navigating".equals(state) || "path_navigation".equals(state)) {
            return "導航中";
        }
        if ("looping".equals(state)) return "循環路線中";
        if ("multi_stop".equals(state)) return "多站移動中";
        if ("random_walk".equals(state)) return "隨機漫步中";
        if ("joystick".equals(state)) return "搖桿控制中";
        if ("flower_planting".equals(state)) return "種花路線中";
        return "移動中";
    }

    private void stopForegroundCompat() {
        stopForeground(STOP_FOREGROUND_REMOVE);
    }
}
