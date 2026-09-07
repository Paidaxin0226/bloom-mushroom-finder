package com.locwarp.mock;

import android.annotation.SuppressLint;
import android.app.AlertDialog;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.location.Address;
import android.location.Geocoder;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.Switch;
import android.widget.TextView;
import android.widget.Toast;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

@SuppressLint({"ViewConstructor", "SetTextI18n"})
final class LocationControlPage extends ScrollView
    implements LocalSimulationEngine.Listener,
        LocalMapView.Listener {

    private static final String ACCENT = "#6c8cff";
    private static final String SURFACE = "#141722";
    private static final String ELEVATED = "#1f2334";
    private static final String BORDER = "#2d3550";
    private static final String TEXT = "#e8eaf0";
    private static final String MUTED = "#9499ac";
    private static final int FLOWER_REQUIRED_PASSES = 2;
    private static final long FLOWER_COOLDOWN_MS = 5L * 60L * 1000L;
    private static final double FLOWER_DEFAULT_RADIUS_M = 36d;
    private static final double FLOWER_DEFAULT_SPACING_M = 6d;

    private final MainActivity activity;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final AsyncTaskGate routeTasks = new AsyncTaskGate("LocWarpRouteTask");
    private final AsyncTaskGate searchTasks = new AsyncTaskGate("LocWarpSearchTask");
    private final LocalSimulationEngine engine = LocalSimulationEngine.get();
    private final LocalDataStore library;
    private final List<LocalSimulationEngine.Point> draftWaypoints = new ArrayList<>();
    private final Map<String, Button> modeButtons = new LinkedHashMap<>();

    private LocalMapView mapView;
    private TextView statusBody;
    private TextView draftSummary;
    private LinearLayout routeWaypointContent;
    private LinearLayout modeOptions;
    private LinearLayout contentRoot;
    private LinearLayout libraryContent;
    private LinearLayout flowerTargetContent;
    private LinearLayout mapAdvancedPanel;
    private LinearLayout movementSettingsPanel;
    private LinearLayout libraryCardView;
    private EditText searchInput;
    private EditText latitudeInput;
    private EditText longitudeInput;
    private EditText speedInput;
    private EditText radiusInput;
    private EditText stopDurationInput;
    private EditText lapCountInput;
    private EditText pauseMinInput;
    private EditText pauseMaxInput;
    private EditText flowerRadiusInput;
    private EditText flowerSpacingInput;
    private EditText autoStopMinutesInput;
    private CheckBox routeLoopCheck;
    private CheckBox pauseCheck;
    private CheckBox restoreOnFinishCheck;
    private CheckBox resumeAfterRestartCheck;
    private Button primaryAction;
    private Button stopAction;
    private Switch mockSwitch;
    private TextView mockSwitchHint;
    private LinearLayout actionCardView;
    private LinearLayout mapHintOptionsPanel;
    private TextView controlStatus;
    private TextView flowerSummary;
    private Switch mapHintSwitch;
    private Switch straightLineSwitch;
    private Switch mapIntentInterceptSwitch;
    private TextView joystickSizeLabel;
    private TextView mapHintSizeLabel;
    private TextView mapTileStatus;

    private String selectedMode = "navigate";
    private LocalSimulationEngine.Point selectedPoint =
        new LocalSimulationEngine.Point(25.033964, 121.564468);
    private long displayedRevision = Long.MIN_VALUE;
    private boolean firstPositionFocus = true;
    private boolean syncingMockSwitch;
    private boolean syncingMapHintSwitch;
    private boolean routePlanning;
    private String routePlanningMessage;
    private LocalDataStore.FlowerTarget activeFlowerTarget;
    private int activeFlowerPasses;
    private boolean flowerRouteRunning;
    private boolean flowerRouteSeenRunning;
    private boolean flowerWaiting;
    private Runnable flowerWaitRunnable;
    private boolean hostForeground = true;
    private boolean pageSelected;
    private boolean detached;
    private long draftRouteRevision;
    private String renderedMapRouteKey;

    LocationControlPage(MainActivity activity) {
        super(activity);
        this.activity = activity;
        library = new LocalDataStore(activity);
        engine.configureSpeed(OverlaySettings.speedKmh(activity), null, null);
        engine.configureSafety(
            OverlaySettings.safetyAutoStopMinutes(activity),
            OverlaySettings.restoreOnMovementFinish(activity)
        );
        setFillViewport(true);
        setClipToPadding(false);
        setBackgroundColor(Color.parseColor("#0a0c12"));
        build();
        engine.attach(activity, this);
    }

    @Override
    protected void onDetachedFromWindow() {
        detached = true;
        hostForeground = false;
        pageSelected = false;
        mainHandler.removeCallbacksAndMessages(null);
        flowerWaitRunnable = null;
        routeTasks.close();
        searchTasks.close();
        if (mapView != null) mapView.release();
        engine.detach(this);
        super.onDetachedFromWindow();
    }

    void onHostForegroundChanged(boolean foreground) {
        hostForeground = foreground;
        syncMapActivity();
        if (foreground && pageSelected) onSimulationUpdate(engine.snapshot());
    }

    void onPageSelected(boolean selected) {
        pageSelected = selected;
        syncMapActivity();
        if (selected && hostForeground) onSimulationUpdate(engine.snapshot());
    }

    private void syncMapActivity() {
        if (mapView != null) {
            mapView.setActive(!detached && hostForeground && pageSelected);
        }
    }

    void refresh() {
        MockLocationKeeper.Snapshot saved = MockLocationKeeper.snapshot(activity);
        if (saved.revision != displayedRevision) {
            displayedRevision = saved.revision;
            if (saved.active && !latitudeInput.hasFocus() && !longitudeInput.hasFocus()) {
                selectedPoint = new LocalSimulationEngine.Point(saved.lat, saved.lng);
                setCoordinateInputs(selectedPoint);
            }
        }
        syncMockSwitch();
        syncMapHintSwitch();
        onSimulationUpdate(engine.snapshot());
        refreshLibrary();
        refreshFlowerTargets();
        maybeStartQueuedFlowerTarget();
    }

    void setBottomContentInset(int bottomPx) {
        if (contentRoot == null) return;
        contentRoot.setPadding(dp(16), dp(16), dp(16), Math.max(dp(24), bottomPx));
    }

    private void build() {
        LinearLayout root = new LinearLayout(activity);
        contentRoot = root;
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(dp(16), dp(16), dp(16), dp(124));
        addView(root, new ScrollView.LayoutParams(
            LayoutParams.MATCH_PARENT,
            LayoutParams.WRAP_CONTENT
        ));

        TextView title = label("定位模擬", 24, TEXT, true);
        title.setPadding(0, 0, 0, dp(12));
        root.addView(title);

        root.addView(buildMockSwitchCard(), cardLp(0));
        root.addView(buildMapCard(), cardLp(12));
        root.addView(buildModeCard(), cardLp(12));

        modeOptions = new LinearLayout(activity);
        modeOptions.setOrientation(LinearLayout.VERTICAL);
        root.addView(modeOptions);

        root.addView(buildActionCard(), cardLp(12));

        movementSettingsPanel = new LinearLayout(activity);
        movementSettingsPanel.setOrientation(LinearLayout.VERTICAL);
        movementSettingsPanel.addView(buildSpeedCard());
        movementSettingsPanel.addView(buildSafetyCard(), cardLp(10));
        movementSettingsPanel.setVisibility(View.GONE);
        root.addView(
            disclosureButton("移動與安全設定", movementSettingsPanel),
            cardLp(12)
        );
        root.addView(movementSettingsPanel, cardLp(10));

        LinearLayout libraryCard = card();
        libraryCardView = libraryCard;
        libraryCard.addView(sectionHeading("本機收藏與路線"));
        libraryContent = new LinearLayout(activity);
        libraryContent.setOrientation(LinearLayout.VERTICAL);
        LinearLayout.LayoutParams libraryLp = matchWrap();
        libraryLp.setMargins(0, dp(10), 0, 0);
        libraryCard.addView(libraryContent, libraryLp);
        libraryCard.setVisibility(View.GONE);
        root.addView(
            disclosureButton("收藏與路線", libraryCard),
            cardLp(10)
        );
        root.addView(libraryCard, cardLp(10));
        root.addView(buildBackupCard(), cardLp(12));
        selectMode("navigate");
        refreshLibrary();
        refresh();
    }

    private LinearLayout buildBackupCard() {
        LinearLayout backupCard = card();
        backupCard.addView(sectionHeading("備份與還原"));
        TextView note = label(
            "匯出收藏、路線、Hook、顯示與安全設定。"
                + "Relay 憑證與執行中的定位工作不會寫入備份。",
            12,
            MUTED,
            false
        );
        note.setPadding(0, dp(8), 0, 0);
        note.setLineSpacing(dp(2), 1f);
        backupCard.addView(note);
        LinearLayout actions = horizontal();
        actions.setPadding(0, dp(10), 0, 0);
        Button export = secondaryButton("匯出完整備份");
        export.setOnClickListener(v -> activity.launchBackupExport());
        Button restore = secondaryButton("從備份還原");
        restore.setOnClickListener(v -> activity.launchBackupImport());
        addEqualButtons(actions, export, restore);
        backupCard.addView(actions);
        return backupCard;
    }

    private LinearLayout buildMockSwitchCard() {
        LinearLayout toggleCard = card();
        LinearLayout row = horizontal();
        toggleCard.addView(row, matchWrap());

        LinearLayout copy = new LinearLayout(activity);
        copy.setOrientation(LinearLayout.VERTICAL);
        row.addView(copy, new LinearLayout.LayoutParams(
            0,
            LayoutParams.WRAP_CONTENT,
            1f
        ));
        copy.addView(label("位置模擬", 17, TEXT, true));
        mockSwitchHint = label("", 12, MUTED, false);
        mockSwitchHint.setPadding(0, dp(3), dp(10), 0);
        copy.addView(mockSwitchHint);

        mockSwitch = new Switch(activity);
        mockSwitch.setShowText(false);
        mockSwitch.setContentDescription("開啟或關閉位置模擬");
        mockSwitch.setThumbTintList(new android.content.res.ColorStateList(
            new int[][] {
                new int[] {android.R.attr.state_checked},
                new int[] {}
            },
            new int[] {
                Color.WHITE,
                Color.parseColor("#a4a9b8")
            }
        ));
        mockSwitch.setTrackTintList(new android.content.res.ColorStateList(
            new int[][] {
                new int[] {android.R.attr.state_checked},
                new int[] {}
            },
            new int[] {
                Color.parseColor(ACCENT),
                Color.parseColor("#454b5d")
            }
        ));
        row.addView(mockSwitch, new LinearLayout.LayoutParams(dp(64), dp(48)));
        mockSwitch.setOnCheckedChangeListener((button, checked) -> {
            if (syncingMockSwitch) return;
            setSimulationEnabled(checked);
        });
        syncMockSwitch();

        statusBody = label("目前沒有執行中的移動", 12, MUTED, false);
        statusBody.setPadding(0, dp(10), 0, 0);
        statusBody.setLineSpacing(dp(3), 1f);
        toggleCard.addView(statusBody);
        return toggleCard;
    }

    private LinearLayout buildMapCard() {
        LinearLayout mapCard = card();
        mapCard.addView(sectionHeading("互動地圖"));

        LinearLayout searchRow = horizontal();
        searchRow.setPadding(0, dp(10), 0, 0);
        searchInput = textInput("搜尋地名或地址");
        searchRow.addView(searchInput, new LinearLayout.LayoutParams(0, dp(46), 1f));
        Button searchButton = secondaryButton("搜尋");
        searchButton.setOnClickListener(v -> searchAddress());
        LinearLayout.LayoutParams searchButtonLp =
            new LinearLayout.LayoutParams(dp(72), dp(46));
        searchButtonLp.setMargins(dp(8), 0, 0, 0);
        searchRow.addView(searchButton, searchButtonLp);
        mapCard.addView(searchRow);

        mapView = new LocalMapView(activity);
        mapView.setListener(this);
        mapView.setBackgroundColor(Color.parseColor("#10131c"));
        LinearLayout.LayoutParams mapLp = matchHeight(270);
        mapLp.setMargins(0, dp(10), 0, 0);
        mapCard.addView(mapView, mapLp);

        LinearLayout quickActions = horizontal();
        quickActions.setPadding(0, dp(10), 0, 0);
        Button teleport = primaryButton("瞬移到選取位置");
        teleport.setOnClickListener(v -> runTeleportShortcut());
        quickActions.addView(teleport, new LinearLayout.LayoutParams(
            LayoutParams.MATCH_PARENT,
            dp(50)
        ));
        mapCard.addView(quickActions);

        mapAdvancedPanel = new LinearLayout(activity);
        mapAdvancedPanel.setOrientation(LinearLayout.VERTICAL);
        mapAdvancedPanel.setVisibility(View.GONE);
        LinearLayout.LayoutParams toolsButtonLp = matchHeight(48);
        toolsButtonLp.setMargins(0, dp(8), 0, 0);
        mapCard.addView(
            disclosureButton("座標與地圖工具", mapAdvancedPanel),
            toolsButtonLp
        );
        mapCard.addView(mapAdvancedPanel, matchWrap());

        LinearLayout coordRow = horizontal();
        coordRow.setPadding(0, dp(10), 0, 0);
        latitudeInput = numberInput("緯度");
        longitudeInput = numberInput("經度");
        coordRow.addView(latitudeInput, new LinearLayout.LayoutParams(0, dp(48), 1f));
        LinearLayout.LayoutParams lngLp = new LinearLayout.LayoutParams(0, dp(48), 1f);
        lngLp.setMargins(dp(8), 0, 0, 0);
        coordRow.addView(longitudeInput, lngLp);
        Button paste = secondaryButton("貼上");
        paste.setOnClickListener(v -> pasteCoordinateFromClipboard());
        LinearLayout.LayoutParams pasteLp =
            new LinearLayout.LayoutParams(dp(66), dp(48));
        pasteLp.setMargins(dp(8), 0, 0, 0);
        coordRow.addView(paste, pasteLp);
        mapAdvancedPanel.addView(coordRow);
        setCoordinateInputs(selectedPoint);

        LinearLayout mapActions = horizontal();
        mapActions.setPadding(0, dp(8), 0, 0);
        Button preview = secondaryButton("地圖定位");
        preview.setOnClickListener(v -> {
            LocalSimulationEngine.Point point = readCoordinate();
            if (point == null) return;
            selectPoint(point, true);
        });
        Button waypoint = secondaryButton("加入節點");
        waypoint.setOnClickListener(v -> addSelectedWaypoint());
        Button bookmark = secondaryButton("加入收藏");
        bookmark.setOnClickListener(v -> addBookmark());
        addEqualButtons(mapActions, preview, waypoint, bookmark);
        mapAdvancedPanel.addView(mapActions);

        LinearLayout mapToggleRow = horizontal();
        mapToggleRow.setPadding(dp(10), dp(8), dp(10), dp(8));
        mapToggleRow.setBackground(roundRect(ELEVATED, BORDER, 12));
        TextView mapToggleText = label("浮動地圖", 13, "#d9deec", true);
        mapToggleRow.addView(mapToggleText, new LinearLayout.LayoutParams(
            0,
            LayoutParams.WRAP_CONTENT,
            1f
        ));
        mapHintSwitch = new Switch(activity);
        mapHintSwitch.setTextColor(Color.parseColor("#cbd1e2"));
        mapHintSwitch.setTextSize(12);
        mapHintSwitch.setOnCheckedChangeListener((button, enabled) -> {
            if (syncingMapHintSwitch) return;
            setMapHintOverlayVisible(enabled);
        });
        mapToggleRow.addView(mapHintSwitch, new LinearLayout.LayoutParams(
            LayoutParams.WRAP_CONTENT,
            LayoutParams.WRAP_CONTENT
        ));
        LinearLayout.LayoutParams mapToggleLp = matchWrap();
        mapToggleLp.setMargins(0, dp(10), 0, 0);
        mapAdvancedPanel.addView(mapToggleRow, mapToggleLp);

        LinearLayout interceptRow = horizontal();
        interceptRow.setPadding(dp(10), dp(8), dp(10), dp(8));
        interceptRow.setBackground(roundRect(ELEVATED, BORDER, 12));
        LinearLayout interceptText = new LinearLayout(activity);
        interceptText.setOrientation(LinearLayout.VERTICAL);
        interceptText.addView(label("外部地圖連結直接套用", 13, "#d9deec", true));
        interceptText.addView(label("關閉時會照常轉交 Google Maps", 12, MUTED, false));
        interceptRow.addView(interceptText, new LinearLayout.LayoutParams(
            0,
            LayoutParams.WRAP_CONTENT,
            1f
        ));
        mapIntentInterceptSwitch = new Switch(activity);
        mapIntentInterceptSwitch.setChecked(
            OverlaySettings.isMapIntentInterceptEnabled(activity)
        );
        mapIntentInterceptSwitch.setContentDescription("允許外部地圖連結直接套用位置");
        mapIntentInterceptSwitch.setOnCheckedChangeListener((button, enabled) -> {
            OverlaySettings.setMapIntentInterceptEnabled(activity, enabled);
            toast(enabled ? "外部地圖連結攔截已開啟" : "外部地圖連結將轉交 Google Maps");
        });
        interceptRow.addView(mapIntentInterceptSwitch, new LinearLayout.LayoutParams(
            LayoutParams.WRAP_CONTENT,
            LayoutParams.WRAP_CONTENT
        ));
        LinearLayout.LayoutParams interceptLp = matchWrap();
        interceptLp.setMargins(0, dp(8), 0, 0);
        mapAdvancedPanel.addView(interceptRow, interceptLp);

        LinearLayout tilePanel = new LinearLayout(activity);
        tilePanel.setOrientation(LinearLayout.VERTICAL);
        tilePanel.setPadding(dp(10), dp(10), dp(10), dp(10));
        tilePanel.setBackground(roundRect(ELEVATED, BORDER, 12));
        tilePanel.addView(label("地圖來源與離線快取", 13, "#d9deec", true));
        mapTileStatus = label("", 12, MUTED, false);
        mapTileStatus.setPadding(0, dp(4), 0, 0);
        tilePanel.addView(mapTileStatus);
        TextView tilePolicy = label(
            "OpenStreetMap 僅快取實際瀏覽內容；純離線模式需使用允許離線的自訂或自架來源。",
            11,
            MUTED,
            false
        );
        tilePolicy.setPadding(0, dp(4), 0, 0);
        tilePanel.addView(tilePolicy);
        LinearLayout tileActions = horizontal();
        tileActions.setPadding(0, dp(8), 0, 0);
        Button tileSettings = secondaryButton("來源設定");
        tileSettings.setOnClickListener(v -> showMapTileSettings());
        Button clearTiles = secondaryButton("清除快取");
        clearTiles.setOnClickListener(v -> confirmClearMapTiles());
        addEqualButtons(tileActions, tileSettings, clearTiles);
        tilePanel.addView(tileActions);
        LinearLayout.LayoutParams tilePanelLp = matchWrap();
        tilePanelLp.setMargins(0, dp(8), 0, 0);
        mapAdvancedPanel.addView(tilePanel, tilePanelLp);
        updateMapTileStatus();

        mapHintOptionsPanel = new LinearLayout(activity);
        mapHintOptionsPanel.setOrientation(LinearLayout.VERTICAL);
        LinearLayout.LayoutParams optionsLp = matchWrap();
        optionsLp.setMargins(0, dp(8), 0, 0);
        mapAdvancedPanel.addView(mapHintOptionsPanel, optionsLp);

        LinearLayout mapSize = overlaySizeRow(
            "浮動地圖大小",
            false,
            () -> adjustMapHintOverlay(-OverlaySettings.MAP_HINT_STEP_DP),
            () -> adjustMapHintOverlay(OverlaySettings.MAP_HINT_STEP_DP)
        );
        LinearLayout.LayoutParams mapSizeLp = matchHeight(52);
        mapHintOptionsPanel.addView(mapSize, mapSizeLp);

        updateOverlaySizeLabels();
        syncMapHintSwitch();
        return mapCard;
    }

    private void showMapTileSettings() {
        String mode = OverlaySettings.mapTileMode(activity);
        String template = OverlaySettings.mapTileTemplate(activity);
        boolean custom = !OverlaySettings.DEFAULT_MAP_TILE_TEMPLATE.equals(template);
        int selected = custom
            ? 2
            : OverlaySettings.MAP_TILE_MODE_OFFLINE_ONLY.equals(mode) ? 1 : 0;
        String[] options = new String[] {
            "OpenStreetMap（線上並自動快取）",
            "僅使用目前的離線快取",
            "自訂 HTTPS 圖磚來源"
        };
        new AlertDialog.Builder(activity)
            .setTitle("地圖來源")
            .setSingleChoiceItems(options, selected, (dialog, which) -> {
                dialog.dismiss();
                if (which == 0) {
                    applyMapTileSettings(
                        OverlaySettings.MAP_TILE_MODE_ONLINE_CACHE,
                        OverlaySettings.DEFAULT_MAP_TILE_TEMPLATE
                    );
                } else if (which == 1) {
                    if (OverlaySettings.DEFAULT_MAP_TILE_TEMPLATE.equals(template)) {
                        showError(
                            "OpenStreetMap 標準圖磚不允許離線模式。"
                                + "請先設定允許離線使用的自訂或自架 HTTPS 圖磚來源。"
                        );
                        return;
                    }
                    applyMapTileSettings(
                        OverlaySettings.MAP_TILE_MODE_OFFLINE_ONLY,
                        template
                    );
                } else {
                    showCustomMapTileTemplate(template);
                }
            })
            .setNegativeButton("取消", null)
            .show();
    }

    private void showCustomMapTileTemplate(String current) {
        EditText input = textInput("https://example.com/{z}/{x}/{y}.png");
        input.setText(current);
        input.setSelectAllOnFocus(true);
        EditText attribution = textInput("圖資標示，例如 © Example Maps");
        attribution.setText(OverlaySettings.mapTileAttribution(activity));
        int padding = dp(20);
        LinearLayout holder = new LinearLayout(activity);
        holder.setOrientation(LinearLayout.VERTICAL);
        holder.setPadding(padding, dp(8), padding, 0);
        holder.addView(input, new LinearLayout.LayoutParams(
            LayoutParams.MATCH_PARENT,
            dp(52)
        ));
        LinearLayout.LayoutParams attributionLp = new LinearLayout.LayoutParams(
            LayoutParams.MATCH_PARENT,
            dp(52)
        );
        attributionLp.setMargins(0, dp(8), 0, 0);
        holder.addView(attribution, attributionLp);
        AlertDialog dialog = new AlertDialog.Builder(activity)
            .setTitle("自訂圖磚 URL")
            .setMessage("必須使用 HTTPS，並包含 {z}、{x}、{y}。請確認來源允許此用法。")
            .setView(holder)
            .setPositiveButton("套用", null)
            .setNegativeButton("取消", null)
            .create();
        dialog.setOnShowListener(ignored -> dialog.getButton(
            AlertDialog.BUTTON_POSITIVE
        ).setOnClickListener(v -> {
            String value = input.getText().toString().trim();
            if (!OverlaySettings.validMapTileTemplate(value)) {
                input.setError("請輸入有效的 HTTPS 圖磚 URL 範本");
                return;
            }
            String attributionText = attribution.getText().toString().trim();
            if (attributionText.isEmpty()) {
                attribution.setError("請輸入圖磚供應者要求的圖資標示");
                return;
            }
            OverlaySettings.setMapTileSettings(
                activity,
                OverlaySettings.MAP_TILE_MODE_ONLINE_CACHE,
                value,
                attributionText
            );
            if (mapView != null) mapView.reloadTiles();
            updateMapTileStatus();
            toast("自訂地圖來源已套用");
            dialog.dismiss();
        }));
        dialog.show();
    }

    private void applyMapTileSettings(String mode, String template) {
        OverlaySettings.setMapTileSettings(activity, mode, template);
        if (mapView != null) mapView.reloadTiles();
        updateMapTileStatus();
        toast(OverlaySettings.MAP_TILE_MODE_OFFLINE_ONLY.equals(mode)
            ? "已切換為純離線地圖"
            : "地圖來源已套用");
    }

    private void confirmClearMapTiles() {
        new AlertDialog.Builder(activity)
            .setTitle("清除離線地圖快取？")
            .setMessage("已瀏覽並下載的圖磚會被移除。收藏、路線與座標不受影響。")
            .setPositiveButton("清除", (dialog, which) -> new Thread(() -> {
                new MapTileCache(activity).clear();
                mainHandler.post(() -> {
                    if (mapView != null) mapView.reloadTiles();
                    updateMapTileStatus();
                    toast("離線地圖快取已清除");
                });
            }, "LocWarpTileClear").start())
            .setNegativeButton("取消", null)
            .show();
    }

    private void updateMapTileStatus() {
        if (mapTileStatus == null) return;
        new Thread(() -> {
            MapTileCache.CacheStats stats = new MapTileCache(activity).stats();
            String mode = OverlaySettings.MAP_TILE_MODE_OFFLINE_ONLY.equals(
                OverlaySettings.mapTileMode(activity)
            ) ? "純離線" : "線上＋快取";
            boolean custom = !OverlaySettings.DEFAULT_MAP_TILE_TEMPLATE.equals(
                OverlaySettings.mapTileTemplate(activity)
            );
            String summary = mode
                + (custom ? " · 自訂來源" : " · OpenStreetMap")
                + " · " + stats.tileCount + " 張圖磚"
                + String.format(Locale.TAIWAN, "（%.1f MB）", stats.bytes / 1048576d);
            mainHandler.post(() -> {
                if (mapTileStatus != null) mapTileStatus.setText(summary);
            });
        }, "LocWarpTileStats").start();
    }

    private LinearLayout buildStatusCard() {
        LinearLayout statusCard = card();
        statusCard.addView(sectionHeading("目前狀態"));
        statusBody = label("尚未啟用模擬位置", 13, MUTED, false);
        statusBody.setPadding(0, dp(10), 0, 0);
        statusBody.setLineSpacing(dp(3), 1f);
        statusCard.addView(statusBody);
        return statusCard;
    }

    private LinearLayout buildModeCard() {
        LinearLayout modeCard = card();
        modeCard.addView(sectionHeading("移動模式"));
        String[][] modes = {
            {"navigate", "➤", "導航"},
            {"loop", "↻", "循環"},
            {"multistop", "⌘", "多點"},
            {"randomwalk", "⌁", "隨機"},
            {"joystick", "✥", "搖桿"},
            {"flower", "✿", "種花"}
        };
        int itemIndex = 0;
        int rowIndex = 0;
        while (itemIndex < modes.length) {
            LinearLayout row = horizontal();
            LinearLayout.LayoutParams rowLp = matchWrap();
            rowLp.setMargins(0, rowIndex == 0 ? dp(10) : dp(8), 0, 0);
            modeCard.addView(row, rowLp);
            int count = Math.min(3, modes.length - itemIndex);
            for (int column = 0; column < count; column++) {
                String[] item = modes[itemIndex++];
                Button button = compactButton(item[1] + "\n" + item[2]);
                button.setOnClickListener(v -> selectMode(item[0]));
                LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(0, dp(64), 1f);
                if (column > 0) lp.setMargins(dp(8), 0, 0, 0);
                row.addView(button, lp);
                modeButtons.put(item[0], button);
            }
            rowIndex++;
        }
        return modeCard;
    }

    private LinearLayout buildSpeedCard() {
        LinearLayout speedCard = card();
        speedCard.addView(sectionHeading("移動設定"));

        LinearLayout speedRow = horizontal();
        speedRow.setPadding(0, dp(10), 0, 0);
        TextView speedLabel = label("速率 km/h", 12, MUTED, true);
        speedLabel.setGravity(Gravity.CENTER_VERTICAL);
        speedRow.addView(speedLabel, new LinearLayout.LayoutParams(dp(82), dp(48)));

        speedInput = numberInput("例如 10.8");
        speedInput.setText(String.format(Locale.US, "%.1f", engine.snapshot().speedKmh));
        speedInput.setOnFocusChangeListener((view, hasFocus) -> {
            if (!hasFocus) applySpeed();
        });
        speedRow.addView(speedInput, new LinearLayout.LayoutParams(0, dp(48), 1f));

        Button saveSpeed = secondaryButton("設定");
        saveSpeed.setOnClickListener(v -> {
            applySpeed();
            toast("速率已設定");
        });
        LinearLayout.LayoutParams saveLp = new LinearLayout.LayoutParams(0, dp(48), 1f);
        saveLp.setMargins(dp(8), 0, 0, 0);
        speedRow.addView(saveSpeed, saveLp);

        speedCard.addView(speedRow, matchWrap());

        LinearLayout straightRow = horizontal();
        straightRow.setPadding(dp(10), dp(8), dp(10), dp(8));
        straightRow.setBackground(roundRect(ELEVATED, BORDER, 12));
        LinearLayout straightText = new LinearLayout(activity);
        straightText.setOrientation(LinearLayout.VERTICAL);
        straightRow.addView(straightText, new LinearLayout.LayoutParams(
            0,
            LayoutParams.WRAP_CONTENT,
            1f
        ));
        straightText.addView(label("直線移動", 13, "#d9deec", true));
        straightLineSwitch = new Switch(activity);
        straightLineSwitch.setChecked(OverlaySettings.isStraightLineMovement(activity));
        straightLineSwitch.setOnCheckedChangeListener((button, enabled) -> {
            OverlaySettings.setStraightLineMovement(activity, enabled);
            toast(enabled ? "已切換為直線移動" : "已切換為路徑計算");
        });
        straightRow.addView(straightLineSwitch, new LinearLayout.LayoutParams(
            LayoutParams.WRAP_CONTENT,
            LayoutParams.WRAP_CONTENT
        ));
        LinearLayout.LayoutParams straightLp = matchWrap();
        straightLp.setMargins(0, dp(8), 0, 0);
        speedCard.addView(straightRow, straightLp);
        TextView routingPrivacy = label(
            "使用道路路徑時，起點與終點會送至公開路由服務；直線移動不會。",
            12,
            MUTED,
            false
        );
        routingPrivacy.setPadding(dp(2), dp(8), dp(2), 0);
        speedCard.addView(routingPrivacy);
        return speedCard;
    }

    private LinearLayout buildSafetyCard() {
        LinearLayout panel = card();
        panel.addView(sectionHeading("安全停止與工作恢復"));
        TextView note = label(
            "可設定移動工作最長時間；完成或逾時後可自動恢復真實定位。",
            12,
            MUTED,
            false
        );
        note.setPadding(0, dp(8), 0, 0);
        panel.addView(note);

        autoStopMinutesInput = numberInput("自動停止分鐘（0＝停用，最多 1440）");
        autoStopMinutesInput.setText(String.valueOf(
            OverlaySettings.safetyAutoStopMinutes(activity)
        ));
        LinearLayout.LayoutParams inputLp = matchHeight(48);
        inputLp.setMargins(0, dp(10), 0, 0);
        panel.addView(autoStopMinutesInput, inputLp);

        restoreOnFinishCheck = checkbox("工作完成或安全逾時後恢復真實定位");
        restoreOnFinishCheck.setChecked(
            OverlaySettings.restoreOnMovementFinish(activity)
        );
        panel.addView(restoreOnFinishCheck);
        resumeAfterRestartCheck = checkbox("程序重啟後恢復未完成的移動工作");
        resumeAfterRestartCheck.setChecked(
            OverlaySettings.resumeMovementAfterRestart(activity)
        );
        panel.addView(resumeAfterRestartCheck);

        Button apply = secondaryButton("套用安全設定");
        apply.setOnClickListener(v -> applyMovementSafety());
        LinearLayout.LayoutParams applyLp = matchHeight(46);
        applyLp.setMargins(0, dp(8), 0, 0);
        panel.addView(apply, applyLp);
        return panel;
    }

    private void applyMovementSafety() {
        int minutes = Math.min(
            24 * 60,
            Math.max(0, integerOr(autoStopMinutesInput, 0))
        );
        boolean restore = restoreOnFinishCheck != null
            && restoreOnFinishCheck.isChecked();
        boolean resume = resumeAfterRestartCheck == null
            || resumeAfterRestartCheck.isChecked();
        autoStopMinutesInput.setText(String.valueOf(minutes));
        OverlaySettings.setMovementSafety(
            activity,
            minutes,
            restore,
            resume
        );
        engine.configureSafety(minutes, restore);
        toast("安全設定已套用");
    }

    private LinearLayout buildActionCard() {
        LinearLayout actionCard = card();
        actionCardView = actionCard;
        actionCard.addView(sectionHeading("控制"));

        controlStatus = label("目前沒有執行中移動", 12, MUTED, false);
        controlStatus.setPadding(0, dp(10), 0, 0);
        actionCard.addView(controlStatus);

        LinearLayout controlRow = horizontal();
        controlRow.setPadding(0, dp(10), 0, 0);

        primaryAction = primaryButton("開始導航");
        primaryAction.setOnClickListener(v -> runPrimaryAction());
        LinearLayout.LayoutParams primaryLp = new LinearLayout.LayoutParams(0, dp(54), 2f);
        controlRow.addView(primaryAction, primaryLp);

        stopAction = secondaryButton("終止");
        stopAction.setTextColor(Color.parseColor("#ff9aa2"));
        stopAction.setTypeface(Typeface.DEFAULT_BOLD);
        stopAction.setOnClickListener(v -> stopActiveMotion());
        LinearLayout.LayoutParams stopLp = new LinearLayout.LayoutParams(0, dp(54), 1f);
        stopLp.setMargins(dp(8), 0, 0, 0);
        controlRow.addView(stopAction, stopLp);

        actionCard.addView(controlRow);

        return actionCard;
    }

    private void selectMode(String mode) {
        if (isFlowerModeActive() && !"flower".equals(mode)) {
            toast("種花模式執行中，請先關閉");
            return;
        }
        selectedMode = mode;
        boolean isJoystick = "joystick".equals(mode);
        for (Map.Entry<String, Button> entry : modeButtons.entrySet()) {
            styleSelectable(entry.getValue(), entry.getKey().equals(mode));
        }
        syncModeButtons();
        if (!isJoystick) {
            activity.hideFloatingJoystick();
        }
        if (actionCardView != null) {
            actionCardView.setVisibility(isJoystick ? View.GONE : View.VISIBLE);
        }
        rebuildModeOptions();
        LocalSimulationEngine.Snapshot snapshot = engine.snapshot();
        onSimulationUpdate(snapshot);
        updateActionControls(snapshot);
        if (isJoystick) {
            post(this::startFloatingJoystick);
        }
    }

    private void syncModeButtons() {
        boolean locked = isFlowerModeActive();
        for (Map.Entry<String, Button> entry : modeButtons.entrySet()) {
            Button button = entry.getValue();
            boolean isFlower = "flower".equals(entry.getKey());
            button.setEnabled(!locked || isFlower);
            button.setAlpha(!locked || isFlower ? 1f : 0.42f);
        }
    }

    private void rebuildModeOptions() {
        modeOptions.removeAllViews();
        draftSummary = null;
        routeWaypointContent = null;
        radiusInput = null;
        stopDurationInput = null;
        lapCountInput = null;
        routeLoopCheck = null;
        pauseCheck = null;
        pauseMinInput = null;
        pauseMaxInput = null;
        flowerRadiusInput = null;
        flowerSpacingInput = null;
        flowerSummary = null;
        flowerTargetContent = null;

        if ("loop".equals(selectedMode) || "multistop".equals(selectedMode)) {
            LinearLayout routeCard = card();
            routeCard.addView(sectionHeading("路線節點"));
            draftSummary = label("", 13, "#cbd1e2", true);
            draftSummary.setPadding(0, dp(10), 0, 0);
            routeCard.addView(draftSummary);

            routeWaypointContent = new LinearLayout(activity);
            routeWaypointContent.setOrientation(LinearLayout.VERTICAL);
            LinearLayout.LayoutParams listLp = matchWrap();
            listLp.setMargins(0, dp(10), 0, 0);
            routeCard.addView(routeWaypointContent, listLp);

            LinearLayout buttons = horizontal();
            buttons.setPadding(0, dp(8), 0, 0);
            Button importButton = secondaryButton("匯入");
            importButton.setOnClickListener(v -> importBulkRoute());
            Button exportButton = secondaryButton("匯出");
            exportButton.setOnClickListener(v -> exportDraftRoute());
            Button reverseButton = secondaryButton("反向");
            reverseButton.setOnClickListener(v -> {
                java.util.Collections.reverse(draftWaypoints);
                updateDraftRoute();
            });
            Button clearButton = secondaryButton("清空節點");
            clearButton.setOnClickListener(v -> {
                draftWaypoints.clear();
                updateDraftRoute();
            });
            Button saveButton = secondaryButton("儲存路線");
            saveButton.setOnClickListener(v -> saveDraftRoute());
            addEqualButtons(buttons, importButton, exportButton, reverseButton);
            routeCard.addView(buttons);
            LinearLayout secondButtons = horizontal();
            secondButtons.setPadding(0, dp(8), 0, 0);
            addEqualButtons(secondButtons, clearButton, saveButton);
            routeCard.addView(secondButtons);

            if ("loop".equals(selectedMode)) {
                lapCountInput = numberInput("圈數（留空＝持續循環）");
                LinearLayout.LayoutParams lapLp = matchHeight(48);
                lapLp.setMargins(0, dp(8), 0, 0);
                routeCard.addView(lapCountInput, lapLp);
            } else {
                stopDurationInput = numberInput("每站停留秒數");
                stopDurationInput.setText("0");
                LinearLayout.LayoutParams stopLp = matchHeight(48);
                stopLp.setMargins(0, dp(8), 0, 0);
                routeCard.addView(stopDurationInput, stopLp);
                routeLoopCheck = checkbox("完成後重複路線");
                routeCard.addView(routeLoopCheck);
            }
            pauseCheck = checkbox(
                "loop".equals(selectedMode)
                    ? "每個節點隨機停留"
                    : "每站額外隨機停留"
            );
            routeCard.addView(pauseCheck);
            LinearLayout pauseRange = horizontal();
            pauseRange.setPadding(0, dp(8), 0, 0);
            pauseMinInput = numberInput("最短秒數");
            pauseMinInput.setText("5");
            pauseMaxInput = numberInput("最長秒數");
            pauseMaxInput.setText("20");
            pauseRange.addView(pauseMinInput, new LinearLayout.LayoutParams(0, dp(48), 1f));
            LinearLayout.LayoutParams pauseMaxLp =
                new LinearLayout.LayoutParams(0, dp(48), 1f);
            pauseMaxLp.setMargins(dp(8), 0, 0, 0);
            pauseRange.addView(pauseMaxInput, pauseMaxLp);
            routeCard.addView(pauseRange);
            modeOptions.addView(routeCard, cardLp(12));
            updateDraftRoute();
        } else if ("randomwalk".equals(selectedMode)) {
            LinearLayout randomCard = card();
            randomCard.addView(sectionHeading("隨機漫步"));
            radiusInput = numberInput("半徑（公尺）");
            radiusInput.setText("500");
            LinearLayout.LayoutParams radiusLp = matchHeight(48);
            radiusLp.setMargins(0, dp(10), 0, 0);
            randomCard.addView(radiusInput, radiusLp);
            pauseCheck = checkbox("每段抵達後隨機停留");
            pauseCheck.setChecked(true);
            randomCard.addView(pauseCheck);
            LinearLayout pauseRange = horizontal();
            pauseRange.setPadding(0, dp(8), 0, 0);
            pauseMinInput = numberInput("最短秒數");
            pauseMinInput.setText("5");
            pauseMaxInput = numberInput("最長秒數");
            pauseMaxInput.setText("20");
            pauseRange.addView(pauseMinInput, new LinearLayout.LayoutParams(0, dp(48), 1f));
            LinearLayout.LayoutParams maxLp = new LinearLayout.LayoutParams(0, dp(48), 1f);
            maxLp.setMargins(dp(8), 0, 0, 0);
            pauseRange.addView(pauseMaxInput, maxLp);
            randomCard.addView(pauseRange);
            modeOptions.addView(randomCard, cardLp(12));
        } else if ("joystick".equals(selectedMode)) {
            LinearLayout joystickCard = card();
            joystickCard.addView(sectionHeading("浮動方向搖桿"));

            LinearLayout joystickSize = overlaySizeRow(
                "搖桿大小",
                true,
                () -> adjustJoystickOverlay(-OverlaySettings.JOYSTICK_STEP_DP),
                () -> adjustJoystickOverlay(OverlaySettings.JOYSTICK_STEP_DP)
            );
            LinearLayout.LayoutParams joystickSizeLp = matchHeight(52);
            joystickSizeLp.setMargins(0, dp(12), 0, 0);
            joystickCard.addView(joystickSize, joystickSizeLp);

            updateOverlaySizeLabels();
            modeOptions.addView(joystickCard, cardLp(12));
        } else if ("flower".equals(selectedMode)) {
            LinearLayout flowerCard = card();
            flowerCard.addView(sectionHeading("種花模式"));
            flowerSummary = label("", 13, "#cbd1e2", false);
            flowerSummary.setPadding(0, dp(10), 0, 0);
            flowerSummary.setLineSpacing(dp(3), 1f);
            flowerCard.addView(flowerSummary);

            LinearLayout settings = horizontal();
            settings.setPadding(0, dp(8), 0, 0);
            flowerRadiusInput = numberInput("種花半徑 m");
            flowerRadiusInput.setText(String.format(Locale.US, "%.0f", FLOWER_DEFAULT_RADIUS_M));
            flowerSpacingInput = numberInput("掃描間距 m");
            flowerSpacingInput.setText(String.format(Locale.US, "%.0f", FLOWER_DEFAULT_SPACING_M));
            settings.addView(flowerRadiusInput, new LinearLayout.LayoutParams(0, dp(48), 1f));
            LinearLayout.LayoutParams spacingLp =
                new LinearLayout.LayoutParams(0, dp(48), 1f);
            spacingLp.setMargins(dp(8), 0, 0, 0);
            settings.addView(flowerSpacingInput, spacingLp);
            flowerCard.addView(settings);

            LinearLayout buttons = horizontal();
            buttons.setPadding(0, dp(8), 0, 0);
            Button importTargets = secondaryButton("匯入文字");
            importTargets.setOnClickListener(v -> importFlowerTargets());
            Button clearTargets = secondaryButton("清空清單");
            clearTargets.setOnClickListener(v -> clearFlowerTargets());
            addEqualButtons(buttons, importTargets, clearTargets);
            flowerCard.addView(buttons);

            flowerTargetContent = new LinearLayout(activity);
            flowerTargetContent.setOrientation(LinearLayout.VERTICAL);
            LinearLayout.LayoutParams listLp = matchWrap();
            listLp.setMargins(0, dp(10), 0, 0);
            flowerCard.addView(flowerTargetContent, listLp);
            modeOptions.addView(flowerCard, cardLp(12));
            refreshFlowerTargets();
        }
    }

    private void runPrimaryAction() {
        if (isFlowerModeActive()) {
            toast("種花模式執行中，請先關閉");
            return;
        }
        if (routePlanning) {
            toast("路徑計算中，請稍候");
            return;
        }
        LocalSimulationEngine.Snapshot snapshot = engine.snapshot();
        if (snapshot.running) {
            if (snapshot.paused) {
                engine.resume();
                toast("已繼續移動");
            } else {
                engine.pause();
                toast("已暫停移動");
            }
            updateActionControls(engine.snapshot());
            return;
        }
        if (!ensureSimulationEnabled()) return;
        if (!activity.ensureMockPermission()) return;
        applySpeed();
        LocalSimulationEngine.Point point = readCoordinate();
        if (point != null) selectedPoint = point;
        if ("joystick".equals(selectedMode)) {
            startFloatingJoystick();
            return;
        }
        if ("flower".equals(selectedMode)) {
            startFlowerMode();
            return;
        }

        boolean straightLine = OverlaySettings.isStraightLineMovement(activity);
        if (!straightLine) {
            if ("navigate".equals(selectedMode)) {
                startPathNavigationToPoint(selectedPoint);
                return;
            }
            if ("loop".equals(selectedMode)) {
                startCalculatedLoop();
                return;
            }
            if ("multistop".equals(selectedMode)) {
                startCalculatedMultiStop();
                return;
            }
        }

        boolean started;
        switch (selectedMode) {
            case "navigate":
                started = engine.navigate(selectedPoint);
                break;
            case "loop":
                started = engine.startLoop(
                    new ArrayList<>(draftWaypoints),
                    optionalPositiveInt(lapCountInput),
                    pauseCheck != null && pauseCheck.isChecked(),
                    positiveDoubleOr(pauseMinInput, 5d),
                    positiveDoubleOr(pauseMaxInput, 20d)
                );
                break;
            case "multistop":
                started = engine.startMultiStop(
                    new ArrayList<>(draftWaypoints),
                    integerOr(stopDurationInput, 0),
                    routeLoopCheck != null && routeLoopCheck.isChecked(),
                    pauseCheck != null && pauseCheck.isChecked(),
                    positiveDoubleOr(pauseMinInput, 5d),
                    positiveDoubleOr(pauseMaxInput, 20d)
                );
                break;
            case "randomwalk":
                started = engine.startRandomWalk(
                    selectedPoint,
                    positiveDoubleOr(radiusInput, 500d),
                    pauseCheck == null || pauseCheck.isChecked(),
                    positiveDoubleOr(pauseMinInput, 5d),
                    positiveDoubleOr(pauseMaxInput, 20d),
                    !straightLine
                );
                break;
            default:
                started = false;
        }

        if (!started) {
            if (("loop".equals(selectedMode) || "multistop".equals(selectedMode))
                && draftWaypoints.size() < 2) {
                showError("路線至少需要兩個節點。");
            } else {
                showError("尚未設定起點，請先在地圖選點並按「瞬移到選取位置」。");
            }
        }
    }

    private void startCalculatedLoop() {
        if (draftWaypoints.size() < 2) {
            showError("路線至少需要兩個節點。");
            return;
        }
        LocalSimulationEngine.Snapshot snapshot = engine.snapshot();
        if (snapshot.current == null) {
            showError("尚未設定起點，請先在地圖選點並按「瞬移到選取位置」。");
            return;
        }

        List<LocalSimulationEngine.Point> waypoints = new ArrayList<>(draftWaypoints);
        int laps = optionalPositiveInt(lapCountInput);
        boolean pauseEnabled = pauseCheck != null && pauseCheck.isChecked();
        double pauseMin = positiveDoubleOr(pauseMinInput, 5d);
        double pauseMax = positiveDoubleOr(pauseMaxInput, 20d);
        LocalSimulationEngine.Point start = snapshot.current;

        if (!beginRoutePlanning("計算循環路徑中…")) return;
        toast("正在計算循環路徑…");
        routeTasks.submit(
            WalkingRoutePlanner.planThroughTask(start, waypoints, true),
            mainHandler,
            planned -> {
                showMapRouteNow(planned.points);
                if (!engine.startLoopRoute(
                    planned.points,
                    planned.stopIndices,
                    laps,
                    pauseEnabled,
                    pauseMin,
                    pauseMax
                )) {
                    finishRoutePlanning();
                    showError("尚未設定起點，請先在地圖選點並按「瞬移到選取位置」。");
                    return;
                }
                finishRoutePlanning();
                toast("已開始路徑循環");
            },
            error -> {
                finishRoutePlanning();
                showError("循環路徑計算失敗，請確認網路可用或開啟直線移動。");
            }
        );
    }

    private void startCalculatedMultiStop() {
        if (draftWaypoints.size() < 2) {
            showError("路線至少需要兩個節點。");
            return;
        }
        LocalSimulationEngine.Snapshot snapshot = engine.snapshot();
        if (snapshot.current == null) {
            showError("尚未設定起點，請先在地圖選點並按「瞬移到選取位置」。");
            return;
        }

        List<LocalSimulationEngine.Point> waypoints = new ArrayList<>(draftWaypoints);
        int stopSeconds = integerOr(stopDurationInput, 0);
        boolean loop = routeLoopCheck != null && routeLoopCheck.isChecked();
        boolean pauseEnabled = pauseCheck != null && pauseCheck.isChecked();
        double pauseMin = positiveDoubleOr(pauseMinInput, 5d);
        double pauseMax = positiveDoubleOr(pauseMaxInput, 20d);
        LocalSimulationEngine.Point start = snapshot.current;

        if (!beginRoutePlanning("計算多點路徑中…")) return;
        toast("正在計算多點路徑…");
        routeTasks.submit(
            WalkingRoutePlanner.planThroughTask(start, waypoints, loop),
            mainHandler,
            planned -> {
                showMapRouteNow(planned.points);
                if (!engine.startMultiStopRoute(
                    planned.points,
                    planned.stopIndices,
                    stopSeconds,
                    loop,
                    pauseEnabled,
                    pauseMin,
                    pauseMax
                )) {
                    finishRoutePlanning();
                    showError("尚未設定起點，請先在地圖選點並按「瞬移到選取位置」。");
                    return;
                }
                finishRoutePlanning();
                toast("已開始多點路徑導航");
            },
            error -> {
                finishRoutePlanning();
                showError("多點路徑計算失敗，請確認網路可用或開啟直線移動。");
            }
        );
    }

    private void stopActiveMotion() {
        if (routePlanning) {
            routeTasks.cancel();
            finishRoutePlanning();
            toast("已取消路徑計算");
            return;
        }
        if (isFlowerModeActive()) {
            cancelFlowerMode();
            toast("種花模式已關閉");
            return;
        }
        if (!engine.snapshot().running) {
            toast("目前沒有執行中移動");
            return;
        }
        engine.stop();
        activity.hideFloatingJoystick();
        updateActionControls(engine.snapshot());
        toast("已終止目前移動");
    }

    private void updateActionControls(LocalSimulationEngine.Snapshot snapshot) {
        if (primaryAction == null) return;
        boolean running = snapshot != null && snapshot.running;
        boolean paused = snapshot != null && snapshot.paused;
        primaryAction.setText(primaryActionLabel(selectedMode, running, paused));
        syncModeButtons();
        if (isFlowerModeActive()) {
            primaryAction.setText(flowerActionLabel());
            primaryAction.setEnabled(false);
            primaryAction.setAlpha(0.55f);
            primaryAction.setBackground(roundRect("#344b92", ACCENT, 12));
            primaryAction.setTextColor(Color.WHITE);
            if (stopAction != null) {
                stopAction.setVisibility(View.VISIBLE);
                stopAction.setText("關閉");
                stopAction.setEnabled(true);
                stopAction.setAlpha(1f);
            }
            if (controlStatus != null) {
                controlStatus.setText(flowerControlStatus());
                controlStatus.setTextColor(Color.parseColor("#d9deec"));
            }
            return;
        }
        if (routePlanning) {
            primaryAction.setText(routePlanningMessage == null
                ? "計算路徑中…"
                : routePlanningMessage);
            primaryAction.setEnabled(false);
            primaryAction.setAlpha(0.55f);
            primaryAction.setBackground(roundRect("#344b92", ACCENT, 12));
            primaryAction.setTextColor(Color.WHITE);
            if (stopAction != null) {
                stopAction.setVisibility(View.VISIBLE);
                stopAction.setText("取消");
                stopAction.setEnabled(true);
                stopAction.setAlpha(1f);
            }
            if (controlStatus != null) {
                controlStatus.setText(routePlanningMessage == null
                    ? "正在計算路徑…"
                    : routePlanningMessage);
                controlStatus.setTextColor(Color.parseColor("#d9deec"));
            }
            return;
        }
        primaryAction.setEnabled(true);
        primaryAction.setAlpha(1f);
        primaryAction.setBackground(roundRect(
            running && !paused ? "#344b92" : ACCENT,
            running && !paused ? ACCENT : "#88a0ff",
            12
        ));
        primaryAction.setTextColor(Color.WHITE);

        if (stopAction != null) {
            stopAction.setVisibility(running ? View.VISIBLE : View.GONE);
            stopAction.setText("終止");
            stopAction.setEnabled(running);
            stopAction.setAlpha(running ? 1f : 0.45f);
        }
        if (controlStatus != null) {
            controlStatus.setText(
                running
                    ? stateLabel(snapshot) + (paused ? " · 已暫停" : " · 移動中")
                    : "目前沒有執行中移動"
            );
            controlStatus.setTextColor(Color.parseColor(running ? "#d9deec" : MUTED));
        }
    }

    private String primaryActionLabel(String mode, boolean running, boolean paused) {
        if (!running) return startActionLabel(mode);
        if (paused) {
            if ("loop".equals(mode)) return "繼續循環";
            if ("multistop".equals(mode)) return "繼續多點";
            if ("randomwalk".equals(mode)) return "繼續隨機";
            return "繼續導航";
        }
        if ("loop".equals(mode)) return "暫停循環";
        if ("multistop".equals(mode)) return "暫停多點";
        if ("randomwalk".equals(mode)) return "暫停隨機";
        return "暫停導航";
    }

    private String startActionLabel(String mode) {
        if ("loop".equals(mode)) return "開始循環";
        if ("multistop".equals(mode)) return "開始多點";
        if ("randomwalk".equals(mode)) return "開始隨機";
        if ("flower".equals(mode)) return "開始種花";
        return "開始導航";
    }

    private boolean beginRoutePlanning(String message) {
        if (routePlanning) {
            toast("路徑計算中，請稍候");
            return false;
        }
        routePlanning = true;
        routePlanningMessage = message == null ? "正在計算路徑…" : message;
        updateActionControls(engine.snapshot());
        return true;
    }

    private void finishRoutePlanning() {
        routePlanning = false;
        routePlanningMessage = null;
        updateActionControls(engine.snapshot());
    }

    private void cancelPendingRoutePlanning() {
        routeTasks.cancel();
        if (routePlanning) finishRoutePlanning();
    }

    private boolean isFlowerModeActive() {
        return OverlaySettings.isFlowerModeActive(activity);
    }

    private void startFlowerMode() {
        if (!ensureSimulationEnabled()) return;
        if (!activity.ensureMockPermission()) return;
        if (RemoteFlowerController.isRemoteControlling()) {
            RemoteFlowerController.get(activity).stop();
        }
        OverlaySettings.setFlowerRemoteControlled(activity, false);
        OverlaySettings.setFlowerModeActive(activity, true);
        OverlaySettings.setFlowerCooldownUntilMs(activity, 0L);
        activeFlowerTarget = null;
        activeFlowerPasses = 0;
        flowerWaiting = false;
        flowerRouteRunning = false;
        flowerRouteSeenRunning = false;
        activity.hideFloatingJoystick();
        refreshFlowerTargets();
        updateActionControls(engine.snapshot());
        maybeStartQueuedFlowerTarget();
        toast("種花模式已開啟，預設地圖攔截會加入種花節點");
    }

    private void startFlowerPass() {
        if (activeFlowerTarget == null) {
            waitForFlowerTargets();
            return;
        }
        if (flowerWaitRunnable != null) {
            mainHandler.removeCallbacks(flowerWaitRunnable);
            flowerWaitRunnable = null;
        }
        OverlaySettings.setFlowerCooldownUntilMs(activity, 0L);
        LocalSimulationEngine.Point center = activeFlowerTarget.center;
        selectedPoint = center;
        setCoordinateInputs(center);
        mapView.setTarget(center);
        mapView.focus(center, 16);

        double radius = flowerRadiusMeters();
        double spacing = flowerSpacingMeters();
        List<LocalSimulationEngine.Point> route = flowerSweepRoute(center, radius, spacing);
        if (route.size() < 2) {
            cancelFlowerMode();
            showError("種花路線產生失敗，請調整半徑或間距。");
            return;
        }

        applySpeed();
        if (!engine.teleport(center.lat, center.lng)) {
            cancelFlowerMode();
            showError("無法定位到種花節點中心，請確認已選取 LocWarp 作為模擬位置 App。");
            return;
        }
        showMapRouteNow(route);
        if (!engine.startFlowerPlantingRoute(route)) {
            cancelFlowerMode();
            showError("種花路線啟動失敗，請確認位置模擬已開啟。");
            return;
        }
        flowerRouteRunning = true;
        flowerRouteSeenRunning = false;
        flowerWaiting = false;
        refreshFlowerTargets();
        updateActionControls(engine.snapshot());
        toast("開始種花第 " + (activeFlowerPasses + 1) + " 輪");
    }

    private void handleFlowerSimulationUpdate(LocalSimulationEngine.Snapshot snapshot) {
        if (activeFlowerTarget == null || snapshot == null) return;
        if (snapshot.running && "flower_planting".equals(snapshot.state)) {
            flowerRouteSeenRunning = true;
            return;
        }
        if (!flowerRouteRunning || !flowerRouteSeenRunning || snapshot.running) return;
        completeFlowerPass();
    }

    private void completeFlowerPass() {
        if (activeFlowerTarget == null) return;
        flowerRouteRunning = false;
        flowerRouteSeenRunning = false;
        activeFlowerPasses++;
        if (activeFlowerPasses >= FLOWER_REQUIRED_PASSES) {
            String finishedId = activeFlowerTarget.id;
            library.deleteFlowerTarget(finishedId);
            toast("種花節點已完成 2 次，已從清單刪除");
            activeFlowerTarget = null;
            activeFlowerPasses = 0;
            refreshFlowerTargets();
            startNextFlowerTargetOrFinish();
            return;
        }

        library.updateFlowerTargetPasses(activeFlowerTarget.id, activeFlowerPasses);
        flowerWaiting = true;
        refreshFlowerTargets();
        updateActionControls(engine.snapshot());
        toast("第 1 輪完成，等待 5 分鐘後進行第 2 輪");
        long cooldownUntilMs = System.currentTimeMillis() + FLOWER_COOLDOWN_MS;
        OverlaySettings.setFlowerCooldownUntilMs(activity, cooldownUntilMs);
        scheduleFlowerCooldown(cooldownUntilMs);
    }

    private void scheduleFlowerCooldown(long cooldownUntilMs) {
        if (flowerWaitRunnable != null) {
            mainHandler.removeCallbacks(flowerWaitRunnable);
        }
        long remainingMs = Math.max(
            0L,
            cooldownUntilMs - System.currentTimeMillis()
        );
        flowerWaiting = remainingMs > 0L;
        flowerWaitRunnable = () -> {
            flowerWaitRunnable = null;
            flowerWaiting = false;
            OverlaySettings.setFlowerCooldownUntilMs(activity, 0L);
            if (detached) return;
            startFlowerPass();
        };
        mainHandler.postDelayed(flowerWaitRunnable, remainingMs);
    }

    private void startNextFlowerTargetOrFinish() {
        List<LocalDataStore.FlowerTarget> targets = library.flowerTargets();
        if (targets.isEmpty()) {
            waitForFlowerTargets();
            return;
        }
        activeFlowerTarget = targets.get(0);
        activeFlowerPasses = Math.min(activeFlowerTarget.passesCompleted, FLOWER_REQUIRED_PASSES);
        OverlaySettings.setFlowerCooldownUntilMs(activity, 0L);
        mainHandler.postDelayed(this::startFlowerPass, 600L);
    }

    private void waitForFlowerTargets() {
        activeFlowerTarget = null;
        activeFlowerPasses = 0;
        flowerRouteRunning = false;
        flowerRouteSeenRunning = false;
        flowerWaiting = false;
        OverlaySettings.setFlowerCooldownUntilMs(activity, 0L);
        if (flowerWaitRunnable != null) {
            mainHandler.removeCallbacks(flowerWaitRunnable);
            flowerWaitRunnable = null;
        }
        refreshFlowerTargets();
        updateActionControls(engine.snapshot());
    }

    private void maybeStartQueuedFlowerTarget() {
        if (!isFlowerModeActive()) return;
        if (RemoteFlowerController.isRemoteControlling()) return;
        if (activeFlowerTarget != null || flowerWaiting || flowerRouteRunning) return;
        List<LocalDataStore.FlowerTarget> targets = library.flowerTargets();
        if (targets.isEmpty()) {
            refreshFlowerTargets();
            updateActionControls(engine.snapshot());
            return;
        }
        activeFlowerTarget = targets.get(0);
        activeFlowerPasses = Math.min(activeFlowerTarget.passesCompleted, FLOWER_REQUIRED_PASSES);
        LocalSimulationEngine.Snapshot snapshot = engine.snapshot();
        if (snapshot.running && "flower_planting".equals(snapshot.state)) {
            flowerRouteRunning = true;
            flowerRouteSeenRunning = true;
            refreshFlowerTargets();
            updateActionControls(snapshot);
            return;
        }
        long cooldownUntilMs = OverlaySettings.flowerCooldownUntilMs(activity);
        if (activeFlowerPasses > 0
            && cooldownUntilMs > System.currentTimeMillis()) {
            scheduleFlowerCooldown(cooldownUntilMs);
            refreshFlowerTargets();
            updateActionControls(snapshot);
            return;
        }
        OverlaySettings.setFlowerCooldownUntilMs(activity, 0L);
        startFlowerPass();
    }

    private void cancelFlowerMode() {
        if (RemoteFlowerController.isRemoteControlling()) {
            RemoteFlowerController.get(activity).stop();
        }
        if (flowerWaitRunnable != null) {
            mainHandler.removeCallbacks(flowerWaitRunnable);
            flowerWaitRunnable = null;
        }
        boolean wasRunning = isFlowerModeActive()
            || flowerRouteRunning
            || flowerWaiting
            || activeFlowerTarget != null;
        OverlaySettings.setFlowerModeActive(activity, false);
        OverlaySettings.setFlowerCooldownUntilMs(activity, 0L);
        activeFlowerTarget = null;
        activeFlowerPasses = 0;
        flowerRouteRunning = false;
        flowerRouteSeenRunning = false;
        flowerWaiting = false;
        LocalSimulationEngine.Snapshot snapshot = engine.snapshot();
        if (snapshot.running && "flower_planting".equals(snapshot.state)) {
            engine.stop();
        } else if (wasRunning) {
            updateActionControls(snapshot);
        }
        refreshFlowerTargets();
    }

    private String flowerControlStatus() {
        if (activeFlowerTarget == null) return "種花模式待命中，請從預設地圖或加入節點新增";
        String point = LocalSimulationEngine.format(activeFlowerTarget.center);
        if (flowerWaiting) {
            return "種花模式 · 等待 5 分鐘冷卻\n"
                + point + "\n已完成 " + activeFlowerPasses + " / " + FLOWER_REQUIRED_PASSES;
        }
        return "種花模式 · 第 " + (activeFlowerPasses + 1)
            + " / " + FLOWER_REQUIRED_PASSES + " 輪\n" + point;
    }

    private String flowerActionLabel() {
        if (flowerWaiting) return "等待冷卻中…";
        if (activeFlowerTarget == null) return "種花待命中…";
        return "種花中…";
    }

    private double flowerRadiusMeters() {
        return clamp(
            positiveDoubleOr(flowerRadiusInput, FLOWER_DEFAULT_RADIUS_M),
            20d,
            38d
        );
    }

    private double flowerSpacingMeters() {
        return clamp(
            positiveDoubleOr(flowerSpacingInput, FLOWER_DEFAULT_SPACING_M),
            4d,
            10d
        );
    }

    private List<LocalSimulationEngine.Point> flowerSweepRoute(
        LocalSimulationEngine.Point center,
        double radiusM,
        double spacingM
    ) {
        List<LocalSimulationEngine.Point> points = new ArrayList<>();
        boolean reverse = false;
        for (double north = -radiusM; north <= radiusM + 0.01d; north += spacingM) {
            double half = Math.sqrt(Math.max(0d, radiusM * radiusM - north * north));
            if (half < 2d) continue;
            double inset = Math.min(2d, half * 0.4d);
            double west = -half + inset;
            double east = half - inset;
            if (reverse) {
                points.add(offsetPoint(center, east, north));
                points.add(offsetPoint(center, west, north));
            } else {
                points.add(offsetPoint(center, west, north));
                points.add(offsetPoint(center, east, north));
            }
            reverse = !reverse;
        }
        if (points.size() < 2) {
            points.clear();
            points.add(offsetPoint(center, 0d, -radiusM * 0.5d));
            points.add(offsetPoint(center, 0d, radiusM * 0.5d));
        }
        return points;
    }

    private LocalSimulationEngine.Point offsetPoint(
        LocalSimulationEngine.Point center,
        double eastM,
        double northM
    ) {
        double metersPerDegreeLat = 111320d;
        double metersPerDegreeLng =
            Math.max(1d, metersPerDegreeLat * Math.cos(Math.toRadians(center.lat)));
        return new LocalSimulationEngine.Point(
            center.lat + northM / metersPerDegreeLat,
            center.lng + eastM / metersPerDegreeLng
        );
    }

    private double clamp(double value, double min, double max) {
        return Math.max(min, Math.min(max, value));
    }

    private void runTeleportShortcut() {
        if (isFlowerModeActive()) {
            toast("種花模式執行中，請先關閉");
            return;
        }
        if (!ensureSimulationEnabled()) return;
        if (!activity.ensureMockPermission()) return;
        LocalSimulationEngine.Point point = readCoordinate();
        if (point == null) return;
        selectedPoint = point;
        if (!engine.teleport(point.lat, point.lng)) {
            showError("無法套用位置，請確認已選取 LocWarp 作為模擬位置 App。");
            return;
        }
        toast("已瞬移到選取位置");
    }

    private void startFloatingJoystick() {
        if (isFlowerModeActive()) {
            toast("種花模式執行中，請先關閉");
            return;
        }
        if (!ensureSimulationEnabled()) return;
        activity.requestOverlayPermission(() -> {
            if (!engine.startJoystick()) {
                showError("尚未設定起點，請先在地圖選點並按「瞬移到選取位置」。");
                return;
            }
            activity.showFloatingJoystick();
            toast("浮動搖桿已啟動");
        });
    }

    private void showMapHintOverlay() {
        if (!ensureSimulationEnabled()) {
            syncMapHintSwitch();
            return;
        }
        activity.requestOverlayPermission(() -> {
            activity.showMapHintOverlay();
            syncMapHintSwitch(true);
            toast("地圖提示已開啟");
        });
    }

    private void setMapHintOverlayVisible(boolean visible) {
        if (isFlowerModeActive()) {
            syncMapHintSwitch();
            toast("種花模式執行中，請先關閉");
            return;
        }
        if (visible) {
            showMapHintOverlay();
            return;
        }
        activity.hideMapHintOverlay();
        syncMapHintSwitch(false);
        toast("浮動地圖已關閉");
    }

    private void setSimulationEnabled(boolean enabled) {
        if (!enabled) cancelPendingRoutePlanning();
        if (!enabled && isFlowerModeActive()) {
            cancelFlowerMode();
        }
        if (enabled) {
            if (!activity.ensureMockPermission()) {
                MockLocationKeeper.setMockEnabled(activity, false);
                syncMockSwitch();
                return;
            }
            MockLocationKeeper.setMockEnabled(activity, true);
            firstPositionFocus = true;
            syncMockSwitch();
            onSimulationUpdate(engine.snapshot());
            toast("位置模擬已開啟，請選點後瞬移設定起點");
            return;
        }

        activity.hideFloatingJoystick();
        activity.hideMapHintOverlay();
        syncMapHintSwitch(false);
        engine.restore();
        MockLocationKeeper.setMockEnabled(activity, false);
        firstPositionFocus = true;
        syncMockSwitch();
        onSimulationUpdate(engine.snapshot());
        toast("位置模擬已關閉");
    }

    private boolean ensureSimulationEnabled() {
        if (MockLocationKeeper.isMockEnabled(activity)) return true;
        showError("請先開啟頁面頂部的「位置模擬」開關。");
        return false;
    }

    private void syncMockSwitch() {
        if (mockSwitch == null || mockSwitchHint == null) return;
        boolean enabled = MockLocationKeeper.isMockEnabled(activity);
        syncingMockSwitch = true;
        mockSwitch.setChecked(enabled);
        syncingMockSwitch = false;
        mockSwitchHint.setText(
            enabled
                ? "已開啟，請從互動地圖選擇起點，再按「瞬移到選取位置」"
                : "已關閉，目前未套用模擬座標"
        );
        mockSwitchHint.setTextColor(Color.parseColor(enabled ? "#4ecdc4" : MUTED));
    }

    private void syncMapHintSwitch() {
        syncMapHintSwitch(OverlaySettings.isMapHintVisible(activity));
    }

    private void syncMapHintSwitch(boolean visible) {
        if (mapHintSwitch == null) return;
        syncingMapHintSwitch = true;
        mapHintSwitch.setChecked(visible);
        mapHintSwitch.setText(visible ? "關閉浮動地圖" : "開啟浮動地圖");
        syncingMapHintSwitch = false;
        if (mapHintOptionsPanel != null) {
            mapHintOptionsPanel.setVisibility(visible ? View.VISIBLE : View.GONE);
        }
    }

    private LinearLayout overlaySizeRow(
        String title,
        boolean joystick,
        Runnable onMinus,
        Runnable onPlus
    ) {
        LinearLayout row = horizontal();
        row.setPadding(dp(10), 0, dp(8), 0);
        row.setBackground(roundRect(ELEVATED, BORDER, 12));

        TextView titleView = label(title, 13, "#d9deec", true);
        titleView.setGravity(Gravity.CENTER_VERTICAL);
        row.addView(titleView, new LinearLayout.LayoutParams(0, LayoutParams.MATCH_PARENT, 1f));

        TextView value = label("", 12, MUTED, true);
        value.setGravity(Gravity.CENTER);
        if (joystick) joystickSizeLabel = value;
        else mapHintSizeLabel = value;
        row.addView(value, new LinearLayout.LayoutParams(dp(58), LayoutParams.MATCH_PARENT));

        Button minus = compactButton("－");
        minus.setTextSize(16);
        minus.setOnClickListener(v -> onMinus.run());
        LinearLayout.LayoutParams minusLp = new LinearLayout.LayoutParams(dp(48), dp(48));
        minusLp.setMargins(dp(6), 0, 0, 0);
        row.addView(minus, minusLp);

        Button plus = compactButton("＋");
        plus.setTextSize(16);
        plus.setOnClickListener(v -> onPlus.run());
        LinearLayout.LayoutParams plusLp = new LinearLayout.LayoutParams(dp(48), dp(48));
        plusLp.setMargins(dp(6), 0, 0, 0);
        row.addView(plus, plusLp);
        return row;
    }

    private void adjustJoystickOverlay(int deltaDp) {
        OverlaySettings.adjustJoystickSize(activity, deltaDp);
        updateOverlaySizeLabels();
        if (OverlaySettings.isJoystickVisible(activity)) {
            activity.showFloatingJoystick();
        }
    }

    private void adjustMapHintOverlay(int deltaDp) {
        OverlaySettings.adjustMapHintSize(activity, deltaDp);
        updateOverlaySizeLabels();
        if (OverlaySettings.isMapHintVisible(activity)) {
            activity.showMapHintOverlay();
        }
    }

    private void updateOverlaySizeLabels() {
        if (joystickSizeLabel != null) {
            int size = OverlaySettings.joystickSizeDp(activity);
            joystickSizeLabel.setText(
                OverlaySettings.percent(size, OverlaySettings.JOYSTICK_DEFAULT_DP) + "%"
            );
        }
        if (mapHintSizeLabel != null) {
            int size = OverlaySettings.mapHintSizeDp(activity);
            mapHintSizeLabel.setText(
                OverlaySettings.percent(size, OverlaySettings.MAP_HINT_DEFAULT_DP) + "%"
            );
        }
    }

    private void applySpeed() {
        double fixed = positiveDoubleOr(speedInput, engine.snapshot().speedKmh);
        OverlaySettings.setSpeedKmh(activity, fixed);
        engine.configureSpeed(fixed, null, null);
    }

    @Override
    public void onSimulationUpdate(LocalSimulationEngine.Snapshot snapshot) {
        if (snapshot == null) return;
        boolean renderUi = !detached && hostForeground && pageSelected;
        if (!MockLocationKeeper.isMockEnabled(activity)) {
            if (!renderUi || statusBody == null || mapView == null) return;
            statusBody.setText("位置模擬已關閉\n目前未套用模擬座標");
            mapView.setCurrent(null, false);
            mapView.setTarget(selectedPoint);
            renderMapRoute("empty", new ArrayList<>());
            updateActionControls(snapshot);
            syncMapHintSwitch();
            return;
        }
        handleFlowerSimulationUpdate(snapshot);
        if (!renderUi || statusBody == null || mapView == null) return;
        String state = stateLabel(snapshot);
        String coordinate = LocalSimulationEngine.format(snapshot.current);
        statusBody.setText(
            coordinate + "\n" + state
                + "  ·  " + String.format(Locale.US, "%.1f km/h", snapshot.speedKmh)
                + (snapshot.lap > 0 ? "  ·  第 " + snapshot.lap + " 圈" : "")
        );
        mapView.setCurrent(snapshot.current, firstPositionFocus && snapshot.current != null);
        if (snapshot.current != null) firstPositionFocus = false;
        mapView.setTarget(snapshot.target != null ? snapshot.target : selectedPoint);
        if (snapshot.running && snapshot.route.size() > 1) {
            renderMapRoute("engine:" + snapshot.routeRevision, snapshot.route);
        } else if (shouldShowDraftRoute()) {
            renderMapRoute("draft:" + draftRouteRevision, draftWaypoints);
        } else {
            renderMapRoute("empty", new ArrayList<>());
        }
        if (snapshot.current == null) {
            statusBody.setText(
                "尚未設定起點\n請從互動地圖挑選位置後按「瞬移到選取位置」"
            );
        }
        updateActionControls(snapshot);
        syncMapHintSwitch();
    }

    @Override
    public void onMapTap(double lat, double lng) {
        LocalSimulationEngine.Point point = new LocalSimulationEngine.Point(lat, lng);
        selectPoint(point, false);
    }

    @Override
    public void onMapLongPress(double lat, double lng) {
        // 長按選單由地圖 WebView 統一處理；放開後才會送出 action。
    }

    @Override
    public void onMapLongPressAction(double lat, double lng, String action) {
        LocalSimulationEngine.Point point = new LocalSimulationEngine.Point(lat, lng);
        runMapAction(point, action);
    }

    @Override
    public void onMapDoubleTap(double lat, double lng) {
        // 雙擊不執行任何定位動作；瞬移請使用長按選單或明確按鈕。
    }

    private void searchAddress() {
        String query = searchInput.getText().toString().trim();
        if (query.isEmpty()) {
            showError("請輸入地名或地址。");
            return;
        }
        toast("搜尋中…");
        searchTasks.submit(
            () -> {
                Geocoder geocoder = new Geocoder(activity, Locale.getDefault());
                return geocoder.getFromLocationName(query, 5);
            },
            mainHandler,
            results -> {
                if (results == null || results.isEmpty()) {
                    showError("找不到這個地點，請換一個關鍵字。");
                    return;
                }
                showSearchResults(results);
            },
            error -> showError("目前無法搜尋地址。地圖點選與經緯度輸入仍可使用。")
        );
    }

    private void showSearchResults(List<Address> results) {
        String[] labels = new String[results.size()];
        for (int i = 0; i < results.size(); i++) {
            Address address = results.get(i);
            String line = address.getAddressLine(0);
            labels[i] = line == null ? address.getFeatureName() : line;
        }
        new AlertDialog.Builder(activity)
            .setTitle("搜尋結果")
            .setItems(labels, (dialog, which) -> {
                Address address = results.get(which);
                LocalSimulationEngine.Point point = new LocalSimulationEngine.Point(
                    address.getLatitude(),
                    address.getLongitude()
                );
                selectPoint(point, true);
            })
            .setNegativeButton("取消", null)
            .show();
    }

    private void selectPoint(LocalSimulationEngine.Point point, boolean focus) {
        selectedPoint = point;
        setCoordinateInputs(point);
        mapView.setTarget(point);
        if (focus) mapView.focus(point, 16);
    }

    private void runMapAction(LocalSimulationEngine.Point point, String action) {
        if (isFlowerModeActive()) {
            if ("waypoint".equals(action)) {
                selectPoint(point, false);
                addWaypoint(point);
                return;
            }
            toast("種花模式執行中，請先關閉");
            return;
        }
        selectPoint(point, false);
        if ("teleport".equals(action)) {
            teleportToPoint(point, "已瞬移");
        } else if ("navigate".equals(action)) {
            navigateByMovementMode(point);
        } else if ("path".equals(action)) {
            startPathNavigationToPoint(point);
        } else if ("waypoint".equals(action)) {
            addWaypoint(point);
        } else if ("bookmark".equals(action)) {
            addBookmark(point);
        }
    }

    private void navigateByMovementMode(LocalSimulationEngine.Point point) {
        if (OverlaySettings.isStraightLineMovement(activity)) {
            navigateToPoint(point);
        } else {
            startPathNavigationToPoint(point);
        }
    }

    private void teleportToPoint(LocalSimulationEngine.Point point, String message) {
        cancelPendingRoutePlanning();
        if (isFlowerModeActive()) {
            toast("種花模式執行中，請先關閉");
            return;
        }
        if (!ensureSimulationEnabled()) return;
        if (!activity.ensureMockPermission()) return;
        applySpeed();
        selectedPoint = point;
        setCoordinateInputs(point);
        mapView.setTarget(point);
        if (!engine.teleport(point.lat, point.lng)) {
            showError("無法套用位置，請確認已選取 LocWarp 作為模擬位置 App。");
            return;
        }
        toast(message);
    }

    private void navigateToPoint(LocalSimulationEngine.Point point) {
        cancelPendingRoutePlanning();
        if (isFlowerModeActive()) {
            toast("種花模式執行中，請先關閉");
            return;
        }
        if (!ensureSimulationEnabled()) return;
        if (!activity.ensureMockPermission()) return;
        applySpeed();
        selectedPoint = point;
        setCoordinateInputs(point);
        mapView.setTarget(point);
        if (!engine.navigate(point)) {
            showError("尚未設定起點，請先在地圖選點並按「瞬移到選取位置」。");
            return;
        }
        toast("已開始直線導航");
    }

    private void startPathNavigationToPoint(LocalSimulationEngine.Point point) {
        if (isFlowerModeActive()) {
            toast("種花模式執行中，請先關閉");
            return;
        }
        if (!ensureSimulationEnabled()) return;
        if (!activity.ensureMockPermission()) return;
        applySpeed();
        selectedPoint = point;
        setCoordinateInputs(point);
        mapView.setTarget(point);

        LocalSimulationEngine.Snapshot snapshot = engine.snapshot();
        if (snapshot.current == null) {
            showError("尚未設定起點，請先在地圖選點並按「瞬移到選取位置」。");
            return;
        }
        if (!beginRoutePlanning("計算路徑中…")) return;
        toast("正在計算路徑…");
        LocalSimulationEngine.Point start = snapshot.current;
        routeTasks.submit(
            WalkingRoutePlanner.planTask(start, point),
            mainHandler,
            route -> {
                showMapRouteNow(route);
                if (!engine.startPathRoute(new ArrayList<>(route))) {
                    finishRoutePlanning();
                    showError("尚未設定起點，請先在地圖選點並按「瞬移到選取位置」。");
                    return;
                }
                finishRoutePlanning();
                toast("已開始路徑導航");
            },
            error -> {
                finishRoutePlanning();
                showError("路徑計算失敗，請確認網路可用或改用直線導航。");
            }
        );
    }

    private void addSelectedWaypoint() {
        LocalSimulationEngine.Point point = readCoordinate();
        if (point == null) return;
        addWaypoint(point);
    }

    private void addWaypoint(LocalSimulationEngine.Point point) {
        selectedPoint = point;
        setCoordinateInputs(point);
        mapView.setTarget(point);
        if ("flower".equals(selectedMode) || isFlowerModeActive()) {
            library.addFlowerTarget(point);
            refreshFlowerTargets();
            maybeStartQueuedFlowerTarget();
            toast("已加入種花節點");
            return;
        }
        draftWaypoints.add(point);
        updateDraftRoute();
        toast("已加入路線節點 " + draftWaypoints.size());
    }

    private void showMapRouteNow(List<LocalSimulationEngine.Point> points) {
        if (mapView == null) return;
        renderedMapRouteKey = null;
        mapView.setRoute(points == null ? new ArrayList<>() : points);
    }

    private void renderMapRoute(
        String key,
        List<LocalSimulationEngine.Point> points
    ) {
        if (mapView == null || key.equals(renderedMapRouteKey)) return;
        renderedMapRouteKey = key;
        mapView.setRoute(points);
    }

    private void updateDraftRoute() {
        draftRouteRevision++;
        renderMapRoute(
            shouldShowDraftRoute() ? "draft:" + draftRouteRevision : "empty",
            shouldShowDraftRoute() ? draftWaypoints : new ArrayList<>()
        );
        if (draftSummary != null) {
            double distanceM = routeDistanceMeters(draftWaypoints);
            double speedKmh = Math.max(0.1d, OverlaySettings.speedKmh(activity));
            long etaSeconds = Math.round(distanceM / (speedKmh / 3.6d));
            draftSummary.setText(
                "目前 " + draftWaypoints.size() + " 個節點 · "
                    + distanceLabel(distanceM) + " · 約 "
                    + durationLabelSeconds(etaSeconds)
            );
        }
        if (routeWaypointContent != null) {
            routeWaypointContent.removeAllViews();
            if (draftWaypoints.isEmpty()) {
                routeWaypointContent.addView(emptyLabel("尚無路線節點"));
            } else {
                for (int i = 0; i < draftWaypoints.size(); i++) {
                    routeWaypointContent.addView(
                        routeWaypointRow(draftWaypoints.get(i), i + 1),
                        rowLp()
                    );
                }
            }
        }
    }

    private boolean shouldShowDraftRoute() {
        return ("loop".equals(selectedMode) || "multistop".equals(selectedMode))
            && !draftWaypoints.isEmpty();
    }

    private double routeDistanceMeters(
        List<LocalSimulationEngine.Point> points
    ) {
        double total = 0d;
        for (int i = 1; i < points.size(); i++) {
            LocalSimulationEngine.Point from = points.get(i - 1);
            LocalSimulationEngine.Point to = points.get(i);
            total += GeoMath.distanceMeters(from.lat, from.lng, to.lat, to.lng);
        }
        return total;
    }

    private String distanceLabel(double meters) {
        return meters >= 1000d
            ? String.format(Locale.US, "%.2f km", meters / 1000d)
            : String.format(Locale.US, "%.0f m", meters);
    }

    private String durationLabelSeconds(long seconds) {
        long safe = Math.max(0L, seconds);
        long hours = safe / 3600L;
        long minutes = (safe % 3600L) / 60L;
        long remaining = safe % 60L;
        if (hours > 0L) return hours + " 小時 " + minutes + " 分";
        if (minutes > 0L) return minutes + " 分 " + remaining + " 秒";
        return remaining + " 秒";
    }

    private void importBulkRoute() {
        showImportPointsDialog("匯入路線節點", points -> {
            draftWaypoints.addAll(points);
            updateDraftRoute();
            toast("已匯入 " + points.size() + " 個節點");
        });
    }

    private void exportDraftRoute() {
        if (draftWaypoints.isEmpty()) {
            showError("目前沒有可匯出的路線節點。");
            return;
        }
        String[] formats = {"GPX", "KML", "GeoJSON"};
        new AlertDialog.Builder(activity)
            .setTitle("匯出路線格式")
            .setItems(formats, (dialog, which) -> {
                String content;
                String mime;
                if (which == 1) {
                    content = RouteFileCodec.toKml("LocWarp route", draftWaypoints);
                    mime = "application/vnd.google-earth.kml+xml";
                } else if (which == 2) {
                    content = RouteFileCodec.toGeoJson(
                        "LocWarp route",
                        draftWaypoints
                    );
                    mime = "application/geo+json";
                } else {
                    content = RouteFileCodec.toGpx("LocWarp route", draftWaypoints);
                    mime = "application/gpx+xml";
                }
                ClipboardManager clipboard = (ClipboardManager) activity
                    .getSystemService(Context.CLIPBOARD_SERVICE);
                if (clipboard != null) {
                    clipboard.setPrimaryClip(
                        ClipData.newPlainText(formats[which] + " route", content)
                    );
                }
                Intent share = new Intent(Intent.ACTION_SEND)
                    .setType(mime)
                    .putExtra(Intent.EXTRA_TEXT, content);
                activity.startActivity(Intent.createChooser(share, "分享路線"));
            })
            .setNegativeButton("取消", null)
            .show();
    }

    private void importFlowerTargets() {
        showImportPointsDialog("匯入種花節點", points -> {
            library.addFlowerTargets(points);
            refreshFlowerTargets();
            maybeStartQueuedFlowerTarget();
            toast("已匯入 " + points.size() + " 個種花節點");
        });
    }

    private interface PointsCallback {
        void onPoints(List<LocalSimulationEngine.Point> points);
    }

    private void showImportPointsDialog(String title, PointsCallback callback) {
        EditText input = multilineInput(
            "25.033964, 121.564468\n25.047800, 121.531900"
        );
        LinearLayout wrapper = new LinearLayout(activity);
        wrapper.setPadding(dp(20), dp(8), dp(20), 0);
        wrapper.addView(input, matchHeight(140));
        new AlertDialog.Builder(activity)
            .setTitle(title)
            .setView(wrapper)
            .setPositiveButton("匯入", (dialog, which) -> {
                List<LocalSimulationEngine.Point> parsed =
                    parsePointLines(input.getText().toString());
                if (parsed != null && !parsed.isEmpty()) {
                    callback.onPoints(parsed);
                }
            })
            .setNegativeButton("取消", null)
            .show();
    }

    private List<LocalSimulationEngine.Point> parsePointLines(String raw) {
        List<LocalSimulationEngine.Point> parsed = RouteFileCodec.parse(raw);
        if (parsed.isEmpty()) {
            showError("無法解析內容；支援座標文字、GPX、KML 與 GeoJSON。");
            return null;
        }
        return parsed;
    }

    private void addBookmark() {
        LocalSimulationEngine.Point point = readCoordinate();
        if (point == null) return;
        addBookmark(point);
    }

    private void addBookmark(LocalSimulationEngine.Point point) {
        showNameDialog("收藏名稱", "新收藏", name -> {
            library.addBookmark(name, point);
            refreshLibrary();
            toast("已加入收藏");
        });
    }

    private void clearFlowerTargets() {
        if (isFlowerModeActive()) {
            toast("種花模式執行中，請先關閉");
            return;
        }
        confirmAction("清空全部種花節點？此操作無法復原。", () -> {
            library.clearFlowerTargets();
            refreshFlowerTargets();
            toast("種花節點已清空");
        });
    }

    private void saveDraftRoute() {
        if (draftWaypoints.size() < 2) {
            showError("至少加入兩個路線節點。");
            return;
        }
        showNameDialog("路線名稱", "我的路線", name -> {
            library.addRoute(name, draftWaypoints);
            refreshLibrary();
            toast("路線已儲存");
        });
    }

    private interface NameCallback {
        void onName(String name);
    }

    private void showNameDialog(String title, String defaultName, NameCallback callback) {
        EditText input = textInput("");
        input.setText(defaultName);
        input.selectAll();
        int padding = dp(20);
        LinearLayout wrapper = new LinearLayout(activity);
        wrapper.setPadding(padding, dp(8), padding, 0);
        wrapper.addView(input, matchHeight(48));
        new AlertDialog.Builder(activity)
            .setTitle(title)
            .setView(wrapper)
            .setPositiveButton("儲存", (dialog, which) -> {
                String name = input.getText().toString().trim();
                callback.onName(name.isEmpty() ? defaultName : name);
            })
            .setNegativeButton("取消", null)
            .show();
    }

    private void refreshLibrary() {
        if (libraryContent == null || mapView == null) return;
        libraryContent.removeAllViews();
        List<LocalDataStore.Bookmark> bookmarks = library.bookmarks();
        List<LocalDataStore.Route> routes = library.routes();
        mapView.setBookmarks(bookmarks);

        libraryContent.addView(label("收藏地點 " + bookmarks.size(), 12, MUTED, true));
        if (bookmarks.isEmpty()) {
            libraryContent.addView(emptyLabel("尚無收藏，從地圖加入即可"));
        } else {
            for (LocalDataStore.Bookmark bookmark : bookmarks) {
                libraryContent.addView(bookmarkRow(bookmark), rowLp());
            }
        }

        TextView routeTitle = label("儲存路線 " + routes.size(), 12, MUTED, true);
        routeTitle.setPadding(0, dp(12), 0, 0);
        libraryContent.addView(routeTitle);
        if (routes.isEmpty()) {
            libraryContent.addView(emptyLabel("尚無路線"));
        } else {
            for (LocalDataStore.Route route : routes) {
                libraryContent.addView(routeRow(route), rowLp());
            }
        }
    }

    private void refreshFlowerTargets() {
        if (flowerSummary == null && flowerTargetContent == null) return;
        List<LocalDataStore.FlowerTarget> targets = library.flowerTargets();
        if (flowerSummary != null) {
            if (isFlowerModeActive()) {
                String status = targets.isEmpty()
                    ? "種花模式已開啟 · 目前沒有種花節點"
                    : "種花模式已開啟 · 種花節點 " + targets.size()
                        + " 筆 · 每筆完成 2 次後自動刪除";
                status += "\n" + flowerControlStatus();
                flowerSummary.setText(status);
            } else {
                String status = targets.isEmpty()
                    ? "開啟種花模式後，可用預設地圖攔截或加入節點新增。"
                    : "種花節點 " + targets.size() + " 筆 · 開啟後可繼續新增";
                status += "\n啟動後會鎖定其他功能，只能按「關閉」停止。";
                flowerSummary.setText(status);
            }
        }
        if (flowerTargetContent == null) return;
        flowerTargetContent.removeAllViews();
        if (targets.isEmpty()) {
            flowerTargetContent.addView(emptyLabel("尚無種花節點"));
            return;
        }
        for (int i = 0; i < targets.size(); i++) {
            flowerTargetContent.addView(flowerTargetRow(targets.get(i), i + 1), rowLp());
        }
    }

    private LinearLayout bookmarkRow(LocalDataStore.Bookmark bookmark) {
        LinearLayout row = horizontal();
        row.setPadding(dp(10), dp(6), dp(6), dp(6));
        row.setBackground(roundRect(ELEVATED, BORDER, 9));
        Button open = transparentButton(
            bookmark.name + "\n"
                + String.format(Locale.US, "%.5f, %.5f",
                    bookmark.point.lat, bookmark.point.lng)
        );
        open.setGravity(Gravity.START | Gravity.CENTER_VERTICAL);
        open.setOnClickListener(v -> selectPoint(bookmark.point, true));
        row.addView(open, new LinearLayout.LayoutParams(0, dp(52), 1f));
        Button delete = compactButton("×");
        delete.setTextColor(Color.parseColor("#ff8a93"));
        delete.setOnClickListener(v -> confirmAction("刪除收藏「" + bookmark.name + "」？", () -> {
            library.deleteBookmark(bookmark.id);
            refreshLibrary();
        }));
        delete.setContentDescription("刪除收藏 " + bookmark.name);
        row.addView(delete, new LinearLayout.LayoutParams(dp(48), dp(48)));
        return row;
    }

    private LinearLayout routeRow(LocalDataStore.Route route) {
        LinearLayout row = horizontal();
        row.setPadding(dp(10), dp(6), dp(6), dp(6));
        row.setBackground(roundRect(ELEVATED, BORDER, 9));
        Button open = transparentButton(route.name + "\n" + route.points.size() + " 個節點");
        open.setGravity(Gravity.START | Gravity.CENTER_VERTICAL);
        open.setOnClickListener(v -> {
            draftWaypoints.clear();
            draftWaypoints.addAll(route.points);
            selectMode("loop");
            updateDraftRoute();
            if (!route.points.isEmpty()) {
                selectPoint(route.points.get(0), true);
            }
        });
        row.addView(open, new LinearLayout.LayoutParams(0, dp(52), 1f));
        Button delete = compactButton("×");
        delete.setTextColor(Color.parseColor("#ff8a93"));
        delete.setOnClickListener(v -> confirmAction("刪除路線「" + route.name + "」？", () -> {
            library.deleteRoute(route.id);
            refreshLibrary();
        }));
        delete.setContentDescription("刪除路線 " + route.name);
        row.addView(delete, new LinearLayout.LayoutParams(dp(48), dp(48)));
        return row;
    }

    private LinearLayout routeWaypointRow(LocalSimulationEngine.Point point, int index) {
        LinearLayout row = horizontal();
        row.setPadding(dp(10), dp(6), dp(6), dp(6));
        row.setBackground(roundRect(ELEVATED, BORDER, 9));
        Button open = transparentButton(
            "路線節點 " + index + "\n"
                + String.format(Locale.US, "%.5f, %.5f", point.lat, point.lng)
        );
        open.setGravity(Gravity.START | Gravity.CENTER_VERTICAL);
        open.setOnClickListener(v -> selectPoint(point, true));
        row.addView(open, new LinearLayout.LayoutParams(0, dp(56), 1f));
        Button up = compactButton("↑");
        up.setEnabled(index > 1);
        up.setContentDescription("將路線節點 " + index + " 上移");
        up.setOnClickListener(v -> moveDraftWaypoint(index - 1, index - 2));
        row.addView(up, new LinearLayout.LayoutParams(dp(40), dp(48)));
        Button down = compactButton("↓");
        down.setEnabled(index < draftWaypoints.size());
        down.setContentDescription("將路線節點 " + index + " 下移");
        down.setOnClickListener(v -> moveDraftWaypoint(index - 1, index));
        row.addView(down, new LinearLayout.LayoutParams(dp(40), dp(48)));
        Button delete = compactButton("×");
        delete.setTextColor(Color.parseColor("#ff8a93"));
        delete.setOnClickListener(v -> confirmAction("刪除路線節點 " + index + "？", () -> {
            int targetIndex = index - 1;
            if (targetIndex >= 0 && targetIndex < draftWaypoints.size()) {
                draftWaypoints.remove(targetIndex);
                updateDraftRoute();
            }
        }));
        delete.setContentDescription("刪除路線節點 " + index);
        row.addView(delete, new LinearLayout.LayoutParams(dp(48), dp(48)));
        return row;
    }

    private void moveDraftWaypoint(int from, int to) {
        if (from < 0 || from >= draftWaypoints.size()
            || to < 0 || to >= draftWaypoints.size()
            || from == to) {
            return;
        }
        LocalSimulationEngine.Point point = draftWaypoints.remove(from);
        draftWaypoints.add(to, point);
        updateDraftRoute();
    }

    private LinearLayout flowerTargetRow(LocalDataStore.FlowerTarget target, int index) {
        LinearLayout row = horizontal();
        row.setPadding(dp(10), dp(6), dp(6), dp(6));
        row.setBackground(roundRect(ELEVATED, BORDER, 9));
        boolean active = activeFlowerTarget != null && activeFlowerTarget.id.equals(target.id);
        Button open = transparentButton(
            (active ? "執行中 · " : "") + "種花節點 " + index + "\n"
                + String.format(
                    Locale.US,
                    "%.5f, %.5f · %d/%d 次",
                    target.center.lat,
                    target.center.lng,
                    Math.min(target.passesCompleted, FLOWER_REQUIRED_PASSES),
                    FLOWER_REQUIRED_PASSES
                )
        );
        open.setGravity(Gravity.START | Gravity.CENTER_VERTICAL);
        open.setOnClickListener(v -> {
            if (isFlowerModeActive()) {
                toast("種花模式執行中，請先關閉");
                return;
            }
            selectPoint(target.center, true);
            showMapRouteNow(flowerSweepRoute(
                target.center,
                flowerRadiusMeters(),
                flowerSpacingMeters()
            ));
        });
        row.addView(open, new LinearLayout.LayoutParams(0, dp(56), 1f));
        Button delete = compactButton("×");
        delete.setTextColor(Color.parseColor("#ff8a93"));
        delete.setEnabled(!isFlowerModeActive());
        delete.setAlpha(isFlowerModeActive() ? 0.4f : 1f);
        delete.setOnClickListener(v -> confirmAction("刪除種花節點 " + index + "？", () -> {
            if (isFlowerModeActive()) {
                toast("種花模式執行中，請先關閉");
                return;
            }
            library.deleteFlowerTarget(target.id);
            refreshFlowerTargets();
        }));
        delete.setContentDescription("刪除種花節點 " + index);
        row.addView(delete, new LinearLayout.LayoutParams(dp(48), dp(48)));
        return row;
    }

    private String stateLabel(LocalSimulationEngine.Snapshot snapshot) {
        if (snapshot.paused) return "已暫停";
        if ("teleporting".equals(snapshot.state)) return "瞬移中";
        if ("navigating".equals(snapshot.state)) return "導航中";
        if ("path_navigation".equals(snapshot.state)) return "路徑導航中";
        if ("looping".equals(snapshot.state)) return "循環移動中";
        if ("multi_stop".equals(snapshot.state)) return "多點移動中";
        if ("random_walk".equals(snapshot.state)) return "隨機漫步中";
        if ("joystick".equals(snapshot.state)) return "搖桿控制中";
        if ("flower_planting".equals(snapshot.state)) return "種花中";
        if ("provider_error".equals(snapshot.state)) return "模擬位置失效，請重新啟用";
        return snapshot.current == null ? "未啟用" : "待命";
    }

    private LocalSimulationEngine.Point readCoordinate() {
        try {
            double lat = Double.parseDouble(latitudeInput.getText().toString().trim());
            double lng = Double.parseDouble(longitudeInput.getText().toString().trim());
            if (!validCoordinate(lat, lng)) throw new NumberFormatException();
            return new LocalSimulationEngine.Point(lat, lng);
        } catch (Exception error) {
            showError("請輸入有效座標：緯度 -90~90，經度 -180~180。");
            return null;
        }
    }

    private void pasteCoordinateFromClipboard() {
        LocalSimulationEngine.Point point = readClipboardCoordinate();
        if (point == null) {
            toast("座標格式錯誤，請使用「緯度, 經度」");
            return;
        }
        selectPoint(point, true);
        toast("座標已貼上");
    }

    private LocalSimulationEngine.Point readClipboardCoordinate() {
        ClipboardManager clipboard =
            (ClipboardManager) activity.getSystemService(Context.CLIPBOARD_SERVICE);
        if (clipboard == null || !clipboard.hasPrimaryClip()) {
            return null;
        }
        ClipData clip = clipboard.getPrimaryClip();
        if (clip == null || clip.getItemCount() == 0) {
            return null;
        }
        CharSequence rawValue = clip.getItemAt(0).coerceToText(activity);
        String raw = rawValue == null ? "" : rawValue.toString().trim();
        return CoordinateParser.parse(raw);
    }

    private void setCoordinateInputs(LocalSimulationEngine.Point point) {
        latitudeInput.setText(String.format(Locale.US, "%.6f", point.lat));
        longitudeInput.setText(String.format(Locale.US, "%.6f", point.lng));
    }

    private boolean validCoordinate(double lat, double lng) {
        return Double.isFinite(lat) && Double.isFinite(lng)
            && lat >= -90d && lat <= 90d
            && lng >= -180d && lng <= 180d;
    }

    private double positiveDoubleOr(EditText input, double fallback) {
        if (input == null) return fallback;
        try {
            double value = Double.parseDouble(input.getText().toString().trim());
            return value > 0d && Double.isFinite(value) ? value : fallback;
        } catch (Exception error) {
            return fallback;
        }
    }

    private Double optionalPositiveDouble(EditText input) {
        if (input == null || input.getText().toString().trim().isEmpty()) return null;
        double value = positiveDoubleOr(input, -1d);
        return value > 0d ? value : null;
    }

    private int optionalPositiveInt(EditText input) {
        if (input == null || input.getText().toString().trim().isEmpty()) return 0;
        return Math.max(0, integerOr(input, 0));
    }

    private int integerOr(EditText input, int fallback) {
        if (input == null) return fallback;
        try {
            return Integer.parseInt(input.getText().toString().trim());
        } catch (Exception error) {
            return fallback;
        }
    }

    private LinearLayout sectionHeading(String title) {
        LinearLayout block = new LinearLayout(activity);
        block.setOrientation(LinearLayout.VERTICAL);
        block.addView(label(title, 17, TEXT, true));
        return block;
    }

    private String appTitle() {
        CharSequence label = activity.getApplicationInfo()
            .loadLabel(activity.getPackageManager());
        return label == null ? "LocWarp" : label.toString();
    }

    private LinearLayout card() {
        LinearLayout card = new LinearLayout(activity);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setPadding(dp(16), dp(16), dp(16), dp(16));
        card.setBackground(roundRect(SURFACE, "#26304d", 16));
        card.setElevation(dp(3));
        return card;
    }

    private EditText numberInput(String hint) {
        EditText input = baseInput(hint);
        input.setSingleLine(true);
        input.setInputType(
            InputType.TYPE_CLASS_NUMBER
                | InputType.TYPE_NUMBER_FLAG_DECIMAL
                | InputType.TYPE_NUMBER_FLAG_SIGNED
        );
        return input;
    }

    private EditText textInput(String hint) {
        EditText input = baseInput(hint);
        input.setSingleLine(true);
        input.setInputType(InputType.TYPE_CLASS_TEXT);
        return input;
    }

    private EditText multilineInput(String hint) {
        EditText input = baseInput(hint);
        input.setSingleLine(false);
        input.setGravity(Gravity.TOP | Gravity.START);
        input.setInputType(
            InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_MULTI_LINE
        );
        input.setPadding(dp(12), dp(10), dp(12), dp(10));
        return input;
    }

    private EditText baseInput(String hint) {
        EditText input = new EditText(activity);
        input.setHint(hint);
        input.setHintTextColor(Color.parseColor("#687087"));
        input.setTextColor(Color.parseColor(TEXT));
        input.setTextSize(14);
        input.setPadding(dp(12), 0, dp(12), 0);
        input.setBackground(roundRect("#0f121b", "#303a58", 10));
        return input;
    }

    private CheckBox checkbox(String text) {
        CheckBox box = new CheckBox(activity);
        box.setText(text);
        box.setTextColor(Color.parseColor("#c7cddd"));
        box.setTextSize(13);
        box.setButtonTintList(new android.content.res.ColorStateList(
            new int[][] {
                new int[] {android.R.attr.state_checked},
                new int[] {}
            },
            new int[] {
                Color.parseColor(ACCENT),
                Color.parseColor("#59627a")
            }
        ));
        box.setPadding(0, dp(4), 0, dp(4));
        return box;
    }

    private Button disclosureButton(String title, View target) {
        Button button = secondaryButton(title + "  ▾");
        button.setGravity(Gravity.START | Gravity.CENTER_VERTICAL);
        button.setPadding(dp(14), 0, dp(14), 0);
        button.setOnClickListener(v -> {
            boolean show = target.getVisibility() != View.VISIBLE;
            target.setVisibility(show ? View.VISIBLE : View.GONE);
            button.setText(title + (show ? "  ▴" : "  ▾"));
        });
        return button;
    }

    private Button compactButton(String text) {
        Button button = new Button(activity);
        button.setAllCaps(false);
        button.setText(text);
        button.setTextSize(13);
        button.setGravity(Gravity.CENTER);
        button.setTextColor(Color.parseColor(MUTED));
        button.setPadding(dp(4), 0, dp(4), 0);
        button.setBackground(roundRect(ELEVATED, BORDER, 11));
        button.setMinHeight(dp(48));
        button.setMinimumHeight(dp(48));
        return button;
    }

    private Button primaryButton(String text) {
        Button button = compactButton(text);
        button.setTextSize(15);
        button.setTypeface(Typeface.DEFAULT_BOLD);
        button.setTextColor(Color.WHITE);
        button.setBackground(roundRect(ACCENT, "#88a0ff", 12));
        button.setElevation(dp(4));
        return button;
    }

    private Button secondaryButton(String text) {
        Button button = compactButton(text);
        button.setTextSize(13);
        button.setTextColor(Color.parseColor("#cbd1e2"));
        return button;
    }

    private Button transparentButton(String text) {
        Button button = new Button(activity);
        button.setAllCaps(false);
        button.setText(text);
        button.setTextSize(13);
        button.setTextColor(Color.parseColor("#d9deec"));
        button.setBackgroundColor(Color.TRANSPARENT);
        button.setPadding(0, 0, dp(6), 0);
        return button;
    }

    private void styleSelectable(Button button, boolean active) {
        button.setTextColor(Color.parseColor(active ? "#ffffff" : MUTED));
        button.setTypeface(active ? Typeface.DEFAULT_BOLD : Typeface.DEFAULT);
        button.setBackground(roundRect(
            active ? "#344b92" : ELEVATED,
            active ? ACCENT : BORDER,
            11
        ));
        button.setElevation(active ? dp(3) : 0);
    }

    private TextView label(String text, int sp, String color, boolean bold) {
        TextView view = new TextView(activity);
        view.setText(text);
        view.setTextSize(sp);
        view.setTextColor(Color.parseColor(color));
        if (bold) view.setTypeface(Typeface.DEFAULT_BOLD);
        return view;
    }

    private TextView emptyLabel(String text) {
        TextView view = label(text, 12, "#6f768c", false);
        view.setPadding(dp(8), dp(8), dp(8), 0);
        return view;
    }

    private LinearLayout horizontal() {
        LinearLayout row = new LinearLayout(activity);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);
        return row;
    }

    private void addEqualButtons(LinearLayout row, Button... buttons) {
        for (int i = 0; i < buttons.length; i++) {
            LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(0, dp(48), 1f);
            if (i > 0) lp.setMargins(dp(7), 0, 0, 0);
            row.addView(buttons[i], lp);
        }
    }

    private LinearLayout.LayoutParams cardLp(int top) {
        LinearLayout.LayoutParams lp = matchWrap();
        lp.setMargins(0, dp(top), 0, 0);
        return lp;
    }

    private LinearLayout.LayoutParams rowLp() {
        LinearLayout.LayoutParams lp = matchWrap();
        lp.setMargins(0, dp(6), 0, 0);
        return lp;
    }

    private LinearLayout.LayoutParams matchWrap() {
        return new LinearLayout.LayoutParams(
            LayoutParams.MATCH_PARENT,
            LayoutParams.WRAP_CONTENT
        );
    }

    private LinearLayout.LayoutParams wrap() {
        return new LinearLayout.LayoutParams(
            LayoutParams.WRAP_CONTENT,
            LayoutParams.WRAP_CONTENT
        );
    }

    private LinearLayout.LayoutParams matchHeight(int heightDp) {
        return new LinearLayout.LayoutParams(
            LayoutParams.MATCH_PARENT,
            dp(heightDp)
        );
    }

    private GradientDrawable roundRect(String fill, String stroke, int radiusDp) {
        GradientDrawable drawable = new GradientDrawable();
        drawable.setColor(Color.parseColor(fill));
        drawable.setCornerRadius(dp(radiusDp));
        drawable.setStroke(dp(1), Color.parseColor(stroke));
        return drawable;
    }

    private void toast(String message) {
        Toast.makeText(activity, message, Toast.LENGTH_SHORT).show();
    }

    private void showError(String message) {
        activity.showControlError(message);
    }

    private void confirmAction(String message, Runnable action) {
        new AlertDialog.Builder(activity)
            .setTitle("確認操作")
            .setMessage(message)
            .setPositiveButton("確認", (dialog, which) -> action.run())
            .setNegativeButton("取消", null)
            .show();
    }

    private int dp(int value) {
        return (int) (value * getResources().getDisplayMetrics().density + 0.5f);
    }
}
