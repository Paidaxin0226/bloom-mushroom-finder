package com.locwarp.mock;

import android.os.Handler;

import java.util.concurrent.Callable;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.Consumer;

final class AsyncTaskGate implements AutoCloseable {
    interface CancelableTask<T> extends Callable<T> {
        void cancel();
    }

    private final AtomicLong generation = new AtomicLong();
    private final ExecutorService executor;
    private Future<?> current;
    private CancelableTask<?> currentCancelable;

    AsyncTaskGate(String threadName) {
        ThreadFactory factory = runnable -> {
            Thread thread = new Thread(runnable, threadName);
            thread.setDaemon(true);
            return thread;
        };
        executor = Executors.newFixedThreadPool(2, factory);
    }

    synchronized <T> void submit(
        Callable<T> task,
        Handler callbackHandler,
        Consumer<T> onSuccess,
        Consumer<Throwable> onFailure
    ) {
        cancelLocked();
        long token = generation.incrementAndGet();
        currentCancelable = task instanceof CancelableTask
            ? (CancelableTask<?>) task
            : null;
        current = executor.submit(() -> {
            try {
                T result = task.call();
                callbackHandler.post(() -> {
                    if (generation.get() == token) onSuccess.accept(result);
                });
            } catch (Throwable error) {
                callbackHandler.post(() -> {
                    if (generation.get() == token) onFailure.accept(error);
                });
            }
        });
    }

    synchronized void cancel() {
        generation.incrementAndGet();
        cancelLocked();
    }

    private void cancelLocked() {
        if (currentCancelable != null) {
            currentCancelable.cancel();
            currentCancelable = null;
        }
        if (current != null) {
            current.cancel(true);
            current = null;
        }
    }

    @Override
    public synchronized void close() {
        cancel();
        executor.shutdownNow();
    }
}
