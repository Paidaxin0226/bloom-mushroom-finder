package com.locwarp.mock;

import android.content.Context;
import android.content.SharedPreferences;

final class OverlaySettings {
    private static final String PREFS = "locwarp_overlay_settings";
    private static final String KEY_JOYSTICK_SIZE_DP = "joystick_size_dp";
    private static final String KEY_MAP_HINT_SIZE_DP = "map_hint_size_dp";
    private static final String KEY_JOYSTICK_VISIBLE = "joystick_visible";
    private static final String KEY_MAP_HINT_VISIBLE = "map_hint_visible";
    private static final String KEY_STRAIGHT_LINE_MOVEMENT = "straight_line_movement";
    private static final String KEY_SPEED_KMH = "speed_kmh";
    private static final String KEY_FLOWER_MODE_ACTIVE = "flower_mode_active";
    private static final String KEY_FLOWER_REMOTE_CONTROLLED = "flower_remote_controlled";
    private static final String KEY_FLOWER_COOLDOWN_UNTIL_MS =
        "flower_cooldown_until_ms";
    private static final String KEY_FLOWER_RADIUS_M = "flower_radius_m";
    private static final String KEY_FLOWER_SPACING_M = "flower_spacing_m";
    private static final String KEY_RELAY_BASE_URL = "relay_base_url";
    private static final String KEY_RELAY_ENABLED = "relay_enabled";
    private static final String KEY_RELAY_SESSION_ID = "relay_session_id";
    private static final String KEY_RELAY_TOKEN = "relay_token";
    private static final String KEY_RELAY_HOST_TOKEN = "relay_host_token";
    private static final String KEY_RELAY_VIEWER_TOKEN = "relay_viewer_token";
    private static final String KEY_MAP_INTENT_INTERCEPT = "map_intent_intercept";
    private static final String KEY_MAP_TILE_MODE = "map_tile_mode";
    private static final String KEY_MAP_TILE_TEMPLATE = "map_tile_template";
    private static final String KEY_MAP_TILE_ATTRIBUTION = "map_tile_attribution";
    private static final String KEY_DEFIT_STEP_COUNT = "defit_step_count";
    private static final String KEY_DEFIT_WALKING_STEPS_ENABLED =
        "defit_walking_steps_enabled";
    private static final String KEY_WALKING_STEP_MULTIPLIER =
        "walking_step_multiplier";
    private static final String KEY_SAFETY_AUTO_STOP_MINUTES =
        "safety_auto_stop_minutes";
    private static final String KEY_SAFETY_RESTORE_ON_FINISH =
        "safety_restore_on_finish";
    private static final String KEY_RESUME_MOVEMENT_AFTER_RESTART =
        "resume_movement_after_restart";

    static final int JOYSTICK_MIN_DP = 150;
    static final int JOYSTICK_MAX_DP = 310;
    static final int JOYSTICK_STEP_DP = 20;
    static final int JOYSTICK_DEFAULT_DP = 210;
    static final double DEFAULT_SPEED_KMH = 10.8d;

    static final int MAP_HINT_MIN_DP = 220;
    static final int MAP_HINT_MAX_DP = 380;
    static final int MAP_HINT_STEP_DP = 20;
    static final int MAP_HINT_DEFAULT_DP = 280;
    static final int DEFIT_DEFAULT_STEP_COUNT = 1000;
    static final int DEFAULT_WALKING_STEP_MULTIPLIER = 1;
    static final String MAP_TILE_MODE_ONLINE_CACHE = "online_cache";
    static final String MAP_TILE_MODE_OFFLINE_ONLY = "offline_only";
    static final String DEFAULT_MAP_TILE_TEMPLATE =
        "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
    static final String DEFAULT_MAP_TILE_ATTRIBUTION =
        "© OpenStreetMap contributors";

    private OverlaySettings() {
    }

    static int joystickSizeDp(Context context) {
        return intPref(context, KEY_JOYSTICK_SIZE_DP, JOYSTICK_DEFAULT_DP);
    }

    static int mapHintSizeDp(Context context) {
        return intPref(context, KEY_MAP_HINT_SIZE_DP, MAP_HINT_DEFAULT_DP);
    }

    static void setJoystickSizeDp(Context context, int sizeDp) {
        prefs(context).edit().putInt(
            KEY_JOYSTICK_SIZE_DP,
            clamp(sizeDp, JOYSTICK_MIN_DP, JOYSTICK_MAX_DP)
        ).apply();
    }

    static void setMapHintSizeDp(Context context, int sizeDp) {
        prefs(context).edit().putInt(
            KEY_MAP_HINT_SIZE_DP,
            clamp(sizeDp, MAP_HINT_MIN_DP, MAP_HINT_MAX_DP)
        ).apply();
    }

    static int adjustJoystickSize(Context context, int delta) {
        int next = clamp(
            joystickSizeDp(context) + delta,
            JOYSTICK_MIN_DP,
            JOYSTICK_MAX_DP
        );
        prefs(context).edit().putInt(KEY_JOYSTICK_SIZE_DP, next).apply();
        return next;
    }

    static int adjustMapHintSize(Context context, int delta) {
        int next = clamp(
            mapHintSizeDp(context) + delta,
            MAP_HINT_MIN_DP,
            MAP_HINT_MAX_DP
        );
        prefs(context).edit().putInt(KEY_MAP_HINT_SIZE_DP, next).apply();
        return next;
    }

    static boolean isJoystickVisible(Context context) {
        return prefs(context).getBoolean(KEY_JOYSTICK_VISIBLE, false);
    }

    static boolean isMapHintVisible(Context context) {
        return prefs(context).getBoolean(KEY_MAP_HINT_VISIBLE, false);
    }

    static boolean isStraightLineMovement(Context context) {
        return prefs(context).getBoolean(KEY_STRAIGHT_LINE_MOVEMENT, false);
    }

    static double speedKmh(Context context) {
        return prefs(context).getFloat(KEY_SPEED_KMH, (float) DEFAULT_SPEED_KMH);
    }

    static boolean isFlowerModeActive(Context context) {
        return prefs(context).getBoolean(KEY_FLOWER_MODE_ACTIVE, false);
    }

    static boolean isFlowerRemoteControlled(Context context) {
        return prefs(context).getBoolean(KEY_FLOWER_REMOTE_CONTROLLED, false);
    }

    static long flowerCooldownUntilMs(Context context) {
        return Math.max(
            0L,
            prefs(context).getLong(KEY_FLOWER_COOLDOWN_UNTIL_MS, 0L)
        );
    }

    static double flowerRadiusMeters(Context context) {
        return clampDouble(
            prefs(context).getFloat(KEY_FLOWER_RADIUS_M, 30f),
            20d,
            38d
        );
    }

    static double flowerSpacingMeters(Context context) {
        return clampDouble(
            prefs(context).getFloat(KEY_FLOWER_SPACING_M, 7f),
            4d,
            10d
        );
    }

    static String relayBaseUrl(Context context) {
        String value = prefs(context).getString(KEY_RELAY_BASE_URL, "");
        if (value == null || value.trim().isEmpty()) return "";
        return value.trim();
    }

    static boolean isRelayEnabled(Context context) {
        return prefs(context).getBoolean(KEY_RELAY_ENABLED, false);
    }

    static String relaySessionId(Context context) {
        return prefs(context).getString(KEY_RELAY_SESSION_ID, "");
    }

    static String relayHostToken(Context context) {
        return relayTokens(context).hostToken;
    }

    static String relayViewerToken(Context context) {
        return relayTokens(context).viewerToken;
    }

    static boolean isMapIntentInterceptEnabled(Context context) {
        return prefs(context).getBoolean(KEY_MAP_INTENT_INTERCEPT, false);
    }

    static String mapTileMode(Context context) {
        String value = prefs(context).getString(
            KEY_MAP_TILE_MODE,
            MAP_TILE_MODE_ONLINE_CACHE
        );
        return MAP_TILE_MODE_OFFLINE_ONLY.equals(value)
            && !DEFAULT_MAP_TILE_TEMPLATE.equals(mapTileTemplate(context))
            ? MAP_TILE_MODE_OFFLINE_ONLY
            : MAP_TILE_MODE_ONLINE_CACHE;
    }

    static String mapTileTemplate(Context context) {
        String value = prefs(context).getString(
            KEY_MAP_TILE_TEMPLATE,
            DEFAULT_MAP_TILE_TEMPLATE
        );
        if (value == null || !validMapTileTemplate(value)) {
            return DEFAULT_MAP_TILE_TEMPLATE;
        }
        return value.trim();
    }

    static String mapTileAttribution(Context context) {
        if (DEFAULT_MAP_TILE_TEMPLATE.equals(mapTileTemplate(context))) {
            return DEFAULT_MAP_TILE_ATTRIBUTION;
        }
        String value = prefs(context).getString(
            KEY_MAP_TILE_ATTRIBUTION,
            "Custom map tiles"
        );
        if (value == null || value.trim().isEmpty()) return "Custom map tiles";
        value = value.trim();
        return value.length() <= 300 ? value : value.substring(0, 300);
    }

    static int defitStepCount(Context context) {
        return clamp(
            prefs(context).getInt(KEY_DEFIT_STEP_COUNT, DEFIT_DEFAULT_STEP_COUNT),
            1,
            200000
        );
    }

    static boolean isDefitWalkingStepsEnabled(Context context) {
        return prefs(context).getBoolean(KEY_DEFIT_WALKING_STEPS_ENABLED, false);
    }

    static int walkingStepMultiplier(Context context) {
        return StepInjectionPolicy.clampWalkingMultiplier(
            prefs(context).getInt(
                KEY_WALKING_STEP_MULTIPLIER,
                DEFAULT_WALKING_STEP_MULTIPLIER
            )
        );
    }

    static int safetyAutoStopMinutes(Context context) {
        return clamp(
            prefs(context).getInt(KEY_SAFETY_AUTO_STOP_MINUTES, 0),
            0,
            24 * 60
        );
    }

    static boolean restoreOnMovementFinish(Context context) {
        return prefs(context).getBoolean(KEY_SAFETY_RESTORE_ON_FINISH, false);
    }

    static boolean resumeMovementAfterRestart(Context context) {
        return prefs(context).getBoolean(KEY_RESUME_MOVEMENT_AFTER_RESTART, true);
    }

    static void setJoystickVisible(Context context, boolean visible) {
        prefs(context).edit().putBoolean(KEY_JOYSTICK_VISIBLE, visible).apply();
    }

    static void setMapHintVisible(Context context, boolean visible) {
        prefs(context).edit().putBoolean(KEY_MAP_HINT_VISIBLE, visible).apply();
    }

    static void setStraightLineMovement(Context context, boolean enabled) {
        prefs(context).edit().putBoolean(KEY_STRAIGHT_LINE_MOVEMENT, enabled).apply();
    }

    static void setSpeedKmh(Context context, double speedKmh) {
        double normalized = Double.isFinite(speedKmh)
            ? Math.max(0.1d, speedKmh)
            : DEFAULT_SPEED_KMH;
        prefs(context).edit().putFloat(KEY_SPEED_KMH, (float) normalized).apply();
    }

    static void setFlowerModeActive(Context context, boolean active) {
        prefs(context).edit().putBoolean(KEY_FLOWER_MODE_ACTIVE, active).apply();
    }

    static void setFlowerRemoteControlled(Context context, boolean controlled) {
        prefs(context).edit()
            .putBoolean(KEY_FLOWER_REMOTE_CONTROLLED, controlled)
            .apply();
    }

    static void setFlowerCooldownUntilMs(Context context, long untilMs) {
        prefs(context).edit()
            .putLong(KEY_FLOWER_COOLDOWN_UNTIL_MS, Math.max(0L, untilMs))
            .apply();
    }

    static void setFlowerGeometry(
        Context context,
        double radiusMeters,
        double spacingMeters
    ) {
        prefs(context).edit()
            .putFloat(
                KEY_FLOWER_RADIUS_M,
                (float) clampDouble(radiusMeters, 20d, 38d)
            )
            .putFloat(
                KEY_FLOWER_SPACING_M,
                (float) clampDouble(spacingMeters, 4d, 10d)
            )
            .apply();
    }

    static void setRelayBaseUrl(Context context, String baseUrl) {
        String normalized = baseUrl == null ? "" : baseUrl.trim();
        prefs(context).edit().putString(KEY_RELAY_BASE_URL, normalized).apply();
    }

    static void setRelaySession(
        Context context,
        String baseUrl,
        String sessionId,
        String hostToken,
        String viewerToken
    ) {
        boolean secureStored = SecureRelayStore.put(
            context,
            hostToken,
            viewerToken
        );
        prefs(context).edit()
            .putString(KEY_RELAY_BASE_URL, baseUrl == null ? "" : baseUrl)
            .putString(KEY_RELAY_SESSION_ID, sessionId == null ? "" : sessionId)
            .remove(KEY_RELAY_TOKEN)
            .remove(KEY_RELAY_HOST_TOKEN)
            .remove(KEY_RELAY_VIEWER_TOKEN)
            .putBoolean(KEY_RELAY_ENABLED, secureStored)
            .apply();
    }

    static void clearRelaySession(Context context) {
        SecureRelayStore.clear(context);
        prefs(context).edit()
            .putBoolean(KEY_RELAY_ENABLED, false)
            .remove(KEY_RELAY_SESSION_ID)
            .remove(KEY_RELAY_TOKEN)
            .remove(KEY_RELAY_HOST_TOKEN)
            .remove(KEY_RELAY_VIEWER_TOKEN)
            .apply();
    }

    private static SecureRelayStore.Tokens relayTokens(Context context) {
        SecureRelayStore.Tokens secure = SecureRelayStore.get(context);
        if (!secure.hostToken.isEmpty() && !secure.viewerToken.isEmpty()) {
            return secure;
        }
        SharedPreferences preferences = prefs(context);
        String legacyHost = preferences.getString(KEY_RELAY_HOST_TOKEN, "");
        String legacyViewer = preferences.getString(KEY_RELAY_VIEWER_TOKEN, "");
        if (legacyHost == null || legacyViewer == null
            || legacyHost.isEmpty() || legacyViewer.isEmpty()) {
            return SecureRelayStore.Tokens.EMPTY;
        }
        if (!SecureRelayStore.put(context, legacyHost, legacyViewer)) {
            return SecureRelayStore.Tokens.EMPTY;
        }
        preferences.edit()
            .remove(KEY_RELAY_TOKEN)
            .remove(KEY_RELAY_HOST_TOKEN)
            .remove(KEY_RELAY_VIEWER_TOKEN)
            .apply();
        return new SecureRelayStore.Tokens(legacyHost, legacyViewer);
    }

    static void setMapIntentInterceptEnabled(Context context, boolean enabled) {
        prefs(context).edit().putBoolean(KEY_MAP_INTENT_INTERCEPT, enabled).apply();
    }

    static void setMapTileSettings(
        Context context,
        String mode,
        String template
    ) {
        setMapTileSettings(
            context,
            mode,
            template,
            DEFAULT_MAP_TILE_TEMPLATE.equals(template)
                ? DEFAULT_MAP_TILE_ATTRIBUTION
                : mapTileAttribution(context)
        );
    }

    static void setMapTileSettings(
        Context context,
        String mode,
        String template,
        String attribution
    ) {
        String normalizedTemplate = validMapTileTemplate(template)
            ? template.trim()
            : DEFAULT_MAP_TILE_TEMPLATE;
        String normalizedMode = MAP_TILE_MODE_OFFLINE_ONLY.equals(mode)
            && !DEFAULT_MAP_TILE_TEMPLATE.equals(normalizedTemplate)
            ? MAP_TILE_MODE_OFFLINE_ONLY
            : MAP_TILE_MODE_ONLINE_CACHE;
        String normalizedAttribution = attribution == null
            ? ""
            : attribution.trim();
        if (DEFAULT_MAP_TILE_TEMPLATE.equals(normalizedTemplate)) {
            normalizedAttribution = DEFAULT_MAP_TILE_ATTRIBUTION;
        } else if (normalizedAttribution.isEmpty()) {
            normalizedAttribution = "Custom map tiles";
        } else if (normalizedAttribution.length() > 300) {
            normalizedAttribution = normalizedAttribution.substring(0, 300);
        }
        prefs(context).edit()
            .putString(KEY_MAP_TILE_MODE, normalizedMode)
            .putString(KEY_MAP_TILE_TEMPLATE, normalizedTemplate)
            .putString(KEY_MAP_TILE_ATTRIBUTION, normalizedAttribution)
            .apply();
    }

    static void setDefitStepCount(Context context, int stepCount) {
        prefs(context).edit()
            .putInt(KEY_DEFIT_STEP_COUNT, clamp(stepCount, 1, 200000))
            .apply();
    }

    static void setDefitWalkingStepsEnabled(Context context, boolean enabled) {
        prefs(context).edit()
            .putBoolean(KEY_DEFIT_WALKING_STEPS_ENABLED, enabled)
            .apply();
    }

    static void setWalkingStepMultiplier(Context context, int multiplier) {
        prefs(context).edit()
            .putInt(
                KEY_WALKING_STEP_MULTIPLIER,
                StepInjectionPolicy.clampWalkingMultiplier(multiplier)
            )
            .apply();
    }

    static void setMovementSafety(
        Context context,
        int autoStopMinutes,
        boolean restoreOnFinish,
        boolean resumeAfterRestart
    ) {
        prefs(context).edit()
            .putInt(
                KEY_SAFETY_AUTO_STOP_MINUTES,
                clamp(autoStopMinutes, 0, 24 * 60)
            )
            .putBoolean(KEY_SAFETY_RESTORE_ON_FINISH, restoreOnFinish)
            .putBoolean(KEY_RESUME_MOVEMENT_AFTER_RESTART, resumeAfterRestart)
            .apply();
    }

    static int percent(int sizeDp, int defaultDp) {
        return Math.round(sizeDp * 100f / defaultDp);
    }

    private static int intPref(Context context, String key, int fallback) {
        return prefs(context).getInt(key, fallback);
    }

    private static SharedPreferences prefs(Context context) {
        return context.getApplicationContext()
            .getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    private static int clamp(int value, int min, int max) {
        return Math.max(min, Math.min(max, value));
    }

    private static double clampDouble(double value, double min, double max) {
        if (!Double.isFinite(value)) return min;
        return Math.max(min, Math.min(max, value));
    }

    static boolean validMapTileTemplate(String template) {
        if (template == null) return false;
        String value = template.trim();
        return value.startsWith("https://")
            && value.contains("{z}")
            && value.contains("{x}")
            && value.contains("{y}")
            && value.length() <= 500;
    }
}
