package com.locwarp.mock;

import android.content.Context;
import android.content.SharedPreferences;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.Arrays;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

final class LocWarpBackupCodec {
    private static final String PRODUCT = "LocWarp";
    private static final int SCHEMA_VERSION = 1;
    private static final int MAX_BACKUP_CHARS = 4 * 1024 * 1024;
    private static final int MAX_KEYS_PER_FILE = 10_000;
    private static final int MAX_VALUE_CHARS = 2 * 1024 * 1024;
    private static final List<String> PREFERENCE_FILES = Arrays.asList(
        "locwarp_overlay_settings",
        "locwarp_local_library",
        "locwarp_hook_scope_settings",
        "locwarp_operation_history"
    );
    private static final Set<String> EXCLUDED_OVERLAY_KEYS = new HashSet<>(
        Arrays.asList(
            "relay_enabled",
            "relay_session_id",
            "relay_token",
            "relay_host_token",
            "relay_viewer_token",
            "flower_mode_active",
            "flower_remote_controlled",
            "flower_cooldown_until_ms"
        )
    );

    private LocWarpBackupCodec() {}

    static String exportJson(Context context) throws Exception {
        JSONObject root = new JSONObject();
        root.put("product", PRODUCT);
        root.put("schemaVersion", SCHEMA_VERSION);
        root.put("exportedAtMs", System.currentTimeMillis());
        root.put("appVersion", appVersion(context));

        JSONObject preferences = new JSONObject();
        for (String fileName : PREFERENCE_FILES) {
            JSONObject file = new JSONObject();
            for (Map.Entry<String, ?> entry : context.getSharedPreferences(
                fileName,
                Context.MODE_PRIVATE
            ).getAll().entrySet()) {
                if (!exportable(fileName, entry.getKey())) continue;
                JSONObject encoded = encodeValue(entry.getValue());
                if (encoded != null) file.put(entry.getKey(), encoded);
            }
            preferences.put(fileName, file);
        }
        root.put("preferences", preferences);
        return root.toString(2);
    }

    static Summary inspect(String raw) throws Exception {
        Parsed parsed = parse(raw);
        int bookmarks = countLibraryArray(parsed, "bookmarks");
        int routes = countLibraryArray(parsed, "routes");
        int apps = countHookPackages(parsed);
        return new Summary(
            parsed.root.optLong("exportedAtMs", 0L),
            parsed.root.optString("appVersion", "unknown"),
            bookmarks,
            routes,
            apps
        );
    }

    static void restore(Context context, String raw) throws Exception {
        Parsed parsed = parse(raw);
        for (String fileName : PREFERENCE_FILES) {
            Map<String, Object> values = parsed.files.get(fileName);
            if (values == null) values = new HashMap<>();
            SharedPreferences.Editor editor = context.getSharedPreferences(
                fileName,
                Context.MODE_PRIVATE
            ).edit().clear();
            for (Map.Entry<String, Object> entry : values.entrySet()) {
                put(editor, entry.getKey(), entry.getValue());
            }
            if (!editor.commit()) {
                throw new IllegalStateException("無法寫入設定檔 " + fileName);
            }
        }
    }

    private static Parsed parse(String raw) throws Exception {
        if (raw == null || raw.isEmpty() || raw.length() > MAX_BACKUP_CHARS) {
            throw new IllegalArgumentException("備份檔是空的或超過 4 MB");
        }
        JSONObject root = new JSONObject(raw);
        if (!PRODUCT.equals(root.optString("product"))) {
            throw new IllegalArgumentException("這不是 LocWarp 備份檔");
        }
        if (root.optInt("schemaVersion", -1) != SCHEMA_VERSION) {
            throw new IllegalArgumentException("不支援此備份格式版本");
        }
        JSONObject preferences = root.optJSONObject("preferences");
        if (preferences == null) {
            throw new IllegalArgumentException("備份檔缺少設定資料");
        }

        Map<String, Map<String, Object>> files = new HashMap<>();
        for (String fileName : PREFERENCE_FILES) {
            JSONObject file = preferences.optJSONObject(fileName);
            Map<String, Object> values = new HashMap<>();
            if (file != null) {
                if (file.length() > MAX_KEYS_PER_FILE) {
                    throw new IllegalArgumentException("備份檔包含過多設定");
                }
                java.util.Iterator<String> keys = file.keys();
                while (keys.hasNext()) {
                    String key = keys.next();
                    if (!exportable(fileName, key)) continue;
                    if (key.length() > 300) {
                        throw new IllegalArgumentException("備份設定名稱過長");
                    }
                    values.put(key, decodeValue(file.getJSONObject(key)));
                }
            }
            files.put(fileName, values);
        }
        return new Parsed(root, files);
    }

    private static JSONObject encodeValue(Object value) throws Exception {
        JSONObject out = new JSONObject();
        if (value instanceof String) {
            out.put("type", "string");
            out.put("value", value);
        } else if (value instanceof Boolean) {
            out.put("type", "boolean");
            out.put("value", value);
        } else if (value instanceof Integer) {
            out.put("type", "int");
            out.put("value", value);
        } else if (value instanceof Long) {
            out.put("type", "long");
            out.put("value", value);
        } else if (value instanceof Float) {
            out.put("type", "float");
            out.put("value", ((Float) value).doubleValue());
        } else if (value instanceof Set) {
            JSONArray array = new JSONArray();
            for (Object item : (Set<?>) value) {
                if (item instanceof String) array.put(item);
            }
            out.put("type", "stringSet");
            out.put("value", array);
        } else {
            return null;
        }
        return out;
    }

    private static Object decodeValue(JSONObject encoded) throws Exception {
        String type = encoded.optString("type", "");
        if ("string".equals(type)) {
            String value = encoded.getString("value");
            validateString(value);
            return value;
        }
        if ("boolean".equals(type)) return encoded.getBoolean("value");
        if ("int".equals(type)) return encoded.getInt("value");
        if ("long".equals(type)) return encoded.getLong("value");
        if ("float".equals(type)) {
            double value = encoded.getDouble("value");
            if (!Double.isFinite(value) || Math.abs(value) > Float.MAX_VALUE) {
                throw new IllegalArgumentException("備份包含無效數字");
            }
            return (float) value;
        }
        if ("stringSet".equals(type)) {
            JSONArray array = encoded.getJSONArray("value");
            if (array.length() > MAX_KEYS_PER_FILE) {
                throw new IllegalArgumentException("備份集合過大");
            }
            Set<String> values = new HashSet<>();
            for (int index = 0; index < array.length(); index++) {
                String value = array.getString(index);
                validateString(value);
                values.add(value);
            }
            return values;
        }
        throw new IllegalArgumentException("備份包含不支援的設定型別");
    }

    @SuppressWarnings("unchecked")
    private static void put(
        SharedPreferences.Editor editor,
        String key,
        Object value
    ) {
        if (value instanceof String) editor.putString(key, (String) value);
        else if (value instanceof Boolean) editor.putBoolean(key, (Boolean) value);
        else if (value instanceof Integer) editor.putInt(key, (Integer) value);
        else if (value instanceof Long) editor.putLong(key, (Long) value);
        else if (value instanceof Float) editor.putFloat(key, (Float) value);
        else if (value instanceof Set) {
            editor.putStringSet(key, new HashSet<>((Set<String>) value));
        }
    }

    private static boolean exportable(String fileName, String key) {
        return key != null
            && (!"locwarp_overlay_settings".equals(fileName)
                || !EXCLUDED_OVERLAY_KEYS.contains(key));
    }

    private static void validateString(String value) {
        if (value != null && value.length() > MAX_VALUE_CHARS) {
            throw new IllegalArgumentException("備份文字內容過大");
        }
    }

    private static int countLibraryArray(Parsed parsed, String key) {
        try {
            Object raw = parsed.files.get("locwarp_local_library").get(key);
            return raw instanceof String ? new JSONArray((String) raw).length() : 0;
        } catch (Exception ignored) {
            return 0;
        }
    }

    private static int countHookPackages(Parsed parsed) {
        Set<String> packages = new HashSet<>();
        Map<String, Object> values = parsed.files.get("locwarp_hook_scope_settings");
        if (values == null) return 0;
        for (Map.Entry<String, Object> entry : values.entrySet()) {
            if (entry.getKey().startsWith("location.")
                || entry.getKey().startsWith("step.")) {
                packages.add(entry.getKey().substring(entry.getKey().indexOf('.') + 1));
            } else if (entry.getValue() instanceof Set) {
                for (Object item : (Set<?>) entry.getValue()) {
                    if (item instanceof String) packages.add((String) item);
                }
            }
        }
        return packages.size();
    }

    private static String appVersion(Context context) {
        try {
            return context.getPackageManager()
                .getPackageInfo(context.getPackageName(), 0)
                .versionName;
        } catch (Exception ignored) {
            return "unknown";
        }
    }

    static final class Summary {
        final long exportedAtMs;
        final String appVersion;
        final int bookmarks;
        final int routes;
        final int hookApps;

        Summary(
            long exportedAtMs,
            String appVersion,
            int bookmarks,
            int routes,
            int hookApps
        ) {
            this.exportedAtMs = exportedAtMs;
            this.appVersion = appVersion;
            this.bookmarks = bookmarks;
            this.routes = routes;
            this.hookApps = hookApps;
        }
    }

    private static final class Parsed {
        final JSONObject root;
        final Map<String, Map<String, Object>> files;

        Parsed(JSONObject root, Map<String, Map<String, Object>> files) {
            this.root = root;
            this.files = files;
        }
    }
}
