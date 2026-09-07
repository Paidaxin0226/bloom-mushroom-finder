package com.locwarp.mock;

import android.content.Context;
import android.os.Handler;
import android.os.Looper;
import android.util.Base64;

import org.json.JSONArray;
import org.json.JSONObject;

import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;

import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.WebSocket;
import okhttp3.WebSocketListener;

final class RemoteRelayClient {
    interface Listener {
        void onRelayChanged(boolean running, String message, String publicUrl);
        void onRelayViewerApprovalRequested(Viewer viewer);
        void onRelayViewerCountChanged(int viewerCount);
        void onRelayViewersChanged(List<Viewer> viewers);
    }

    static final class Viewer {
        final String id;
        final String name;
        final String deviceId;
        final String accessRole;
        final long connectedAt;
        final String e2eNonce;
        final String e2eProof;
        final String pairingCode;

        Viewer(
            String id,
            String name,
            String deviceId,
            String accessRole,
            long connectedAt,
            String e2eNonce,
            String e2eProof,
            String pairingCode
        ) {
            this.id = id;
            this.name = name;
            this.deviceId = deviceId;
            this.accessRole = "controller".equals(accessRole)
                ? "controller"
                : "readonly";
            this.connectedAt = Math.max(0L, connectedAt);
            this.e2eNonce = e2eNonce == null ? "" : e2eNonce;
            this.e2eProof = e2eProof == null ? "" : e2eProof;
            this.pairingCode = pairingCode == null ? "" : pairingCode;
        }
    }

    private static final class ApprovedViewer {
        final String id;
        final String deviceId;
        final String e2eNonce;
        final String capability;
        final String accessRole;
        final long grantGeneration;
        long statusSequence;

        ApprovedViewer(
            String id,
            String deviceId,
            String e2eNonce,
            String capability,
            String accessRole,
            long grantGeneration,
            long statusSequence
        ) {
            this.id = id;
            this.deviceId = deviceId;
            this.e2eNonce = e2eNonce;
            this.capability = capability;
            this.accessRole = "controller".equals(accessRole)
                ? "controller"
                : "readonly";
            this.grantGeneration = Math.max(1L, grantGeneration);
            this.statusSequence = Math.max(0L, statusSequence);
        }
    }

    private static final long STATUS_INTERVAL_MS = 2500L;
    private static final long RECONNECT_MAX_MS = 30_000L;
    private static final int MAX_PENDING_VIEWERS = 16;
    private static RemoteRelayClient shared;

    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final SecureRandom random = new SecureRandom();
    private final RemoteCommandExecutor executor;
    private final OkHttpClient httpClient;

    private Listener listener;
    private WebSocket webSocket;
    private String baseUrl;
    private String publicUrl;
    private String sessionId;
    private String hostToken;
    private String viewerToken;
    private boolean connected;
    private boolean stopping;
    private boolean enabled;
    private int viewerCount;
    private List<Viewer> viewers = Collections.emptyList();
    private long reconnectDelayMs = 1_000L;
    private final Map<String, Viewer> pendingApprovalViewers =
        new LinkedHashMap<>();
    private final Map<String, ApprovedViewer> approvedViewers =
        new LinkedHashMap<>();
    private final LinkedHashMap<String, Long> seenMessageIds =
        new LinkedHashMap<>();

    private final Runnable statusLoop = new Runnable() {
        @Override
        public void run() {
            if (!connected || webSocket == null) return;
            sendStatus();
            mainHandler.postDelayed(this, STATUS_INTERVAL_MS);
        }
    };

    private final Runnable reconnect = new Runnable() {
        @Override
        public void run() {
            synchronized (RemoteRelayClient.this) {
                if (!enabled || stopping || webSocket != null) return;
                connectLocked();
            }
        }
    };

    static synchronized RemoteRelayClient get(Context context) {
        if (shared == null) shared = new RemoteRelayClient(context);
        return shared;
    }

    private RemoteRelayClient(Context context) {
        executor = new RemoteCommandExecutor(context.getApplicationContext());
        httpClient = new OkHttpClient.Builder()
            .readTimeout(0, TimeUnit.MILLISECONDS)
            .pingInterval(25, TimeUnit.SECONDS)
            .build();
    }

    synchronized void setListener(Listener listener) {
        this.listener = listener;
        dispatch(connected, connected ? "Relay connected" : "Relay off");
        if (listener != null) {
            listener.onRelayViewerCountChanged(viewerCount);
            listener.onRelayViewersChanged(new ArrayList<>(viewers));
        }
    }

    synchronized boolean isRunning() {
        return enabled;
    }

    synchronized boolean isConnected() {
        return connected;
    }

    synchronized String publicUrl() {
        return publicUrl == null ? "" : publicUrl;
    }

    synchronized int viewerCount() {
        return viewerCount;
    }

    synchronized List<Viewer> viewers() {
        return new ArrayList<>(viewers);
    }

    synchronized List<Viewer> pendingViewers() {
        return new ArrayList<>(pendingApprovalViewers.values());
    }

    String admissionKeyId() {
        return RelayAdmissionIdentity.keyId();
    }

    String admissionFingerprint() {
        return RelayAdmissionIdentity.fingerprint();
    }

    String admissionPublicKeyEntry() {
        return RelayAdmissionIdentity.publicKeyEntry();
    }

    synchronized void start(String nextBaseUrl) {
        if (enabled) {
            dispatch(connected, connected ? "Relay connected" : "Relay connecting");
            return;
        }
        stopping = false;
        enabled = true;
        connected = false;
        viewerCount = 0;
        viewers = Collections.emptyList();
        pendingApprovalViewers.clear();
        approvedViewers.clear();
        seenMessageIds.clear();
        baseUrl = normalizeBaseUrl(nextBaseUrl);
        if (baseUrl.isEmpty()) {
            enabled = false;
            dispatch(false, "Relay URL required");
            return;
        }
        String savedSession = OverlaySettings.relaySessionId(executor.context());
        String savedHostToken = OverlaySettings.relayHostToken(executor.context());
        String savedViewerToken = OverlaySettings.relayViewerToken(executor.context());
        boolean canRestore = OverlaySettings.isRelayEnabled(executor.context())
            && baseUrl.equals(OverlaySettings.relayBaseUrl(executor.context()))
            && !savedSession.isEmpty()
            && !savedHostToken.isEmpty()
            && !savedViewerToken.isEmpty();
        sessionId = canRestore ? savedSession : randomToken(12);
        hostToken = canRestore ? savedHostToken : randomToken(32);
        viewerToken = canRestore ? savedViewerToken : randomToken(24);
        publicUrl = baseUrl + "/r/" + encode(sessionId)
            + "#token=" + encode(viewerToken);
        OverlaySettings.setRelaySession(
            executor.context(),
            baseUrl,
            sessionId,
            hostToken,
            viewerToken
        );
        reconnectDelayMs = 1_000L;
        connectLocked();
        dispatch(false, "Relay connecting");
    }

    synchronized void restoreIfEnabled() {
        if (enabled || !OverlaySettings.isRelayEnabled(executor.context())) return;
        start(OverlaySettings.relayBaseUrl(executor.context()));
    }

    synchronized void stop() {
        stopping = true;
        enabled = false;
        connected = false;
        viewerCount = 0;
        viewers = Collections.emptyList();
        pendingApprovalViewers.clear();
        approvedViewers.clear();
        seenMessageIds.clear();
        mainHandler.removeCallbacks(statusLoop);
        mainHandler.removeCallbacks(reconnect);
        WebSocket socket = webSocket;
        webSocket = null;
        if (socket != null) socket.close(1000, "stopped");
        publicUrl = null;
        sessionId = null;
        hostToken = null;
        viewerToken = null;
        OverlaySettings.clearRelaySession(executor.context());
        dispatch(false, "Relay off");
    }

    private void onSocketOpen(WebSocket socket) {
        synchronized (this) {
            if (socket != webSocket) return;
        }
        String deviceKeyId = RelayAdmissionIdentity.keyId();
        if (deviceKeyId.isEmpty()) {
            socket.close(1011, "host device key unavailable");
            return;
        }
        JSONObject init = new JSONObject();
        try {
            init.put("type", "host_init");
            init.put("session", sessionId);
            init.put("deviceKeyId", deviceKeyId);
        } catch (Exception ignored) {
            socket.close(1011, "host init failed");
            return;
        }
        socket.send(init.toString());
    }

    private void answerHostChallenge(WebSocket socket, JSONObject challenge) {
        String challengeValue = challenge.optString("challenge", "");
        long issuedAtMs = challenge.optLong("issuedAtMs", 0L);
        if (!RelayAdmissionProtocol.isOpaque(challengeValue, 43)
            || issuedAtMs <= 0L
            || Math.abs(System.currentTimeMillis() - issuedAtMs)
                > RelayAdmissionProtocol.CHALLENGE_MAX_AGE_MS) {
            socket.close(1008, "invalid host challenge");
            return;
        }
        String deviceKeyId = RelayAdmissionIdentity.keyId();
        String viewerVerifier = RelayE2eCrypto.viewerVerifier(viewerToken);
        String signature = RelayAdmissionIdentity.sign(
            sessionId,
            challengeValue,
            issuedAtMs,
            hostToken,
            viewerVerifier
        );
        if (!RelayAdmissionProtocol.isOpaque(deviceKeyId, 43)
            || viewerVerifier.isEmpty()
            || signature.isEmpty()) {
            socket.close(1011, "host signature unavailable");
            return;
        }
        JSONObject auth = new JSONObject();
        try {
            auth.put("type", "auth");
            auth.put("session", sessionId);
            auth.put("token", hostToken);
            auth.put("viewerVerifier", viewerVerifier);
            auth.put("deviceKeyId", deviceKeyId);
            auth.put("challenge", challengeValue);
            auth.put("issuedAtMs", issuedAtMs);
            auth.put("signature", signature);
        } catch (Exception ignored) {
            socket.close(1011, "host auth failed");
            return;
        }
        socket.send(auth.toString());
    }

    private void onAuthenticated(WebSocket socket) {
        synchronized (this) {
            if (socket != webSocket || connected) return;
            connected = true;
            reconnectDelayMs = 1_000L;
        }
        sendStatus();
        mainHandler.removeCallbacks(statusLoop);
        mainHandler.postDelayed(statusLoop, STATUS_INTERVAL_MS);
        dispatch(true, "Relay connected");
    }

    private void onClosed(WebSocket socket, String message) {
        synchronized (this) {
            if (socket != webSocket && webSocket != null) return;
            connected = false;
            webSocket = null;
            if (enabled && !stopping && "expired".equalsIgnoreCase(message)) {
                rotateCredentialsLocked();
            }
        }
        mainHandler.removeCallbacks(statusLoop);
        if (enabled && !stopping) {
            scheduleReconnect();
            dispatch(false, "Relay reconnecting: " + message);
        } else {
            dispatch(false, "Relay off");
        }
    }

    private void onMessage(WebSocket socket, String raw) {
        synchronized (this) {
            if (socket != webSocket) return;
        }
        JSONObject message;
        try {
            message = new JSONObject(raw);
        } catch (Exception ignored) {
            return;
        }
        String type = message.optString("type", "");
        if ("host_challenge".equals(type)) {
            answerHostChallenge(socket, message);
            return;
        }
        if ("hello".equals(type) && "host".equals(message.optString("role"))) {
            mainHandler.post(() -> onAuthenticated(socket));
            return;
        }
        if ("http_request".equals(type)) {
            send(RelayWebPage.response(executor.context(), message));
            return;
        }
        if ("viewer_request".equals(type)) {
            Viewer viewer = viewerFrom(message);
            if (!validViewerRequest(viewer)) {
                rejectViewer(viewer.id);
                OperationHistoryStore.record(
                    executor.context(),
                    "relay_security",
                    "拒絕無效的端對端配對證明",
                    false
                );
                return;
            }
            Listener target;
            synchronized (this) {
                putPendingViewerLocked(viewer);
                target = listener;
            }
            if (target != null && !viewer.id.isEmpty()) {
                mainHandler.post(
                    () -> target.onRelayViewerApprovalRequested(viewer)
                );
            }
            return;
        }
        if ("viewer_cancelled".equals(type)) {
            synchronized (this) {
                pendingApprovalViewers.remove(
                    message.optString("viewerId", "")
                );
            }
            return;
        }
        if ("viewer_list".equals(type)) {
            List<Viewer> next = parseViewers(message.optJSONArray("viewers"));
            Listener target;
            synchronized (this) {
                viewers = Collections.unmodifiableList(next);
                viewerCount = next.size();
                pruneApprovedViewers(next);
                target = listener;
            }
            if (target != null) {
                List<Viewer> delivered = new ArrayList<>(next);
                mainHandler.post(() -> {
                    target.onRelayViewerCountChanged(delivered.size());
                    target.onRelayViewersChanged(delivered);
                });
            }
            return;
        }
        if ("viewer_count".equals(type)) {
            int count = Math.max(0, message.optInt("count", 0));
            Listener target;
            synchronized (this) {
                viewerCount = count;
                target = listener;
            }
            if (target != null) {
                mainHandler.post(() -> target.onRelayViewerCountChanged(count));
            }
            return;
        }
        if ("status_request".equals(type)) {
            sendStatusFor(message.optString("viewerId", ""));
            return;
        }
        if ("command".equals(type)) handleEncryptedCommand(message);
    }

    private void sendStatus() {
        List<ApprovedViewer> targets;
        synchronized (this) {
            targets = new ArrayList<>(approvedViewers.values());
        }
        for (ApprovedViewer viewer : targets) sendStatusFor(viewer.id);
    }

    private void sendStatusFor(String viewerId) {
        ApprovedViewer viewer;
        long statusSequence;
        synchronized (this) {
            viewer = approvedViewers.get(viewerId);
            if (viewer == null) return;
            statusSequence = ++viewer.statusSequence;
        }
        JSONObject plaintext = new JSONObject();
        try {
            plaintext.put("v", 2);
            plaintext.put("type", "status");
            plaintext.put("viewerId", viewer.id);
            plaintext.put("deviceId", viewer.deviceId);
            plaintext.put("capability", viewer.capability);
            plaintext.put("issuedAtMs", System.currentTimeMillis());
            plaintext.put("statusSequence", statusSequence);
            plaintext.put("status", executor.status());
        } catch (Exception ignored) {
            return;
        }
        String envelope = RelayE2eCrypto.seal(viewerToken, plaintext);
        if (envelope.isEmpty()) return;
        JSONObject out = new JSONObject();
        try {
            out.put("type", "viewer_status");
            out.put("viewerId", viewer.id);
            out.put("envelope", envelope);
        } catch (Exception ignored) {
            return;
        }
        send(out);
    }

    private void handleEncryptedCommand(JSONObject message) {
        String relayId = bounded(message.optString("id", ""), 120);
        JSONObject outerViewer = message.optJSONObject("viewer");
        String viewerId = outerViewer == null
            ? ""
            : bounded(outerViewer.optString("id", ""), 80);
        ApprovedViewer approved;
        synchronized (this) {
            approved = approvedViewers.get(viewerId);
        }
        if (approved == null || !"controller".equals(approved.accessRole)) {
            return;
        }
        JSONObject plaintext = RelayE2eCrypto.open(
            viewerToken,
            message.optString("envelope", "")
        );
        if (!validCommandEnvelope(approved, plaintext)) {
            OperationHistoryStore.record(
                executor.context(),
                "relay_security",
                "已阻擋無效、逾時或重放的遠端指令",
                false
            );
            return;
        }
        String messageId = bounded(plaintext.optString("messageId", ""), 100);
        String command = bounded(plaintext.optString("command", ""), 80);
        JSONObject payload = plaintext.optJSONObject("payload");
        String name = outerViewer == null
            ? "Browser"
            : bounded(outerViewer.optString("name", "Browser"), 60);
        OperationHistoryStore.record(
            executor.context(),
            "relay_command",
            name + "（控制）執行 " + command,
            true
        );
        executor.execute(command, payload, result -> {
            sendEncryptedResult(
                relayId,
                approved,
                messageId,
                result == null ? new JSONObject() : result
            );
            sendStatus();
        });
    }

    private void sendEncryptedResult(
        String relayId,
        ApprovedViewer viewer,
        String requestMessageId,
        JSONObject result
    ) {
        JSONObject plaintext = new JSONObject();
        try {
            plaintext.put("v", 2);
            plaintext.put("type", "result");
            plaintext.put("viewerId", viewer.id);
            plaintext.put("deviceId", viewer.deviceId);
            plaintext.put("capability", viewer.capability);
            plaintext.put("requestMessageId", requestMessageId);
            plaintext.put("issuedAtMs", System.currentTimeMillis());
            plaintext.put("result", result);
        } catch (Exception ignored) {
            return;
        }
        String envelope = RelayE2eCrypto.seal(viewerToken, plaintext);
        if (envelope.isEmpty()) return;
        JSONObject out = new JSONObject();
        try {
            out.put("type", "result");
            out.put("id", relayId);
            out.put("envelope", envelope);
        } catch (Exception ignored) {
            return;
        }
        send(out);
    }

    private synchronized void send(JSONObject message) {
        WebSocket socket = webSocket;
        if (socket != null) socket.send(message.toString());
    }

    private void dispatch(boolean running, String message) {
        Listener target;
        String url;
        synchronized (this) {
            target = listener;
            url = publicUrl;
        }
        if (target != null) target.onRelayChanged(running, message, url);
    }

    private String normalizeBaseUrl(String value) {
        String out = value == null ? "" : value.trim();
        if (out.isEmpty()) return "";
        while (out.endsWith("/")) out = out.substring(0, out.length() - 1);
        if (!out.startsWith("https://") && !out.startsWith("http://")) {
            out = "https://" + out;
        }
        if (!out.startsWith("https://")) return "";
        return out;
    }

    private String websocketBaseUrl(String value) {
        if (value.startsWith("https://")) return "wss://" + value.substring("https://".length());
        return value;
    }

    private void connectLocked() {
        if (!enabled
            || baseUrl == null
            || sessionId == null
            || hostToken == null
            || viewerToken == null) {
            return;
        }
        String wsUrl = websocketBaseUrl(baseUrl) + "/ws/host";
        Request request = new Request.Builder().url(wsUrl).build();
        webSocket = httpClient.newWebSocket(request, new RelaySocketListener());
    }

    synchronized void approveViewer(
        String viewerId,
        boolean approved,
        String accessRole
    ) {
        if (viewerId == null || viewerId.isEmpty()) return;
        Viewer viewer = pendingApprovalViewers.remove(viewerId);
        String safeRole = "controller".equals(accessRole)
            ? "controller"
            : "readonly";
        JSONObject message = new JSONObject();
        try {
            message.put("type", "viewer_decision");
            message.put("viewerId", viewerId);
            if (approved && viewer != null && validViewerRequest(viewer)) {
                ApprovedViewer grant = new ApprovedViewer(
                    viewer.id,
                    viewer.deviceId,
                    viewer.e2eNonce,
                    randomToken(24),
                    safeRole,
                    1L,
                    0L
                );
                String envelope = grantEnvelope(grant);
                if (envelope.isEmpty()) {
                    message.put("approved", false);
                    approvedViewers.remove(viewerId);
                    approved = false;
                } else {
                    approvedViewers.put(viewerId, grant);
                    message.put("approved", true);
                    message.put("accessRole", safeRole);
                    message.put("grant", envelope);
                }
            } else {
                message.put("approved", false);
                approvedViewers.remove(viewerId);
                approved = false;
            }
        } catch (Exception ignored) {
            approved = false;
        }
        send(message);
        OperationHistoryStore.record(
            executor.context(),
            "relay",
            (approved
                ? "允許遠端裝置 " + viewerId + "，權限 " + safeRole
                : "拒絕遠端裝置 " + viewerId),
            true
        );
    }

    synchronized void disconnectViewer(String viewerId) {
        if (viewerId == null || viewerId.isEmpty()) return;
        pendingApprovalViewers.remove(viewerId);
        approvedViewers.remove(viewerId);
        JSONObject message = new JSONObject();
        try {
            message.put("type", "viewer_disconnect");
            message.put("viewerId", viewerId);
        } catch (Exception ignored) {
        }
        send(message);
        OperationHistoryStore.record(
            executor.context(),
            "relay",
            "中斷遠端裝置 " + viewerId,
            true
        );
    }

    synchronized void changeViewerRole(String viewerId, String accessRole) {
        if (viewerId == null || viewerId.isEmpty()) return;
        ApprovedViewer current = approvedViewers.get(viewerId);
        if (current == null) return;
        String safeRole = "controller".equals(accessRole)
            ? "controller"
            : "readonly";
        ApprovedViewer next = new ApprovedViewer(
            current.id,
            current.deviceId,
            current.e2eNonce,
            randomToken(24),
            safeRole,
            current.grantGeneration + 1L,
            current.statusSequence
        );
        String grant = grantEnvelope(next);
        if (grant.isEmpty()) return;
        approvedViewers.put(viewerId, next);
        JSONObject message = new JSONObject();
        try {
            message.put("type", "viewer_role");
            message.put("viewerId", viewerId);
            message.put("accessRole", safeRole);
            message.put("grant", grant);
        } catch (Exception ignored) {
        }
        send(message);
    }

    synchronized void rotateSession() {
        if (!enabled || baseUrl == null) return;
        WebSocket old = webSocket;
        connected = false;
        viewerCount = 0;
        viewers = Collections.emptyList();
        pendingApprovalViewers.clear();
        approvedViewers.clear();
        seenMessageIds.clear();
        webSocket = null;
        rotateCredentialsLocked();
        reconnectDelayMs = 1_000L;
        connectLocked();
        if (old != null) old.close(1000, "rotated");
        dispatch(false, "Relay QR rotated");
    }

    private void rotateCredentialsLocked() {
        sessionId = randomToken(12);
        hostToken = randomToken(32);
        viewerToken = randomToken(24);
        publicUrl = baseUrl + "/r/" + encode(sessionId)
            + "#token=" + encode(viewerToken);
        OverlaySettings.setRelaySession(
            executor.context(),
            baseUrl,
            sessionId,
            hostToken,
            viewerToken
        );
        pendingApprovalViewers.clear();
        approvedViewers.clear();
        seenMessageIds.clear();
    }

    private synchronized void scheduleReconnect() {
        if (!enabled || stopping) return;
        mainHandler.removeCallbacks(reconnect);
        mainHandler.postDelayed(reconnect, reconnectDelayMs);
        reconnectDelayMs = Math.min(RECONNECT_MAX_MS, reconnectDelayMs * 2L);
    }

    private String randomToken(int byteCount) {
        byte[] bytes = new byte[byteCount];
        random.nextBytes(bytes);
        return Base64.encodeToString(
            bytes,
            Base64.URL_SAFE | Base64.NO_PADDING | Base64.NO_WRAP
        );
    }

    private Viewer viewerFrom(JSONObject raw) {
        String deviceId = raw == null
            ? ""
            : bounded(raw.optString("deviceId", ""), 100);
        String nonce = raw == null
            ? ""
            : bounded(raw.optString("e2eNonce", ""), 100);
        return new Viewer(
            raw == null ? "" : bounded(raw.optString("viewerId", raw.optString("id", "")), 80),
            raw == null ? "Browser" : bounded(raw.optString("name", "Browser"), 60),
            deviceId,
            raw == null
                ? "readonly"
                : raw.optString("requestedRole", raw.optString("accessRole", "readonly")),
            raw == null ? 0L : raw.optLong("connectedAt", 0L),
            nonce,
            raw == null ? "" : bounded(raw.optString("e2eProof", ""), 100),
            viewerToken == null || sessionId == null
                ? ""
                : RelayE2eCrypto.pairingCode(
                    viewerToken,
                    sessionId,
                    deviceId,
                    nonce
                )
        );
    }

    private List<Viewer> parseViewers(JSONArray array) {
        List<Viewer> result = new ArrayList<>();
        if (array == null) return result;
        for (int index = 0; index < array.length() && result.size() < 5; index++) {
            JSONObject raw = array.optJSONObject(index);
            if (raw == null) continue;
            Viewer viewer = viewerFrom(raw);
            if (!viewer.id.isEmpty()) result.add(viewer);
        }
        return result;
    }

    private boolean validViewerRequest(Viewer viewer) {
        return viewer != null
            && validOpaque(viewer.id, 8, 80)
            && validOpaque(viewer.deviceId, 8, 100)
            && validOpaque(viewer.e2eNonce, 16, 100)
            && validOpaque(viewer.e2eProof, 40, 100)
            && RelayE2eCrypto.verifyViewerProof(
                viewerToken,
                sessionId,
                viewer.deviceId,
                viewer.e2eNonce,
                viewer.e2eProof
            );
    }

    private boolean validCommandEnvelope(
        ApprovedViewer approved,
        JSONObject plaintext
    ) {
        if (plaintext == null
            || plaintext.optInt("v", 0) != 2
            || !"command".equals(plaintext.optString("type", ""))
            || !RelayE2eCrypto.same(
                approved.id,
                plaintext.optString("viewerId", "")
            )
            || !RelayE2eCrypto.same(
                approved.deviceId,
                plaintext.optString("deviceId", "")
            )
            || !RelayE2eCrypto.same(
                approved.capability,
                plaintext.optString("capability", "")
            )) {
            return false;
        }
        long issuedAtMs = plaintext.optLong("issuedAtMs", 0L);
        if (issuedAtMs <= 0L
            || Math.abs(System.currentTimeMillis() - issuedAtMs)
                > RelayE2eCrypto.MAX_MESSAGE_AGE_MS) {
            return false;
        }
        String messageId = bounded(
            plaintext.optString("messageId", ""),
            100
        );
        if (!validOpaque(messageId, 16, 100)) return false;
        String command = bounded(plaintext.optString("command", ""), 80);
        if (command.isEmpty()) return false;
        return acceptMessageId(approved.id + ":" + messageId);
    }

    private synchronized boolean acceptMessageId(String messageId) {
        long now = System.currentTimeMillis();
        Iterator<Map.Entry<String, Long>> iterator =
            seenMessageIds.entrySet().iterator();
        while (iterator.hasNext()) {
            Map.Entry<String, Long> entry = iterator.next();
            if (now - entry.getValue() > 5 * 60 * 1000L) iterator.remove();
        }
        if (seenMessageIds.containsKey(messageId)) return false;
        seenMessageIds.put(messageId, now);
        while (seenMessageIds.size() > 512) {
            Iterator<String> keys = seenMessageIds.keySet().iterator();
            if (!keys.hasNext()) break;
            keys.next();
            keys.remove();
        }
        return true;
    }

    private void putPendingViewerLocked(Viewer viewer) {
        if (!pendingApprovalViewers.containsKey(viewer.id)) {
            while (pendingApprovalViewers.size() >= MAX_PENDING_VIEWERS) {
                Iterator<String> iterator =
                    pendingApprovalViewers.keySet().iterator();
                if (!iterator.hasNext()) break;
                iterator.next();
                iterator.remove();
            }
        }
        pendingApprovalViewers.put(viewer.id, viewer);
    }

    private synchronized void pruneApprovedViewers(List<Viewer> active) {
        java.util.HashSet<String> ids = new java.util.HashSet<>();
        for (Viewer viewer : active) ids.add(viewer.id);
        approvedViewers.keySet().removeIf(id -> !ids.contains(id));
    }

    private void rejectViewer(String viewerId) {
        if (viewerId == null || viewerId.isEmpty()) return;
        JSONObject out = new JSONObject();
        try {
            out.put("type", "viewer_decision");
            out.put("viewerId", viewerId);
            out.put("approved", false);
            out.put("accessRole", "readonly");
        } catch (Exception ignored) {
            return;
        }
        send(out);
    }

    private String grantEnvelope(ApprovedViewer viewer) {
        JSONObject grant = new JSONObject();
        try {
            grant.put("v", 2);
            grant.put("type", "grant");
            grant.put("viewerId", viewer.id);
            grant.put("deviceId", viewer.deviceId);
            grant.put("e2eNonce", viewer.e2eNonce);
            grant.put("capability", viewer.capability);
            grant.put("accessRole", viewer.accessRole);
            grant.put("grantGeneration", viewer.grantGeneration);
            grant.put("issuedAtMs", System.currentTimeMillis());
        } catch (Exception ignored) {
            return "";
        }
        return RelayE2eCrypto.seal(viewerToken, grant);
    }

    private boolean validOpaque(
        String value,
        int minimumLength,
        int maximumLength
    ) {
        if (value == null
            || value.length() < minimumLength
            || value.length() > maximumLength) {
            return false;
        }
        for (int index = 0; index < value.length(); index++) {
            char item = value.charAt(index);
            if ((item >= 'a' && item <= 'z')
                || (item >= 'A' && item <= 'Z')
                || (item >= '0' && item <= '9')
                || item == '_'
                || item == '-') {
                continue;
            }
            return false;
        }
        return true;
    }

    private String bounded(String value, int maxLength) {
        String out = value == null ? "" : value.trim();
        return out.length() <= maxLength ? out : out.substring(0, maxLength);
    }

    private String encode(String value) {
        try {
            return URLEncoder.encode(value, StandardCharsets.UTF_8.name());
        } catch (Exception ignored) {
            return value;
        }
    }

    private final class RelaySocketListener extends WebSocketListener {
        @Override
        public void onOpen(WebSocket webSocket, Response response) {
            mainHandler.post(() -> RemoteRelayClient.this.onSocketOpen(webSocket));
        }

        @Override
        public void onMessage(WebSocket webSocket, String text) {
            RemoteRelayClient.this.onMessage(webSocket, text);
        }

        @Override
        public void onClosed(WebSocket webSocket, int code, String reason) {
            String message = reason == null || reason.trim().isEmpty()
                ? "Relay off"
                : reason.trim();
            mainHandler.post(
                () -> RemoteRelayClient.this.onClosed(webSocket, message)
            );
        }

        @Override
        public void onFailure(WebSocket webSocket, Throwable error, Response response) {
            String message = error == null || error.getMessage() == null
                ? "Relay connection failed"
                : error.getMessage();
            mainHandler.post(() -> RemoteRelayClient.this.onClosed(webSocket, message));
        }
    }
}
