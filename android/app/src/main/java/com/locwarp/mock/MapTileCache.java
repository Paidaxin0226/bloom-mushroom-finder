package com.locwarp.mock;

import android.content.Context;
import android.util.Base64;
import android.webkit.WebResourceResponse;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.TimeUnit;

import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.ResponseBody;

final class MapTileCache {
    static final String LOCAL_TILE_HOST = "locwarp.tiles";

    private static final long MAX_CACHE_BYTES = 100L * 1024L * 1024L;
    private static final long MAX_TILE_BYTES = 2L * 1024L * 1024L;
    private static final byte[] EMPTY_TILE = Base64.decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
            + "AAAADUlEQVQIHWP4z8DwHwAFgAI/ScL9WQAAAABJRU5ErkJggg==",
        Base64.DEFAULT
    );
    private static final OkHttpClient CLIENT = new OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(15, TimeUnit.SECONDS)
        .followRedirects(true)
        .followSslRedirects(true)
        .build();

    private final Context context;
    private final File root;

    MapTileCache(Context context) {
        this.context = context.getApplicationContext();
        root = new File(this.context.getCacheDir(), "map_tiles");
    }

    WebResourceResponse load(int zoom, int x, int y) {
        if (!validCoordinate(zoom, x, y)) return missing();
        String template = OverlaySettings.mapTileTemplate(context);
        File target = tileFile(template, zoom, x, y);
        WebResourceResponse cached = cached(target);
        if (cached != null) return cached;

        if (OverlaySettings.MAP_TILE_MODE_OFFLINE_ONLY.equals(
            OverlaySettings.mapTileMode(context)
        )) {
            return missing();
        }

        String url = template
            .replace("{z}", String.valueOf(zoom))
            .replace("{x}", String.valueOf(x))
            .replace("{y}", String.valueOf(y));
        try {
            Request request = new Request.Builder()
                .url(url)
                .header("User-Agent", userAgent())
                .build();
            try (Response response = CLIENT.newCall(request).execute()) {
                ResponseBody body = response.body();
                if (!response.isSuccessful() || body == null) return missing();
                long length = body.contentLength();
                if (length > MAX_TILE_BYTES) return missing();
                byte[] bytes = readBounded(body.byteStream(), MAX_TILE_BYTES);
                if (bytes.length == 0) return missing();
                save(target, bytes);
                prune();
                String contentType = response.header("Content-Type", "image/png");
                return response(bytes, contentType);
            }
        } catch (Exception ignored) {
            return missing();
        }
    }

    synchronized CacheStats stats() {
        List<File> files = tileFiles();
        long bytes = 0L;
        for (File file : files) bytes += Math.max(0L, file.length());
        return new CacheStats(files.size(), bytes);
    }

    synchronized void clear() {
        deleteChildren(root);
    }

    private WebResourceResponse cached(File target) {
        if (!target.isFile() || target.length() <= 0L) return null;
        try {
            target.setLastModified(System.currentTimeMillis());
            return new WebResourceResponse(
                "image/png",
                null,
                new FileInputStream(target)
            );
        } catch (Exception ignored) {
            return null;
        }
    }

    private synchronized void save(File target, byte[] bytes) {
        File parent = target.getParentFile();
        if (parent == null || (!parent.isDirectory() && !parent.mkdirs())) return;
        File temporary = new File(parent, target.getName() + ".tmp");
        try (FileOutputStream output = new FileOutputStream(temporary)) {
            output.write(bytes);
            output.flush();
            if (target.exists() && !target.delete()) return;
            if (!temporary.renameTo(target)) {
                try (FileOutputStream fallback = new FileOutputStream(target)) {
                    fallback.write(bytes);
                }
                temporary.delete();
            }
        } catch (Exception ignored) {
            temporary.delete();
        }
    }

    private synchronized void prune() {
        List<File> files = tileFiles();
        long total = 0L;
        for (File file : files) total += Math.max(0L, file.length());
        if (total <= MAX_CACHE_BYTES) return;
        files.sort(Comparator.comparingLong(File::lastModified));
        for (File file : files) {
            if (total <= MAX_CACHE_BYTES) break;
            long length = Math.max(0L, file.length());
            if (file.delete()) total -= length;
        }
    }

    private List<File> tileFiles() {
        List<File> files = new ArrayList<>();
        collectFiles(root, files);
        return files;
    }

    private void collectFiles(File directory, List<File> output) {
        File[] children = directory.listFiles();
        if (children == null) return;
        for (File child : children) {
            if (child.isDirectory()) collectFiles(child, output);
            else if (!child.getName().endsWith(".tmp")) output.add(child);
        }
    }

    private void deleteChildren(File directory) {
        File[] children = directory.listFiles();
        if (children == null) return;
        for (File child : children) {
            if (child.isDirectory()) deleteChildren(child);
            child.delete();
        }
    }

    private File tileFile(String template, int zoom, int x, int y) {
        File source = new File(root, digest(template));
        File zoomDir = new File(source, String.valueOf(zoom));
        File xDir = new File(zoomDir, String.valueOf(x));
        return new File(xDir, y + ".tile");
    }

    private String digest(String value) {
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            byte[] bytes = digest.digest(value.getBytes(StandardCharsets.UTF_8));
            StringBuilder out = new StringBuilder();
            for (int index = 0; index < 8; index++) {
                out.append(String.format(java.util.Locale.US, "%02x", bytes[index]));
            }
            return out.toString();
        } catch (Exception ignored) {
            return Integer.toHexString(value.hashCode());
        }
    }

    private byte[] readBounded(InputStream input, long maxBytes) throws Exception {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        byte[] buffer = new byte[16 * 1024];
        long total = 0L;
        int read;
        while ((read = input.read(buffer)) >= 0) {
            total += read;
            if (total > maxBytes) throw new IllegalArgumentException("Tile too large");
            output.write(buffer, 0, read);
        }
        return output.toByteArray();
    }

    private WebResourceResponse response(byte[] bytes, String contentType) {
        String mime = contentType == null
            ? "image/png"
            : contentType.split(";", 2)[0].trim();
        Map<String, String> headers = new HashMap<>();
        headers.put("Cache-Control", "no-store");
        return new WebResourceResponse(
            mime.isEmpty() ? "image/png" : mime,
            null,
            200,
            "OK",
            headers,
            new ByteArrayInputStream(bytes)
        );
    }

    private WebResourceResponse missing() {
        Map<String, String> headers = new HashMap<>();
        headers.put("Cache-Control", "no-store");
        return new WebResourceResponse(
            "image/png",
            null,
            404,
            "Not Found",
            headers,
            new ByteArrayInputStream(EMPTY_TILE)
        );
    }

    private boolean validCoordinate(int zoom, int x, int y) {
        if (zoom < 0 || zoom > 22 || x < 0 || y < 0) return false;
        int limit = 1 << Math.min(zoom, 22);
        return x < limit && y < limit;
    }

    private String userAgent() {
        String version;
        try {
            version = context.getPackageManager()
                .getPackageInfo(context.getPackageName(), 0)
                .versionName;
        } catch (Exception ignored) {
            version = "unknown";
        }
        return "LocWarp-Android/" + version + " map tile cache";
    }

    static final class CacheStats {
        final int tileCount;
        final long bytes;

        CacheStats(int tileCount, long bytes) {
            this.tileCount = tileCount;
            this.bytes = bytes;
        }
    }
}
