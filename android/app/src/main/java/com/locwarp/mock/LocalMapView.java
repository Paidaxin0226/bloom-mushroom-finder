package com.locwarp.mock;

import android.annotation.SuppressLint;
import android.content.Context;
import android.net.Uri;
import android.view.MotionEvent;
import android.view.View;
import android.webkit.JavascriptInterface;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

final class LocalMapView extends WebView {
    private static final String MAP_URL = "file:///android_asset/map/map.html";
    private static final Pattern TILE_PATH = Pattern.compile(
        "^/tiles/(\\d{1,2})/(\\d{1,8})/(\\d{1,8})\\.png$"
    );

    interface Listener {
        void onMapTap(double lat, double lng);
        void onMapLongPress(double lat, double lng);
        void onMapLongPressAction(double lat, double lng, String action);
        void onMapDoubleTap(double lat, double lng);
    }

    private Listener listener;
    private boolean ready;
    private boolean scriptDrainPosted;
    private boolean released;
    private final Map<String, String> pendingScripts = new LinkedHashMap<>();
    private final MapTileCache tileCache;

    @SuppressLint({"SetJavaScriptEnabled", "AddJavascriptInterface"})
    LocalMapView(Context context) {
        super(context);
        tileCache = new MapTileCache(context);
        WebSettings settings = getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setAllowContentAccess(false);
        settings.setAllowFileAccess(true);
        settings.setAllowFileAccessFromFileURLs(false);
        settings.setAllowUniversalAccessFromFileURLs(false);
        settings.setCacheMode(WebSettings.LOAD_DEFAULT);
        settings.setUserAgentString(
            "LocWarp-Android/" + appVersion(context) + " " + settings.getUserAgentString()
        );
        settings.setBuiltInZoomControls(false);
        settings.setDisplayZoomControls(false);
        setOverScrollMode(View.OVER_SCROLL_NEVER);
        setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                if (request == null || !request.isForMainFrame()) return false;
                return !isMapDocumentUrl(request.getUrl());
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, String url) {
                return !isMapDocumentUrl(url == null ? null : Uri.parse(url));
            }

            @Override
            public WebResourceResponse shouldInterceptRequest(
                WebView view,
                WebResourceRequest request
            ) {
                return request == null ? null : interceptTile(request.getUrl());
            }

            @Override
            public WebResourceResponse shouldInterceptRequest(WebView view, String url) {
                return interceptTile(url == null ? null : Uri.parse(url));
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                if (!isMapDocumentUrl(url == null ? null : Uri.parse(url))) return;
                ready = true;
                runScript(
                    "attribution",
                    "window.locwarpMap.setAttribution("
                        + JSONObject.quote(OverlaySettings.mapTileAttribution(context))
                        + ")"
                );
                drainScripts();
            }
        });
        addJavascriptInterface(new Bridge(), "LocWarpAndroid");
        loadUrl(MAP_URL);
    }

    @Override
    public boolean onTouchEvent(MotionEvent event) {
        int action = event.getActionMasked();
        if (action == MotionEvent.ACTION_DOWN
            || action == MotionEvent.ACTION_POINTER_DOWN
            || action == MotionEvent.ACTION_MOVE) {
            if (getParent() != null) {
                getParent().requestDisallowInterceptTouchEvent(true);
            }
        }
        boolean handled = super.onTouchEvent(event);
        if (action == MotionEvent.ACTION_UP || action == MotionEvent.ACTION_CANCEL) {
            if (getParent() != null) {
                getParent().requestDisallowInterceptTouchEvent(false);
            }
        }
        if (action == MotionEvent.ACTION_UP) performClick();
        return handled;
    }

    @Override
    public boolean performClick() {
        super.performClick();
        return true;
    }

    void setListener(Listener nextListener) {
        listener = nextListener;
    }

    void setCurrent(LocalSimulationEngine.Point point, boolean focus) {
        if (point == null) {
            runScript("current", "window.locwarpMap.clearCurrent()");
            return;
        }
        runScript(
            "current",
            "window.locwarpMap.setCurrent("
                + point.lat + "," + point.lng + "," + focus + ")"
        );
    }

    void setTarget(LocalSimulationEngine.Point point) {
        if (point == null) {
            runScript("target", "window.locwarpMap.clearTarget()");
            return;
        }
        runScript(
            "target",
            "window.locwarpMap.setTarget(" + point.lat + "," + point.lng + ")"
        );
    }

    void setRoute(List<LocalSimulationEngine.Point> points) {
        JSONArray array = new JSONArray();
        try {
            for (LocalSimulationEngine.Point point : points) {
                JSONArray raw = new JSONArray();
                raw.put(point.lat);
                raw.put(point.lng);
                array.put(raw);
            }
        } catch (Exception ignored) {
        }
        runScript("route", "window.locwarpMap.setRoute(" + array + ")");
    }

    void setBookmarks(List<LocalDataStore.Bookmark> bookmarks) {
        JSONArray array = new JSONArray();
        try {
            for (LocalDataStore.Bookmark bookmark : bookmarks) {
                JSONObject raw = new JSONObject();
                raw.put("name", bookmark.name);
                raw.put("lat", bookmark.point.lat);
                raw.put("lng", bookmark.point.lng);
                array.put(raw);
            }
        } catch (Exception ignored) {
        }
        runScript("bookmarks", "window.locwarpMap.setBookmarks(" + array + ")");
    }

    void focus(LocalSimulationEngine.Point point, int zoom) {
        if (point == null) return;
        runScript(
            "focus",
            "window.locwarpMap.focus(" + point.lat + "," + point.lng + "," + zoom + ")"
        );
    }

    void setMapControlsVisible(boolean visible) {
        runScript(
            "chrome",
            "window.locwarpMap.setChromeVisible(" + visible + ")"
        );
    }

    void reloadTiles() {
        runScript(
            "tiles",
            "window.locwarpMap.setAttribution("
                + JSONObject.quote(OverlaySettings.mapTileAttribution(getContext()))
                + ");window.locwarpMap.reloadTiles()"
        );
    }

    void setActive(boolean active) {
        if (released) return;
        if (active) onResume();
        else onPause();
    }

    private void runScript(String key, String script) {
        synchronized (pendingScripts) {
            if (released) return;
            pendingScripts.put(key, script);
            if (scriptDrainPosted) return;
            scriptDrainPosted = true;
        }
        post(this::drainScripts);
    }

    private void drainScripts() {
        Map<String, String> scripts;
        synchronized (pendingScripts) {
            if (released) {
                pendingScripts.clear();
                scriptDrainPosted = false;
                return;
            }
            if (!ready) return;
            scripts = new LinkedHashMap<>(pendingScripts);
            pendingScripts.clear();
            scriptDrainPosted = false;
        }
        for (String script : scripts.values()) {
            evaluateJavascript(script, null);
        }
    }

    void release() {
        listener = null;
        synchronized (pendingScripts) {
            released = true;
            ready = false;
            pendingScripts.clear();
        }
        removeJavascriptInterface("LocWarpAndroid");
        stopLoading();
        loadUrl("about:blank");
        destroy();
    }

    private boolean isMapDocumentUrl(Uri uri) {
        return uri != null && isMapDocumentUrl(uri.toString());
    }

    private boolean isMapDocumentUrl(String url) {
        return MAP_URL.equals(url) || (url != null && url.startsWith(MAP_URL + "#"));
    }

    private WebResourceResponse interceptTile(Uri uri) {
        if (uri == null || !MapTileCache.LOCAL_TILE_HOST.equals(uri.getHost())) {
            return null;
        }
        Matcher match = TILE_PATH.matcher(uri.getPath() == null ? "" : uri.getPath());
        if (!match.matches()) return null;
        try {
            return tileCache.load(
                Integer.parseInt(match.group(1)),
                Integer.parseInt(match.group(2)),
                Integer.parseInt(match.group(3))
            );
        } catch (Exception ignored) {
            return null;
        }
    }

    private String appVersion(Context context) {
        try {
            return context.getPackageManager()
                .getPackageInfo(context.getPackageName(), 0)
                .versionName;
        } catch (Exception ignored) {
            return "unknown";
        }
    }

    private static LocalSimulationEngine.Point normalizeMapPoint(double lat, double lng) {
        if (!Double.isFinite(lat) || !Double.isFinite(lng)) return null;
        double normalizedLat = Math.max(-90d, Math.min(90d, lat));
        double normalizedLng = ((lng + 180d) % 360d + 360d) % 360d - 180d;
        return new LocalSimulationEngine.Point(normalizedLat, normalizedLng);
    }

    private final class Bridge {
        @JavascriptInterface
        public void mapTap(double lat, double lng) {
            LocalSimulationEngine.Point point = normalizeMapPoint(lat, lng);
            if (point == null) return;
            post(() -> {
                if (listener != null) listener.onMapTap(point.lat, point.lng);
            });
        }

        @JavascriptInterface
        public void mapLongPress(double lat, double lng) {
            LocalSimulationEngine.Point point = normalizeMapPoint(lat, lng);
            if (point == null) return;
            post(() -> {
                if (listener != null) listener.onMapLongPress(point.lat, point.lng);
            });
        }

        @JavascriptInterface
        public void mapLongPressAction(double lat, double lng, String action) {
            LocalSimulationEngine.Point point = normalizeMapPoint(lat, lng);
            if (point == null) return;
            post(() -> {
                if (listener != null) {
                    listener.onMapLongPressAction(point.lat, point.lng, action);
                }
            });
        }

        @JavascriptInterface
        public void mapDoubleTap(double lat, double lng) {
            LocalSimulationEngine.Point point = normalizeMapPoint(lat, lng);
            if (point == null) return;
            post(() -> {
                if (listener != null) listener.onMapDoubleTap(point.lat, point.lng);
            });
        }
    }
}
