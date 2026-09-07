package com.locwarp.mock;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.os.IBinder;

public final class UsbControlService extends Service {
    static final String ACTION_START = "com.locwarp.mock.USB_CONTROL_START";
    static final String ACTION_STOP = "com.locwarp.mock.USB_CONTROL_STOP";

    private static final String CHANNEL_ID = "bloompilot_usb_control";
    private static final int NOTIFICATION_ID = 2612;

    private static volatile boolean running;
    private static volatile String serverError = "";

    private UsbControlServer server;

    static boolean isRunning() {
        return running;
    }

    static String serverError() {
        return serverError;
    }

    static void reportServerError(Exception error) {
        if (error == null) return;
        String message = error.getMessage();
        serverError = message == null ? error.getClass().getSimpleName() : message;
    }

    @Override
    public void onCreate() {
        super.onCreate();
        createNotificationChannel();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? ACTION_START : intent.getAction();
        if (ACTION_STOP.equals(action)) {
            shutdown();
            stopSelf();
            return START_NOT_STICKY;
        }
        startForeground(NOTIFICATION_ID, buildNotification());
        if (server == null) {
            try {
                server = new UsbControlServer(this);
                server.start();
                running = true;
                serverError = "";
            } catch (Exception error) {
                reportServerError(error);
                shutdown();
                stopSelf();
                return START_NOT_STICKY;
            }
        }
        // Long desktop jobs must survive Android reclaiming the app process.
        // Access remains limited to computers already authorized by Android's
        // system USB debugging dialog because the socket is reached via ADB.
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        shutdown();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private void shutdown() {
        running = false;
        UsbControlServer current = server;
        server = null;
        if (current != null) current.close();
        stopForeground(STOP_FOREGROUND_REMOVE);
    }

    private void createNotificationChannel() {
        NotificationChannel channel = new NotificationChannel(
            CHANNEL_ID,
            "BloomPilot USB 控制",
            NotificationManager.IMPORTANCE_LOW
        );
        channel.setDescription("保持使用者啟動的 USB 電腦控制通道");
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
            new Intent(this, UsbControlService.class).setAction(ACTION_STOP),
            PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
        return new Notification.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.mipmap.ic_launcher)
            .setContentTitle("BloomPilot USB 控制已啟動")
            .setContentText("僅接受 Android 已授權的 USB 偵錯電腦")
            .setContentIntent(open)
            .setOngoing(true)
            .addAction(android.R.drawable.ic_menu_close_clear_cancel, "停止", stop)
            .build();
    }
}
