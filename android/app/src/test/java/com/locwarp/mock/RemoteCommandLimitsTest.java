package com.locwarp.mock;

import org.junit.Test;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

public final class RemoteCommandLimitsTest {
    @Test
    public void speedAndDurationsAreBounded() {
        assertEquals(
            200d,
            RemoteCommandLimits.boundedPositive(999d, 10d, 200d),
            0d
        );
        assertEquals(
            10d,
            RemoteCommandLimits.boundedPositive(Double.NaN, 10d, 200d),
            0d
        );
        assertEquals(
            86_400,
            RemoteCommandLimits.boundedNonNegative(999_999, 86_400)
        );
        assertEquals(0, RemoteCommandLimits.boundedNonNegative(-5, 86_400));
        assertNull(RemoteCommandLimits.optionalPositive(-1d, 200d));
    }

    @Test
    public void namesAndCoordinatesAreSanitized() {
        assertEquals("fallback", RemoteCommandLimits.boundedName("  ", "fallback"));
        assertEquals(
            100,
            RemoteCommandLimits.boundedName("x".repeat(120), "fallback").length()
        );
        assertTrue(RemoteCommandLimits.validCoordinate(90d, -180d));
        assertFalse(RemoteCommandLimits.validCoordinate(90.01d, 0d));
        assertFalse(RemoteCommandLimits.validCoordinate(0d, Double.NaN));
        assertEquals(1, RemoteCommandLimits.requireStepCount(1));
        assertEquals(200_000, RemoteCommandLimits.requireStepCount(200_000));
        assertEquals(
            "台北 101",
            RemoteCommandLimits.requireSearchQuery("  台北 101  ")
        );
    }

    @Test(expected = IllegalArgumentException.class)
    public void oversizedRoutesAreRejected() {
        RemoteCommandLimits.requireRouteSize(501);
    }

    @Test(expected = IllegalArgumentException.class)
    public void invalidStepCountsAreRejected() {
        RemoteCommandLimits.requireStepCount(200_001);
    }

    @Test(expected = IllegalArgumentException.class)
    public void oversizedSearchQueryIsRejected() {
        RemoteCommandLimits.requireSearchQuery("x".repeat(201));
    }

}
