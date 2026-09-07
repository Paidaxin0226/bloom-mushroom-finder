package com.locwarp.mock;

import android.content.Context;
import android.content.SharedPreferences;

import java.util.Collections;
import java.util.HashSet;
import java.util.Set;
import java.util.regex.Pattern;

final class HookScopeSettings {
    private static final String PREFS = "locwarp_hook_scope_settings";
    private static final String KEY_OBSERVED_PACKAGES = "observed_packages";
    private static final String KEY_MANUAL_PACKAGES = "manual_packages";
    private static final String KEY_LOCATION_PREFIX = "location.";
    private static final String KEY_STEP_PREFIX = "step.";
    private static final String KEY_LAST_SEEN_PREFIX = "last_seen.";
    private static final Pattern PACKAGE_PATTERN = Pattern.compile(
        "[A-Za-z_][A-Za-z0-9_]*(?:\\.[A-Za-z_][A-Za-z0-9_]*)*"
    );

    private HookScopeSettings() {}

    static boolean isValidPackageName(String packageName) {
        return packageName != null
            && PACKAGE_PATTERN.matcher(packageName).matches();
    }

    static boolean isLocationEnabled(Context context, String packageName) {
        return prefs(context).getBoolean(KEY_LOCATION_PREFIX + packageName, true);
    }

    static boolean isStepEnabled(Context context, String packageName) {
        return prefs(context).getBoolean(KEY_STEP_PREFIX + packageName, true);
    }

    static void setLocationEnabled(
        Context context,
        String packageName,
        boolean enabled
    ) {
        prefs(context).edit()
            .putBoolean(KEY_LOCATION_PREFIX + packageName, enabled)
            .apply();
    }

    static void setStepEnabled(
        Context context,
        String packageName,
        boolean enabled
    ) {
        prefs(context).edit()
            .putBoolean(KEY_STEP_PREFIX + packageName, enabled)
            .apply();
    }

    static long lastSeenAt(Context context, String packageName) {
        if (!isValidPackageName(packageName)) return 0L;
        return Math.max(
            0L,
            prefs(context).getLong(KEY_LAST_SEEN_PREFIX + packageName, 0L)
        );
    }

    static void rememberObservedPackage(Context context, String packageName) {
        if (!isValidPackageName(packageName)
            || context.getPackageName().equals(packageName)) {
            return;
        }
        SharedPreferences preferences = prefs(context);
        Set<String> current = preferences.getStringSet(
            KEY_OBSERVED_PACKAGES,
            Collections.emptySet()
        );
        Set<String> updated = new HashSet<>(current);
        updated.add(packageName);
        preferences.edit()
            .putStringSet(KEY_OBSERVED_PACKAGES, updated)
            .putLong(KEY_LAST_SEEN_PREFIX + packageName, System.currentTimeMillis())
            .apply();
    }

    static Set<String> observedPackages(Context context) {
        return new HashSet<>(prefs(context).getStringSet(
            KEY_OBSERVED_PACKAGES,
            Collections.emptySet()
        ));
    }

    static void rememberManualPackage(Context context, String packageName) {
        updatePackageSet(context, KEY_MANUAL_PACKAGES, packageName, true);
    }

    static void forgetManualPackage(Context context, String packageName) {
        updatePackageSet(context, KEY_MANUAL_PACKAGES, packageName, false);
    }

    static Set<String> manualPackages(Context context) {
        return new HashSet<>(prefs(context).getStringSet(
            KEY_MANUAL_PACKAGES,
            Collections.emptySet()
        ));
    }

    static Set<String> fallbackPackages(Context context) {
        Set<String> result = observedPackages(context);
        result.addAll(manualPackages(context));
        return result;
    }

    private static void updatePackageSet(
        Context context,
        String key,
        String packageName,
        boolean add
    ) {
        if (!isValidPackageName(packageName)
            || context.getPackageName().equals(packageName)) {
            return;
        }
        SharedPreferences preferences = prefs(context);
        Set<String> updated = new HashSet<>(preferences.getStringSet(
            key,
            Collections.emptySet()
        ));
        if (add) updated.add(packageName);
        else updated.remove(packageName);
        preferences.edit().putStringSet(key, updated).apply();
    }

    private static SharedPreferences prefs(Context context) {
        return context.getApplicationContext().getSharedPreferences(
            PREFS,
            Context.MODE_PRIVATE
        );
    }
}
