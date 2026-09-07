package com.locwarp.mock;

import static org.junit.Assert.assertEquals;

import org.junit.Test;

import java.util.Random;

public final class StepInjectionPolicyTest {
    @Test
    public void oneRealStepAddsOnlyTheMissingMultiplierSteps() {
        assertEquals(0, StepInjectionPolicy.extraStepsForRealDelta(1, 1));
        assertEquals(2, StepInjectionPolicy.extraStepsForRealDelta(1, 3));
        assertEquals(8, StepInjectionPolicy.extraStepsForRealDelta(2, 5));
    }

    @Test
    public void walkingMultiplierIsBounded() {
        assertEquals(1, StepInjectionPolicy.clampWalkingMultiplier(-5));
        assertEquals(100, StepInjectionPolicy.clampWalkingMultiplier(1000));
    }

    @Test
    public void randomLoopAlwaysStaysInsideFixedRange() {
        Random random = new Random(42L);
        for (int index = 0; index < 10_000; index++) {
            long interval = StepInjectionPolicy.randomStepIntervalMillis(random);
            org.junit.Assert.assertTrue(interval >= 200L);
            org.junit.Assert.assertTrue(interval <= 600L);
        }
    }

    @Test
    public void randomBatchAlwaysContainsOneToThreeSteps() {
        Random random = new Random(84L);
        for (int index = 0; index < 10_000; index++) {
            int steps = StepInjectionPolicy.randomBatchSteps(random);
            org.junit.Assert.assertTrue(steps >= 1);
            org.junit.Assert.assertTrue(steps <= 3);
        }
    }
}
