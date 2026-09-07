package com.locwarp.mock;

import android.annotation.SuppressLint;
import android.content.Context;
import android.content.SharedPreferences;
import android.location.Location;
import android.location.LocationManager;
import android.location.provider.ProviderProperties;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.SystemClock;
import android.util.Log;

public final class MockLocationKeeper {
    private static final String TAG = "LocWarpMockKeeper";
    private static final long KEEPALIVE_MS = 1200L;
    private static final long STATE_PERSIST_INTERVAL_MS = 5_000L;

    public static final String PREFS = "locwarp_mock_state";
    public static final String KEY_MOCK_ENABLED = "mock_enabled";
    private static final String KEY_ACTIVE = "active";
    private static final String KEY_LAT = "lat";
    private static final String KEY_LNG = "lng";
    private static final String KEY_SPEED = "speed";
    private static final String KEY_BEARING = "bearing";
    private static final String KEY_HAS_SPEED = "has_speed";
    private static final String KEY_HAS_BEARING = "has_bearing";
    private static final String KEY_REVISION = "revision";
    private static final String KEY_SOURCE = "source";
    private static final String KEY_UPDATED_AT_MS = "updated_at_ms";

    private static HandlerThread workerThread;
    private static Handler workerHandler;
    private static Context appContext;
    private static boolean active;
    private static double lat;
    private static double lng;
    private static float speed = Float.NaN;
    private static float bearing = Float.NaN;
    private static long revision;
    private static long updatedAtMs;
    private static long lastPersistedAtMs;
    private static boolean lastPersistedActive;
    private static String source = "phone";
    private static boolean metadataLoaded;

    private static final Runnable TICK = new Runnable() {
        @Override
        public void run() {
            Context context;
            synchronized (MockLocationKeeper.class) {
                if (!active || appContext == null || !isMockEnabled(appContext)) {
                    stopLoopLocked();
                    return;
                }
                context = appContext;
            }

            boolean accepted = pushCurrent(context);

            synchronized (MockLocationKeeper.class) {
                if (!accepted) {
                    active = false;
                    Snapshot snapshot = snapshot(context);
                    saveState(context, false, snapshot.source, false);
                    stopLoopLocked();
                    return;
                }
                if (active && workerHandler != null && appContext != null && isMockEnabled(appContext)) {
                    workerHandler.postDelayed(this, KEEPALIVE_MS);
                }
            }
        }
    };

    private MockLocationKeeper() {
    }

    public static synchronized boolean isMockEnabled(Context context) {
        return context.getApplicationContext()
            .getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getBoolean(KEY_MOCK_ENABLED, true);
    }

    public static synchronized boolean isActive(Context context) {
        Context targetContext = context.getApplicationContext();
        ensureMetadataLoaded(targetContext);
        return isMockEnabled(targetContext) && active;
    }

    public static synchronized void setMockEnabled(Context context, boolean enabled) {
        Context targetContext = context.getApplicationContext();
        ensureMetadataLoaded(targetContext);
        targetContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
            .putBoolean(KEY_MOCK_ENABLED, enabled)
            .apply();
        appContext = targetContext;

        if (enabled) {
            restoreIfEnabled(targetContext);
        } else {
            active = false;
            Snapshot snapshot = snapshot(targetContext);
            saveState(targetContext, false, snapshot.source, false);
            stopLoopLocked();
            clearProviders(targetContext);
        }
    }

    public static synchronized boolean restoreIfEnabled(Context context) {
        Context targetContext = context.getApplicationContext();
        if (!isMockEnabled(targetContext)) return false;

        SharedPreferences prefs = targetContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        if (!prefs.getBoolean(KEY_ACTIVE, false)) return false;

        double savedLat = Double.longBitsToDouble(prefs.getLong(KEY_LAT, Double.doubleToLongBits(0.0d)));
        double savedLng = Double.longBitsToDouble(prefs.getLong(KEY_LNG, Double.doubleToLongBits(0.0d)));
        float savedSpeed = prefs.getBoolean(KEY_HAS_SPEED, false) ? prefs.getFloat(KEY_SPEED, 0.0f) : Float.NaN;
        float savedBearing = prefs.getBoolean(KEY_HAS_BEARING, false) ? prefs.getFloat(KEY_BEARING, 0.0f) : Float.NaN;
        String savedSource = prefs.getString(KEY_SOURCE, "phone");
        return startInternal(
            targetContext,
            savedLat,
            savedLng,
            savedSpeed,
            savedBearing,
            savedSource,
            false
        );
    }

    public static synchronized boolean startLocal(
        Context context,
        double nextLat,
        double nextLng,
        float nextSpeed,
        float nextBearing
    ) {
        return startInternal(context, nextLat, nextLng, nextSpeed, nextBearing, "phone", true);
    }

    private static boolean startInternal(
        Context context,
        double nextLat,
        double nextLng,
        float nextSpeed,
        float nextBearing,
        String source,
        boolean bumpRevision
    ) {
        if (!Double.isFinite(nextLat) || !Double.isFinite(nextLng)
            || nextLat < -90.0d || nextLat > 90.0d
            || nextLng < -180.0d || nextLng > 180.0d) {
            return false;
        }
        Context targetContext = context.getApplicationContext();
        ensureMetadataLoaded(targetContext);
        appContext = targetContext;
        lat = nextLat;
        lng = nextLng;
        speed = nextSpeed;
        bearing = nextBearing;
        active = true;
        saveState(targetContext, true, source, bumpRevision);

        if (!isMockEnabled(targetContext)) {
            stopLoopLocked();
            clearProviders(targetContext);
            return false;
        }

        boolean accepted = pushCurrent(targetContext);
        if (!accepted) {
            active = false;
            saveState(targetContext, false, source, false);
            stopLoopLocked();
            clearProviders(targetContext);
            return false;
        }

        ensureWorkerLocked();
        workerHandler.removeCallbacks(TICK);
        workerHandler.postDelayed(TICK, KEEPALIVE_MS);
        return true;
    }

    public static synchronized void clearLocal(Context context) {
        clearInternal(context, "phone", true);
    }

    private static void clearInternal(Context context, String source, boolean bumpRevision) {
        Context targetContext = appContext != null ? appContext : context.getApplicationContext();
        ensureMetadataLoaded(targetContext);
        active = false;
        saveState(targetContext, false, source, bumpRevision);
        stopLoopLocked();
        clearProviders(targetContext);
    }

    public static synchronized Snapshot snapshot(Context context) {
        Context targetContext = context.getApplicationContext();
        ensureMetadataLoaded(targetContext);
        return new Snapshot(
            isMockEnabled(targetContext) && active,
            lat,
            lng,
            revision,
            source,
            updatedAtMs
        );
    }

    static synchronized void persistNow(Context context) {
        if (context == null) return;
        Context targetContext = context.getApplicationContext();
        ensureMetadataLoaded(targetContext);
        lastPersistedAtMs = 0L;
        saveState(targetContext, active, source, false);
    }

    private static void saveState(
        Context context,
        boolean isActive,
        String source,
        boolean bumpRevision
    ) {
        ensureMetadataLoaded(context);
        long now = System.currentTimeMillis();
        if (bumpRevision) revision++;
        MockLocationKeeper.source = source == null ? "phone" : source;
        updatedAtMs = now;
        boolean force = !isActive
            || isActive != lastPersistedActive
            || lastPersistedAtMs == 0L
            || now - lastPersistedAtMs >= STATE_PERSIST_INTERVAL_MS;
        if (!force) return;
        SharedPreferences prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        SharedPreferences.Editor editor = prefs.edit()
            .putBoolean(KEY_ACTIVE, isActive)
            .putLong(KEY_LAT, Double.doubleToLongBits(lat))
            .putLong(KEY_LNG, Double.doubleToLongBits(lng))
            .putBoolean(KEY_HAS_SPEED, !Float.isNaN(speed))
            .putBoolean(KEY_HAS_BEARING, !Float.isNaN(bearing))
            .putLong(KEY_REVISION, revision)
            .putString(KEY_SOURCE, MockLocationKeeper.source)
            .putLong(KEY_UPDATED_AT_MS, updatedAtMs);
        if (!Float.isNaN(speed)) editor.putFloat(KEY_SPEED, speed);
        if (!Float.isNaN(bearing)) editor.putFloat(KEY_BEARING, bearing);
        editor.apply();
        lastPersistedAtMs = now;
        lastPersistedActive = isActive;
    }

    private static void ensureMetadataLoaded(Context context) {
        if (metadataLoaded) return;
        SharedPreferences prefs = context.getApplicationContext()
            .getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        active = prefs.getBoolean(KEY_ACTIVE, false);
        lat = Double.longBitsToDouble(
            prefs.getLong(KEY_LAT, Double.doubleToLongBits(0d))
        );
        lng = Double.longBitsToDouble(
            prefs.getLong(KEY_LNG, Double.doubleToLongBits(0d))
        );
        speed = prefs.getBoolean(KEY_HAS_SPEED, false)
            ? prefs.getFloat(KEY_SPEED, 0f)
            : Float.NaN;
        bearing = prefs.getBoolean(KEY_HAS_BEARING, false)
            ? prefs.getFloat(KEY_BEARING, 0f)
            : Float.NaN;
        revision = prefs.getLong(KEY_REVISION, 0L);
        source = prefs.getString(KEY_SOURCE, "phone");
        updatedAtMs = prefs.getLong(KEY_UPDATED_AT_MS, 0L);
        lastPersistedActive = active;
        lastPersistedAtMs = System.currentTimeMillis();
        metadataLoaded = true;
    }

    private static void ensureWorkerLocked() {
        if (workerThread != null && workerThread.isAlive() && workerHandler != null) return;
        workerThread = new HandlerThread("LocWarpMockKeeper", android.os.Process.THREAD_PRIORITY_BACKGROUND);
        workerThread.start();
        workerHandler = new Handler(workerThread.getLooper());
    }

    private static void stopLoopLocked() {
        if (workerHandler != null) {
            workerHandler.removeCallbacksAndMessages(null);
        }
    }

    private static boolean pushCurrent(Context context) {
        LocationManager manager = (LocationManager) context.getSystemService(Context.LOCATION_SERVICE);
        if (manager == null) return false;

        boolean gpsOk = setProvider(manager, LocationManager.GPS_PROVIDER, lat, lng, speed, bearing);
        boolean networkOk = setProvider(manager, LocationManager.NETWORK_PROVIDER, lat, lng, speed, bearing);
        if (!gpsOk && !networkOk) {
            Log.w(TAG, "No Android framework test provider accepted the mock location.");
        }
        return gpsOk || networkOk;
    }

    private static boolean setProvider(LocationManager manager, String provider, double lat, double lng, float speed, float bearing) {
        try {
            ensureProvider(manager, provider);
            Location location = new Location(provider);
            location.setLatitude(lat);
            location.setLongitude(lng);
            location.setAccuracy(3.0f);
            if (!Float.isNaN(speed)) {
                location.setSpeed(Math.max(speed, 0.0f));
            }
            if (!Float.isNaN(bearing)) {
                float normalizedBearing = ((bearing % 360.0f) + 360.0f) % 360.0f;
                location.setBearing(normalizedBearing);
            }
            location.setTime(System.currentTimeMillis());
            location.setElapsedRealtimeNanos(SystemClock.elapsedRealtimeNanos());
            location.setBearingAccuracyDegrees(0.1f);
            location.setSpeedAccuracyMetersPerSecond(0.1f);
            location.setVerticalAccuracyMeters(0.1f);
            manager.setTestProviderEnabled(provider, true);
            manager.setTestProviderLocation(provider, location);
            return true;
        } catch (IllegalArgumentException e) {
            Log.w(TAG, "Provider unavailable for app-side mock location: " + provider, e);
            return false;
        } catch (SecurityException e) {
            Log.w(TAG, "Mock location permission denied for provider: " + provider, e);
            return false;
        }
    }

    @SuppressLint("InlinedApi")
    private static void ensureProvider(LocationManager manager, String provider) {
        try {
            manager.addTestProvider(
                provider,
                false,
                false,
                false,
                false,
                true,
                true,
                true,
                ProviderProperties.POWER_USAGE_LOW,
                ProviderProperties.ACCURACY_FINE
            );
        } catch (IllegalArgumentException alreadyExists) {
            // Provider may already be registered from a previous coordinate.
        }
    }

    private static void clearProviders(Context context) {
        LocationManager manager = (LocationManager) context.getSystemService(Context.LOCATION_SERVICE);
        if (manager == null) return;
        clearProvider(manager, LocationManager.GPS_PROVIDER);
        clearProvider(manager, LocationManager.NETWORK_PROVIDER);
    }

    private static void clearProvider(LocationManager manager, String provider) {
        try {
            manager.removeTestProvider(provider);
        } catch (SecurityException e) {
            Log.w(TAG, "Mock location permission denied while clearing provider: " + provider, e);
        } catch (IllegalArgumentException ignored) {
        }
    }

    public static final class Snapshot {
        public final boolean active;
        public final double lat;
        public final double lng;
        public final long revision;
        public final String source;
        public final long updatedAtMs;

        Snapshot(
            boolean active,
            double lat,
            double lng,
            long revision,
            String source,
            long updatedAtMs
        ) {
            this.active = active;
            this.lat = lat;
            this.lng = lng;
            this.revision = revision;
            this.source = source == null ? "phone" : source;
            this.updatedAtMs = updatedAtMs;
        }
    }
}
