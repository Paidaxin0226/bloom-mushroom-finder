package com.locwarp.mock;

final class RelayAdmissionProtocol {
    static final String DOMAIN = "locwarp-host-admission-v1";
    static final long CHALLENGE_MAX_AGE_MS = 30_000L;

    private RelayAdmissionProtocol() {
    }

    static String canonical(
        String sessionId,
        String deviceKeyId,
        String challenge,
        long issuedAtMs,
        String hostToken,
        String viewerVerifier
    ) {
        return DOMAIN
            + "|" + sessionId
            + "|" + deviceKeyId
            + "|" + challenge
            + "|" + issuedAtMs
            + "|" + hostToken
            + "|" + viewerVerifier;
    }

    static boolean isOpaque(String value, int expectedLength) {
        if (value == null || value.length() != expectedLength) return false;
        for (int index = 0; index < value.length(); index++) {
            char item = value.charAt(index);
            if ((item >= 'a' && item <= 'z')
                || (item >= 'A' && item <= 'Z')
                || (item >= '0' && item <= '9')
                || item == '_'
                || item == '-') {
                continue;
            }
            return false;
        }
        return true;
    }
}
