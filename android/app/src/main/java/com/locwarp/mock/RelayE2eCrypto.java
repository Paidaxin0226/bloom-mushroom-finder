package com.locwarp.mock;

import org.json.JSONObject;

import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.Base64;

import javax.crypto.Cipher;
import javax.crypto.Mac;
import javax.crypto.spec.GCMParameterSpec;
import javax.crypto.spec.SecretKeySpec;

final class RelayE2eCrypto {
    static final long MAX_MESSAGE_AGE_MS = 2 * 60 * 1000L;

    private static final byte[] ENVELOPE_AAD =
        "locwarp-relay-envelope-v2".getBytes(StandardCharsets.UTF_8);
    private static final String VIEWER_VERIFIER_DOMAIN =
        "locwarp-relay-viewer-v2\u0000";
    private static final String VIEWER_PROOF_DOMAIN =
        "locwarp-viewer-proof-v2|";
    private static final String PAIRING_DOMAIN =
        "locwarp-pairing-v2|";
    private static final String KEY_DOMAIN =
        "locwarp-e2e-key-v2\u0000";
    private static final SecureRandom RANDOM = new SecureRandom();

    private RelayE2eCrypto() {}

    static String viewerVerifier(String viewerToken) {
        return encode(sha256(
            VIEWER_VERIFIER_DOMAIN + safe(viewerToken)
        ));
    }

    static String viewerProof(
        String viewerToken,
        String sessionId,
        String deviceId,
        String nonce
    ) {
        return encode(hmac(
            viewerToken,
            VIEWER_PROOF_DOMAIN
                + safe(sessionId) + "|"
                + safe(deviceId) + "|"
                + safe(nonce)
        ));
    }

    static boolean verifyViewerProof(
        String viewerToken,
        String sessionId,
        String deviceId,
        String nonce,
        String proof
    ) {
        return same(
            viewerProof(viewerToken, sessionId, deviceId, nonce),
            proof
        );
    }

    static String pairingCode(
        String viewerToken,
        String sessionId,
        String deviceId,
        String nonce
    ) {
        byte[] digest = hmac(
            viewerToken,
            PAIRING_DOMAIN
                + safe(sessionId) + "|"
                + safe(deviceId) + "|"
                + safe(nonce)
        );
        long value = Integer.toUnsignedLong(ByteBuffer.wrap(digest).getInt());
        return String.format(java.util.Locale.US, "%06d", value % 1_000_000L);
    }

    static String seal(String viewerToken, JSONObject payload) {
        if (viewerToken == null || viewerToken.isEmpty() || payload == null) {
            return "";
        }
        try {
            byte[] iv = new byte[12];
            RANDOM.nextBytes(iv);
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(
                Cipher.ENCRYPT_MODE,
                new SecretKeySpec(encryptionKey(viewerToken), "AES"),
                new GCMParameterSpec(128, iv)
            );
            cipher.updateAAD(ENVELOPE_AAD);
            byte[] encrypted = cipher.doFinal(
                payload.toString().getBytes(StandardCharsets.UTF_8)
            );
            return "v2." + encode(iv) + "." + encode(encrypted);
        } catch (Exception ignored) {
            return "";
        }
    }

    static JSONObject open(String viewerToken, String envelope) {
        if (viewerToken == null || viewerToken.isEmpty() || envelope == null) {
            return null;
        }
        String[] parts = envelope.split("\\.", -1);
        if (parts.length != 3 || !"v2".equals(parts[0])) return null;
        try {
            byte[] iv = decode(parts[1]);
            byte[] encrypted = decode(parts[2]);
            // Java's URL decoder accepts non-canonical trailing bits. Reject
            // alternate text encodings so changing any envelope character is
            // always treated as tampering before AES-GCM is attempted.
            if (!encode(iv).equals(parts[1]) || !encode(encrypted).equals(parts[2])) {
                return null;
            }
            if (iv.length != 12 || encrypted.length < 16) return null;
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(
                Cipher.DECRYPT_MODE,
                new SecretKeySpec(encryptionKey(viewerToken), "AES"),
                new GCMParameterSpec(128, iv)
            );
            cipher.updateAAD(ENVELOPE_AAD);
            byte[] plaintext = cipher.doFinal(encrypted);
            return new JSONObject(new String(
                plaintext,
                StandardCharsets.UTF_8
            ));
        } catch (Exception ignored) {
            return null;
        }
    }

    static boolean same(String left, String right) {
        byte[] a = safe(left).getBytes(StandardCharsets.UTF_8);
        byte[] b = safe(right).getBytes(StandardCharsets.UTF_8);
        return a.length == b.length && MessageDigest.isEqual(a, b);
    }

    private static byte[] encryptionKey(String viewerToken) {
        return sha256(KEY_DOMAIN + safe(viewerToken));
    }

    private static byte[] sha256(String value) {
        try {
            return MessageDigest.getInstance("SHA-256").digest(
                safe(value).getBytes(StandardCharsets.UTF_8)
            );
        } catch (Exception error) {
            throw new IllegalStateException(error);
        }
    }

    private static byte[] hmac(String key, String value) {
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(
                safe(key).getBytes(StandardCharsets.UTF_8),
                "HmacSHA256"
            ));
            return mac.doFinal(safe(value).getBytes(StandardCharsets.UTF_8));
        } catch (Exception error) {
            throw new IllegalStateException(error);
        }
    }

    private static String encode(byte[] value) {
        return Base64.getUrlEncoder().withoutPadding().encodeToString(value);
    }

    private static byte[] decode(String value) {
        return Base64.getUrlDecoder().decode(value);
    }

    private static String safe(String value) {
        return value == null ? "" : value;
    }
}
