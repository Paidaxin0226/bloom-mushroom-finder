package com.locwarp.mock;

import org.json.JSONObject;

import java.util.Iterator;

final class UsbControlProtocol {
    static final int VERSION = 1;
    // Portable backups are limited to 4 MiB by LocWarpBackupCodec. The USB
    // envelope needs a small amount of additional JSON/protocol headroom.
    static final int MAX_MESSAGE_BYTES = 5 * 1024 * 1024;
    static final int MAX_HANDSHAKE_ATTEMPTS = 3;
    static final long COMMAND_TIMEOUT_MS = 45_000L;
    // The desktop sends a heartbeat every 15 seconds during long jobs. Bound
    // abandoned ADB-forward sockets so interrupted clients cannot occupy every
    // handler forever while still allowing normal recognition to remain idle.
    static final int CLIENT_IDLE_TIMEOUT_MS = 45_000;

    private UsbControlProtocol() {
    }

    static boolean validRequestId(String value) {
        return value != null
            && value.length() >= 1
            && value.length() <= 100
            && value.matches("[A-Za-z0-9._-]+");
    }

    static JSONObject result(String requestId, boolean ok, String message) {
        JSONObject out = new JSONObject();
        try {
            out.put("v", VERSION);
            out.put("type", "result");
            out.put("id", validRequestId(requestId) ? requestId : "invalid");
            out.put("ok", ok);
            out.put("message", message == null ? "" : message);
        } catch (Exception ignored) {
        }
        return out;
    }

    static JSONObject wrapExecutorResult(String requestId, JSONObject executorResult) {
        JSONObject out = result(requestId, false, "Empty result");
        if (executorResult == null) return out;
        try {
            Iterator<String> keys = executorResult.keys();
            while (keys.hasNext()) {
                String key = keys.next();
                out.put(key, executorResult.opt(key));
            }
            out.put("v", VERSION);
            out.put("type", "result");
            out.put("id", validRequestId(requestId) ? requestId : "invalid");
        } catch (Exception ignored) {
        }
        return out;
    }
}
