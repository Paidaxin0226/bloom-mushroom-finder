package com.locwarp.mock;

import org.junit.Test;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

public final class LocalSimulationEngineTest {
    @Test
    public void movingNorthPreservesLongitudeAndDistance() {
        double startLat = 25.033964d;
        double startLng = 121.564468d;
        double[] moved = GeoMath.move(startLat, startLng, 0d, 100d);

        assertEquals(startLng, moved[1], 0.000001d);
        assertEquals(100d, GeoMath.distanceMeters(startLat, startLng, moved[0], moved[1]), 0.05d);
        assertTrue(moved[0] > startLat);
    }

    @Test
    public void movingAcrossDateLineNormalizesLongitude() {
        double[] moved = GeoMath.move(0d, 179.9999d, 90d, 1000d);

        assertTrue(moved[1] >= -180d && moved[1] <= 180d);
        assertTrue(moved[1] < 0d);
    }

    @Test
    public void knownLatitudeDeltaHasExpectedDistance() {
        double distance = GeoMath.distanceMeters(0d, 0d, 1d, 0d);
        assertEquals(111_195d, distance, 2d);
    }
}
