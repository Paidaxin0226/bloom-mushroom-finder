package com.locwarp.mock;

import java.util.concurrent.ThreadLocalRandom;

final class StepSyncSchedule {
    static final long MIN_INTERVAL_MS = 1000L;
    static final long MAX_INTERVAL_MS = 10_000L;

    private StepSyncSchedule() {}

    static long nextIntervalMillis() {
        return ThreadLocalRandom.current().nextInt(1, 11) * 1000L;
    }

    static int stepsForDuration(int stepsPerMinute, long durationMillis) {
        return Math.max(
            1,
            (int) Math.round(stepsPerMinute * Math.max(1000L, durationMillis) / 60000d)
        );
    }
}
