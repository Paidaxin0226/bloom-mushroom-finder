package com.locwarp.mock;

import android.app.Activity;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.ActivityNotFoundException;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.widget.Toast;

import java.util.Locale;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

public final class MapIntentProxyActivity extends Activity {
    private static final String GOOGLE_MAPS_PACKAGE = "com.google.android.apps.maps";
    private static final Pattern COORDINATE_PATTERN = Pattern.compile(
        "([-+]?\\d{1,2}(?:\\.\\d+)?)\\s*[,，]\\s*([-+]?\\d{1,3}(?:\\.\\d+)?)"
    );
    private static final Pattern GOOGLE_AT_PATTERN = Pattern.compile(
        "/@([-+]?\\d{1,2}(?:\\.\\d+)?),([-+]?\\d{1,3}(?:\\.\\d+)?)"
    );
    private static final Pattern GOOGLE_DATA_PATTERN = Pattern.compile(
        "!3d([-+]?\\d{1,2}(?:\\.\\d+)?)!4d([-+]?\\d{1,3}(?:\\.\\d+)?)"
    );

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        handleIntent(getIntent());
        finish();
        overridePendingTransition(0, 0);
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        handleIntent(intent);
        finish();
        overridePendingTransition(0, 0);
    }

    private void handleIntent(Intent intent) {
        Uri uri = intent == null ? null : intent.getData();
        LocalSimulationEngine.Point point = parseLocationRequest(uri);
        // Recognition uses the app only as a coordinate decoder. This branch
        // must run before flower mode and normal map-link interception so the
        // temporary lease can never teleport, bookmark, or launch navigation.
        if (CoordinateCaptureStore.isActive(this)) {
            if (point != null) {
                CoordinateCaptureStore.capture(this, point, uri == null ? "" : uri.toString());
                Toast.makeText(this, "已取得辨識座標（未瞬移）", Toast.LENGTH_SHORT).show();
            } else {
                Toast.makeText(this, "地圖連結沒有可用座標（未瞬移）", Toast.LENGTH_SHORT).show();
            }
            return;
        }
        if (point != null && OverlaySettings.isFlowerModeActive(this)) {
            new LocalDataStore(this).addFlowerTarget(point);
            copyPointToClipboard(point);
            Toast.makeText(
                this,
                "已加入種花節點：" + LocalSimulationEngine.format(point),
                Toast.LENGTH_SHORT
            ).show();
            return;
        }
        if (point != null
            && OverlaySettings.isMapIntentInterceptEnabled(this)
            && MockLocationKeeper.isMockEnabled(this)) {
            LocalSimulationEngine engine = LocalSimulationEngine.get();
            engine.attach(this, null);
            if (engine.teleport(point.lat, point.lng)) {
                copyPointToClipboard(point);
                Toast.makeText(
                    this,
                    "已攔截地圖定位、複製座標並瞬移：" + LocalSimulationEngine.format(point),
                    Toast.LENGTH_SHORT
                ).show();
                return;
            }
            Toast.makeText(
                this,
                "定位攔截失敗，請確認已選取 LocWarp 作為模擬位置 App",
                Toast.LENGTH_LONG
            ).show();
            return;
        }
        forwardToGoogleMaps(uri);
    }

    private void copyPointToClipboard(LocalSimulationEngine.Point point) {
        ClipboardManager clipboard =
            (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
        if (clipboard == null) return;
        String value = String.format(Locale.US, "%.6f, %.6f", point.lat, point.lng);
        clipboard.setPrimaryClip(ClipData.newPlainText("LocWarp coordinate", value));
    }

    private LocalSimulationEngine.Point parseLocationRequest(Uri uri) {
        if (uri == null) return null;
        String scheme = lower(uri.getScheme());
        String raw = uri.toString();
        String decoded = Uri.decode(raw);
        if ("geo".equals(scheme)) {
            return parseGeoUri(decoded);
        }
        if ("google.navigation".equals(scheme)) {
            return null;
        }
        if ("http".equals(scheme) || "https".equals(scheme)) {
            return parseGoogleMapsUrl(uri, decoded);
        }
        return null;
    }

    private LocalSimulationEngine.Point parseGeoUri(String decoded) {
        String body = decoded.startsWith("geo:") ? decoded.substring(4) : decoded;
        int queryIndex = body.indexOf('?');
        String head = queryIndex >= 0 ? body.substring(0, queryIndex) : body;
        String query = queryIndex >= 0 ? body.substring(queryIndex + 1) : "";
        LocalSimulationEngine.Point q = parameterCoordinate(query, "q");
        if (q != null) return q;
        LocalSimulationEngine.Point ll = parameterCoordinate(query, "ll");
        if (ll != null) return ll;

        LocalSimulationEngine.Point direct = firstCoordinate(head);
        if (direct != null && !(queryIndex >= 0 && isZeroPoint(direct))) {
            return direct;
        }
        return null;
    }

    private LocalSimulationEngine.Point parseGoogleMapsUrl(Uri uri, String decoded) {
        String host = lower(uri.getHost());
        if (host == null) return null;
        if (host.contains("maps.app.goo.gl") || host.contains("goo.gl")) {
            return null;
        }
        if (decoded.contains("/dir/")) {
            return null;
        }

        LocalSimulationEngine.Point query = queryParameterCoordinate(uri, "query");
        if (query != null) return query;
        LocalSimulationEngine.Point q = queryParameterCoordinate(uri, "q");
        if (q != null) return q;
        LocalSimulationEngine.Point ll = queryParameterCoordinate(uri, "ll");
        if (ll != null) return ll;

        Matcher at = GOOGLE_AT_PATTERN.matcher(decoded);
        if (at.find()) return pointFrom(at.group(1), at.group(2));
        Matcher data = GOOGLE_DATA_PATTERN.matcher(decoded);
        if (data.find()) return pointFrom(data.group(1), data.group(2));
        return null;
    }

    private LocalSimulationEngine.Point queryParameterCoordinate(Uri uri, String key) {
        try {
            String value = uri.getQueryParameter(key);
            return firstCoordinate(value);
        } catch (Exception ignored) {
            return null;
        }
    }

    private LocalSimulationEngine.Point parameterCoordinate(String query, String key) {
        if (query == null || query.isEmpty()) return null;
        String prefix = key + "=";
        String[] parts = query.split("&");
        for (String part : parts) {
            if (!part.startsWith(prefix)) continue;
            return firstCoordinate(part.substring(prefix.length()));
        }
        return null;
    }

    private LocalSimulationEngine.Point firstCoordinate(String value) {
        if (value == null) return null;
        Matcher matcher = COORDINATE_PATTERN.matcher(value);
        if (!matcher.find()) return null;
        return pointFrom(matcher.group(1), matcher.group(2));
    }

    private LocalSimulationEngine.Point pointFrom(String rawLat, String rawLng) {
        try {
            double lat = Double.parseDouble(rawLat);
            double lng = Double.parseDouble(rawLng);
            if (!validCoordinate(lat, lng)) return null;
            return new LocalSimulationEngine.Point(lat, lng);
        } catch (Exception ignored) {
            return null;
        }
    }

    private boolean validCoordinate(double lat, double lng) {
        return Double.isFinite(lat) && Double.isFinite(lng)
            && lat >= -90d && lat <= 90d
            && lng >= -180d && lng <= 180d;
    }

    private boolean isZeroPoint(LocalSimulationEngine.Point point) {
        return Math.abs(point.lat) < 0.000001d && Math.abs(point.lng) < 0.000001d;
    }

    private void forwardToGoogleMaps(Uri uri) {
        if (uri == null) {
            launchGoogleMapsHome();
            return;
        }
        Intent forward = new Intent(Intent.ACTION_VIEW, uri)
            .setPackage(GOOGLE_MAPS_PACKAGE)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        try {
            startActivity(forward);
        } catch (ActivityNotFoundException error) {
            launchGoogleMapsHome();
        }
    }

    private void launchGoogleMapsHome() {
        Intent home = new Intent(
            Intent.ACTION_VIEW,
            Uri.parse("https://www.google.com/maps")
        ).setPackage(GOOGLE_MAPS_PACKAGE)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        try {
            startActivity(home);
        } catch (ActivityNotFoundException ignored) {
            Toast.makeText(this, "找不到 Google Maps", Toast.LENGTH_SHORT).show();
        }
    }

    private String lower(String value) {
        return value == null ? null : value.toLowerCase(Locale.ROOT);
    }
}
