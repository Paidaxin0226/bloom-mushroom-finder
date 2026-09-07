package com.locwarp.mock;

import android.content.Context;
import android.content.SharedPreferences;

import org.json.JSONArray;
import org.json.JSONObject;

import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.List;
import java.util.Locale;
import java.util.UUID;

final class OperationHistoryStore {
    static final class Entry {
        final String id;
        final long timestampMs;
        final String type;
        final String message;
        final boolean success;

        Entry(String id, long timestampMs, String type, String message, boolean success) {
            this.id = id;
            this.timestampMs = timestampMs;
            this.type = type;
            this.message = message;
            this.success = success;
        }
    }

    private static final String PREFS = "locwarp_operation_history";
    private static final String KEY_ENTRIES = "entries";
    private static final int MAX_ENTRIES = 200;

    private OperationHistoryStore() {}

    static synchronized void record(
        Context context,
        String type,
        String message,
        boolean success
    ) {
        if (context == null) return;
        List<Entry> entries = entries(context);
        entries.add(0, new Entry(
            UUID.randomUUID().toString(),
            System.currentTimeMillis(),
            clean(type, "event"),
            clean(message, ""),
            success
        ));
        if (entries.size() > MAX_ENTRIES) {
            entries = new ArrayList<>(entries.subList(0, MAX_ENTRIES));
        }
        save(context, entries);
    }

    static synchronized List<Entry> entries(Context context) {
        List<Entry> out = new ArrayList<>();
        String raw = prefs(context).getString(KEY_ENTRIES, "[]");
        try {
            JSONArray array = new JSONArray(raw);
            for (int i = 0; i < array.length(); i++) {
                JSONObject item = array.getJSONObject(i);
                out.add(new Entry(
                    item.optString("id", ""),
                    item.optLong("timestampMs", 0L),
                    item.optString("type", "event"),
                    item.optString("message", ""),
                    item.optBoolean("success", true)
                ));
            }
        } catch (Exception ignored) {
        }
        return out;
    }

    static synchronized void clear(Context context) {
        prefs(context).edit().remove(KEY_ENTRIES).apply();
    }

    static synchronized String toCsv(Context context) {
        StringBuilder out = new StringBuilder("time,type,success,message\r\n");
        SimpleDateFormat format = new SimpleDateFormat(
            "yyyy-MM-dd HH:mm:ss",
            Locale.US
        );
        for (Entry entry : entries(context)) {
            out.append(csv(format.format(new Date(entry.timestampMs)))).append(',')
                .append(csv(entry.type)).append(',')
                .append(entry.success).append(',')
                .append(csv(entry.message)).append("\r\n");
        }
        return out.toString();
    }

    private static void save(Context context, List<Entry> entries) {
        JSONArray array = new JSONArray();
        try {
            for (Entry entry : entries) {
                JSONObject item = new JSONObject();
                item.put("id", entry.id);
                item.put("timestampMs", entry.timestampMs);
                item.put("type", entry.type);
                item.put("message", entry.message);
                item.put("success", entry.success);
                array.put(item);
            }
        } catch (Exception ignored) {
        }
        prefs(context).edit().putString(KEY_ENTRIES, array.toString()).apply();
    }

    private static SharedPreferences prefs(Context context) {
        return context.getApplicationContext().getSharedPreferences(
            PREFS,
            Context.MODE_PRIVATE
        );
    }

    private static String clean(String value, String fallback) {
        String out = value == null ? "" : value.trim();
        if (out.isEmpty()) out = fallback;
        return out.length() > 500 ? out.substring(0, 500) : out;
    }

    private static String csv(String value) {
        String safe = value == null ? "" : value;
        if (!safe.isEmpty() && "=+-@".indexOf(safe.charAt(0)) >= 0) {
            safe = "'" + safe;
        }
        return "\"" + safe.replace("\"", "\"\"") + "\"";
    }
}
