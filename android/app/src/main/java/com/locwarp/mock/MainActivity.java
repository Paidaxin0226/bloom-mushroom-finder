package com.locwarp.mock;

import android.app.Activity;
import android.annotation.SuppressLint;
import android.app.AlertDialog;
import android.app.AppOpsManager;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.location.Location;
import android.location.LocationManager;
import android.location.provider.ProviderProperties;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.Process;
import android.os.SystemClock;
import android.provider.Settings;
import android.view.Gravity;
import android.view.View;
import android.view.WindowInsets;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.Toast;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.text.DateFormat;
import java.util.Date;

public class MainActivity extends Activity {
    private static final String CHECK_PROVIDER = "locwarp_self_check";
    private static final String PAGE_LOCATION = "location";
    private static final String PAGE_FITNESS = "fitness";
    private static final String PAGE_HOOK_SCOPE = "hookScope";
    private static final String PAGE_RELAY = "relay";
    private static final String PREF_SELECTED_PAGE = "selectedPage";
    private static final int REQUEST_EXPORT_BACKUP = 2610;
    private static final int REQUEST_IMPORT_BACKUP = 2611;
    private static final int MAX_BACKUP_BYTES = 4 * 1024 * 1024;

    private final Handler uiHandler = new Handler(Looper.getMainLooper());
    private LocationControlPage locationPage;
    private FitnessDebuggerPage fitnessPage;
    private HookScopePage hookScopePage;
    private RelayControlPage relayPage;
    private FrameLayout pageHost;
    private Button locationNavButton;
    private Button fitnessNavButton;
    private Button hookScopeNavButton;
    private Button relayNavButton;
    private FrameLayout rootView;
    private View bottomNavigation;
    private FrameLayout.LayoutParams pageHostLayoutParams;
    private FrameLayout.LayoutParams bottomNavigationLayoutParams;
    private String selectedPage = PAGE_LOCATION;
    private Runnable overlayPermissionCallback;
    private boolean awaitingOverlayPermission;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        configureEdgeToEdge();

        selectedPage = savedInstanceState != null
            ? savedInstanceState.getString(PREF_SELECTED_PAGE, PAGE_LOCATION)
            : getPreferences(Context.MODE_PRIVATE)
                .getString(PREF_SELECTED_PAGE, PAGE_LOCATION);

        locationPage = new LocationControlPage(this);
        fitnessPage = new FitnessDebuggerPage(this);
        hookScopePage = new HookScopePage(this);
        relayPage = new RelayControlPage(this);
        pageHost = new FrameLayout(this);
        pageHost.setBackgroundColor(Color.parseColor("#0a0c12"));
        pageHost.addView(locationPage, new FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT,
            FrameLayout.LayoutParams.MATCH_PARENT
        ));
        pageHost.addView(fitnessPage, new FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT,
            FrameLayout.LayoutParams.MATCH_PARENT
        ));
        pageHost.addView(hookScopePage, new FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT,
            FrameLayout.LayoutParams.MATCH_PARENT
        ));
        pageHost.addView(relayPage, new FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT,
            FrameLayout.LayoutParams.MATCH_PARENT
        ));

        rootView = new FrameLayout(this);
        rootView.setBackgroundColor(Color.parseColor("#0a0c12"));
        pageHostLayoutParams = new FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT,
            FrameLayout.LayoutParams.MATCH_PARENT
        );
        rootView.addView(pageHost, pageHostLayoutParams);

        bottomNavigationLayoutParams = new FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT,
            dp(68),
            Gravity.BOTTOM
        );
        bottomNavigationLayoutParams.setMargins(dp(12), 0, dp(12), dp(8));
        bottomNavigation = buildBottomNavigation();
        rootView.addView(bottomNavigation, bottomNavigationLayoutParams);
        setContentView(rootView);
        applySystemBarInsets();
        selectPage(selectedPage);
        startUsbControlService();
        if (OverlaySettings.isRelayEnabled(this)) {
            startRelayService(OverlaySettings.relayBaseUrl(this));
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (locationPage != null) locationPage.onHostForegroundChanged(true);
        if (fitnessPage != null) fitnessPage.onHostForegroundChanged(true);
        if (locationPage != null) locationPage.refresh();
        if (fitnessPage != null) fitnessPage.refresh();
        if (hookScopePage != null) hookScopePage.refresh();
        if (relayPage != null) relayPage.refresh();
        if (awaitingOverlayPermission) {
            awaitingOverlayPermission = false;
            Runnable callback = overlayPermissionCallback;
            overlayPermissionCallback = null;
            if (Settings.canDrawOverlays(this)) {
                if (callback != null) callback.run();
            } else {
                showErrorDialog(
                    "需要顯示在其他 App 上層",
                    "請允許 LocWarp 顯示浮動控制，才能使用浮動搖桿或地圖提示。"
                );
            }
        }
    }

    @Override
    protected void onPause() {
        if (locationPage != null) locationPage.onHostForegroundChanged(false);
        if (fitnessPage != null) fitnessPage.onHostForegroundChanged(false);
        super.onPause();
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        outState.putString(PREF_SELECTED_PAGE, selectedPage);
        super.onSaveInstanceState(outState);
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (resultCode != RESULT_OK || data == null || data.getData() == null) return;
        Uri uri = data.getData();
        if (requestCode == REQUEST_EXPORT_BACKUP) {
            exportBackupTo(uri);
        } else if (requestCode == REQUEST_IMPORT_BACKUP) {
            readBackupForImport(uri);
        }
    }

    private LinearLayout buildBottomNavigation() {
        LinearLayout nav = new LinearLayout(this);
        nav.setOrientation(LinearLayout.HORIZONTAL);
        nav.setGravity(Gravity.CENTER);
        nav.setPadding(dp(8), dp(8), dp(8), dp(8));
        nav.setBackground(roundRect(0xcc141722, 0x662d3550, 36));
        nav.setElevation(dp(14));

        locationNavButton = navButton("定位");
        locationNavButton.setContentDescription("定位控制");
        locationNavButton.setOnClickListener(v -> selectPage(PAGE_LOCATION));
        fitnessNavButton = navButton("步數");
        fitnessNavButton.setContentDescription("LSPosed 步數注入");
        fitnessNavButton.setOnClickListener(v -> selectPage(PAGE_FITNESS));
        hookScopeNavButton = navButton("應用");
        hookScopeNavButton.setContentDescription("LSPosed 作用域應用設定");
        hookScopeNavButton.setOnClickListener(v -> selectPage(PAGE_HOOK_SCOPE));
        relayNavButton = navButton("連線");
        relayNavButton.setContentDescription("USB 與 Relay 連線控制");
        relayNavButton.setOnClickListener(v -> selectPage(PAGE_RELAY));

        LinearLayout.LayoutParams locationLp = new LinearLayout.LayoutParams(0,
            LinearLayout.LayoutParams.MATCH_PARENT, 1f);
        nav.addView(locationNavButton, locationLp);
        LinearLayout.LayoutParams fitLp = new LinearLayout.LayoutParams(0,
            LinearLayout.LayoutParams.MATCH_PARENT, 1f);
        nav.addView(fitnessNavButton, fitLp);
        LinearLayout.LayoutParams hookLp = new LinearLayout.LayoutParams(0,
            LinearLayout.LayoutParams.MATCH_PARENT, 1f);
        nav.addView(hookScopeNavButton, hookLp);
        LinearLayout.LayoutParams relayLp = new LinearLayout.LayoutParams(0,
            LinearLayout.LayoutParams.MATCH_PARENT, 1f);
        nav.addView(relayNavButton, relayLp);
        return nav;
    }

    private Button navButton(String text) {
        Button button = new Button(this);
        button.setAllCaps(false);
        button.setText(text);
        button.setTextSize(13);
        button.setGravity(Gravity.CENTER);
        button.setPadding(0, dp(3), 0, dp(3));
        button.setIncludeFontPadding(false);
        button.setMinHeight(0);
        button.setMinimumHeight(0);
        button.setMinWidth(0);
        button.setMinimumWidth(0);
        button.setStateListAnimator(null);
        button.setBackgroundColor(Color.TRANSPARENT);
        return button;
    }

    private void selectPage(String page) {
        if (PAGE_FITNESS.equals(page)) {
            selectedPage = PAGE_FITNESS;
        } else if (PAGE_HOOK_SCOPE.equals(page)) {
            selectedPage = PAGE_HOOK_SCOPE;
        } else if (PAGE_RELAY.equals(page)) {
            selectedPage = PAGE_RELAY;
        } else {
            selectedPage = PAGE_LOCATION;
        }
        getPreferences(Context.MODE_PRIVATE)
            .edit()
            .putString(PREF_SELECTED_PAGE, selectedPage)
            .apply();
        if (locationPage != null) {
            boolean locationSelected = PAGE_LOCATION.equals(selectedPage);
            locationPage.setVisibility(locationSelected
                ? View.VISIBLE
                : View.GONE);
            locationPage.onPageSelected(locationSelected);
        }
        if (fitnessPage != null) {
            boolean fitnessSelected = PAGE_FITNESS.equals(selectedPage);
            fitnessPage.setVisibility(fitnessSelected ? View.VISIBLE : View.GONE);
            fitnessPage.onPageSelected(fitnessSelected);
        }
        if (hookScopePage != null) {
            boolean hookScopeSelected = PAGE_HOOK_SCOPE.equals(selectedPage);
            hookScopePage.setVisibility(hookScopeSelected ? View.VISIBLE : View.GONE);
            hookScopePage.onPageSelected(hookScopeSelected);
        }
        if (relayPage != null) {
            boolean relaySelected = PAGE_RELAY.equals(selectedPage);
            relayPage.setVisibility(relaySelected ? View.VISIBLE : View.GONE);
            relayPage.onPageSelected(relaySelected);
        }
        styleNavButton(locationNavButton, PAGE_LOCATION.equals(selectedPage));
        styleNavButton(fitnessNavButton, PAGE_FITNESS.equals(selectedPage));
        styleNavButton(hookScopeNavButton, PAGE_HOOK_SCOPE.equals(selectedPage));
        styleNavButton(relayNavButton, PAGE_RELAY.equals(selectedPage));
    }

    private void styleNavButton(Button button, boolean selected) {
        if (button == null) return;
        button.setTextColor(Color.parseColor(selected ? "#ffffff" : "#9499ac"));
        button.setTypeface(selected ? Typeface.DEFAULT_BOLD : Typeface.DEFAULT);
        if (selected) {
            button.setBackground(roundRect(0x556c8cff, 0x6688a0ff, 32));
        } else {
            button.setBackgroundColor(Color.TRANSPARENT);
        }
        button.setElevation(0);
        int icon;
        if (button == locationNavButton) {
            icon = selected
                ? R.drawable.ic_nav_location_filled
                : R.drawable.ic_nav_location_outline;
        } else if (button == fitnessNavButton) {
            icon = selected
                ? R.drawable.ic_nav_fitness_filled
                : R.drawable.ic_nav_fitness_outline;
        } else if (button == hookScopeNavButton) {
            icon = selected
                ? R.drawable.ic_nav_apps_filled
                : R.drawable.ic_nav_apps_outline;
        } else {
            icon = selected
                ? R.drawable.ic_nav_relay_filled
                : R.drawable.ic_nav_relay_outline;
        }
        button.setCompoundDrawablesWithIntrinsicBounds(0, icon, 0, 0);
        button.setCompoundDrawablePadding(dp(2));
    }

    private void configureEdgeToEdge() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            getWindow().setDecorFitsSystemWindows(false);
        } else {
            getWindow().getDecorView().setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                    | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                    | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
            );
        }
        getWindow().setStatusBarColor(Color.TRANSPARENT);
        getWindow().setNavigationBarColor(Color.TRANSPARENT);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            getWindow().setStatusBarContrastEnforced(false);
            getWindow().setNavigationBarContrastEnforced(true);
        }
    }

    private void applySystemBarInsets() {
        if (rootView == null) return;
        rootView.setOnApplyWindowInsetsListener((view, insets) -> {
            int top;
            int bottom;
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                android.graphics.Insets bars = insets.getInsets(
                    WindowInsets.Type.systemBars() | WindowInsets.Type.displayCutout()
                );
                top = bars.top;
                bottom = bars.bottom;
            } else {
                top = insets.getSystemWindowInsetTop();
                bottom = insets.getSystemWindowInsetBottom();
            }
            pageHost.setPadding(0, top, 0, 0);
            bottomNavigationLayoutParams.setMargins(dp(12), 0, dp(12), bottom + dp(8));
            bottomNavigation.setLayoutParams(bottomNavigationLayoutParams);
            pageHostLayoutParams.setMargins(0, 0, 0, bottom + dp(92));
            pageHost.setLayoutParams(pageHostLayoutParams);
            int contentBottom = dp(24);
            locationPage.setBottomContentInset(contentBottom);
            fitnessPage.setBottomContentInset(contentBottom);
            hookScopePage.setBottomContentInset(contentBottom);
            relayPage.setBottomContentInset(contentBottom);
            return insets;
        });
        rootView.requestApplyInsets();
    }

    boolean applyLocalCoordinate(double lat, double lng) {
        if (!ensureMockPermission()) return false;
        if (!MockLocationKeeper.startLocal(this, lat, lng, Float.NaN, Float.NaN)) {
            showErrorDialog(
                "套用定位失敗",
                "請確認已在開發者選項中把 LocWarp Mock Provider 設為模擬位置應用程式。"
            );
            return false;
        }
        startLocalMockService();
        if (locationPage != null) locationPage.refresh();
        return true;
    }

    boolean ensureMockPermission() {
        PermissionCheck permission = checkMockPermission();
        if (permission.allowed) return true;
        showMockPermissionDialog(permission.smokeText);
        return false;
    }

    void requestOverlayPermission(Runnable onGranted) {
        if (Settings.canDrawOverlays(this)) {
            onGranted.run();
            return;
        }
        overlayPermissionCallback = onGranted;
        new AlertDialog.Builder(this)
            .setTitle("需要浮動視窗權限")
            .setMessage("請允許 LocWarp 顯示在其他 App 上層，才能使用浮動搖桿或浮動地圖提示。")
            .setPositiveButton("前往設定", (dialog, which) -> {
                awaitingOverlayPermission = true;
                Intent intent = new Intent(
                    Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                    Uri.parse("package:" + getPackageName())
                );
                startActivity(intent);
            })
            .setNegativeButton("取消", (dialog, which) -> {
                overlayPermissionCallback = null;
                awaitingOverlayPermission = false;
            })
            .show();
    }

    void showFloatingJoystick() {
        requestNotificationPermissionIfNeeded();
        Intent intent = new Intent(this, JoystickOverlayService.class)
            .setAction(JoystickOverlayService.ACTION_SHOW);
        startForegroundService(intent);
    }

    void hideFloatingJoystick() {
        OverlaySettings.setJoystickVisible(this, false);
        LocalSimulationEngine.get().joystickInput(0d, 0d);
        LocalSimulationEngine.get().stop();
        Intent intent = new Intent(this, JoystickOverlayService.class)
            .setAction(JoystickOverlayService.ACTION_HIDE);
        stopService(intent);
    }

    void showMapHintOverlay() {
        requestNotificationPermissionIfNeeded();
        Intent intent = new Intent(this, MapHintOverlayService.class)
            .setAction(MapHintOverlayService.ACTION_SHOW);
        startForegroundService(intent);
    }

    void hideMapHintOverlay() {
        OverlaySettings.setMapHintVisible(this, false);
        Intent intent = new Intent(this, MapHintOverlayService.class)
            .setAction(MapHintOverlayService.ACTION_HIDE);
        stopService(intent);
    }

    void startRelayService(String baseUrl) {
        requestNotificationPermissionIfNeeded();
        Intent intent = new Intent(this, RemoteRelayService.class)
            .setAction(RemoteRelayService.ACTION_START)
            .putExtra(RemoteRelayService.EXTRA_BASE_URL, baseUrl);
        startForegroundService(intent);
        if (locationPage != null) locationPage.refresh();
    }

    void stopRelayService() {
        Intent intent = new Intent(this, RemoteRelayService.class)
            .setAction(RemoteRelayService.ACTION_STOP);
        startService(intent);
    }

    void startUsbControlService() {
        requestNotificationPermissionIfNeeded();
        Intent intent = new Intent(this, UsbControlService.class)
            .setAction(UsbControlService.ACTION_START);
        startForegroundService(intent);
    }

    void stopUsbControlService() {
        Intent intent = new Intent(this, UsbControlService.class)
            .setAction(UsbControlService.ACTION_STOP);
        startService(intent);
    }

    void clearLocalCoordinate() {
        MockLocationKeeper.clearLocal(this);
        stopLocalMockService();
        uiHandler.postDelayed(() -> {
            if (locationPage != null) locationPage.refresh();
        }, 150L);
    }

    void showControlError(String message) {
        showErrorDialog("定位控制失敗", message);
    }

    void launchBackupExport() {
        Intent intent = new Intent(Intent.ACTION_CREATE_DOCUMENT)
            .addCategory(Intent.CATEGORY_OPENABLE)
            .setType("application/json")
            .putExtra(Intent.EXTRA_TITLE, "LocWarp-backup.json");
        startActivityForResult(intent, REQUEST_EXPORT_BACKUP);
    }

    void launchBackupImport() {
        Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT)
            .addCategory(Intent.CATEGORY_OPENABLE)
            .setType("application/json");
        startActivityForResult(intent, REQUEST_IMPORT_BACKUP);
    }

    private void exportBackupTo(Uri uri) {
        new Thread(() -> {
            try {
                String backup = LocWarpBackupCodec.exportJson(this);
                try (OutputStream output = getContentResolver().openOutputStream(uri, "wt")) {
                    if (output == null) throw new IllegalStateException("無法開啟目的檔案");
                    output.write(backup.getBytes(StandardCharsets.UTF_8));
                    output.flush();
                }
                uiHandler.post(() -> Toast.makeText(
                    this,
                    "LocWarp 備份已匯出（不含 Relay 憑證）",
                    Toast.LENGTH_LONG
                ).show());
            } catch (Exception error) {
                uiHandler.post(() -> showErrorDialog(
                    "備份失敗",
                    safeMessage(error)
                ));
            }
        }, "LocWarpBackupExport").start();
    }

    private void readBackupForImport(Uri uri) {
        new Thread(() -> {
            try {
                String raw;
                try (InputStream input = getContentResolver().openInputStream(uri)) {
                    if (input == null) throw new IllegalStateException("無法開啟備份檔");
                    raw = readBoundedUtf8(input, MAX_BACKUP_BYTES);
                }
                LocWarpBackupCodec.Summary summary = LocWarpBackupCodec.inspect(raw);
                uiHandler.post(() -> confirmBackupImport(raw, summary));
            } catch (Exception error) {
                uiHandler.post(() -> showErrorDialog(
                    "無法讀取備份",
                    safeMessage(error)
                ));
            }
        }, "LocWarpBackupImport").start();
    }

    private void confirmBackupImport(
        String raw,
        LocWarpBackupCodec.Summary summary
    ) {
        String exported = summary.exportedAtMs > 0L
            ? DateFormat.getDateTimeInstance().format(new Date(summary.exportedAtMs))
            : "未知";
        String message = "版本：" + summary.appVersion
            + "\n匯出時間：" + exported
            + "\n收藏：" + summary.bookmarks
            + "\n路線：" + summary.routes
            + "\nHook 應用：" + summary.hookApps
            + "\n\n還原會覆蓋目前的收藏、路線、Hook、顯示與安全設定，"
            + "並停止目前的定位與 Relay。Relay 憑證不會從備份匯入。";
        new AlertDialog.Builder(this)
            .setTitle("還原 LocWarp 備份？")
            .setMessage(message)
            .setPositiveButton("還原", (dialog, which) -> restoreBackup(raw))
            .setNegativeButton("取消", null)
            .show();
    }

    private void restoreBackup(String raw) {
        stopRelayService();
        OverlaySettings.clearRelaySession(this);
        LocalSimulationEngine.get().attach(this, null);
        LocalSimulationEngine.get().restore();
        new Thread(() -> {
            try {
                LocWarpBackupCodec.restore(this, raw);
                uiHandler.post(() -> {
                    Toast.makeText(
                        this,
                        "備份已還原",
                        Toast.LENGTH_LONG
                    ).show();
                    recreate();
                });
            } catch (Exception error) {
                uiHandler.post(() -> showErrorDialog(
                    "還原失敗",
                    safeMessage(error)
                ));
            }
        }, "LocWarpBackupRestore").start();
    }

    private String readBoundedUtf8(InputStream input, int maxBytes) throws Exception {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        byte[] buffer = new byte[16 * 1024];
        int total = 0;
        int read;
        while ((read = input.read(buffer)) >= 0) {
            total += read;
            if (total > maxBytes) {
                throw new IllegalArgumentException("備份檔超過 4 MB");
            }
            output.write(buffer, 0, read);
        }
        return new String(output.toByteArray(), StandardCharsets.UTF_8);
    }

    private void startLocalMockService() {
        requestNotificationPermissionIfNeeded();
        Intent intent = new Intent(this, LocalMockService.class)
            .setAction(LocalMockService.ACTION_START);
        startForegroundService(intent);
    }

    private void stopLocalMockService() {
        Intent intent = new Intent(this, LocalMockService.class)
            .setAction(LocalMockService.ACTION_CLEAR);
        startService(intent);
    }

    private PermissionCheck checkMockPermission() {
        String appOpsText = "unknown";
        boolean allowed = false;
        try {
            AppOpsManager appOps = (AppOpsManager) getSystemService(Context.APP_OPS_SERVICE);
            if (appOps != null) {
                int mode = appOps.checkOpNoThrow(
                    AppOpsManager.OPSTR_MOCK_LOCATION,
                    Process.myUid(),
                    getPackageName()
                );
                appOpsText = modeToText(mode);
                allowed = mode == AppOpsManager.MODE_ALLOWED;
            }
        } catch (Throwable t) {
            appOpsText = t.getClass().getSimpleName() + ": " + safeMessage(t);
        }

        SmokeResult smoke = runProviderSmokeTest();
        return new PermissionCheck(allowed || smoke.ok, smoke.ok, appOpsText, smoke.message);
    }

    @SuppressLint("InlinedApi")
    private SmokeResult runProviderSmokeTest() {
        LocationManager manager = (LocationManager) getSystemService(Context.LOCATION_SERVICE);
        if (manager == null) return new SmokeResult(false, "LocationManager unavailable");

        try {
            try {
                manager.removeTestProvider(CHECK_PROVIDER);
            } catch (IllegalArgumentException ignored) {
            }

            manager.addTestProvider(
                CHECK_PROVIDER,
                false,
                false,
                false,
                false,
                true,
                true,
                true,
                ProviderProperties.POWER_USAGE_LOW,
                ProviderProperties.ACCURACY_FINE
            );
            manager.setTestProviderEnabled(CHECK_PROVIDER, true);

            Location location = new Location(CHECK_PROVIDER);
            location.setLatitude(25.033964);
            location.setLongitude(121.564468);
            location.setAccuracy(3.0f);
            location.setTime(System.currentTimeMillis());
            location.setElapsedRealtimeNanos(SystemClock.elapsedRealtimeNanos());
            manager.setTestProviderLocation(CHECK_PROVIDER, location);
            manager.removeTestProvider(CHECK_PROVIDER);
            return new SmokeResult(true, "pass");
        } catch (SecurityException e) {
            return new SmokeResult(false, "permission denied");
        } catch (Throwable t) {
            try {
                manager.removeTestProvider(CHECK_PROVIDER);
            } catch (Throwable ignored) {
            }
            return new SmokeResult(false, t.getClass().getSimpleName() + ": " + safeMessage(t));
        }
    }

    private void showMockPermissionDialog(String detail) {
        new AlertDialog.Builder(this)
            .setTitle("需要模擬位置權限")
            .setMessage(
                "請到 Android 開發者選項，將模擬位置應用程式設為 LocWarp Mock Provider。\n\n"
                    + "檢查結果: " + detail
            )
            .setPositiveButton("開啟開發者選項", (dialog, which) -> openDeveloperOptions())
            .setNegativeButton("取消", null)
            .show();
    }

    private void showErrorDialog(String title, String message) {
        new AlertDialog.Builder(this)
            .setTitle(title)
            .setMessage(message)
            .setPositiveButton("確定", null)
            .show();
    }

    private void requestNotificationPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT < 33) return;
        if (checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS)
            == PackageManager.PERMISSION_GRANTED) return;
        requestPermissions(new String[] {android.Manifest.permission.POST_NOTIFICATIONS}, 2603);
    }

    private void openDeveloperOptions() {
        try {
            startActivity(new Intent(Settings.ACTION_APPLICATION_DEVELOPMENT_SETTINGS));
        } catch (Exception ignored) {
            startActivity(new Intent(Settings.ACTION_SETTINGS));
        }
    }

    private String modeToText(int mode) {
        if (mode == AppOpsManager.MODE_ALLOWED) return "allowed";
        if (mode == AppOpsManager.MODE_IGNORED) return "ignored";
        if (mode == AppOpsManager.MODE_ERRORED) return "errored";
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q
            && mode == AppOpsManager.MODE_FOREGROUND) {
            return "foreground";
        }
        return "mode " + mode;
    }

    private String safeMessage(Throwable t) {
        String message = t.getMessage();
        return message == null ? "" : message;
    }

    private GradientDrawable roundRect(String fill, String stroke, int radiusDp) {
        return roundRect(Color.parseColor(fill), Color.parseColor(stroke), radiusDp);
    }

    private GradientDrawable roundRect(int fill, int stroke, int radiusDp) {
        GradientDrawable drawable = new GradientDrawable();
        drawable.setColor(fill);
        drawable.setCornerRadius(dp(radiusDp));
        drawable.setStroke(dp(1), stroke);
        return drawable;
    }

    private int dp(int value) {
        return (int) (value * getResources().getDisplayMetrics().density + 0.5f);
    }

    private static class PermissionCheck {
        final boolean allowed;
        final boolean smokeOk;
        final String appOpsText;
        final String smokeText;

        PermissionCheck(boolean allowed, boolean smokeOk, String appOpsText, String smokeText) {
            this.allowed = allowed;
            this.smokeOk = smokeOk;
            this.appOpsText = appOpsText;
            this.smokeText = smokeText;
        }
    }

    private static class SmokeResult {
        final boolean ok;
        final String message;

        SmokeResult(boolean ok, String message) {
            this.ok = ok;
            this.message = message;
        }
    }
}
