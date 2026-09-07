package com.locwarp.mock;

final class SimulationStateMachine {
    enum Event {
        START,
        PAUSE,
        RESUME,
        STOP,
        FINISH,
        FAIL,
        RESTORE
    }

    static final class State {
        final String name;
        final boolean running;
        final boolean paused;

        State(String name, boolean running, boolean paused) {
            this.name = name;
            this.running = running;
            this.paused = paused;
        }
    }

    private SimulationStateMachine() {}

    static State idle() {
        return new State("idle", false, false);
    }

    static State transition(
        State current,
        Event event,
        String requestedState,
        boolean restoredPaused
    ) {
        State safeCurrent = current == null ? idle() : current;
        switch (event) {
            case START:
                return new State(requireActiveName(requestedState), true, false);
            case PAUSE:
                return safeCurrent.running
                    ? new State(safeCurrent.name, true, true)
                    : safeCurrent;
            case RESUME:
                return safeCurrent.running
                    ? new State(safeCurrent.name, true, false)
                    : safeCurrent;
            case FAIL:
                return new State("provider_error", false, false);
            case RESTORE:
                return new State(
                    requireActiveName(requestedState),
                    true,
                    restoredPaused
                );
            case STOP:
            case FINISH:
            default:
                return idle();
        }
    }

    private static String requireActiveName(String value) {
        String normalized = value == null ? "" : value.trim();
        switch (normalized) {
            case "teleporting":
            case "navigating":
            case "path_navigation":
            case "flower_planting":
            case "looping":
            case "multi_stop":
            case "random_walk":
            case "joystick":
                return normalized;
            default:
                throw new IllegalArgumentException(
                    "A known active movement state is required"
                );
        }
    }
}
