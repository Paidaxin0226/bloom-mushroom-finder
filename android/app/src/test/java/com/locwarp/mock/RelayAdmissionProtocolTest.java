package com.locwarp.mock;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class RelayAdmissionProtocolTest {
    @Test
    public void canonicalPayloadMatchesRelayServerFormat() {
        assertEquals(
            "locwarp-host-admission-v1|session|device|challenge|123|host|viewer",
            RelayAdmissionProtocol.canonical(
                "session",
                "device",
                "challenge",
                123L,
                "host",
                "viewer"
            )
        );
    }

    @Test
    public void opaqueValidationRejectsSeparatorsAndWrongLength() {
        assertTrue(RelayAdmissionProtocol.isOpaque("Abc_123-x", 9));
        assertFalse(RelayAdmissionProtocol.isOpaque("Abc|123-x", 9));
        assertFalse(RelayAdmissionProtocol.isOpaque("Abc_123-x", 8));
    }
}
