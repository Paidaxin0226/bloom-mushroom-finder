package com.locwarp.mock;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public final class LatestValueQueueTest {
    @Test
    public void replacesPendingValueWithoutSchedulingAnotherDelivery() {
        LatestValueQueue<String> queue = new LatestValueQueue<>();

        assertTrue(queue.offer("first"));
        assertFalse(queue.offer("latest"));
        assertEquals("latest", queue.take());
        assertNull(queue.take());
    }

    @Test
    public void schedulesAgainAfterPendingValueIsTaken() {
        LatestValueQueue<Integer> queue = new LatestValueQueue<>();

        assertTrue(queue.offer(1));
        assertEquals(Integer.valueOf(1), queue.take());
        assertTrue(queue.offer(2));
        assertEquals(Integer.valueOf(2), queue.take());
    }
}
