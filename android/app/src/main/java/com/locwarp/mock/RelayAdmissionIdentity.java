package com.locwarp.mock;

import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;

import java.nio.charset.StandardCharsets;
import java.security.KeyPair;
import java.security.KeyPairGenerator;
import java.security.KeyStore;
import java.security.MessageDigest;
import java.security.PrivateKey;
import java.security.Signature;
import java.security.cert.Certificate;
import java.security.spec.ECGenParameterSpec;
import java.util.Locale;

final class RelayAdmissionIdentity {
    private static final String KEYSTORE = "AndroidKeyStore";
    private static final String ALIAS = "locwarp_relay_admission_v1";
    private static final Object LOCK = new Object();

    private RelayAdmissionIdentity() {
    }

    static String keyId() {
        byte[] publicKey = publicKeyBytes();
        if (publicKey.length == 0) return "";
        return encode(digest(publicKey));
    }

    static String publicKeyEntry() {
        byte[] publicKey = publicKeyBytes();
        if (publicKey.length == 0) return "";
        return encode(digest(publicKey)) + ":" + encode(publicKey);
    }

    static String fingerprint() {
        byte[] publicKey = publicKeyBytes();
        if (publicKey.length == 0) return "";
        byte[] fingerprint = digest(publicKey);
        if (fingerprint.length == 0) return "";
        StringBuilder out = new StringBuilder(fingerprint.length * 3);
        for (int index = 0; index < fingerprint.length; index++) {
            if (index > 0 && index % 2 == 0) out.append(' ');
            out.append(String.format(
                Locale.ROOT,
                "%02X",
                fingerprint[index] & 0xff
            ));
        }
        return out.toString();
    }

    static String sign(
        String sessionId,
        String challenge,
        long issuedAtMs,
        String hostToken,
        String viewerVerifier
    ) {
        synchronized (LOCK) {
            try {
                KeyPair pair = keyPair();
                String deviceKeyId = encode(digest(
                    pair.getPublic().getEncoded()
                ));
                Signature signature = Signature.getInstance(
                    "SHA256withECDSA"
                );
                signature.initSign(pair.getPrivate());
                signature.update(RelayAdmissionProtocol.canonical(
                    sessionId,
                    deviceKeyId,
                    challenge,
                    issuedAtMs,
                    hostToken,
                    viewerVerifier
                ).getBytes(StandardCharsets.UTF_8));
                return encode(signature.sign());
            } catch (Exception ignored) {
                return "";
            }
        }
    }

    private static byte[] publicKeyBytes() {
        synchronized (LOCK) {
            try {
                return keyPair().getPublic().getEncoded();
            } catch (Exception ignored) {
                return new byte[0];
            }
        }
    }

    private static KeyPair keyPair() throws Exception {
        KeyStore keyStore = KeyStore.getInstance(KEYSTORE);
        keyStore.load(null);
        if (!keyStore.containsAlias(ALIAS)) {
            KeyPairGenerator generator = KeyPairGenerator.getInstance(
                KeyProperties.KEY_ALGORITHM_EC,
                KEYSTORE
            );
            generator.initialize(new KeyGenParameterSpec.Builder(
                ALIAS,
                KeyProperties.PURPOSE_SIGN | KeyProperties.PURPOSE_VERIFY
            )
                .setAlgorithmParameterSpec(new ECGenParameterSpec("secp256r1"))
                .setDigests(KeyProperties.DIGEST_SHA256)
                .setUserAuthenticationRequired(false)
                .build());
            generator.generateKeyPair();
            keyStore.load(null);
        }
        PrivateKey privateKey = (PrivateKey) keyStore.getKey(ALIAS, null);
        Certificate certificate = keyStore.getCertificate(ALIAS);
        if (privateKey == null || certificate == null) {
            throw new IllegalStateException("Relay admission key unavailable");
        }
        return new KeyPair(certificate.getPublicKey(), privateKey);
    }

    private static byte[] digest(byte[] value) {
        try {
            return MessageDigest.getInstance("SHA-256").digest(value);
        } catch (Exception ignored) {
            return new byte[0];
        }
    }

    private static String encode(byte[] value) {
        return Base64.encodeToString(
            value,
            Base64.URL_SAFE | Base64.NO_PADDING | Base64.NO_WRAP
        );
    }
}
