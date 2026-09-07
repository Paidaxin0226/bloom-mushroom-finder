package com.locwarp.mock;

import android.annotation.SuppressLint;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.Switch;
import android.widget.TextView;
import android.widget.Toast;

import java.util.Locale;
import java.util.Random;

@SuppressLint({"ViewConstructor", "SetTextI18n"})
final class FitnessDebuggerPage extends ScrollView
    implements LocalSimulationEngine.Listener {

    private static final String ACCENT = "#6c8cff";
    private static final String SURFACE = "#141722";
    private static final String ELEVATED = "#1f2334";
    private static final String BORDER = "#2d3550";
    private static final String TEXT = "#e8eaf0";
    private static final String MUTED = "#9499ac";
    private static final double WALKING_SPEED_LIMIT_KMH = 15d;

    private enum JobKind {
        NONE,
        SINGLE,
        LOOP,
        FOLLOW
    }

    private final MainActivity activity;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final LocalSimulationEngine engine = LocalSimulationEngine.get();
    private final Random random = new Random();
    private final Runnable jobTicker = new Runnable() {
        @Override
        public void run() {
            updateJobUi();
            if (activeJob != JobKind.NONE) {
                mainHandler.postDelayed(this, 200L);
            }
        }
    };
    private final Runnable jobEmitter = this::emitNextBatch;
    private LinearLayout contentRoot;
    private TextView singleStatus;
    private TextView singleProgress;
    private TextView loopStatus;
    private TextView loopProgress;
    private TextView walkingStatus;
    private TextView multiplierStatus;
    private EditText singleStepsInput;
    private EditText multiplierInput;
    private Button singlePrimaryAction;
    private Button singleStopAction;
    private Button loopPrimaryAction;
    private Button loopStopAction;
    private Button multiplierApplyButton;
    private Switch walkingSwitch;
    private LinearLayout moduleCard;
    private LinearLayout singleCard;
    private LinearLayout loopCard;
    private LinearLayout multiplierCard;
    private LinearLayout walkingCard;
    private Button singleModeButton;
    private Button loopModeButton;
    private Button followModeButton;
    private String selectedUiMode = "single";
    private boolean syncingWalkingSwitch;
    private JobKind activeJob = JobKind.NONE;
    private boolean jobPaused;
    private int singleTargetSteps;
    private int injectedSteps;
    private long jobStartedElapsedMs;
    private long accumulatedPausedMs;
    private long pauseStartedElapsedMs;
    private String lastSingleSummary;
    private String lastLoopSummary;

    FitnessDebuggerPage(MainActivity activity) {
        super(activity);
        this.activity = activity;
        if (!StepHookController.isManualJobActive()) {
            StepHookController.setManualJobActive(activity, false);
        }
        setFillViewport(true);
        setClipToPadding(false);
        setBackgroundColor(Color.parseColor("#0a0c12"));
        build();
        engine.attach(activity, this);
    }

    void refresh() {
        if (singleStepsInput != null && singleStepsInput.getText().length() == 0) {
            singleStepsInput.setText(String.valueOf(
                OverlaySettings.defitStepCount(activity)
            ));
        }
        if (multiplierInput != null && multiplierInput.getText().length() == 0) {
            multiplierInput.setText(String.valueOf(
                OverlaySettings.walkingStepMultiplier(activity)
            ));
        }
        syncWalkingSwitch();
        updateMultiplierStatus();
        updateJobUi();
        updateWalkingFromSimulation(engine.snapshot());
    }

    void onPageSelected(boolean selected) {
        if (selected) refresh();
    }

    void onHostForegroundChanged(boolean foreground) {
        // Hook timers run in every process enabled on the Apps page.
    }

    void setBottomContentInset(int bottomPx) {
        if (contentRoot == null) return;
        contentRoot.setPadding(dp(16), dp(16), dp(16), Math.max(dp(24), bottomPx));
    }

    @Override
    protected void onDetachedFromWindow() {
        if (activeJob != JobKind.NONE) {
            StepHookController.setManualJobActive(activity, false);
        }
        mainHandler.removeCallbacksAndMessages(null);
        activeJob = JobKind.NONE;
        engine.detach(this);
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
        TextView title = label("步數注入", 24, TEXT, true);
        titleRow.addView(title, new LinearLayout.LayoutParams(
            0,
            LayoutParams.WRAP_CONTENT,
            1f
        ));
        Button moduleButton = compactButton("Hook 說明");
        titleRow.addView(moduleButton, new LinearLayout.LayoutParams(dp(92), dp(44)));
        contentRoot.addView(titleRow);

        moduleCard = buildModuleCard();
        moduleCard.setVisibility(View.GONE);
        moduleButton.setOnClickListener(v -> {
            boolean show = moduleCard.getVisibility() != View.VISIBLE;
            moduleCard.setVisibility(show ? View.VISIBLE : View.GONE);
            moduleButton.setText(show ? "收起說明" : "Hook 說明");
        });
        contentRoot.addView(moduleCard, cardLp(10));
        contentRoot.addView(buildModeSelector(), cardLp(12));

        singleCard = buildSingleCard();
        loopCard = buildLoopCard();
        multiplierCard = buildMultiplierCard();
        walkingCard = buildWalkingCard();
        contentRoot.addView(singleCard, cardLp(10));
        contentRoot.addView(loopCard, cardLp(10));
        contentRoot.addView(walkingCard, cardLp(10));
        contentRoot.addView(multiplierCard, cardLp(10));
        selectUiMode("single");
        updateJobUi();
    }

    private LinearLayout buildModeSelector() {
        LinearLayout panel = card();
        LinearLayout row = horizontal();
        singleModeButton = compactButton("指定步數");
        loopModeButton = compactButton("循環");
        followModeButton = compactButton("自動跟隨");
        singleModeButton.setOnClickListener(v -> selectUiMode("single"));
        loopModeButton.setOnClickListener(v -> selectUiMode("loop"));
        followModeButton.setOnClickListener(v -> selectUiMode("follow"));
        Button[] buttons = {singleModeButton, loopModeButton, followModeButton};
        for (int index = 0; index < buttons.length; index++) {
            LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(0, dp(48), 1f);
            if (index > 0) lp.setMargins(dp(7), 0, 0, 0);
            row.addView(buttons[index], lp);
        }
        panel.addView(row);
        return panel;
    }

    private void selectUiMode(String mode) {
        selectedUiMode = mode;
        boolean single = "single".equals(mode);
        boolean loop = "loop".equals(mode);
        boolean follow = "follow".equals(mode);
        if (singleCard != null) singleCard.setVisibility(single ? View.VISIBLE : View.GONE);
        if (loopCard != null) loopCard.setVisibility(loop ? View.VISIBLE : View.GONE);
        if (walkingCard != null) walkingCard.setVisibility(follow ? View.VISIBLE : View.GONE);
        if (multiplierCard != null) {
            multiplierCard.setVisibility(follow ? View.VISIBLE : View.GONE);
        }
        styleModeButton(singleModeButton, single);
        styleModeButton(loopModeButton, loop);
        styleModeButton(followModeButton, follow);
    }

    private void styleModeButton(Button button, boolean selected) {
        if (button == null) return;
        button.setTextColor(Color.parseColor(selected ? "#ffffff" : MUTED));
        button.setTypeface(selected ? Typeface.DEFAULT_BOLD : Typeface.DEFAULT);
        button.setBackground(roundRect(
            selected ? "#344b92" : ELEVATED,
            selected ? ACCENT : BORDER,
            11
        ));
    }

    private LinearLayout buildModuleCard() {
        LinearLayout panel = card();
        panel.addView(sectionHeading("Hook"));
        TextView description = label(
            "作用域與 Location / Step 開關請到下方「應用」頁調整。"
                + "步數指令會自動送給所有已啟用 Step Hook 的執行中 App。"
                + "控制指令受 LocWarp 簽章權限保護。",
            13,
            "#cbd1e2",
            false
        );
        description.setPadding(0, dp(8), 0, 0);
        panel.addView(description);
        return panel;
    }

    private LinearLayout buildSingleCard() {
        LinearLayout panel = card();
        panel.addView(sectionHeading("指定步數"));
        TextView note = label(
            "每次隨機等待 0.2～0.6 秒並注入 1～3 步，最後一批不會超過指定總數。",
            12,
            MUTED,
            false
        );
        note.setPadding(0, dp(8), 0, 0);
        note.setLineSpacing(dp(2), 1f);
        panel.addView(note);

        singleStepsInput = numberInput("要注入的步數（1～200000）", false);
        singleStepsInput.setText(String.valueOf(
            OverlaySettings.defitStepCount(activity)
        ));
        LinearLayout.LayoutParams inputLp = new LinearLayout.LayoutParams(
            LayoutParams.MATCH_PARENT,
            dp(50)
        );
        inputLp.setMargins(0, dp(10), 0, 0);
        panel.addView(singleStepsInput, inputLp);

        singleStatus = label("目前沒有執行中指定步數", 12, MUTED, false);
        singleStatus.setPadding(0, dp(10), 0, 0);
        panel.addView(singleStatus);
        singleProgress = label("", 12, "#cbd1e2", false);
        singleProgress.setPadding(0, dp(6), 0, 0);
        panel.addView(singleProgress);

        LinearLayout controlRow = horizontal();
        controlRow.setPadding(0, dp(10), 0, 0);
        singlePrimaryAction = primaryButton("開始注入");
        singlePrimaryAction.setOnClickListener(v -> runSingleAction());
        controlRow.addView(
            singlePrimaryAction,
            new LinearLayout.LayoutParams(0, dp(54), 2f)
        );
        singleStopAction = stopButton();
        singleStopAction.setOnClickListener(v -> terminateActiveJob());
        LinearLayout.LayoutParams stopLp = new LinearLayout.LayoutParams(0, dp(54), 1f);
        stopLp.setMargins(dp(8), 0, 0, 0);
        controlRow.addView(singleStopAction, stopLp);
        panel.addView(controlRow);
        return panel;
    }

    private LinearLayout buildLoopCard() {
        LinearLayout panel = card();
        panel.addView(sectionHeading("循環新增"));
        TextView note = label(
            "開始後持續使用固定隨機規則，直到你暫停或終止。",
            12,
            MUTED,
            false
        );
        note.setPadding(0, dp(8), 0, 0);
        panel.addView(note);

        loopStatus = label("目前沒有執行中循環新增", 12, MUTED, false);
        loopStatus.setPadding(0, dp(10), 0, 0);
        panel.addView(loopStatus);
        loopProgress = label("", 12, "#cbd1e2", false);
        loopProgress.setPadding(0, dp(6), 0, 0);
        panel.addView(loopProgress);

        LinearLayout controlRow = horizontal();
        controlRow.setPadding(0, dp(10), 0, 0);
        loopPrimaryAction = primaryButton("開始循環");
        loopPrimaryAction.setOnClickListener(v -> runLoopAction());
        controlRow.addView(
            loopPrimaryAction,
            new LinearLayout.LayoutParams(0, dp(54), 2f)
        );
        loopStopAction = stopButton();
        loopStopAction.setOnClickListener(v -> terminateActiveJob());
        LinearLayout.LayoutParams stopLp = new LinearLayout.LayoutParams(0, dp(54), 1f);
        stopLp.setMargins(dp(8), 0, 0, 0);
        controlRow.addView(loopStopAction, stopLp);
        panel.addView(controlRow);
        return panel;
    }

    private LinearLayout buildMultiplierCard() {
        LinearLayout panel = card();
        panel.addView(sectionHeading("真實走路放大"));
        TextView note = label(
            "保留真實步數後補足放大倍數；補步會隨機等待 0.2～0.6 秒，"
                + "每批隨機補 1～3 步，直到補足。",
            12,
            MUTED,
            false
        );
        note.setPadding(0, dp(8), 0, 0);
        note.setLineSpacing(dp(2), 1f);
        panel.addView(note);

        LinearLayout row = horizontal();
        row.setPadding(0, dp(10), 0, 0);
        TextView multiplierLabel = label("放大倍數", 12, MUTED, true);
        multiplierLabel.setGravity(Gravity.CENTER_VERTICAL);
        row.addView(multiplierLabel, new LinearLayout.LayoutParams(dp(82), dp(48)));
        multiplierInput = numberInput("1～100", false);
        multiplierInput.setText(String.valueOf(
            OverlaySettings.walkingStepMultiplier(activity)
        ));
        row.addView(multiplierInput, new LinearLayout.LayoutParams(0, dp(48), 1f));
        multiplierApplyButton = secondaryButton("設定");
        multiplierApplyButton.setOnClickListener(v -> applyWalkingMultiplier());
        LinearLayout.LayoutParams applyLp = new LinearLayout.LayoutParams(0, dp(48), 1f);
        applyLp.setMargins(dp(8), 0, 0, 0);
        row.addView(multiplierApplyButton, applyLp);
        panel.addView(row);

        multiplierStatus = label("", 12, "#cbd1e2", false);
        multiplierStatus.setPadding(0, dp(9), 0, 0);
        panel.addView(multiplierStatus);
        updateMultiplierStatus();
        return panel;
    }

    private LinearLayout buildWalkingCard() {
        LinearLayout panel = card();
        panel.addView(sectionHeading("跟隨模擬走路"));
        TextView note = label(
            "LocWarp 模擬移動且不超過 15 km/h 時自動注入步數，不需設定步頻。",
            12,
            MUTED,
            false
        );
        note.setPadding(0, dp(8), 0, dp(10));
        note.setLineSpacing(dp(2), 1f);
        panel.addView(note);

        LinearLayout switchRow = horizontal();
        switchRow.setPadding(dp(10), dp(8), dp(10), dp(8));
        switchRow.setBackground(roundRect(ELEVATED, BORDER, 12));
        TextView switchLabel = label("啟用跟隨走路", 13, "#d9deec", true);
        switchRow.addView(switchLabel, new LinearLayout.LayoutParams(
            0,
            LayoutParams.WRAP_CONTENT,
            1f
        ));
        walkingSwitch = new Switch(activity);
        walkingSwitch.setOnCheckedChangeListener((button, enabled) -> {
            if (!syncingWalkingSwitch) setWalkingEnabled(enabled);
        });
        switchRow.addView(walkingSwitch);
        panel.addView(switchRow);

        walkingStatus = label("已關閉。", 12, MUTED, false);
        walkingStatus.setPadding(0, dp(10), 0, 0);
        walkingStatus.setLineSpacing(dp(3), 1f);
        panel.addView(walkingStatus);
        syncWalkingSwitch();
        return panel;
    }

    private void runSingleAction() {
        if (activeJob == JobKind.SINGLE) {
            togglePause();
        } else if (activeJob == JobKind.NONE) {
            startSingleJob();
        }
    }

    private void runLoopAction() {
        if (activeJob == JobKind.LOOP) {
            togglePause();
        } else if (activeJob == JobKind.NONE) {
            startLoopJob();
        }
    }

    private Integer readSingleSteps() {
        Integer steps = parseInt(singleStepsInput);
        if (steps == null || steps < 1 || steps > 200_000) {
            singleStatus.setText("指定步數需介於 1 到 200000。");
            singleStatus.setTextColor(Color.parseColor("#ff9aa2"));
            return null;
        }
        OverlaySettings.setDefitStepCount(activity, steps);
        return steps;
    }

    private void startSingleJob() {
        if (activeJob != JobKind.NONE || StepHookController.isManualJobActive()) {
            toast("其他步數工作執行中");
            return;
        }
        Integer steps = readSingleSteps();
        if (steps == null) return;
        disableFollowingWalking();
        activeJob = JobKind.SINGLE;
        singleTargetSteps = steps;
        injectedSteps = 0;
        lastSingleSummary = null;
        beginJobClock();
        StepHookController.setManualJobActive(activity, true);
        scheduleNextBatch();
        startJobTicker();
        updateJobUi();
    }

    private void startLoopJob() {
        if (activeJob != JobKind.NONE || StepHookController.isManualJobActive()) {
            toast("其他步數工作執行中");
            return;
        }
        disableFollowingWalking();
        activeJob = JobKind.LOOP;
        singleTargetSteps = 0;
        injectedSteps = 0;
        lastLoopSummary = null;
        beginJobClock();
        StepHookController.setManualJobActive(activity, true);
        scheduleNextBatch();
        startJobTicker();
        updateJobUi();
    }

    private void startFollowJob(LocalSimulationEngine.Snapshot snapshot) {
        if (activeJob != JobKind.NONE || StepHookController.isManualJobActive()) {
            return;
        }
        activeJob = JobKind.FOLLOW;
        singleTargetSteps = 0;
        injectedSteps = 0;
        beginJobClock();
        StepHookController.setManualJobActive(activity, true);
        scheduleNextBatch();
        startJobTicker();
        updateJobUi();
        if (snapshot != null) updateWalkingActiveStatus(snapshot);
    }

    private void togglePause() {
        if (activeJob != JobKind.SINGLE && activeJob != JobKind.LOOP) return;
        long now = SystemClock.elapsedRealtime();
        if (jobPaused) {
            accumulatedPausedMs += Math.max(0L, now - pauseStartedElapsedMs);
            pauseStartedElapsedMs = 0L;
            jobPaused = false;
            scheduleNextBatch();
        } else {
            jobPaused = true;
            pauseStartedElapsedMs = now;
            mainHandler.removeCallbacks(jobEmitter);
        }
        updateJobUi();
    }

    private void terminateActiveJob() {
        if (activeJob == JobKind.NONE) return;
        JobKind ending = activeJob;
        int total = injectedSteps;
        resetActiveJob();
        if (ending == JobKind.SINGLE) {
            lastSingleSummary = "已終止：本次共送出 " + total + " 步";
        } else if (ending == JobKind.LOOP) {
            lastLoopSummary = "已終止：本次共送出 " + total + " 步";
        } else if (ending == JobKind.FOLLOW) {
            OverlaySettings.setDefitWalkingStepsEnabled(activity, false);
            syncWalkingSwitch();
        }
        OperationHistoryStore.record(
            activity,
            "step",
            "已終止 " + ending + "，共送出 " + total + " 步",
            true
        );
        updateJobUi();
        updateWalkingFromSimulation(engine.snapshot());
    }

    private void completeSingleJob() {
        if (activeJob != JobKind.SINGLE) return;
        int completed = Math.min(singleTargetSteps, injectedSteps);
        lastSingleSummary = "已完成：本次共送出 " + completed + " 步";
        OperationHistoryStore.record(
            activity,
            "step",
            "指定步數指令完成：" + completed + " 步",
            true
        );
        resetActiveJob();
        updateJobUi();
        updateWalkingFromSimulation(engine.snapshot());
    }

    private void stopFollowSession() {
        if (activeJob != JobKind.FOLLOW) return;
        resetActiveJob();
        updateJobUi();
    }

    private void beginJobClock() {
        jobPaused = false;
        jobStartedElapsedMs = SystemClock.elapsedRealtime();
        accumulatedPausedMs = 0L;
        pauseStartedElapsedMs = 0L;
    }

    private void startJobTicker() {
        mainHandler.removeCallbacks(jobTicker);
        mainHandler.post(jobTicker);
    }

    private void scheduleNextBatch() {
        mainHandler.removeCallbacks(jobEmitter);
        if (activeJob == JobKind.NONE || jobPaused) return;
        mainHandler.postDelayed(
            jobEmitter,
            StepInjectionPolicy.randomStepIntervalMillis(random)
        );
    }

    private void emitNextBatch() {
        if (activeJob == JobKind.NONE || jobPaused) return;
        int batch = StepInjectionPolicy.randomBatchSteps(random);
        if (activeJob == JobKind.SINGLE) {
            int remaining = Math.max(0, singleTargetSteps - injectedSteps);
            batch = Math.min(batch, remaining);
            if (batch <= 0) {
                completeSingleJob();
                return;
            }
        }
        StepHookController.injectBatch(activity, batch);
        injectedSteps = saturatedAdd(injectedSteps, batch);
        if (activeJob == JobKind.SINGLE && injectedSteps >= singleTargetSteps) {
            completeSingleJob();
            return;
        }
        updateJobUi();
        scheduleNextBatch();
    }

    private long activeJobElapsedMs() {
        if (activeJob == JobKind.NONE) return 0L;
        long now = SystemClock.elapsedRealtime();
        long currentPause = jobPaused
            ? Math.max(0L, now - pauseStartedElapsedMs)
            : 0L;
        return Math.max(
            0L,
            now - jobStartedElapsedMs - accumulatedPausedMs - currentPause
        );
    }

    private void resetActiveJob() {
        mainHandler.removeCallbacks(jobTicker);
        mainHandler.removeCallbacks(jobEmitter);
        StepHookController.setManualJobActive(activity, false);
        activeJob = JobKind.NONE;
        jobPaused = false;
        singleTargetSteps = 0;
        injectedSteps = 0;
        jobStartedElapsedMs = 0L;
        accumulatedPausedMs = 0L;
        pauseStartedElapsedMs = 0L;
    }

    private void updateJobUi() {
        boolean externalJob = activeJob == JobKind.NONE
            && StepHookController.isManualJobActive();
        boolean running = activeJob != JobKind.NONE || externalJob;
        boolean singleRunning = activeJob == JobKind.SINGLE;
        boolean loopRunning = activeJob == JobKind.LOOP;

        setEnabled(singleStepsInput, !running);
        setEnabled(multiplierInput, !running);
        setEnabled(multiplierApplyButton, !running);
        setEnabled(walkingSwitch, !running || activeJob == JobKind.FOLLOW);

        setEnabled(singlePrimaryAction, !running || singleRunning);
        setEnabled(singleStopAction, singleRunning);
        setEnabled(loopPrimaryAction, !running || loopRunning);
        setEnabled(loopStopAction, loopRunning);
        singleStopAction.setVisibility(singleRunning ? View.VISIBLE : View.GONE);
        loopStopAction.setVisibility(loopRunning ? View.VISIBLE : View.GONE);

        singlePrimaryAction.setText(singleRunning
            ? (jobPaused ? "繼續" : "暫停")
            : "開始注入");
        loopPrimaryAction.setText(loopRunning
            ? (jobPaused ? "繼續" : "暫停")
            : "開始循環");
        stylePrimaryState(singlePrimaryAction, singleRunning);
        stylePrimaryState(loopPrimaryAction, loopRunning);

        if (singleRunning) {
            singleStatus.setText("指定步數" + (jobPaused ? " · 已暫停" : " · 執行中"));
            singleStatus.setTextColor(Color.parseColor("#d9deec"));
            singleProgress.setText(
                "已送出 " + injectedSteps + " / " + singleTargetSteps
                    + " 步 · 已執行 " + durationLabel(activeJobElapsedMs())
            );
        } else {
            singleStatus.setText(externalJob
                ? "其他步數工作正由 Relay 執行"
                : (lastSingleSummary == null
                    ? "目前沒有執行中指定步數"
                    : lastSingleSummary));
            singleStatus.setTextColor(Color.parseColor(
                externalJob || lastSingleSummary != null ? "#cbd1e2" : MUTED
            ));
            singleProgress.setText("");
        }

        if (loopRunning) {
            loopStatus.setText("循環新增" + (jobPaused ? " · 已暫停" : " · 執行中"));
            loopStatus.setTextColor(Color.parseColor("#d9deec"));
            loopProgress.setText(
                "本次共送出 " + injectedSteps + " 步 · 已執行 "
                    + durationLabel(activeJobElapsedMs())
            );
        } else {
            loopStatus.setText(externalJob
                ? "其他步數工作正由 Relay 執行"
                : (lastLoopSummary == null
                    ? "目前沒有執行中循環新增"
                    : lastLoopSummary));
            loopStatus.setTextColor(Color.parseColor(
                externalJob || lastLoopSummary != null ? "#cbd1e2" : MUTED
            ));
            loopProgress.setText("");
        }
    }

    private void stylePrimaryState(Button button, boolean active) {
        if (button == null) return;
        button.setBackground(roundRect(
            active && !jobPaused ? "#344b92" : ACCENT,
            ACCENT,
            12
        ));
    }

    private void applyWalkingMultiplier() {
        Integer multiplier = parseInt(multiplierInput);
        if (multiplier == null
            || multiplier < StepInjectionPolicy.MIN_WALKING_MULTIPLIER
            || multiplier > StepInjectionPolicy.MAX_WALKING_MULTIPLIER) {
            multiplierStatus.setText("放大倍數需介於 1 到 100。");
            multiplierStatus.setTextColor(Color.parseColor("#ff9aa2"));
            return;
        }
        OverlaySettings.setWalkingStepMultiplier(activity, multiplier);
        StepHookController.broadcastWalkingMultiplier(activity, multiplier);
        updateMultiplierStatus();
        toast("走路倍數已更新");
    }

    private void updateMultiplierStatus() {
        if (multiplierStatus == null) return;
        int multiplier = OverlaySettings.walkingStepMultiplier(activity);
        multiplierStatus.setText("所有已啟用 Step Hook 的 App · " + (multiplier == 1
            ? "1 倍：不補額外步數。"
            : multiplier + " 倍：每個真實步會補足 "
                + (multiplier - 1) + " 步。"));
        multiplierStatus.setTextColor(Color.parseColor("#cbd1e2"));
    }

    private void setWalkingEnabled(boolean enabled) {
        if (activeJob != JobKind.NONE && activeJob != JobKind.FOLLOW) {
            syncWalkingSwitch();
            return;
        }
        if (!enabled && activeJob == JobKind.FOLLOW) stopFollowSession();
        OverlaySettings.setDefitWalkingStepsEnabled(activity, enabled);
        syncWalkingSwitch();
        updateWalkingFromSimulation(engine.snapshot());
    }

    private void disableFollowingWalking() {
        if (!OverlaySettings.isDefitWalkingStepsEnabled(activity)) return;
        OverlaySettings.setDefitWalkingStepsEnabled(activity, false);
        syncWalkingSwitch();
    }

    private void syncWalkingSwitch() {
        if (walkingSwitch == null) return;
        syncingWalkingSwitch = true;
        walkingSwitch.setChecked(
            OverlaySettings.isDefitWalkingStepsEnabled(activity)
        );
        syncingWalkingSwitch = false;
    }

    @Override
    public void onSimulationUpdate(LocalSimulationEngine.Snapshot snapshot) {
        if (Looper.myLooper() == Looper.getMainLooper()) {
            updateWalkingFromSimulation(snapshot);
        } else {
            mainHandler.post(() -> updateWalkingFromSimulation(snapshot));
        }
    }

    private void updateWalkingFromSimulation(LocalSimulationEngine.Snapshot snapshot) {
        if (walkingStatus == null) return;
        if (RemoteStepController.isRemoteFollowControlling()) {
            walkingStatus.setText(RemoteStepController.isRemoteJobActive()
                ? "跟隨走路目前由 Relay 控制中。"
                : "Relay 已啟用跟隨，正在等待定位頁開始模擬移動。");
            walkingStatus.setTextColor(Color.parseColor("#cbd1e2"));
            return;
        }
        boolean enabled = OverlaySettings.isDefitWalkingStepsEnabled(activity);
        boolean eligible = enabled
            && snapshot != null
            && snapshot.running
            && !snapshot.paused
            && snapshot.speedKmh <= WALKING_SPEED_LIMIT_KMH;

        if (activeJob == JobKind.FOLLOW && !eligible) stopFollowSession();
        if (!enabled) {
            walkingStatus.setText("已關閉。");
            walkingStatus.setTextColor(Color.parseColor(MUTED));
            return;
        }
        if (activeJob != JobKind.NONE && activeJob != JobKind.FOLLOW) {
            walkingStatus.setText("其他步數工作執行中，跟隨走路暫停使用。");
            walkingStatus.setTextColor(Color.parseColor(MUTED));
            return;
        }
        if (!eligible) {
            if (snapshot == null || !snapshot.running || snapshot.paused) {
                walkingStatus.setText("等待定位頁開始模擬移動。");
                walkingStatus.setTextColor(Color.parseColor("#cbd1e2"));
            } else {
                walkingStatus.setText(String.format(
                    Locale.US,
                    "目前 %.1f km/h，超過 15 km/h，暫停注入。",
                    snapshot.speedKmh
                ));
                walkingStatus.setTextColor(Color.parseColor("#ffcf7a"));
            }
            return;
        }
        if (activeJob == JobKind.NONE) startFollowJob(snapshot);
        if (activeJob == JobKind.FOLLOW) updateWalkingActiveStatus(snapshot);
    }

    private void updateWalkingActiveStatus(LocalSimulationEngine.Snapshot snapshot) {
        if (walkingStatus == null || snapshot == null) return;
        walkingStatus.setText(String.format(
            Locale.US,
            "跟隨中：%.1f km/h · 本次共送出 %d 步",
            snapshot.speedKmh,
            injectedSteps
        ));
        walkingStatus.setTextColor(Color.parseColor("#cbd1e2"));
    }

    private Integer parseInt(EditText input) {
        if (input == null) return null;
        try {
            String raw = input.getText().toString().trim();
            if (raw.isEmpty()) return null;
            return Integer.parseInt(raw);
        } catch (Exception ignored) {
            return null;
        }
    }

    private int saturatedAdd(int current, int delta) {
        return (int) Math.min(Integer.MAX_VALUE, (long) current + delta);
    }

    private String durationLabel(long millis) {
        if (millis < 1000L) return millis + " ms";
        long wholeSeconds = millis / 1000L;
        if (wholeSeconds < 60L) return wholeSeconds + " 秒";
        long minutes = wholeSeconds / 60L;
        long seconds = wholeSeconds % 60L;
        if (minutes < 60L) return minutes + " 分 " + seconds + " 秒";
        return (minutes / 60L) + " 小時 " + (minutes % 60L) + " 分";
    }

    private void setEnabled(View view, boolean enabled) {
        if (view == null) return;
        view.setEnabled(enabled);
        view.setAlpha(enabled ? 1f : 0.45f);
    }

    private LinearLayout sectionHeading(String title) {
        LinearLayout block = new LinearLayout(activity);
        block.setOrientation(LinearLayout.VERTICAL);
        block.addView(label(title, 17, TEXT, true));
        return block;
    }

    private LinearLayout card() {
        LinearLayout card = new LinearLayout(activity);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setPadding(dp(16), dp(16), dp(16), dp(16));
        card.setBackground(roundRect(SURFACE, "#26304d", 16));
        card.setElevation(dp(3));
        return card;
    }

    private EditText numberInput(String hint, boolean decimal) {
        EditText input = new EditText(activity);
        input.setHint(hint);
        input.setHintTextColor(Color.parseColor("#687087"));
        input.setTextColor(Color.parseColor(TEXT));
        input.setTextSize(14);
        input.setSingleLine(true);
        input.setPadding(dp(12), 0, dp(12), 0);
        input.setInputType(
            InputType.TYPE_CLASS_NUMBER
                | (decimal ? InputType.TYPE_NUMBER_FLAG_DECIMAL : 0)
        );
        input.setBackground(roundRect("#0f121b", "#303a58", 10));
        return input;
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
        button.setTextSize(14);
        button.setTypeface(Typeface.DEFAULT_BOLD);
        button.setTextColor(Color.WHITE);
        button.setBackground(roundRect(ACCENT, "#88a0ff", 12));
        button.setElevation(dp(4));
        return button;
    }

    private Button secondaryButton(String text) {
        Button button = compactButton(text);
        button.setTextColor(Color.parseColor("#cbd1e2"));
        return button;
    }

    private Button stopButton() {
        Button button = secondaryButton("終止");
        button.setTextColor(Color.parseColor("#ff9aa2"));
        button.setTypeface(Typeface.DEFAULT_BOLD);
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

    private LinearLayout.LayoutParams cardLp(int top) {
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
            LayoutParams.MATCH_PARENT,
            LayoutParams.WRAP_CONTENT
        );
        lp.setMargins(0, dp(top), 0, 0);
        return lp;
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
}
