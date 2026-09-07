package com.locwarp.mock;

/** Keeps one scheduled delivery while replacing stale pending values. */
final class LatestValueQueue<T> {
    private T latest;
    private boolean scheduled;

    synchronized boolean offer(T value) {
        latest = value;
        if (scheduled) return false;
        scheduled = true;
        return true;
    }

    synchronized T take() {
        T value = latest;
        latest = null;
        scheduled = false;
        return value;
    }
}
