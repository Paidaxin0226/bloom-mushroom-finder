"use strict";

const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const { once } = require("node:events");
const WebSocket = require("ws");

async function main() {
  const origin = process.argv[2];
  assert.match(origin || "", /^https:\/\/[A-Za-z0-9.-]+$/);

  const health = await fetch(`${origin}/health`, {
    redirect: "error",
    signal: AbortSignal.timeout(10_000)
  });
  assert.equal(health.status, 200);
  assert.deepEqual(await health.json(), { ok: true });

  const socket = new WebSocket(
    `wss://${new URL(origin).host}/ws/host`,
    { handshakeTimeout: 10_000 }
  );
  await once(socket, "open");
  socket.send(JSON.stringify({
    type: "host_init",
    session: crypto.randomBytes(12).toString("base64url"),
    deviceKeyId: crypto.randomBytes(32).toString("base64url")
  }));
  const [code, reason] = await once(socket, "close");
  assert.equal(code, 1008);
  assert.equal(String(reason), "host device not allowed");

  process.stdout.write("Public Relay health and host allowlist verified.\n");
}

main().catch(error => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
