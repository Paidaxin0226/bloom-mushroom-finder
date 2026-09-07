package com.locwarp.mock;

import android.annotation.SuppressLint;
import android.app.AlertDialog;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.Switch;
import android.widget.TextView;
import android.widget.Toast;

import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

@SuppressLint({"ViewConstructor", "SetTextI18n"})
final class HookScopePage extends ScrollView {
    private static final String SURFACE = "#141722";
    private static final String TEXT = "#e8eaf0";
    private static final String MUTED = "#9499ac";

    private final MainActivity activity;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final AsyncTaskGate scopeTasks = new AsyncTaskGate("LocWarpScope");
    private final Runnable serviceListener = () -> mainHandler.post(this::refresh);

    private LinearLayout contentRoot;
    private LinearLayout appList;
    private TextView sourceStatus;
    private Button addFallbackAppButton;
    private TextView diagnosticsText;
    private LinearLayout diagnosticsPanel;
    private boolean detached;

    HookScopePage(MainActivity activity) {
        super(activity);
        this.activity = activity;
        setFillViewport(true);
        setClipToPadding(false);
        setBackgroundColor(Color.parseColor("#0a0c12"));
        build();
        XposedScopeRepository.addListener(serviceListener);
        XposedScopeRepository.initialize();
        refresh();
    }

    void refresh() {
        if (detached || appList == null) return;
        sourceStatus.setText("正在讀取 LSPosed 作用域…");
        scopeTasks.submit(
            this::loadScope,
            mainHandler,
            this::renderScope,
            error -> renderFallback(error)
        );
    }

    void onPageSelected(boolean selected) {
        if (selected) refresh();
    }

    void setBottomContentInset(int bottomPx) {
        if (contentRoot == null) return;
        contentRoot.setPadding(dp(16), dp(16), dp(16), Math.max(dp(24), bottomPx));
    }

    @Override
    protected void onDetachedFromWindow() {
        detached = true;
        XposedScopeRepository.removeListener(serviceListener);
        scopeTasks.close();
        mainHandler.removeCallbacksAndMessages(null);
        super.onDetachedFromWindow();
    }

    private void build() {
        contentRoot = new LinearLayout(activity);
        contentRoot.setOrientation(LinearLayout.VERTICAL);
        contentRoot.setPadding(dp(16), dp(16), dp(16), dp(124));
        addView(contentRoot, new ScrollView.LayoutParams(
            LayoutParams.MATCH_PARENT,
            LayoutParams.WRAP_CONTENT
        ));

        TextView title = label("LSPosed 應用", 24, TEXT, true);
        title.setPadding(0, 0, 0, dp(12));
        contentRoot.addView(title);

        LinearLayout summary = card();
        LinearLayout summaryRow = horizontal();
        TextView heading = label("目前作用域", 17, TEXT, true);
        summaryRow.addView(heading, new LinearLayout.LayoutParams(
            0,
            LayoutParams.WRAP_CONTENT,
            1f
        ));
        Button refreshButton = compactButton("重新整理");
        refreshButton.setOnClickListener(v -> refresh());
        summaryRow.addView(refreshButton, new LinearLayout.LayoutParams(
            dp(96),
            dp(42)
        ));
        summary.addView(summaryRow);

        sourceStatus = label("正在讀取…", 12, "#cbd1e2", false);
        sourceStatus.setPadding(0, dp(10), 0, 0);
        summary.addView(sourceStatus);
        addFallbackAppButton = compactButton("補入未顯示的 App");
        addFallbackAppButton.setOnClickListener(v -> showInstalledAppPicker());
        LinearLayout.LayoutParams addAppLp = new LinearLayout.LayoutParams(
            LayoutParams.MATCH_PARENT,
            dp(44)
        );
        addAppLp.setMargins(0, dp(10), 0, 0);
        summary.addView(addFallbackAppButton, addAppLp);
        contentRoot.addView(summary);

        appList = new LinearLayout(activity);
        appList.setOrientation(LinearLayout.VERTICAL);
        LinearLayout.LayoutParams listLp = new LinearLayout.LayoutParams(
            LayoutParams.MATCH_PARENT,
            LayoutParams.WRAP_CONTENT
        );
        listLp.setMargins(0, dp(12), 0, 0);
        contentRoot.addView(appList, listLp);

        diagnosticsPanel = buildDiagnosticsCard();
        diagnosticsPanel.setVisibility(View.GONE);
        Button diagnosticsToggle = compactButton("診斷與紀錄  ▾");
        diagnosticsToggle.setGravity(Gravity.START | Gravity.CENTER_VERTICAL);
        diagnosticsToggle.setPadding(dp(14), 0, dp(14), 0);
        diagnosticsToggle.setOnClickListener(v -> {
            boolean show = diagnosticsPanel.getVisibility() != View.VISIBLE;
            diagnosticsPanel.setVisibility(show ? View.VISIBLE : View.GONE);
            diagnosticsToggle.setText(show ? "診斷與紀錄  ▴" : "診斷與紀錄  ▾");
        });
        LinearLayout.LayoutParams diagnosticsToggleLp =
            new LinearLayout.LayoutParams(LayoutParams.MATCH_PARENT, dp(48));
        diagnosticsToggleLp.setMargins(0, dp(12), 0, 0);
        contentRoot.addView(diagnosticsToggle, diagnosticsToggleLp);
        contentRoot.addView(diagnosticsPanel, cardLp(10));
    }

    private LinearLayout buildDiagnosticsCard() {
        LinearLayout panel = card();
        TextView heading = label("一鍵診斷與紀錄", 17, TEXT, true);
        panel.addView(heading);
        diagnosticsText = label(
            "按下診斷可檢查版本、簽章、權限、LSPosed、Hook 握手與 Relay。",
            12,
            MUTED,
            false
        );
        diagnosticsText.setPadding(0, dp(8), 0, 0);
        diagnosticsText.setTextIsSelectable(true);
        panel.addView(diagnosticsText);

        Button run = compactButton("執行診斷");
        run.setOnClickListener(v -> diagnosticsText.setText(
            DiagnosticsReport.create(activity)
        ));
        Button copy = compactButton("複製報告");
        copy.setOnClickListener(v -> copyText(
            "LocWarp diagnostics",
            DiagnosticsReport.create(activity)
        ));
        Button csv = compactButton("歷史 CSV");
        csv.setOnClickListener(v -> copyText(
            "LocWarp history CSV",
            OperationHistoryStore.toCsv(activity)
        ));
        Button clear = compactButton("清除歷史");
        clear.setOnClickListener(v -> new AlertDialog.Builder(activity)
            .setTitle("清除操作歷史？")
            .setMessage("收藏與路線不會被刪除。")
            .setPositiveButton("清除", (dialog, which) -> {
                OperationHistoryStore.clear(activity);
                toast("操作歷史已清除");
            })
            .setNegativeButton("取消", null)
            .show());
        Button[] buttons = {run, copy, csv, clear};
        for (int rowIndex = 0; rowIndex < 2; rowIndex++) {
            LinearLayout actions = horizontal();
            actions.setPadding(0, rowIndex == 0 ? dp(10) : dp(8), 0, 0);
            for (int column = 0; column < 2; column++) {
                Button button = buttons[rowIndex * 2 + column];
                LinearLayout.LayoutParams lp =
                    new LinearLayout.LayoutParams(0, dp(48), 1f);
                if (column > 0) lp.setMargins(dp(8), 0, 0, 0);
                actions.addView(button, lp);
            }
            panel.addView(actions);
        }
        return panel;
    }

    private void copyText(String label, String value) {
        ClipboardManager clipboard = (ClipboardManager) activity.getSystemService(
            Context.CLIPBOARD_SERVICE
        );
        if (clipboard == null) {
            toast("無法使用剪貼簿");
            return;
        }
        clipboard.setPrimaryClip(ClipData.newPlainText(label, value));
        toast("已複製");
    }

    private ScopeResult loadScope() {
        if (XposedScopeRepository.isServiceConnected()) {
            Set<String> unique = new LinkedHashSet<>(
                XposedScopeRepository.getScope()
            );
            unique.remove(activity.getPackageName());
            return new ScopeResult(
                sortedPackages(unique),
                true,
                XposedScopeRepository.frameworkLabel()
            );
        }
        return new ScopeResult(
            sortedPackages(HookScopeSettings.fallbackPackages(activity)),
            false,
            ""
        );
    }

    private void renderFallback(Throwable error) {
        renderScope(new ScopeResult(
            sortedPackages(HookScopeSettings.fallbackPackages(activity)),
            false,
            ""
        ));
    }

    private List<AppRow> sortedPackages(Set<String> packages) {
        List<AppRow> rows = new ArrayList<>();
        PackageManager manager = activity.getPackageManager();
        for (String packageName : packages) {
            if (!HookScopeSettings.isValidPackageName(packageName)) continue;
            String label = packageName;
            try {
                ApplicationInfo info = manager.getApplicationInfo(packageName, 0);
                CharSequence loaded = manager.getApplicationLabel(info);
                if (loaded != null && loaded.length() > 0) {
                    label = loaded.toString();
                }
            } catch (Throwable ignored) {
            }
            rows.add(new AppRow(packageName, label));
        }
        rows.sort(Comparator
            .comparing((AppRow row) -> row.label, String.CASE_INSENSITIVE_ORDER)
            .thenComparing(row -> row.packageName));
        return rows;
    }

    private void renderScope(ScopeResult result) {
        if (detached || appList == null) return;
        appList.removeAllViews();
        if (result.officialScope) {
            addFallbackAppButton.setVisibility(View.GONE);
            sourceStatus.setText(
                "已連接 " + result.frameworkLabel + "，共 "
                    + result.rows.size() + " 個應用。"
            );
            sourceStatus.setTextColor(Color.parseColor("#9fe3b1"));
        } else {
            addFallbackAppButton.setVisibility(View.VISIBLE);
            Throwable initError = XposedScopeRepository.initializationError();
            String reason = initError == null
                ? "框架未提供 scope service"
                : "scope service 無法載入";
            sourceStatus.setText(
                reason + "；目前顯示已回報或手動補入的 "
                    + result.rows.size() + " 個應用。未啟動的 Google Fit 可由下方補入。"
            );
            sourceStatus.setTextColor(Color.parseColor("#ffcf7a"));
        }

        if (result.rows.isEmpty()) {
            LinearLayout empty = card();
            TextView message = label(
                "尚未找到作用域應用。請先到 LSPosed 勾選目標 App，再強制停止並重開它。",
                13,
                "#cbd1e2",
                false
            );
            message.setLineSpacing(dp(3), 1f);
            empty.addView(message);
            appList.addView(empty);
            return;
        }

        for (int index = 0; index < result.rows.size(); index++) {
            AppRow row = result.rows.get(index);
            LinearLayout card = buildAppCard(row, result.officialScope);
            LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                LayoutParams.MATCH_PARENT,
                LayoutParams.WRAP_CONTENT
            );
            if (index > 0) lp.setMargins(0, dp(10), 0, 0);
            appList.addView(card, lp);
            StepHookController.sendConfig(activity, row.packageName);
        }
    }

    private LinearLayout buildAppCard(AppRow row, boolean officialScope) {
        LinearLayout panel = card();
        TextView appLabel = label(row.label, 16, TEXT, true);
        panel.addView(appLabel);
        if (!row.label.equals(row.packageName)) {
            TextView packageLabel = label(row.packageName, 11, MUTED, false);
            packageLabel.setPadding(0, dp(3), 0, dp(8));
            panel.addView(packageLabel);
        } else {
            appLabel.setPadding(0, 0, 0, dp(8));
        }

        Switch locationSwitch = switchRow(
            panel,
            "Location Hook",
            HookScopeSettings.isLocationEnabled(activity, row.packageName)
        );
        Switch stepSwitch = switchRow(
            panel,
            "Step Hook",
            HookScopeSettings.isStepEnabled(activity, row.packageName)
        );

        locationSwitch.setOnCheckedChangeListener((button, enabled) -> {
            HookScopeSettings.setLocationEnabled(
                activity,
                row.packageName,
                enabled
            );
            StepHookController.sendConfig(activity, row.packageName);
            toast(row.label + "：定位 Hook " + (enabled ? "已開啟" : "已關閉"));
        });
        stepSwitch.setOnCheckedChangeListener((button, enabled) -> {
            HookScopeSettings.setStepEnabled(
                activity,
                row.packageName,
                enabled
            );
            StepHookController.sendConfig(activity, row.packageName);
            toast(row.label + "：步數 Hook " + (enabled ? "已開啟" : "已關閉"));
        });
        if (!officialScope
            && HookScopeSettings.manualPackages(activity).contains(row.packageName)) {
            Button remove = compactButton("從備援清單移除");
            remove.setOnClickListener(v -> {
                HookScopeSettings.forgetManualPackage(activity, row.packageName);
                refresh();
            });
            LinearLayout.LayoutParams removeLp = new LinearLayout.LayoutParams(
                LayoutParams.MATCH_PARENT,
                dp(42)
            );
            removeLp.setMargins(0, dp(8), 0, 0);
            panel.addView(remove, removeLp);
        }
        return panel;
    }

    private void showInstalledAppPicker() {
        Intent launcherIntent = new Intent(Intent.ACTION_MAIN)
            .addCategory(Intent.CATEGORY_LAUNCHER);
        PackageManager manager = activity.getPackageManager();
        List<ResolveInfo> resolved = manager.queryIntentActivities(launcherIntent, 0);
        Set<String> packages = new LinkedHashSet<>();
        for (ResolveInfo info : resolved) {
            if (info.activityInfo == null) continue;
            String packageName = info.activityInfo.packageName;
            if (activity.getPackageName().equals(packageName)) continue;
            packages.add(packageName);
        }
        List<AppRow> candidates = sortedPackages(packages);
        if (candidates.isEmpty()) {
            toast("找不到可加入的已安裝 App");
            return;
        }
        String[] labels = new String[candidates.size()];
        for (int index = 0; index < candidates.size(); index++) {
            AppRow item = candidates.get(index);
            labels[index] = item.label + "\n" + item.packageName;
        }
        new AlertDialog.Builder(activity)
            .setTitle("補入未顯示的 App")
            .setItems(labels, (dialog, which) -> {
                AppRow selected = candidates.get(which);
                HookScopeSettings.rememberManualPackage(
                    activity,
                    selected.packageName
                );
                StepHookController.sendConfig(activity, selected.packageName);
                refresh();
                toast("已補入 " + selected.label);
            })
            .setNegativeButton("取消", null)
            .show();
    }

    private Switch switchRow(
        LinearLayout panel,
        String title,
        boolean checked
    ) {
        LinearLayout row = horizontal();
        row.setPadding(0, dp(4), 0, dp(4));
        TextView text = label(title, 13, "#d9deec", true);
        row.addView(text, new LinearLayout.LayoutParams(
            0,
            LayoutParams.WRAP_CONTENT,
            1f
        ));
        Switch toggle = new Switch(activity);
        toggle.setChecked(checked);
        row.addView(toggle);
        panel.addView(row);
        return toggle;
    }

    private LinearLayout card() {
        LinearLayout card = new LinearLayout(activity);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setPadding(dp(16), dp(16), dp(16), dp(16));
        card.setBackground(roundRect(SURFACE, "#26304d", 16));
        card.setElevation(dp(3));
        return card;
    }

    private LinearLayout.LayoutParams cardLp(int topMarginDp) {
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
            LayoutParams.MATCH_PARENT,
            LayoutParams.WRAP_CONTENT
        );
        params.setMargins(0, dp(topMarginDp), 0, 0);
        return params;
    }

    private Button compactButton(String text) {
        Button button = new Button(activity);
        button.setAllCaps(false);
        button.setText(text);
        button.setTextSize(13);
        button.setTextColor(Color.parseColor("#cbd1e2"));
        button.setPadding(dp(8), 0, dp(8), 0);
        button.setMinHeight(0);
        button.setMinimumHeight(0);
        button.setBackground(roundRect("#1f2334", "#2d3550", 11));
        return button;
    }

    private TextView label(String text, int sp, String color, boolean bold) {
        TextView view = new TextView(activity);
        view.setText(text);
        view.setTextSize(sp);
        view.setTextColor(Color.parseColor(color));
        if (bold) view.setTypeface(Typeface.DEFAULT_BOLD);
        return view;
    }

    private LinearLayout horizontal() {
        LinearLayout row = new LinearLayout(activity);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);
        return row;
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

    private int dp(int value) {
        return (int) (value * getResources().getDisplayMetrics().density + 0.5f);
    }

    private static final class AppRow {
        final String packageName;
        final String label;

        AppRow(String packageName, String label) {
            this.packageName = packageName;
            this.label = label;
        }
    }

    private static final class ScopeResult {
        final List<AppRow> rows;
        final boolean officialScope;
        final String frameworkLabel;

        ScopeResult(
            List<AppRow> rows,
            boolean officialScope,
            String frameworkLabel
        ) {
            this.rows = Collections.unmodifiableList(new ArrayList<>(rows));
            this.officialScope = officialScope;
            this.frameworkLabel = frameworkLabel;
        }
    }
}
