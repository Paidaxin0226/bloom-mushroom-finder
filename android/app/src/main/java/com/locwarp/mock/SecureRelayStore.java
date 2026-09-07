package com.locwarp.mock;

import android.content.Context;
import android.content.SharedPreferences;
import android.security.keystore.KeyGenParameterSpec;
import android.security.keystore.KeyProperties;
import android.util.Base64;

import org.json.JSONObject;

import java.nio.charset.StandardCharsets;
import java.security.KeyStore;

import javax.crypto.Cipher;
import javax.crypto.KeyGenerator;
import javax.crypto.SecretKey;
import javax.crypto.spec.GCMParameterSpec;

final class SecureRelayStore {
    private static final String ANDROID_KEY_STORE = "AndroidKeyStore";
    private static final String KEY_ALIAS = "locwarp_relay_session_v1";
    private static final String PREFS = "locwarp_relay_secrets";
    private static final String KEY_IV = "iv";
    private static final String KEY_CIPHERTEXT = "ciphertext";
    private static final int GCM_TAG_BITS = 128;

    private SecureRelayStore() {}

    static synchronized boolean put(
        Context context,
        String hostToken,
        String viewerToken
    ) {
        try {
            JSONObject payload = new JSONObject();
            payload.put("hostToken", hostToken == null ? "" : hostToken);
            payload.put("viewerToken", viewerToken == null ? "" : viewerToken);
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(Cipher.ENCRYPT_MODE, key());
            byte[] encrypted = cipher.doFinal(
                payload.toString().getBytes(StandardCharsets.UTF_8)
            );
            return prefs(context).edit()
                .putString(
                    KEY_IV,
                    Base64.encodeToString(cipher.getIV(), Base64.NO_WRAP)
                )
                .putString(
                    KEY_CIPHERTEXT,
                    Base64.encodeToString(encrypted, Base64.NO_WRAP)
                )
                .commit();
        } catch (Exception ignored) {
            clear(context);
            return false;
        }
    }

    static synchronized Tokens get(Context context) {
        SharedPreferences preferences = prefs(context);
        String rawIv = preferences.getString(KEY_IV, "");
        String rawCiphertext = preferences.getString(KEY_CIPHERTEXT, "");
        if (rawIv == null || rawIv.isEmpty()
            || rawCiphertext == null || rawCiphertext.isEmpty()) {
            return Tokens.EMPTY;
        }
        try {
            byte[] iv = Base64.decode(rawIv, Base64.NO_WRAP);
            byte[] encrypted = Base64.decode(rawCiphertext, Base64.NO_WRAP);
            Cipher cipher = Cipher.getInstance("AES/GCM/NoPadding");
            cipher.init(
                Cipher.DECRYPT_MODE,
                key(),
                new GCMParameterSpec(GCM_TAG_BITS, iv)
            );
            JSONObject payload = new JSONObject(new String(
                cipher.doFinal(encrypted),
                StandardCharsets.UTF_8
            ));
            return new Tokens(
                payload.optString("hostToken", ""),
                payload.optString("viewerToken", "")
            );
        } catch (Exception ignored) {
            clear(context);
            return Tokens.EMPTY;
        }
    }

    static synchronized void clear(Context context) {
        prefs(context).edit().clear().apply();
    }

    private static SecretKey key() throws Exception {
        KeyStore keyStore = KeyStore.getInstance(ANDROID_KEY_STORE);
        keyStore.load(null);
        java.security.Key existing = keyStore.getKey(KEY_ALIAS, null);
        if (existing instanceof SecretKey) return (SecretKey) existing;

        KeyGenerator generator = KeyGenerator.getInstance(
            KeyProperties.KEY_ALGORITHM_AES,
            ANDROID_KEY_STORE
        );
        generator.init(new KeyGenParameterSpec.Builder(
            KEY_ALIAS,
            KeyProperties.PURPOSE_ENCRYPT | KeyProperties.PURPOSE_DECRYPT
        )
            .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
            .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
            .setKeySize(256)
            .build());
        return generator.generateKey();
    }

    private static SharedPreferences prefs(Context context) {
        return context.getApplicationContext().getSharedPreferences(
            PREFS,
            Context.MODE_PRIVATE
        );
    }

    static final class Tokens {
        static final Tokens EMPTY = new Tokens("", "");

        final String hostToken;
        final String viewerToken;

        Tokens(String hostToken, String viewerToken) {
            this.hostToken = hostToken == null ? "" : hostToken;
            this.viewerToken = viewerToken == null ? "" : viewerToken;
        }
    }
}
