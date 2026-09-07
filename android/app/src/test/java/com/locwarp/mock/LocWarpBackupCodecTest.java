package com.locwarp.mock;

import static org.junit.Assert.assertEquals;

import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Test;

public final class LocWarpBackupCodecTest {
    @Test
    public void inspectReportsPortableDataCounts() throws Exception {
        JSONObject library = new JSONObject()
            .put("bookmarks", encodedString(new JSONArray()
                .put(new JSONObject().put("id", "b1"))
                .put(new JSONObject().put("id", "b2"))
                .toString()))
            .put("routes", encodedString(new JSONArray()
                .put(new JSONObject().put("id", "r1"))
                .toString()));
        JSONObject hooks = new JSONObject()
            .put("location.com.example.one", encodedBoolean(true))
            .put("step.com.example.one", encodedBoolean(true))
            .put("step.com.example.two", encodedBoolean(false));
        JSONObject preferences = emptyPreferenceFiles()
            .put("locwarp_local_library", library)
            .put("locwarp_hook_scope_settings", hooks);
        String raw = backup(preferences).toString();

        LocWarpBackupCodec.Summary summary = LocWarpBackupCodec.inspect(raw);

        assertEquals("0.2.161", summary.appVersion);
        assertEquals(2, summary.bookmarks);
        assertEquals(1, summary.routes);
        assertEquals(2, summary.hookApps);
    }

    @Test(expected = IllegalArgumentException.class)
    public void inspectRejectsUnknownProduct() throws Exception {
        JSONObject raw = backup(emptyPreferenceFiles());
        raw.put("product", "Other");
        LocWarpBackupCodec.inspect(raw.toString());
    }

    @Test(expected = IllegalArgumentException.class)
    public void inspectRejectsUnknownSchema() throws Exception {
        JSONObject raw = backup(emptyPreferenceFiles());
        raw.put("schemaVersion", 99);
        LocWarpBackupCodec.inspect(raw.toString());
    }

    private JSONObject backup(JSONObject preferences) throws Exception {
        return new JSONObject()
            .put("product", "LocWarp")
            .put("schemaVersion", 1)
            .put("exportedAtMs", 1L)
            .put("appVersion", "0.2.161")
            .put("preferences", preferences);
    }

    private JSONObject emptyPreferenceFiles() throws Exception {
        return new JSONObject()
            .put("locwarp_overlay_settings", new JSONObject())
            .put("locwarp_local_library", new JSONObject())
            .put("locwarp_hook_scope_settings", new JSONObject())
            .put("locwarp_operation_history", new JSONObject());
    }

    private JSONObject encodedString(String value) throws Exception {
        return new JSONObject().put("type", "string").put("value", value);
    }

    private JSONObject encodedBoolean(boolean value) throws Exception {
        return new JSONObject().put("type", "boolean").put("value", value);
    }
}
