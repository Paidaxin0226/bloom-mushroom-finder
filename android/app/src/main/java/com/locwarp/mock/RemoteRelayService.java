package com.locwarp.mock;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.os.IBinder;

public final class RemoteRelayService extends Service {
    static final String ACTION_START = "com.locwarp.mock.RELAY_START";
    static final String ACTION_STOP = "com.locwarp.mock.RELAY_STOP";
    static final String EXTRA_BASE_URL = "base_url";

    private static final String CHANNEL_ID = "locwarp_remote_relay";
    private static final int NOTIFICATION_ID = 2608;

    private RemoteRelayClient client;

    @Override
    public void onCreate() {
        super.onCreate();
        createNotificationChannel();
        client = RemoteRelayClient.get(this);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? ACTION_START : intent.getAction();
        if (ACTION_STOP.equals(action)) {
            client.stop();
            stopForegroundCompat();
            stopSelf();
            return START_NOT_STICKY;
        }

        startForeground(NOTIFICATION_ID, buildNotification());
        String baseUrl = intent == null ? "" : intent.getStringExtra(EXTRA_BASE_URL);
        if (baseUrl == null || baseUrl.trim().isEmpty()) {
            client.restoreIfEnabled();
        } else {
            client.start(baseUrl);
        }
        if (!client.isRunning()) {
            stopForegroundCompat();
            stopSelf();
            return START_NOT_STICKY;
        }
        return START_STICKY;
    }

    @Override
    public void onTimeout(int startId, int fgsType) {
        client.stop();
        stopForegroundCompat();
        stopSelf(startId);
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private void createNotificationChannel() {
        NotificationChannel channel = new NotificationChannel(
            CHANNEL_ID,
            "LocWarp Relay",
            NotificationManager.IMPORTANCE_LOW
        );
        channel.setDescription("保持使用者啟動的遠端控制連線");
        NotificationManager manager = getSystemService(NotificationManager.class);
        if (manager != null) manager.createNotificationChannel(channel);
    }

    private Notification buildNotification() {
        PendingIntent open = PendingIntent.getActivity(
            this,
            0,
            new Intent(this, MainActivity.class),
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
        PendingIntent stop = PendingIntent.getService(
            this,
            1,
            new Intent(this, RemoteRelayService.class).setAction(ACTION_STOP),
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
        return new Notification.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentTitle("LocWarp Relay 已啟動")
            .setContentText("遠端控制會在網路中斷後自動重連")
            .setContentIntent(open)
            .setOngoing(true)
            .addAction(android.R.drawable.ic_menu_close_clear_cancel, "停止", stop)
            .build();
    }

    private void stopForegroundCompat() {
        stopForeground(STOP_FOREGROUND_REMOVE);
    }
}
