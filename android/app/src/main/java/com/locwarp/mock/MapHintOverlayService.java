package com.locwarp.mock;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.PixelFormat;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.provider.Settings;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.WindowManager;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

public final class MapHintOverlayService extends Service
    implements LocalSimulationEngine.Listener, LocalMapView.Listener {

    static final String ACTION_SHOW = "com.locwarp.mock.SHOW_MAP_HINT_OVERLAY";
    static final String ACTION_HIDE = "com.locwarp.mock.HIDE_MAP_HINT_OVERLAY";

    private static final String CHANNEL_ID = "locwarp_map_hint";
    private static final int NOTIFICATION_ID = 2606;

    private final LocalSimulationEngine engine = LocalSimulationEngine.get();
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final AsyncTaskGate routeTasks = new AsyncTaskGate("LocWarpOverlayRoute");
    private WindowManager windowManager;
    private WindowManager.LayoutParams windowParams;
    private FrameLayout rootView;
    private LocalMapView mapView;
    private LocalDataStore library;
    private TextView statusText;
    private View actionMenu;
    private final List<LocalSimulationEngine.Point> floatingRoute = new ArrayList<>();
    private long floatingRouteRevision;
    private String renderedMapRouteKey;
    private boolean firstPositionFocus = true;
    private boolean routePlanning;

    @Override
    public void onCreate() {
        super.onCreate();
        createNotificationChannel();
        windowManager = (WindowManager) getSystemService(WINDOW_SERVICE);
        library = new LocalDataStore(this);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? ACTION_SHOW : intent.getAction();
        if (ACTION_HIDE.equals(action)) {
            closeOverlay();
            return START_NOT_STICKY;
        }
        if (!Settings.canDrawOverlays(this)) {
            stopSelf();
            return START_NOT_STICKY;
        }
        startForeground(NOTIFICATION_ID, buildNotification());
        if (rootView == null) showOverlay();
        else applySizeFromSettings();
        OverlaySettings.setMapHintVisible(this, true);
        return START_NOT_STICKY;
    }

    @Override
    public void onDestroy() {
        routeTasks.close();
        engine.detach(this);
        removeOverlay();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private void showOverlay() {
        if (rootView != null || windowManager == null) return;

        rootView = new FrameLayout(this);
        rootView.setPadding(dp(2), dp(2), dp(2), dp(2));
        rootView.setBackground(roundRect("#ee111520", "#7f8fa8ff", 18));
        rootView.setElevation(dp(18));
        rootView.setClipToOutline(true);

        mapView = new LocalMapView(this);
        renderedMapRouteKey = null;
        mapView.setBackgroundColor(Color.parseColor("#10131c"));
        mapView.setListener(this);
        mapView.setMapControlsVisible(false);
        if (library != null) mapView.setBookmarks(library.bookmarks());
        rootView.addView(mapView, new FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT,
            FrameLayout.LayoutParams.MATCH_PARENT
        ));

        LinearLayout controls = new LinearLayout(this);
        controls.setOrientation(LinearLayout.HORIZONTAL);
        controls.setGravity(Gravity.CENTER_VERTICAL);
        controls.setPadding(dp(3), dp(1), dp(3), dp(1));
        controls.setBackground(roundRect("#dd10131c", "#36566a90", 999));
        FrameLayout.LayoutParams controlsLp = new FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.WRAP_CONTENT,
            dp(50),
            Gravity.TOP | Gravity.START
        );
        controlsLp.setMargins(dp(8), dp(8), 0, 0);
        rootView.addView(controls, controlsLp);

        DragHandleView drag = dragHandle("⋮⋮", 14, "#d7dcef");
        drag.setContentDescription("拖曳浮動地圖");
        controls.addView(drag, new LinearLayout.LayoutParams(dp(48), dp(48)));

        TextView close = toolIcon("×", 18, "#ff9aa2");
        close.setContentDescription("關閉浮動地圖");
        close.setOnClickListener(v -> closeOverlay());
        controls.addView(close, new LinearLayout.LayoutParams(dp(48), dp(48)));

        statusText = new TextView(this);
        statusText.setTextColor(Color.WHITE);
        statusText.setTextSize(12);
        statusText.setPadding(dp(8), dp(5), dp(8), dp(5));
        statusText.setBackground(roundRect("#cc10131c", "#30566a90", 12));
        FrameLayout.LayoutParams statusLp = new FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.WRAP_CONTENT,
            FrameLayout.LayoutParams.WRAP_CONTENT,
            Gravity.BOTTOM | Gravity.CENTER_HORIZONTAL
        );
        statusLp.setMargins(dp(8), 0, dp(8), dp(8));
        rootView.addView(statusText, statusLp);

        int size = OverlaySettings.mapHintSizeDp(this);
        windowParams = new WindowManager.LayoutParams(
            dp(size),
            dp(size),
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
                | WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
            PixelFormat.TRANSLUCENT
        );
        windowParams.gravity = Gravity.START | Gravity.BOTTOM;
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
                    windowParams.x = startX + Math.round(event.getRawX() - startRawX);
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
        engine.attach(this, this);
    }

    private void applySizeFromSettings() {
        if (rootView == null || windowManager == null || windowParams == null) return;
        int size = OverlaySettings.mapHintSizeDp(this);
        windowParams.width = dp(size);
        windowParams.height = dp(size);
        clampWindowPosition();
        windowManager.updateViewLayout(rootView, windowParams);
        rootView.requestLayout();
    }

    private void closeOverlay() {
        routeTasks.cancel();
        routePlanning = false;
        engine.detach(this);
        removeOverlay();
        stopForeground(STOP_FOREGROUND_REMOVE);
        stopSelf();
    }

    private void removeOverlay() {
        if (rootView == null || windowManager == null) return;
        LocalMapView oldMapView = mapView;
        try {
            windowManager.removeView(rootView);
        } catch (Exception ignored) {
        }
        rootView = null;
        mapView = null;
        if (oldMapView != null) oldMapView.release();
        statusText = null;
        OverlaySettings.setMapHintVisible(this, false);
    }

    @Override
    public void onSimulationUpdate(LocalSimulationEngine.Snapshot snapshot) {
        if (mapView == null) return;
        boolean focusCurrent = firstPositionFocus && snapshot.current != null;
        mapView.setCurrent(snapshot.current, focusCurrent);
        if (snapshot.current != null) firstPositionFocus = false;
        mapView.setTarget(snapshot.target);
        if (snapshot.route != null && snapshot.route.size() > 1) {
            renderMapRoute("engine:" + snapshot.routeRevision, snapshot.route);
        } else if (!floatingRoute.isEmpty()) {
            renderMapRoute("floating:" + floatingRouteRevision, floatingRoute);
        } else {
            renderMapRoute("empty", new ArrayList<>());
        }
        if (statusText != null) {
            statusText.setText(LocalSimulationEngine.format(snapshot.current));
        }
    }

    @Override
    public void onMapTap(double lat, double lng) {
        hideActionMenu();
    }

    @Override
    public void onMapLongPress(double lat, double lng) {
        // 長按選單由地圖 WebView 統一處理；放開後才會送出 action。
    }

    @Override
    public void onMapLongPressAction(double lat, double lng, String action) {
        runMapAction(new LocalSimulationEngine.Point(lat, lng), action);
    }

    private void clampWindowPosition() {
        if (windowParams == null) return;
        android.util.DisplayMetrics metrics = getResources().getDisplayMetrics();
        int maxX = Math.max(0, metrics.widthPixels - windowParams.width);
        int maxY = Math.max(0, metrics.heightPixels - windowParams.height);
        windowParams.x = Math.max(0, Math.min(maxX, windowParams.x));
        windowParams.y = Math.max(0, Math.min(maxY, windowParams.y));
    }

    @Override
    public void onMapDoubleTap(double lat, double lng) {
        // 雙擊不執行任何定位動作；瞬移請使用長按選單。
    }

    private void showActionMenu(LocalSimulationEngine.Point point) {
        if (rootView == null) return;
        hideActionMenu();
        LinearLayout menu = new LinearLayout(this);
        menu.setOrientation(LinearLayout.VERTICAL);
        menu.setPadding(dp(10), dp(10), dp(10), dp(10));
        menu.setBackground(roundRect("#ee111520", "#7f8fa8ff", 16));
        menu.setElevation(dp(20));

        TextView title = toolIcon("選擇動作", 13, "#e8eaf0");
        title.setPadding(0, 0, 0, dp(8));
        menu.addView(title, new LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT,
            dp(32)
        ));
        menu.addView(menuButton("瞬移", () -> teleportFromGesture(point)));
        menu.addView(menuButton("導航", () -> navigateByMovementMode(point)));
        menu.addView(menuButton("加入節點", () -> addFloatingWaypoint(point)));
        menu.addView(menuButton("加入收藏", () -> addFloatingBookmark(point)));

        FrameLayout.LayoutParams lp = new FrameLayout.LayoutParams(
            dp(164),
            FrameLayout.LayoutParams.WRAP_CONTENT,
            Gravity.CENTER
        );
        rootView.addView(menu, lp);
        actionMenu = menu;
    }

    private void runMapAction(LocalSimulationEngine.Point point, String action) {
        if ("teleport".equals(action)) {
            teleportFromGesture(point);
        } else if ("navigate".equals(action)) {
            navigateByMovementMode(point);
        } else if ("path".equals(action)) {
            pathNavigateFromGesture(point);
        } else if ("waypoint".equals(action)) {
            addFloatingWaypoint(point);
        } else if ("bookmark".equals(action)) {
            addFloatingBookmark(point);
        }
    }

    private void navigateByMovementMode(LocalSimulationEngine.Point point) {
        if (OverlaySettings.isStraightLineMovement(this)) {
            navigateFromGesture(point);
        } else {
            pathNavigateFromGesture(point);
        }
    }

    private Button menuButton(String text, Runnable action) {
        Button button = new Button(this);
        button.setAllCaps(false);
        button.setText(text);
        button.setTextSize(13);
        button.setTypeface(Typeface.DEFAULT_BOLD);
        button.setTextColor(Color.WHITE);
        button.setBackground(roundRect("#26304d", "#40566a90", 12));
        button.setOnClickListener(v -> {
            hideActionMenu();
            action.run();
        });
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT,
            dp(48)
        );
        lp.setMargins(0, dp(6), 0, 0);
        button.setLayoutParams(lp);
        return button;
    }

    private void hideActionMenu() {
        if (rootView == null || actionMenu == null) return;
        try {
            rootView.removeView(actionMenu);
        } catch (Exception ignored) {
        }
        actionMenu = null;
    }

    private void teleportFromGesture(LocalSimulationEngine.Point point) {
        if (!MockLocationKeeper.isMockEnabled(this)) {
            Toast.makeText(this, "請先在 App 開啟位置模擬", Toast.LENGTH_SHORT).show();
            return;
        }
        if (!engine.teleport(point.lat, point.lng)) {
            Toast.makeText(this, "瞬移失敗，請確認已選取 LocWarp 作為模擬位置 App", Toast.LENGTH_LONG).show();
            return;
        }
        floatingRoute.clear();
        floatingRouteRevision++;
        renderMapRoute("empty", new ArrayList<>());
        Toast.makeText(
            this,
            "已瞬移：" + LocalSimulationEngine.format(point),
            Toast.LENGTH_SHORT
        ).show();
    }

    private void navigateFromGesture(LocalSimulationEngine.Point point) {
        if (!MockLocationKeeper.isMockEnabled(this)) {
            Toast.makeText(this, "請先在 App 開啟位置模擬", Toast.LENGTH_SHORT).show();
            return;
        }
        if (!engine.navigate(point)) {
            Toast.makeText(this, "尚未設定起點，請先在 App 選點並瞬移", Toast.LENGTH_LONG).show();
            return;
        }
        floatingRoute.clear();
        floatingRouteRevision++;
        renderMapRoute("empty", new ArrayList<>());
        Toast.makeText(this, "已開始直線導航", Toast.LENGTH_SHORT).show();
    }

    private void pathNavigateFromGesture(LocalSimulationEngine.Point point) {
        if (routePlanning) {
            Toast.makeText(this, "路徑計算中，請稍候", Toast.LENGTH_SHORT).show();
            return;
        }
        if (!MockLocationKeeper.isMockEnabled(this)) {
            Toast.makeText(this, "請先在 App 開啟位置模擬", Toast.LENGTH_SHORT).show();
            return;
        }
        LocalSimulationEngine.Snapshot snapshot = engine.snapshot();
        if (snapshot.current == null) {
            Toast.makeText(this, "尚未設定起點，請先在 App 選點並瞬移", Toast.LENGTH_LONG).show();
            return;
        }
        routePlanning = true;
        Toast.makeText(this, "正在計算路徑…", Toast.LENGTH_SHORT).show();
        LocalSimulationEngine.Point start = snapshot.current;
        routeTasks.submit(
            WalkingRoutePlanner.planTask(start, point),
            mainHandler,
            route -> {
                floatingRoute.clear();
                floatingRoute.addAll(route);
                floatingRouteRevision++;
                renderMapRoute("floating:" + floatingRouteRevision, floatingRoute);
                if (!engine.startPathRoute(new ArrayList<>(route))) {
                    routePlanning = false;
                    Toast.makeText(
                        this,
                        "尚未設定起點，請先在 App 選點並瞬移",
                        Toast.LENGTH_LONG
                    ).show();
                    return;
                }
                routePlanning = false;
                Toast.makeText(this, "已開始路徑導航", Toast.LENGTH_SHORT).show();
            },
            error -> {
                routePlanning = false;
                Toast.makeText(
                    this,
                    "路徑計算失敗，請確認網路可用或改用直線導航",
                    Toast.LENGTH_LONG
                ).show();
            }
        );
    }

    private void addFloatingWaypoint(LocalSimulationEngine.Point point) {
        floatingRoute.add(point);
        floatingRouteRevision++;
        if (mapView != null) {
            mapView.setTarget(point);
            renderMapRoute("floating:" + floatingRouteRevision, floatingRoute);
        }
        Toast.makeText(
            this,
            "已加入浮動路線節點 " + floatingRoute.size(),
            Toast.LENGTH_SHORT
        ).show();
    }

    private void addFloatingBookmark(LocalSimulationEngine.Point point) {
        if (library == null) library = new LocalDataStore(this);
        String name = String.format(
            Locale.US,
            "浮動收藏 %.5f, %.5f",
            point.lat,
            point.lng
        );
        library.addBookmark(name, point);
        if (mapView != null) {
            mapView.setTarget(point);
            mapView.setBookmarks(library.bookmarks());
        }
        Toast.makeText(this, "已加入收藏", Toast.LENGTH_SHORT).show();
    }

    private void renderMapRoute(
        String key,
        List<LocalSimulationEngine.Point> points
    ) {
        if (mapView == null || key.equals(renderedMapRouteKey)) return;
        renderedMapRouteKey = key;
        mapView.setRoute(points);
    }

    private void createNotificationChannel() {
        NotificationManager manager =
            (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
        if (manager == null) return;
        NotificationChannel channel = new NotificationChannel(
            CHANNEL_ID,
            "LocWarp 地圖提示",
            NotificationManager.IMPORTANCE_LOW
        );
        channel.setDescription("顯示跨 App 的浮動地圖提示");
        manager.createNotificationChannel(channel);
    }

    private Notification buildNotification() {
        Intent closeIntent = new Intent(this, MapHintOverlayService.class)
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
            .setContentTitle("LocWarp 地圖提示")
            .setContentText("點擊通知可關閉浮動地圖")
            .setContentIntent(closePending)
            .setOngoing(true)
            .build();
    }

    private TextView toolIcon(String text, int textSizeSp, String color) {
        TextView view = new TextView(this);
        styleToolIcon(view, text, textSizeSp, color);
        return view;
    }

    private DragHandleView dragHandle(String text, int textSizeSp, String color) {
        DragHandleView view = new DragHandleView(this);
        styleToolIcon(view, text, textSizeSp, color);
        return view;
    }

    private void styleToolIcon(TextView view, String text, int textSizeSp, String color) {
        view.setText(text);
        view.setTextSize(textSizeSp);
        view.setTypeface(Typeface.DEFAULT_BOLD);
        view.setTextColor(Color.parseColor(color));
        view.setGravity(Gravity.CENTER);
        view.setPadding(0, 0, 0, 0);
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
}
