package com.locwarp.mock;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public final class StepSyncScheduleTest {
    @Test
    public void nextIntervalIsAnInclusiveWholeSecondFromOneToTen() {
        for (int index = 0; index < 1000; index++) {
            long interval = StepSyncSchedule.nextIntervalMillis();
            assertTrue(interval >= StepSyncSchedule.MIN_INTERVAL_MS);
            assertTrue(interval <= StepSyncSchedule.MAX_INTERVAL_MS);
            assertEquals(0L, interval % 1000L);
        }
    }

    @Test
    public void stepsForDurationUsesCadenceAndAlwaysProducesWritableRecord() {
        assertEquals(2, StepSyncSchedule.stepsForDuration(120, 1000L));
        assertEquals(20, StepSyncSchedule.stepsForDuration(120, 10_000L));
        assertEquals(1, StepSyncSchedule.stepsForDuration(1, 1000L));
    }
}
