package com.locwarp.mock;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import org.json.JSONObject;
import org.junit.Test;

public final class RelayE2eCryptoTest {
    private static final String TOKEN =
        "viewer-token-with-at-least-256-bits-of-randomness";

    @Test
    public void verifierProofAndPairingAreDomainSeparated() {
        String verifier = RelayE2eCrypto.viewerVerifier(TOKEN);
        String proof = RelayE2eCrypto.viewerProof(
            TOKEN,
            "session-001",
            "device-001",
            "nonce-001-long-enough"
        );
        String pairing = RelayE2eCrypto.pairingCode(
            TOKEN,
            "session-001",
            "device-001",
            "nonce-001-long-enough"
        );

        assertEquals(43, verifier.length());
        assertEquals(43, proof.length());
        assertTrue(pairing.matches("\\d{6}"));
        assertNotEquals(verifier, proof);
        assertTrue(RelayE2eCrypto.verifyViewerProof(
            TOKEN,
            "session-001",
            "device-001",
            "nonce-001-long-enough",
            proof
        ));
        assertFalse(RelayE2eCrypto.verifyViewerProof(
            TOKEN,
            "session-001",
            "different-device",
            "nonce-001-long-enough",
            proof
        ));
    }

    @Test
    public void aesGcmRoundTripRejectsTamperingAndWrongSecret() throws Exception {
        JSONObject payload = new JSONObject()
            .put("v", 2)
            .put("type", "command")
            .put("messageId", "message-id-001-long");

        String envelope = RelayE2eCrypto.seal(TOKEN, payload);
        assertTrue(envelope.startsWith("v2."));
        JSONObject opened = RelayE2eCrypto.open(TOKEN, envelope);
        assertNotNull(opened);
        assertEquals("command", opened.getString("type"));

        char finalCharacter = envelope.charAt(envelope.length() - 1);
        String tampered = envelope.substring(0, envelope.length() - 1)
            + (finalCharacter == 'A' ? 'B' : 'A');
        assertNull(RelayE2eCrypto.open(TOKEN, tampered));
        assertNull(RelayE2eCrypto.open("wrong-viewer-secret", envelope));
    }

    @Test
    public void constantTimeComparisonUsesExactValues() {
        assertTrue(RelayE2eCrypto.same("exact", "exact"));
        assertFalse(RelayE2eCrypto.same("exact", "Exact"));
        assertFalse(RelayE2eCrypto.same("short", "longer"));
    }
}
