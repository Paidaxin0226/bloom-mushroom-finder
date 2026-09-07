package com.locwarp.mock;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.IOException;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;

import okhttp3.Call;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.ResponseBody;
import okio.BufferedSource;

final class WalkingRoutePlanner {
    private static final String OSRM_BASE_URL = "https://router.project-osrm.org";
    private static final String OSRM_FOSSGIS_BASE_URL = "https://routing.openstreetmap.de";
    private static final String BROUTER_BASE_URL = "https://brouter.de";
    private static final double MIN_ROUTE_POINT_SPACING_M = 8d;
    private static final long CACHE_TTL_MS = 15L * 60L * 1000L;
    private static final int CACHE_MAX_ROUTES = 64;
    private static final int CACHE_MAX_TOTAL_POINTS = 50_000;
    private static final long MAX_RESPONSE_BYTES = 2L * 1024L * 1024L;
    private static final Map<String, CachedRoute> CACHE =
        new LinkedHashMap<>(CACHE_MAX_ROUTES, 0.75f, true);
    private static int cachedPointCount;

    private static final OkHttpClient CLIENT = new OkHttpClient.Builder()
        .connectTimeout(4, TimeUnit.SECONDS)
        .readTimeout(8, TimeUnit.SECONDS)
        .writeTimeout(8, TimeUnit.SECONDS)
        .build();

    private WalkingRoutePlanner() {
    }

    static final class PlannedRoute {
        final List<LocalSimulationEngine.Point> points;
        final List<Integer> stopIndices;

        PlannedRoute(
            List<LocalSimulationEngine.Point> points,
            List<Integer> stopIndices
        ) {
            this.points = points;
            this.stopIndices = stopIndices;
        }
    }

    static List<LocalSimulationEngine.Point> plan(
        LocalSimulationEngine.Point start,
        LocalSimulationEngine.Point destination
    ) throws Exception {
        return plan(start, destination, new RequestContext());
    }

    static AsyncTaskGate.CancelableTask<List<LocalSimulationEngine.Point>> planTask(
        LocalSimulationEngine.Point start,
        LocalSimulationEngine.Point destination
    ) {
        RequestContext context = new RequestContext();
        return new AsyncTaskGate.CancelableTask<List<LocalSimulationEngine.Point>>() {
            @Override
            public List<LocalSimulationEngine.Point> call() throws Exception {
                return plan(start, destination, context);
            }

            @Override
            public void cancel() {
                context.cancel();
            }
        };
    }

    private static List<LocalSimulationEngine.Point> plan(
        LocalSimulationEngine.Point start,
        LocalSimulationEngine.Point destination,
        RequestContext context
    ) throws Exception {
        context.throwIfCanceled();
        if (start == null || destination == null) {
            throw new IllegalArgumentException("start and destination are required");
        }
        if (distanceMeters(start, destination) < 35d) {
            List<LocalSimulationEngine.Point> direct = new ArrayList<>();
            direct.add(start);
            direct.add(destination);
            return direct;
        }
        String cacheKey = cacheKey(start, destination);
        List<LocalSimulationEngine.Point> cached = cachedRoute(cacheKey);
        if (cached != null) return cached;

        List<RouteRequest> requests = new ArrayList<>();
        requests.add(new RouteRequest(
            "osrm",
            osrmUrl(OSRM_BASE_URL, "foot", start, destination)
        ));
        requests.add(new RouteRequest(
            "osrm_fossgis",
            osrmUrl(OSRM_FOSSGIS_BASE_URL + "/routed-foot", "foot", start, destination)
        ));
        requests.add(new RouteRequest(
            "brouter",
            brouterUrl(start, destination)
        ));

        Exception lastError = null;
        for (RouteRequest request : requests) {
            context.throwIfCanceled();
            try {
                List<LocalSimulationEngine.Point> route = request.brouter
                    ? fetchBRouter(request.url, context)
                    : fetchOsrm(request.url, context);
                if (route.size() >= 2) {
                    List<LocalSimulationEngine.Point> compacted =
                        compactRoute(route, start, destination);
                    cacheRoute(cacheKey, compacted);
                    return compacted;
                }
            } catch (Exception error) {
                lastError = error;
            }
        }

        if (lastError != null) throw lastError;
        throw new IOException("route planner returned no points");
    }

    static PlannedRoute planThrough(
        LocalSimulationEngine.Point start,
        List<LocalSimulationEngine.Point> destinations,
        boolean closeLoop
    ) throws Exception {
        return planThrough(start, destinations, closeLoop, new RequestContext());
    }

    static AsyncTaskGate.CancelableTask<PlannedRoute> planThroughTask(
        LocalSimulationEngine.Point start,
        List<LocalSimulationEngine.Point> destinations,
        boolean closeLoop
    ) {
        RequestContext context = new RequestContext();
        return new AsyncTaskGate.CancelableTask<PlannedRoute>() {
            @Override
            public PlannedRoute call() throws Exception {
                return planThrough(start, destinations, closeLoop, context);
            }

            @Override
            public void cancel() {
                context.cancel();
            }
        };
    }

    private static PlannedRoute planThrough(
        LocalSimulationEngine.Point start,
        List<LocalSimulationEngine.Point> destinations,
        boolean closeLoop,
        RequestContext context
    ) throws Exception {
        if (start == null || destinations == null || destinations.isEmpty()) {
            throw new IllegalArgumentException("start and destinations are required");
        }

        List<LocalSimulationEngine.Point> points = new ArrayList<>();
        List<Integer> stopIndices = new ArrayList<>();
        points.add(start);

        LocalSimulationEngine.Point cursor = start;
        for (LocalSimulationEngine.Point destination : destinations) {
            appendLeg(points, plan(cursor, destination, context));
            stopIndices.add(points.size() - 1);
            cursor = destination;
        }

        if (closeLoop && distanceMeters(cursor, start) > 1d) {
            appendLeg(points, plan(cursor, start, context));
        }

        return new PlannedRoute(points, stopIndices);
    }

    private static void appendLeg(
        List<LocalSimulationEngine.Point> target,
        List<LocalSimulationEngine.Point> leg
    ) {
        if (leg == null || leg.isEmpty()) return;
        int startIndex = target.isEmpty() ? 0 : 1;
        for (int i = startIndex; i < leg.size(); i++) {
            target.add(leg.get(i));
        }
    }

    private static String osrmUrl(
        String base,
        String profile,
        LocalSimulationEngine.Point start,
        LocalSimulationEngine.Point destination
    ) {
        String coords = String.format(
            Locale.US,
            "%.7f,%.7f;%.7f,%.7f",
            start.lng,
            start.lat,
            destination.lng,
            destination.lat
        );
        return base + "/route/v1/" + profile + "/" + coords
            + "?overview=full&geometries=geojson&steps=true&annotations=duration,distance";
    }

    private static String brouterUrl(
        LocalSimulationEngine.Point start,
        LocalSimulationEngine.Point destination
    ) throws Exception {
        String lonlats = String.format(
            Locale.US,
            "%.7f,%.7f|%.7f,%.7f",
            start.lng,
            start.lat,
            destination.lng,
            destination.lat
        );
        String encoded = URLEncoder.encode(lonlats, StandardCharsets.UTF_8.name());
        return BROUTER_BASE_URL + "/brouter?lonlats=" + encoded
            + "&profile=shortest&alternativeidx=0&format=geojson";
    }

    private static List<LocalSimulationEngine.Point> fetchOsrm(
        String url,
        RequestContext context
    ) throws Exception {
        JSONObject data = fetchJson(url, context);
        if (!"Ok".equals(data.optString("code"))) {
            throw new IOException("OSRM error: " + data.optString("message", "unknown"));
        }
        JSONArray routes = data.getJSONArray("routes");
        if (routes.length() == 0) throw new IOException("OSRM returned no routes");
        JSONObject geometry = routes.getJSONObject(0).getJSONObject("geometry");
        JSONArray coords = geometry.getJSONArray("coordinates");

        List<LocalSimulationEngine.Point> points = new ArrayList<>();
        for (int i = 0; i < coords.length(); i++) {
            JSONArray raw = coords.getJSONArray(i);
            points.add(new LocalSimulationEngine.Point(
                raw.getDouble(1),
                raw.getDouble(0)
            ));
        }
        return points;
    }

    private static List<LocalSimulationEngine.Point> fetchBRouter(
        String url,
        RequestContext context
    ) throws Exception {
        JSONObject data = fetchJson(url, context);
        JSONArray features = data.getJSONArray("features");
        if (features.length() == 0) throw new IOException("BRouter returned no routes");
        JSONObject geometry = features.getJSONObject(0).getJSONObject("geometry");
        JSONArray coords = geometry.getJSONArray("coordinates");

        List<LocalSimulationEngine.Point> points = new ArrayList<>();
        for (int i = 0; i < coords.length(); i++) {
            JSONArray raw = coords.getJSONArray(i);
            points.add(new LocalSimulationEngine.Point(
                raw.getDouble(1),
                raw.getDouble(0)
            ));
        }
        return points;
    }

    private static JSONObject fetchJson(
        String url,
        RequestContext context
    ) throws Exception {
        Request request = new Request.Builder()
            .url(url)
            .header("User-Agent", "LocWarp-Android/0.2")
            .get()
            .build();
        Call call = CLIENT.newCall(request);
        context.attach(call);
        try (Response response = call.execute()) {
            if (!response.isSuccessful()) {
                throw new IOException("HTTP " + response.code());
            }
            ResponseBody body = response.body();
            if (body == null) throw new IOException("empty response body");
            if (body.contentLength() > MAX_RESPONSE_BYTES) {
                throw new IOException("route response is too large");
            }
            BufferedSource source = body.source();
            source.request(MAX_RESPONSE_BYTES + 1L);
            if (source.getBuffer().size() > MAX_RESPONSE_BYTES) {
                throw new IOException("route response is too large");
            }
            String raw = source.readUtf8();
            return new JSONObject(raw);
        } finally {
            context.detach(call);
        }
    }

    private static String cacheKey(
        LocalSimulationEngine.Point start,
        LocalSimulationEngine.Point destination
    ) {
        return String.format(
            Locale.US,
            "%.5f,%.5f>%.5f,%.5f",
            start.lat,
            start.lng,
            destination.lat,
            destination.lng
        );
    }

    private static synchronized List<LocalSimulationEngine.Point> cachedRoute(
        String key
    ) {
        CachedRoute cached = CACHE.get(key);
        if (cached == null) return null;
        if (System.currentTimeMillis() - cached.createdAtMs > CACHE_TTL_MS) {
            CACHE.remove(key);
            cachedPointCount -= cached.points.size();
            return null;
        }
        return new ArrayList<>(cached.points);
    }

    private static synchronized void cacheRoute(
        String key,
        List<LocalSimulationEngine.Point> points
    ) {
        if (points == null || points.isEmpty()
            || points.size() > CACHE_MAX_TOTAL_POINTS) {
            return;
        }
        CachedRoute previous = CACHE.remove(key);
        if (previous != null) cachedPointCount -= previous.points.size();
        CachedRoute cached = new CachedRoute(
            System.currentTimeMillis(),
            new ArrayList<>(points)
        );
        CACHE.put(key, cached);
        cachedPointCount += cached.points.size();
        while (CACHE.size() > CACHE_MAX_ROUTES
            || cachedPointCount > CACHE_MAX_TOTAL_POINTS) {
            java.util.Iterator<Map.Entry<String, CachedRoute>> iterator =
                CACHE.entrySet().iterator();
            if (!iterator.hasNext()) break;
            CachedRoute oldest = iterator.next().getValue();
            cachedPointCount -= oldest.points.size();
            iterator.remove();
        }
    }

    private static List<LocalSimulationEngine.Point> compactRoute(
        List<LocalSimulationEngine.Point> raw,
        LocalSimulationEngine.Point start,
        LocalSimulationEngine.Point destination
    ) {
        List<LocalSimulationEngine.Point> points = new ArrayList<>();
        points.add(start);
        LocalSimulationEngine.Point last = start;

        for (int i = 0; i < raw.size(); i++) {
            LocalSimulationEngine.Point point = raw.get(i);
            boolean isLast = i == raw.size() - 1;
            if (isLast || distanceMeters(last, point) >= MIN_ROUTE_POINT_SPACING_M) {
                points.add(point);
                last = point;
            }
        }

        if (distanceMeters(points.get(points.size() - 1), destination) > 1d) {
            points.add(destination);
        } else {
            points.set(points.size() - 1, destination);
        }
        return removeNearDuplicates(points);
    }

    private static List<LocalSimulationEngine.Point> removeNearDuplicates(
        List<LocalSimulationEngine.Point> raw
    ) {
        List<LocalSimulationEngine.Point> points = new ArrayList<>();
        for (LocalSimulationEngine.Point point : raw) {
            if (points.isEmpty()
                || distanceMeters(points.get(points.size() - 1), point) > 0.5d) {
                points.add(point);
            }
        }
        return points;
    }

    private static double distanceMeters(
        LocalSimulationEngine.Point a,
        LocalSimulationEngine.Point b
    ) {
        if (a == null || b == null) return Double.POSITIVE_INFINITY;
        double radius = 6371000d;
        double lat1 = Math.toRadians(a.lat);
        double lat2 = Math.toRadians(b.lat);
        double dLat = lat2 - lat1;
        double dLng = Math.toRadians(b.lng - a.lng);
        double h = Math.sin(dLat / 2d) * Math.sin(dLat / 2d)
            + Math.cos(lat1) * Math.cos(lat2)
            * Math.sin(dLng / 2d) * Math.sin(dLng / 2d);
        return radius * 2d * Math.atan2(Math.sqrt(h), Math.sqrt(1d - h));
    }

    private static final class RouteRequest {
        final String name;
        final String url;
        final boolean brouter;

        RouteRequest(String name, String url) {
            this.name = name;
            this.url = url;
            this.brouter = "brouter".equals(name);
        }
    }

    private static final class CachedRoute {
        final long createdAtMs;
        final List<LocalSimulationEngine.Point> points;

        CachedRoute(long createdAtMs, List<LocalSimulationEngine.Point> points) {
            this.createdAtMs = createdAtMs;
            this.points = points;
        }
    }

    private static final class RequestContext {
        private final AtomicBoolean canceled = new AtomicBoolean();
        private final AtomicReference<Call> activeCall = new AtomicReference<>();

        void attach(Call call) throws IOException {
            throwIfCanceled();
            activeCall.set(call);
            if (canceled.get() && activeCall.compareAndSet(call, null)) {
                call.cancel();
                throw new IOException("route request canceled");
            }
        }

        void detach(Call call) {
            activeCall.compareAndSet(call, null);
        }

        void cancel() {
            canceled.set(true);
            Call call = activeCall.getAndSet(null);
            if (call != null) call.cancel();
        }

        void throwIfCanceled() throws IOException {
            if (canceled.get() || Thread.currentThread().isInterrupted()) {
                throw new IOException("route request canceled");
            }
        }
    }
}
