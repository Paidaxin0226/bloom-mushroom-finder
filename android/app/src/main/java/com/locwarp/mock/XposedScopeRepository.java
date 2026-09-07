package com.locwarp.mock;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;

import io.github.libxposed.service.XposedService;
import io.github.libxposed.service.XposedServiceHelper;

final class XposedScopeRepository {
    private static final Object LOCK = new Object();
    private static final CopyOnWriteArrayList<Runnable> listeners =
        new CopyOnWriteArrayList<>();

    private static volatile boolean initialized;
    private static volatile XposedService service;
    private static volatile Throwable initializationError;

    private XposedScopeRepository() {}

    static void initialize() {
        if (initialized) return;
        synchronized (LOCK) {
            if (initialized) return;
            initialized = true;
            try {
                XposedServiceHelper.registerListener(
                    new XposedServiceHelper.OnServiceListener() {
                        @Override
                        public void onServiceBind(XposedService boundService) {
                            service = boundService;
                            initializationError = null;
                            notifyListeners();
                        }

                        @Override
                        public void onServiceDied(XposedService deadService) {
                            if (service == deadService) service = null;
                            notifyListeners();
                        }
                    }
                );
            } catch (Throwable error) {
                initializationError = error;
            }
        }
    }

    static List<String> getScope() {
        XposedService current = service;
        if (current == null) return Collections.emptyList();
        return new ArrayList<>(current.getScope());
    }

    static boolean isServiceConnected() {
        return service != null;
    }

    static String frameworkLabel() {
        XposedService current = service;
        if (current == null) return "";
        try {
            return current.getFrameworkName() + " " + current.getFrameworkVersion();
        } catch (Throwable ignored) {
            return "LSPosed service";
        }
    }

    static Throwable initializationError() {
        return initializationError;
    }

    static void addListener(Runnable listener) {
        listeners.addIfAbsent(listener);
    }

    static void removeListener(Runnable listener) {
        listeners.remove(listener);
    }

    private static void notifyListeners() {
        for (Runnable listener : listeners) {
            try {
                listener.run();
            } catch (Throwable ignored) {
            }
        }
    }
}
