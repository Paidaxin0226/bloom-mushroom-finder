package com.locwarp.mock;

import org.junit.Test;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertSame;
import static org.junit.Assert.assertTrue;

public final class SimulationStateMachineTest {
    @Test
    public void activeMovementCanPauseResumeAndFinish() {
        SimulationStateMachine.State state = SimulationStateMachine.transition(
            SimulationStateMachine.idle(),
            SimulationStateMachine.Event.START,
            "navigating",
            false
        );
        assertEquals("navigating", state.name);
        assertTrue(state.running);
        assertFalse(state.paused);

        state = SimulationStateMachine.transition(
            state,
            SimulationStateMachine.Event.PAUSE,
            null,
            false
        );
        assertTrue(state.paused);

        state = SimulationStateMachine.transition(
            state,
            SimulationStateMachine.Event.RESUME,
            null,
            false
        );
        assertFalse(state.paused);

        state = SimulationStateMachine.transition(
            state,
            SimulationStateMachine.Event.FINISH,
            null,
            false
        );
        assertEquals("idle", state.name);
        assertFalse(state.running);
        assertFalse(state.paused);
    }

    @Test
    public void pauseOnIdleIsANoOpAndFailureIsTerminal() {
        SimulationStateMachine.State idle = SimulationStateMachine.idle();
        assertSame(
            idle,
            SimulationStateMachine.transition(
                idle,
                SimulationStateMachine.Event.PAUSE,
                null,
                false
            )
        );

        SimulationStateMachine.State failed = SimulationStateMachine.transition(
            idle,
            SimulationStateMachine.Event.FAIL,
            null,
            false
        );
        assertEquals("provider_error", failed.name);
        assertFalse(failed.running);
        assertFalse(failed.paused);
    }

    @Test
    public void restoredMovementPreservesPausedState() {
        SimulationStateMachine.State restored = SimulationStateMachine.transition(
            SimulationStateMachine.idle(),
            SimulationStateMachine.Event.RESTORE,
            "multi_stop",
            true
        );
        assertEquals("multi_stop", restored.name);
        assertTrue(restored.running);
        assertTrue(restored.paused);
    }

    @Test(expected = IllegalArgumentException.class)
    public void idleCannotBeRestoredAsAnActiveMovement() {
        SimulationStateMachine.transition(
            SimulationStateMachine.idle(),
            SimulationStateMachine.Event.RESTORE,
            "idle",
            false
        );
    }

    @Test(expected = IllegalArgumentException.class)
    public void unknownMovementCannotBeRestored() {
        SimulationStateMachine.transition(
            SimulationStateMachine.idle(),
            SimulationStateMachine.Event.RESTORE,
            "corrupted",
            false
        );
    }
}
