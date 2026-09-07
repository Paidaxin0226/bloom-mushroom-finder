package com.locwarp.mock;

import org.json.JSONObject;
import org.junit.Test;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

public class UsbControlProtocolTest {
    @Test
    public void requestIdValidationIsStrict() {
        assertTrue(UsbControlProtocol.validRequestId("request-1_test.ok"));
        assertFalse(UsbControlProtocol.validRequestId(""));
        assertFalse(UsbControlProtocol.validRequestId("contains spaces"));
    }

    @Test
    public void executorResultKeepsPayloadAndAddsEnvelope() throws Exception {
        JSONObject source = new JSONObject()
            .put("ok", true)
            .put("message", "done")
            .put("backup", "portable-data")
            .put("status", new JSONObject().put("running", false));
        JSONObject result = UsbControlProtocol.wrapExecutorResult("request-1", source);
        assertEquals(1, result.getInt("v"));
        assertEquals("result", result.getString("type"));
        assertEquals("request-1", result.getString("id"));
        assertTrue(result.getBoolean("ok"));
        assertEquals("portable-data", result.getString("backup"));
        assertFalse(result.getJSONObject("status").getBoolean("running"));
    }

    @Test
    public void messageLimitCanCarryMaximumPortableBackup() {
        assertTrue(UsbControlProtocol.MAX_MESSAGE_BYTES > 4 * 1024 * 1024);
    }
}
