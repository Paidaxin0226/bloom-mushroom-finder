package com.locwarp.mock;

import android.annotation.SuppressLint;
import android.app.AlertDialog;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.os.Handler;
import android.os.Looper;
import android.os.PersistableBundle;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import java.util.List;

@SuppressLint({"ViewConstructor", "SetTextI18n"})
final class RelayControlPage extends ScrollView
    implements RemoteRelayClient.Listener {

    private static final String ACCENT = "#6c8cff";
    private static final String SURFACE = "#141722";
    private static final String ELEVATED = "#1f2334";
    private static final String BORDER = "#2d3550";
    private static final String TEXT = "#e8eaf0";
    private static final String MUTED = "#9499ac";

    private final MainActivity activity;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final RemoteRelayClient relayClient;

    private LinearLayout contentRoot;
    private TextView usbStatus;
    private Button usbStartButton;
    private Button usbStopButton;
    private LinearLayout relaySetupPanel;
    private LinearLayout relayActivePanel;
    private EditText relayBaseUrlInput;
    private TextView relayStatus;
    private TextView relayUrlText;
    private Button relayStartButton;
    private Button relayStopButton;
    private Button relayCopyButton;
    private Button relayRotateButton;
    private ImageView relayQrImage;
    private LinearLayout relayViewerList;
    private long relayQrLoadToken;
    private boolean detached;

    RelayControlPage(MainActivity activity) {
        super(activity);
        this.activity = activity;
        relayClient = RemoteRelayClient.get(activity);
        setFillViewport(true);
        setClipToPadding(false);
        setBackgroundColor(Color.parseColor("#0a0c12"));
        build();
        relayClient.setListener(this);
        refresh();
    }

    void refresh() {
        if (detached) return;
        renderUsbState();
        boolean running = relayClient.isRunning();
        if (!running
            && relayBaseUrlInput != null
            && !relayBaseUrlInput.hasFocus()) {
            relayBaseUrlInput.setText(OverlaySettings.relayBaseUrl(activity));
        }
        String message = relayClient.isConnected()
            ? "Relay connected"
            : running ? "Relay connecting" : "Relay off";
        renderRelayState(relayClient.isConnected(), message, relayClient.publicUrl());
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
        relayQrLoadToken++;
        relayClient.setListener(null);
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

        LinearLayout titleRow = horizontal();
        TextView title = label("連線", 24, TEXT, true);
        titleRow.addView(title, new LinearLayout.LayoutParams(
            0,
            LayoutParams.WRAP_CONTENT,
            1f
        ));
        relayStatus = label("Relay off", 12, MUTED, true);
        relayStatus.setGravity(Gravity.CENTER);
        relayStatus.setPadding(dp(12), dp(7), dp(12), dp(7));
        relayStatus.setBackground(roundRect(ELEVATED, BORDER, 18));
        titleRow.addView(relayStatus);
        contentRoot.addView(titleRow);

        LinearLayout usbCard = card();
        usbCard.addView(label("電腦 USB 控制", 17, TEXT, true));
        TextView usbNote = label(
            "App 啟動時會自動開啟 USB 控制；Windows 端也會自動連回上次使用的手機。"
                + "通道沿用 Android 系統的 USB 偵錯授權，"
                + "不需要額外配對碼，也不會開放區域網路連接。",
            12,
            MUTED,
            false
        );
        usbNote.setPadding(0, dp(8), 0, 0);
        usbNote.setLineSpacing(dp(2), 1f);
        usbCard.addView(usbNote);

        usbStatus = label("USB 控制未啟動", 12, MUTED, true);
        usbStatus.setPadding(dp(12), dp(10), dp(12), dp(10));
        usbStatus.setBackground(roundRect("#0f121b", "#303a58", 10));
        usbCard.addView(usbStatus, cardLp(10));

        LinearLayout usbActions = horizontal();
        usbActions.setPadding(0, dp(10), 0, 0);
        usbStartButton = primaryButton("啟動 USB 控制");
        usbStartButton.setOnClickListener(v -> {
            activity.startUsbControlService();
            mainHandler.postDelayed(this::refresh, 250L);
        });
        usbStopButton = secondaryButton("停止");
        usbStopButton.setTextColor(Color.parseColor("#ff9aa2"));
        usbStopButton.setTypeface(Typeface.DEFAULT_BOLD);
        usbStopButton.setOnClickListener(v -> {
            activity.stopUsbControlService();
            mainHandler.postDelayed(this::refresh, 150L);
        });
        usbActions.addView(usbStartButton, new LinearLayout.LayoutParams(0, dp(50), 1f));
        LinearLayout.LayoutParams usbStopLp = new LinearLayout.LayoutParams(0, dp(50), 1f);
        usbStopLp.setMargins(dp(8), 0, 0, 0);
        usbActions.addView(usbStopButton, usbStopLp);
        usbCard.addView(usbActions);
        contentRoot.addView(usbCard, cardLp(12));

        LinearLayout admissionCard = card();
        admissionCard.addView(label("伺服器裝置授權", 17, TEXT, true));
        TextView admissionNote = label(
            "Relay 只接受白名單內的手機建立工作階段。"
                + "私鑰保存在本機 Android Keystore，不會傳出手機。",
            12,
            MUTED,
            false
        );
        admissionNote.setPadding(0, dp(8), 0, 0);
        admissionNote.setLineSpacing(dp(2), 1f);
        admissionCard.addView(admissionNote);

        String admissionFingerprint = relayClient.admissionFingerprint();
        TextView fingerprint = label(
            admissionFingerprint.isEmpty()
                ? "裝置金鑰無法使用"
                : "SHA-256  " + admissionFingerprint,
            11,
            admissionFingerprint.isEmpty() ? "#ff9aa2" : "#cbd1e2",
            false
        );
        fingerprint.setTextIsSelectable(true);
        fingerprint.setPadding(dp(12), dp(12), dp(12), dp(12));
        fingerprint.setBackground(roundRect("#0f121b", "#303a58", 10));
        admissionCard.addView(fingerprint, cardLp(10));

        String admissionEntry = relayClient.admissionPublicKeyEntry();
        TextView admissionValue = label(admissionEntry, 10, MUTED, false);
        admissionValue.setTextIsSelectable(true);
        admissionValue.setContentDescription(
            "Relay 主機白名單 " + admissionEntry
        );
        admissionValue.setPadding(dp(2), dp(10), dp(2), 0);
        admissionCard.addView(admissionValue);

        Button copyAdmission = secondaryButton("複製伺服器白名單公鑰");
        copyAdmission.setEnabled(!admissionEntry.isEmpty());
        copyAdmission.setOnClickListener(v -> copyAdmissionPublicKey());
        admissionCard.addView(copyAdmission, cardLp(10));
        contentRoot.addView(admissionCard, cardLp(12));

        relaySetupPanel = card();
        relaySetupPanel.addView(label("啟動遠端控制", 17, TEXT, true));
        TextView note = label(
            "啟動後，另一支裝置可掃描 QR Code 開啟控制頁。"
                + "每個新裝置都必須先在這支手機上核准。",
            12,
            MUTED,
            false
        );
        note.setPadding(0, dp(8), 0, 0);
        note.setLineSpacing(dp(2), 1f);
        relaySetupPanel.addView(note);

        relayBaseUrlInput = textInput("中繼伺服器 URL");
        relayBaseUrlInput.setText(OverlaySettings.relayBaseUrl(activity));
        LinearLayout.LayoutParams relayBaseLp = new LinearLayout.LayoutParams(
            LayoutParams.MATCH_PARENT,
            dp(50)
        );
        relayBaseLp.setMargins(0, dp(12), 0, 0);
        relaySetupPanel.addView(relayBaseUrlInput, relayBaseLp);

        relayStartButton = primaryButton("啟動 Relay");
        relayStartButton.setOnClickListener(v -> startRelayControl());
        LinearLayout.LayoutParams startLp = new LinearLayout.LayoutParams(
            LayoutParams.MATCH_PARENT,
            dp(54)
        );
        startLp.setMargins(0, dp(12), 0, 0);
        relaySetupPanel.addView(relayStartButton, startLp);
        contentRoot.addView(relaySetupPanel, cardLp(12));

        relayActivePanel = card();
        relayActivePanel.addView(label("掃描 QR Code 開始控制", 17, TEXT, true));

        relayQrImage = new ImageView(activity);
        relayQrImage.setContentDescription("Relay 遠端控制 QR Code");
        relayQrImage.setBackgroundColor(Color.WHITE);
        relayQrImage.setPadding(dp(10), dp(10), dp(10), dp(10));
        relayQrImage.setAdjustViewBounds(true);
        relayQrImage.setScaleType(ImageView.ScaleType.FIT_CENTER);
        relayQrImage.setVisibility(View.GONE);
        LinearLayout.LayoutParams qrLp = new LinearLayout.LayoutParams(
            LayoutParams.MATCH_PARENT,
            dp(260)
        );
        qrLp.setMargins(0, dp(12), 0, 0);
        relayActivePanel.addView(relayQrImage, qrLp);

        relayUrlText = label("", 12, "#cbd1e2", false);
        relayUrlText.setPadding(dp(12), dp(12), dp(12), dp(12));
        relayUrlText.setTextIsSelectable(true);
        relayUrlText.setBackground(roundRect("#0f121b", "#303a58", 10));
        relayUrlText.setVisibility(View.GONE);
        relayActivePanel.addView(relayUrlText, cardLp(10));

        relayCopyButton = primaryButton("複製控制網址");
        relayCopyButton.setOnClickListener(v -> copyRelayUrl());
        relayActivePanel.addView(relayCopyButton, cardLp(10));

        LinearLayout activeActions = horizontal();
        activeActions.setPadding(0, dp(8), 0, 0);
        relayRotateButton = secondaryButton("更換 QR");
        relayRotateButton.setOnClickListener(v -> relayClient.rotateSession());
        relayStopButton = secondaryButton("停止");
        relayStopButton.setTextColor(Color.parseColor("#ff9aa2"));
        relayStopButton.setTypeface(Typeface.DEFAULT_BOLD);
        relayStopButton.setOnClickListener(v -> stopRelayControl());
        activeActions.addView(
            relayRotateButton,
            new LinearLayout.LayoutParams(0, dp(50), 1f)
        );
        LinearLayout.LayoutParams stopLp =
            new LinearLayout.LayoutParams(0, dp(50), 1f);
        stopLp.setMargins(dp(8), 0, 0, 0);
        activeActions.addView(relayStopButton, stopLp);
        relayActivePanel.addView(activeActions);

        TextView viewersTitle = label("已連線裝置", 15, TEXT, true);
        viewersTitle.setPadding(0, dp(14), 0, 0);
        relayActivePanel.addView(viewersTitle);
        relayViewerList = new LinearLayout(activity);
        relayViewerList.setOrientation(LinearLayout.VERTICAL);
        relayActivePanel.addView(relayViewerList);
        renderViewerList(relayClient.viewers());

        relayActivePanel.setVisibility(View.GONE);
        contentRoot.addView(relayActivePanel, cardLp(12));
    }

    private void renderUsbState() {
        boolean running = UsbControlService.isRunning();
        String error = UsbControlService.serverError();
        if (usbStatus != null) {
            usbStatus.setText(!error.isEmpty()
                ? "USB 控制錯誤：" + error
                : running ? "USB 控制已啟動，等待 ADB 電腦連線" : "USB 控制未啟動");
            usbStatus.setTextColor(Color.parseColor(
                !error.isEmpty() ? "#ff9aa2" : running ? "#9fe3b1" : MUTED
            ));
        }
        setControlEnabled(usbStartButton, !running, 0.55f);
        setControlEnabled(usbStopButton, running, 0.45f);
    }

    private void startRelayControl() {
        if (!activity.ensureMockPermission()) return;
        String baseUrl = readRelayBaseUrl();
        if (baseUrl == null) return;
        OverlaySettings.setRelayBaseUrl(activity, baseUrl);
        MockLocationKeeper.setMockEnabled(activity, true);
        activity.startRelayService(baseUrl);
    }

    private void stopRelayControl() {
        activity.stopRelayService();
        relayQrLoadToken++;
        clearQr();
    }

    private void copyRelayUrl() {
        String url = relayClient.publicUrl();
        if (url.isEmpty()) {
            toast("尚未產生 Relay 網址");
            return;
        }
        ClipboardManager clipboard =
            (ClipboardManager) activity.getSystemService(Context.CLIPBOARD_SERVICE);
        if (clipboard == null) {
            toast("無法使用剪貼簿");
            return;
        }
        ClipData clip = ClipData.newPlainText("LocWarp relay", url);
        PersistableBundle extras = new PersistableBundle();
        extras.putBoolean("android.content.extra.IS_SENSITIVE", true);
        clip.getDescription().setExtras(extras);
        clipboard.setPrimaryClip(clip);
        toast("已複製 Relay 網址");
    }

    private void copyAdmissionPublicKey() {
        String entry = relayClient.admissionPublicKeyEntry();
        if (entry.isEmpty()) {
            toast("裝置授權金鑰無法使用");
            return;
        }
        ClipboardManager clipboard =
            (ClipboardManager) activity.getSystemService(Context.CLIPBOARD_SERVICE);
        if (clipboard == null) {
            toast("無法使用剪貼簿");
            return;
        }
        clipboard.setPrimaryClip(
            ClipData.newPlainText("LocWarp Relay host public key", entry)
        );
        toast("已複製伺服器白名單公鑰");
    }

    @Override
    public void onRelayChanged(boolean connected, String message, String publicUrl) {
        mainHandler.post(() -> renderRelayState(connected, message, publicUrl));
    }

    @Override
    public void onRelayViewerApprovalRequested(RemoteRelayClient.Viewer viewer) {
        mainHandler.post(() -> {
            if (detached || activity.isFinishing()) return;
            String requested = "readonly".equals(viewer.accessRole)
                ? "唯讀"
                : "控制";
            String device = viewer.deviceId.isEmpty()
                ? ""
                : "\n裝置識別：" + shortId(viewer.deviceId);
            String pairing = viewer.pairingCode.isEmpty()
                ? ""
                : "\n配對碼：" + viewer.pairingCode;
            new AlertDialog.Builder(activity)
                .setTitle("遠端控制連線請求")
                .setMessage(
                    viewer.name + " 要求「" + requested + "」權限。"
                        + device
                        + pairing
                        + "\n\n請確認瀏覽器顯示相同配對碼。"
                        + "\n你也可以只允許查看目前狀態。"
                )
                .setPositiveButton(
                    "允許控制",
                    (dialog, which) -> relayClient.approveViewer(
                        viewer.id,
                        true,
                        "controller"
                    )
                )
                .setNeutralButton(
                    "僅查看",
                    (dialog, which) -> relayClient.approveViewer(
                        viewer.id,
                        true,
                        "readonly"
                    )
                )
                .setNegativeButton(
                    "拒絕",
                    (dialog, which) -> relayClient.approveViewer(
                        viewer.id,
                        false,
                        "readonly"
                    )
                )
                .setOnCancelListener(dialog -> relayClient.approveViewer(
                    viewer.id,
                    false,
                    "readonly"
                ))
                .setCancelable(true)
                .show();
        });
    }

    @Override
    public void onRelayViewerCountChanged(int viewerCount) {
        mainHandler.post(() -> {
            if (detached || relayStatus == null) return;
            String base = relayClient.isConnected()
                ? "Relay connected"
                : relayClient.isRunning() ? "Relay connecting" : "Relay off";
            relayStatus.setText(withViewerCount(base, viewerCount));
        });
    }

    @Override
    public void onRelayViewersChanged(List<RemoteRelayClient.Viewer> viewers) {
        mainHandler.post(() -> renderViewerList(viewers));
    }

    private void renderRelayState(
        boolean connected,
        String message,
        String publicUrl
    ) {
        if (detached) return;
        syncRelayControls(relayClient.isRunning());
        renderViewerList(relayClient.viewers());
        if (relayStatus != null) {
            relayStatus.setText(withViewerCount(
                message == null ? "" : message,
                relayClient.viewerCount()
            ));
            relayStatus.setTextColor(Color.parseColor(
                connected ? "#9fe3b1" : relayClient.isRunning() ? "#ffcf7a" : MUTED
            ));
        }
        if (publicUrl == null || publicUrl.isEmpty()) {
            if (relayUrlText != null) {
                relayUrlText.setText("");
                relayUrlText.setVisibility(View.GONE);
            }
            clearQr();
            return;
        }
        if (relayUrlText != null) {
            relayUrlText.setText(publicUrl);
            relayUrlText.setVisibility(View.VISIBLE);
        }
        if (relayQrImage != null && !publicUrl.equals(relayQrImage.getTag())) {
            relayQrImage.setTag(publicUrl);
            relayQrImage.setVisibility(View.VISIBLE);
            renderRelayQr(publicUrl);
        }
    }

    private String withViewerCount(String message, int viewerCount) {
        return message + (viewerCount > 0
            ? " · " + viewerCount + " 個遠端裝置"
            : "");
    }

    private void renderViewerList(List<RemoteRelayClient.Viewer> viewers) {
        if (relayViewerList == null || detached) return;
        relayViewerList.removeAllViews();
        if (viewers == null || viewers.isEmpty()) {
            TextView empty = label("目前沒有已核准的遠端裝置", 12, MUTED, false);
            empty.setPadding(0, dp(8), 0, 0);
            relayViewerList.addView(empty);
            return;
        }
        for (RemoteRelayClient.Viewer viewer : viewers) {
            LinearLayout card = new LinearLayout(activity);
            card.setOrientation(LinearLayout.VERTICAL);
            card.setPadding(dp(10), dp(10), dp(10), dp(10));
            card.setBackground(roundRect(ELEVATED, BORDER, 10));
            TextView title = label(viewer.name, 13, TEXT, true);
            card.addView(title);
            String role = "readonly".equals(viewer.accessRole) ? "唯讀" : "可控制";
            String detail = role + " · " + shortId(viewer.deviceId);
            card.addView(label(detail, 11, MUTED, false));

            LinearLayout actions = horizontal();
            actions.setPadding(0, dp(7), 0, 0);
            Button roleButton = secondaryButton(
                "readonly".equals(viewer.accessRole) ? "改為可控制" : "改為唯讀"
            );
            roleButton.setOnClickListener(v -> relayClient.changeViewerRole(
                viewer.id,
                "readonly".equals(viewer.accessRole) ? "controller" : "readonly"
            ));
            Button disconnect = secondaryButton("中斷");
            disconnect.setTextColor(Color.parseColor("#ff9aa2"));
            disconnect.setOnClickListener(v -> relayClient.disconnectViewer(viewer.id));
            addEqualButtons(actions, roleButton, disconnect);
            card.addView(actions);
            LinearLayout.LayoutParams lp = cardLp(7);
            relayViewerList.addView(card, lp);
        }
    }

    private String shortId(String value) {
        if (value == null || value.isEmpty()) return "未知裝置";
        return value.length() <= 12 ? value : value.substring(0, 12) + "…";
    }

    private void syncRelayControls(boolean running) {
        if (relaySetupPanel != null) {
            relaySetupPanel.setVisibility(running ? View.GONE : View.VISIBLE);
        }
        if (relayActivePanel != null) {
            relayActivePanel.setVisibility(running ? View.VISIBLE : View.GONE);
        }
        setControlEnabled(relayBaseUrlInput, !running, 0.65f);
        setControlEnabled(relayStartButton, !running, 0.55f);
        setControlEnabled(relayStopButton, running, 0.45f);
        setControlEnabled(relayCopyButton, running, 0.45f);
        setControlEnabled(relayRotateButton, running, 0.45f);
    }

    private void setControlEnabled(View view, boolean enabled, float disabledAlpha) {
        if (view == null) return;
        view.setEnabled(enabled);
        view.setAlpha(enabled ? 1f : disabledAlpha);
    }

    private void clearQr() {
        if (relayQrImage == null) return;
        relayQrImage.setImageBitmap(null);
        relayQrImage.setTag(null);
        relayQrImage.setVisibility(View.GONE);
    }

    private void renderRelayQr(String value) {
        long token = ++relayQrLoadToken;
        new Thread(() -> {
            try {
                Bitmap bitmap = QrCodeBitmap.render(value, dp(240));
                mainHandler.post(() -> {
                    if (detached
                        || token != relayQrLoadToken
                        || relayQrImage == null) {
                        return;
                    }
                    relayQrImage.setImageBitmap(bitmap);
                    relayQrImage.setVisibility(View.VISIBLE);
                });
            } catch (Exception error) {
                mainHandler.post(() -> {
                    if (detached
                        || token != relayQrLoadToken
                        || relayStatus == null) {
                        return;
                    }
                    relayStatus.setText("QR Code 產生失敗");
                    relayStatus.setTextColor(Color.parseColor("#ff9aa2"));
                });
            }
        }, "LocWarpRelayQr").start();
    }

    private String readRelayBaseUrl() {
        String value = relayBaseUrlInput == null
            ? OverlaySettings.relayBaseUrl(activity)
            : relayBaseUrlInput.getText().toString().trim();
        if (value.isEmpty()) {
            showError("請輸入中繼伺服器 URL。");
            return null;
        }
        while (value.endsWith("/")) {
            value = value.substring(0, value.length() - 1);
        }
        if (!value.startsWith("https://")) {
            showError("Relay 必須使用 HTTPS，例如 https://relay.example.com");
            return null;
        }
        return value;
    }

    private LinearLayout card() {
        LinearLayout card = new LinearLayout(activity);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setPadding(dp(16), dp(16), dp(16), dp(16));
        card.setBackground(roundRect(SURFACE, "#26304d", 16));
        card.setElevation(dp(3));
        return card;
    }

    private EditText textInput(String hint) {
        EditText input = new EditText(activity);
        input.setHint(hint);
        input.setHintTextColor(Color.parseColor("#687087"));
        input.setTextColor(Color.parseColor(TEXT));
        input.setTextSize(14);
        input.setSingleLine(true);
        input.setInputType(
            InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI
        );
        input.setPadding(dp(12), 0, dp(12), 0);
        input.setBackground(roundRect("#0f121b", "#303a58", 10));
        return input;
    }

    private Button primaryButton(String text) {
        Button button = secondaryButton(text);
        button.setTextSize(15);
        button.setTypeface(Typeface.DEFAULT_BOLD);
        button.setTextColor(Color.WHITE);
        button.setBackground(roundRect(ACCENT, "#88a0ff", 12));
        button.setElevation(dp(4));
        return button;
    }

    private Button secondaryButton(String text) {
        Button button = new Button(activity);
        button.setAllCaps(false);
        button.setText(text);
        button.setTextSize(13);
        button.setGravity(Gravity.CENTER);
        button.setTextColor(Color.parseColor("#cbd1e2"));
        button.setPadding(dp(4), 0, dp(4), 0);
        button.setMinHeight(dp(48));
        button.setMinimumHeight(dp(48));
        button.setBackground(roundRect(ELEVATED, BORDER, 11));
        return button;
    }

    private LinearLayout.LayoutParams cardLp(int topMarginDp) {
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
            LayoutParams.MATCH_PARENT,
            LayoutParams.WRAP_CONTENT
        );
        params.setMargins(0, dp(topMarginDp), 0, 0);
        return params;
    }

    private void addEqualButtons(LinearLayout row, Button... buttons) {
        for (int index = 0; index < buttons.length; index++) {
            LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                0,
                dp(50),
                1f
            );
            if (index > 0) lp.setMargins(dp(7), 0, 0, 0);
            row.addView(buttons[index], lp);
        }
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

    private void showError(String message) {
        new AlertDialog.Builder(activity)
            .setTitle("Relay 設定錯誤")
            .setMessage(message)
            .setPositiveButton("確定", null)
            .show();
    }

    private int dp(int value) {
        return (int) (
            value * getResources().getDisplayMetrics().density + 0.5f
        );
    }
}
