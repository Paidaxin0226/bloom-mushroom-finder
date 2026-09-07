package com.locwarp.mock;

import android.Manifest;
import android.content.Context;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.content.pm.Signature;
import android.os.Build;
import android.provider.Settings;

import java.security.MessageDigest;
import java.util.Locale;
import java.util.Set;

final class DiagnosticsReport {
    private DiagnosticsReport() {}

    static String create(Context context) {
        Context app = context.getApplicationContext();
        StringBuilder out = new StringBuilder();
        out.append("LocWarp diagnostics\n");
        try {
            int signatureFlags = Build.VERSION.SDK_INT >= Build.VERSION_CODES.P
                ? PackageManager.GET_SIGNING_CERTIFICATES
                : PackageManager.GET_SIGNATURES;
            PackageInfo info = app.getPackageManager().getPackageInfo(
                app.getPackageName(),
                signatureFlags
            );
            long versionCode = Build.VERSION.SDK_INT >= Build.VERSION_CODES.P
                ? info.getLongVersionCode()
                : info.versionCode;
            out.append("Version: ").append(info.versionName)
                .append(" (").append(versionCode).append(")\n");
            out.append("Certificate SHA-256: ")
                .append(certificateDigest(info))
                .append("\n");
        } catch (Exception error) {
            out.append("Package: unavailable\n");
        }
        out.append("Android: ").append(Build.VERSION.RELEASE)
            .append(" / API ").append(Build.VERSION.SDK_INT).append("\n");
        out.append("Mock switch: ")
            .append(MockLocationKeeper.isMockEnabled(app) ? "on" : "off")
            .append("\n");
        MockLocationKeeper.Snapshot snapshot = MockLocationKeeper.snapshot(app);
        out.append("Mock provider: ")
            .append(snapshot.active ? "active" : "inactive")
            .append(" / ").append(snapshot.lat).append(", ").append(snapshot.lng)
            .append("\n");
        out.append("Overlay permission: ")
            .append(Settings.canDrawOverlays(app) ? "granted" : "missing")
            .append("\n");
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            out.append("Notification permission: ")
                .append(app.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                    == PackageManager.PERMISSION_GRANTED ? "granted" : "missing")
                .append("\n");
        }
        out.append("LSPosed service: ")
            .append(XposedScopeRepository.isServiceConnected()
                ? XposedScopeRepository.frameworkLabel()
                : "not connected")
            .append("\n");
        Set<String> packages = HookScopeSettings.fallbackPackages(app);
        out.append("Known Hook apps: ").append(packages.size()).append("\n");
        long now = System.currentTimeMillis();
        for (String packageName : packages) {
            long lastSeen = HookScopeSettings.lastSeenAt(app, packageName);
            out.append("  - ").append(packageName).append(": ");
            if (lastSeen <= 0L) out.append("never");
            else out.append(Math.max(0L, (now - lastSeen) / 1000L)).append("s ago");
            out.append("\n");
        }
        RemoteRelayClient relay = RemoteRelayClient.get(app);
        out.append("Relay: ").append(relay.isRunning() ? "enabled" : "off")
            .append(relay.isConnected() ? " / connected" : " / disconnected")
            .append("\n");
        out.append("Saved routes: ")
            .append(new LocalDataStore(app).routes().size())
            .append("\n");
        out.append("History entries: ")
            .append(OperationHistoryStore.entries(app).size())
            .append("\n");
        return out.toString();
    }

    private static String certificateDigest(PackageInfo info) throws Exception {
        Signature[] signatures;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            signatures = info.signingInfo == null
                ? null
                : info.signingInfo.getApkContentsSigners();
        } else {
            signatures = info.signatures;
        }
        if (signatures == null || signatures.length == 0) return "unavailable";
        byte[] digest = MessageDigest.getInstance("SHA-256")
            .digest(signatures[0].toByteArray());
        StringBuilder out = new StringBuilder();
        for (byte value : digest) out.append(String.format(Locale.US, "%02x", value));
        return out.toString();
    }
}
