package com.locwarp.mock;

import java.util.Random;

final class StepInjectionPolicy {
    static final long MIN_STEP_INTERVAL_MS = 200L;
    static final long MAX_RANDOM_STEP_INTERVAL_MS = 600L;
    static final int MIN_RANDOM_BATCH_STEPS = 1;
    static final int MAX_RANDOM_BATCH_STEPS = 3;
    static final int MIN_WALKING_MULTIPLIER = 1;
    static final int MAX_WALKING_MULTIPLIER = 100;

    private StepInjectionPolicy() {}

    static int clampWalkingMultiplier(int multiplier) {
        return Math.max(
            MIN_WALKING_MULTIPLIER,
            Math.min(MAX_WALKING_MULTIPLIER, multiplier)
        );
    }

    static int extraStepsForRealDelta(int realStepDelta, int multiplier) {
        if (realStepDelta <= 0) return 0;
        long extra = (long) realStepDelta
            * (clampWalkingMultiplier(multiplier) - 1L);
        return (int) Math.min(Integer.MAX_VALUE, extra);
    }

    static long randomStepIntervalMillis(Random random) {
        if (random == null) return MIN_STEP_INTERVAL_MS;
        int range = (int) (
            MAX_RANDOM_STEP_INTERVAL_MS - MIN_STEP_INTERVAL_MS + 1L
        );
        return MIN_STEP_INTERVAL_MS + random.nextInt(range);
    }

    static int randomBatchSteps(Random random) {
        if (random == null) return MIN_RANDOM_BATCH_STEPS;
        return MIN_RANDOM_BATCH_STEPS + random.nextInt(
            MAX_RANDOM_BATCH_STEPS - MIN_RANDOM_BATCH_STEPS + 1
        );
    }
}
