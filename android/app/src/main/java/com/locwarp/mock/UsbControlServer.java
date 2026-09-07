package com.locwarp.mock;

import android.content.Context;
import android.net.Credentials;
import android.net.LocalServerSocket;
import android.net.LocalSocket;
import android.os.Process;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.Closeable;
import java.io.EOFException;
import java.io.InputStream;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

final class UsbControlServer implements Closeable {
    static final String SOCKET_NAME = "bloompilot_control_v1";

    private final RemoteCommandExecutor commandExecutor;
    private final ExecutorService clients = Executors.newFixedThreadPool(4, runnable -> {
        Thread thread = new Thread(runnable, "BloomPilotUsbClient");
        thread.setDaemon(true);
        return thread;
    });

    private volatile boolean closed;
    private LocalServerSocket serverSocket;
    private Thread acceptThread;

    UsbControlServer(Context context) {
        commandExecutor = new RemoteCommandExecutor(context.getApplicationContext());
    }

    synchronized void start() throws Exception {
        if (closed) throw new IllegalStateException("USB control server is closed");
        if (acceptThread != null) return;
        serverSocket = new LocalServerSocket(SOCKET_NAME);
        acceptThread = new Thread(this::acceptLoop, "BloomPilotUsbAccept");
        acceptThread.setDaemon(true);
        acceptThread.start();
    }

    private void acceptLoop() {
        while (!closed) {
            LocalSocket socket = null;
            try {
                LocalServerSocket server = serverSocket;
                if (server == null) return;
                socket = server.accept();
                LocalSocket accepted = socket;
                clients.execute(() -> handleClient(accepted));
            } catch (Exception error) {
                closeQuietly(socket);
                if (!closed) UsbControlService.reportServerError(error);
            }
        }
    }

    private void handleClient(LocalSocket socket) {
        try (LocalSocket client = socket) {
            Credentials peer = client.getPeerCredentials();
            int peerUid = peer == null ? -1 : peer.getUid();
            if (peerUid != Process.SHELL_UID && peerUid != Process.ROOT_UID) return;
            // Recognition sends a heartbeat every 15 seconds. A finite idle
            // timeout releases ADB-forward connections left behind by an
            // interrupted desktop process instead of exhausting all handlers.
            client.setSoTimeout(UsbControlProtocol.CLIENT_IDLE_TIMEOUT_MS);
            InputStream input = client.getInputStream();
            OutputStream output = client.getOutputStream();
            if (!pair(input, output)) return;
            while (!closed) {
                String line = readLine(input);
                if (line == null) return;
                JSONObject request;
                try {
                    request = new JSONObject(line);
                } catch (Exception error) {
                    write(output, UsbControlProtocol.result("invalid", false, "Invalid JSON"));
                    continue;
                }
                String id = request.optString("id", "invalid");
                if (request.optInt("v", 0) != UsbControlProtocol.VERSION
                    || !"command".equals(request.optString("type", ""))
                    || !UsbControlProtocol.validRequestId(id)) {
                    write(output, UsbControlProtocol.result(id, false, "Invalid command envelope"));
                    continue;
                }
                String command = request.optString("command", "").trim();
                if (command.isEmpty() || command.length() > 80) {
                    write(output, UsbControlProtocol.result(id, false, "Invalid command"));
                    continue;
                }
                JSONObject payload = request.optJSONObject("payload");
                write(output, execute(id, command, payload == null ? new JSONObject() : payload));
            }
        } catch (Exception ignored) {
        }
    }

    private boolean pair(InputStream input, OutputStream output) throws Exception {
        for (int attempt = 0; attempt < UsbControlProtocol.MAX_HANDSHAKE_ATTEMPTS; attempt++) {
            String line = readLine(input);
            if (line == null) return false;
            JSONObject request;
            try {
                request = new JSONObject(line);
            } catch (Exception error) {
                write(output, UsbControlProtocol.result("invalid", false, "Invalid JSON"));
                continue;
            }
            String id = request.optString("id", "invalid");
            boolean valid = request.optInt("v", 0) == UsbControlProtocol.VERSION
                && "pair".equals(request.optString("type", ""))
                && UsbControlProtocol.validRequestId(id);
            if (!valid) {
                write(output, UsbControlProtocol.result(id, false, "Invalid USB handshake"));
                continue;
            }
            JSONObject response = UsbControlProtocol.result(id, true, "ADB USB control connected");
            try {
                response.put("status", commandExecutor.status());
            } catch (Exception ignored) {
            }
            write(output, response);
            return true;
        }
        return false;
    }

    private JSONObject execute(String id, String command, JSONObject payload) {
        CountDownLatch latch = new CountDownLatch(1);
        AtomicReference<JSONObject> result = new AtomicReference<>();
        try {
            commandExecutor.execute(command, payload, value -> {
                result.set(value);
                latch.countDown();
            });
            if (!latch.await(UsbControlProtocol.COMMAND_TIMEOUT_MS, TimeUnit.MILLISECONDS)) {
                return UsbControlProtocol.result(id, false, "Command timed out");
            }
            return UsbControlProtocol.wrapExecutorResult(id, result.get());
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
            return UsbControlProtocol.result(id, false, "Command interrupted");
        } catch (Exception error) {
            String message = error.getMessage();
            return UsbControlProtocol.result(
                id,
                false,
                message == null ? error.getClass().getSimpleName() : message
            );
        }
    }

    private String readLine(InputStream input) throws Exception {
        ByteArrayOutputStream buffer = new ByteArrayOutputStream();
        while (buffer.size() <= UsbControlProtocol.MAX_MESSAGE_BYTES) {
            int value = input.read();
            if (value < 0) {
                if (buffer.size() == 0) return null;
                throw new EOFException("Truncated USB control message");
            }
            if (value == '\n') {
                return buffer.toString(StandardCharsets.UTF_8.name());
            }
            if (value != '\r') buffer.write(value);
        }
        throw new IllegalArgumentException("USB control message is too large");
    }

    private void write(OutputStream output, JSONObject value) throws Exception {
        byte[] encoded = (value.toString() + "\n").getBytes(StandardCharsets.UTF_8);
        if (encoded.length > UsbControlProtocol.MAX_MESSAGE_BYTES) {
            encoded = (UsbControlProtocol.result("invalid", false, "Response is too large")
                .toString() + "\n").getBytes(StandardCharsets.UTF_8);
        }
        output.write(encoded);
        output.flush();
    }

    @Override
    public synchronized void close() {
        if (closed) return;
        closed = true;
        closeQuietly(serverSocket);
        serverSocket = null;
        clients.shutdownNow();
        Thread thread = acceptThread;
        acceptThread = null;
        if (thread != null) thread.interrupt();
    }

    private static void closeQuietly(Closeable value) {
        if (value == null) return;
        try {
            value.close();
        } catch (Exception ignored) {
        }
    }
}
