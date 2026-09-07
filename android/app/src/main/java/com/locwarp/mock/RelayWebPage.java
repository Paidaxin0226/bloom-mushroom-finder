package com.locwarp.mock;

import android.content.Context;
import android.util.Base64;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;
import java.util.Locale;

final class RelayWebPage {
    private static final String ASSET_PATH = "relay/control-page.html";
    private static final String LEAFLET_CSS_PATH = "map/leaflet/leaflet.css";
    private static final String LEAFLET_JS_PATH = "map/leaflet/leaflet.js";
    private static final SecureRandom RANDOM = new SecureRandom();
    private static volatile String cachedTemplate;

    private RelayWebPage() {}

    static JSONObject response(Context context, JSONObject request) {
        String id = request == null ? "" : request.optString("id", "");
        String method = request == null
            ? ""
            : request.optString("method", "GET").toUpperCase(Locale.ROOT);
        String path = request == null ? "" : request.optString("path", "");
        int queryIndex = path.indexOf('?');
        String pathname = queryIndex >= 0 ? path.substring(0, queryIndex) : path;
        if (!"GET".equals(method) && !"HEAD".equals(method)) {
            return textResponse(id, 405, "Method not allowed");
        }
        if (!pathname.matches("^/r/[A-Za-z0-9_-]{8,80}/?$")) {
            return textResponse(id, 404, "Not found");
        }
        try {
            String nonce = randomNonce();
            String body = template(context).replace("{{NONCE}}", nonce);
            JSONObject headers = securityHeaders(nonce);
            return response(
                id,
                200,
                headers,
                "HEAD".equals(method) ? "" : body
            );
        } catch (Exception error) {
            return textResponse(id, 500, "Page unavailable");
        }
    }

    private static JSONObject securityHeaders(String nonce) {
        JSONObject headers = new JSONObject();
        try {
            headers.put("content-type", "text/html; charset=utf-8");
            headers.put("cache-control", "no-store");
            headers.put(
                "content-security-policy",
                "default-src 'none'; "
                    + "style-src 'nonce-" + nonce + "'; "
                    + "script-src 'nonce-" + nonce + "'; "
                    + "connect-src 'self'; "
                    + "img-src 'self' data: https://tile.openstreetmap.org; "
                    + "base-uri 'none'; frame-ancestors 'none'; "
                    + "form-action 'none'"
            );
            headers.put("referrer-policy", "no-referrer");
            headers.put(
                "permissions-policy",
                "camera=(), microphone=(), geolocation=()"
            );
            headers.put("x-content-type-options", "nosniff");
            headers.put("x-frame-options", "DENY");
        } catch (Exception ignored) {
        }
        return headers;
    }

    private static JSONObject textResponse(String id, int status, String body) {
        JSONObject headers = new JSONObject();
        try {
            headers.put("content-type", "text/plain; charset=utf-8");
            headers.put("cache-control", "no-store");
            headers.put("x-content-type-options", "nosniff");
        } catch (Exception ignored) {
        }
        return response(id, status, headers, body);
    }

    private static JSONObject response(
        String id,
        int status,
        JSONObject headers,
        String body
    ) {
        JSONObject out = new JSONObject();
        try {
            out.put("type", "http_response");
            out.put("id", id == null ? "" : id);
            out.put("status", status);
            out.put("headers", headers == null ? new JSONObject() : headers);
            out.put("body", body == null ? "" : body);
        } catch (Exception ignored) {
        }
        return out;
    }

    private static String template(Context context) throws Exception {
        String value = cachedTemplate;
        if (value != null) return value;
        synchronized (RelayWebPage.class) {
            value = cachedTemplate;
            if (value != null) return value;
            value = readAsset(context, ASSET_PATH)
                .replace("{{LEAFLET_CSS}}", readAsset(context, LEAFLET_CSS_PATH))
                .replace("{{LEAFLET_JS}}", readAsset(context, LEAFLET_JS_PATH));
            if (
                !value.contains("{{NONCE}}")
                    || value.contains("{{LEAFLET_CSS}}")
                    || value.contains("{{LEAFLET_JS}}")
            ) {
                throw new IllegalStateException("Relay page placeholder invalid");
            }
            cachedTemplate = value;
            return value;
        }
    }

    private static String readAsset(Context context, String path) throws Exception {
        StringBuilder out = new StringBuilder();
        try (
            BufferedReader reader = new BufferedReader(
                new InputStreamReader(
                    context.getAssets().open(path),
                    StandardCharsets.UTF_8
                )
            )
        ) {
            String line;
            while ((line = reader.readLine()) != null) {
                out.append(line).append('\n');
            }
        }
        return out.toString();
    }

    private static String randomNonce() {
        byte[] bytes = new byte[18];
        RANDOM.nextBytes(bytes);
        return Base64.encodeToString(bytes, Base64.NO_WRAP);
    }
}
