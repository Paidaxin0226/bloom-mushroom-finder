const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const { once } = require("node:events");
const fs = require("node:fs");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const vm = require("node:vm");
const WebSocket = require("ws");

const hostKeyPair = crypto.generateKeyPairSync("ec", {
  namedCurve: "prime256v1"
});
const hostPublicKeyDer = hostKeyPair.publicKey.export({
  format: "der",
  type: "spki"
});
const hostDeviceKeyId = crypto.createHash("sha256")
  .update(hostPublicKeyDer)
  .digest("base64url");

process.env.TRUST_PROXY = "true";
process.env.HOST_DEVICE_PUBLIC_KEYS =
  `${hostDeviceKeyId}:${hostPublicKeyDer.toString("base64url")}`;

const {
  createPendingRequest,
  sameSecret,
  sanitizeResponseHeaders,
  startServer
} = require("../src/index.js");

function jsonInbox(ws) {
  const queued = [];
  const waiting = [];
  ws.on("message", data => {
    const message = JSON.parse(String(data));
    const resolve = waiting.shift();
    if (resolve) resolve(message);
    else queued.push(message);
  });
  return () => queued.length > 0
    ? Promise.resolve(queued.shift())
    : new Promise(resolve => waiting.push(resolve));
}

function verifier(secret) {
  return crypto.createHash("sha256")
    .update(`locwarp-viewer-verifier-v2|${secret}`)
    .digest("base64url");
}

function opaqueEnvelope(fill = "A") {
  return `v2.${fill.repeat(16)}.${fill.repeat(32)}`;
}

function hostAdmissionPayload(
  session,
  challenge,
  issuedAtMs,
  token,
  viewerVerifier
) {
  return [
    "locwarp-host-admission-v1",
    session,
    hostDeviceKeyId,
    challenge,
    String(issuedAtMs),
    token,
    viewerVerifier
  ].join("|");
}

function hostAuthMessage(
  session,
  token,
  viewerVerifier,
  challenge,
  privateKey = hostKeyPair.privateKey
) {
  const payload = hostAdmissionPayload(
    session,
    challenge.challenge,
    challenge.issuedAtMs,
    token,
    viewerVerifier
  );
  return {
    type: "auth",
    session,
    token,
    viewerVerifier,
    deviceKeyId: hostDeviceKeyId,
    challenge: challenge.challenge,
    issuedAtMs: challenge.issuedAtMs,
    signature: crypto.sign(
      "sha256",
      Buffer.from(payload, "utf8"),
      privateKey
    ).toString("base64url")
  };
}

async function beginHostAuthentication(ws, nextMessage, session) {
  ws.send(JSON.stringify({
    type: "host_init",
    session,
    deviceKeyId: hostDeviceKeyId
  }));
  const challenge = await nextMessage();
  assert.equal(challenge.type, "host_challenge");
  assert.match(challenge.challenge, /^[A-Za-z0-9_-]{43}$/);
  return challenge;
}

test("same browser command id is namespaced by a unique relay id", () => {
  const first = createPendingRequest({ viewerId: "viewer-a" }, { id: "1" });
  const second = createPendingRequest({ viewerId: "viewer-b" }, { id: "1" });

  assert.notEqual(first.id, second.id);
  assert.equal(first.pending.clientId, "1");
  assert.equal(second.pending.clientId, "1");
  assert.equal(first.pending.viewerId, "viewer-a");
  assert.equal(second.pending.viewerId, "viewer-b");
});

test("host HTTP response headers are allowlisted and newline-safe", () => {
  assert.deepEqual(sanitizeResponseHeaders({
    "content-type": "text/html; charset=utf-8",
    "content-security-policy": "default-src 'none'",
    "cache-control": "no-store",
    "set-cookie": "admin=true",
    "content-length": "999",
    location: "/next\r\nx-injected: true"
  }), {
    "content-type": "text/html; charset=utf-8",
    "content-security-policy": "default-src 'none'",
    "cache-control": "no-store"
  });
});

test("relay secrets use exact constant-time comparison semantics", () => {
  assert.equal(sameSecret("abcdefgh1234", "abcdefgh1234"), true);
  assert.equal(sameSecret("abcdefgh1234", "abcdefgh1235"), false);
  assert.equal(sameSecret("short", "longer"), false);
});

test("relay fails closed when no host device is allowlisted", () => {
  const environment = { ...process.env };
  delete environment.HOST_DEVICE_PUBLIC_KEYS;
  const child = spawnSync(
    process.execPath,
    ["-e", "require('./src/index.js')"],
    {
      cwd: path.join(__dirname, ".."),
      env: environment,
      encoding: "utf8"
    }
  );
  assert.notEqual(child.status, 0);
  assert.match(child.stderr, /HOST_DEVICE_PUBLIC_KEYS/);
});

test("phone-owned control page parses and never accepts query-string secrets", () => {
  const html = fs.readFileSync(
    path.join(
      __dirname,
      "..",
      "..",
      "app",
      "src",
      "main",
      "assets",
      "relay",
      "control-page.html"
    ),
    "utf8"
  );
  const scripts = Array.from(
    html.matchAll(/<script nonce="\{\{NONCE\}\}">([\s\S]*?)<\/script>/g),
    match => match[1]
  );
  const applicationScript = scripts.find(script =>
    script.includes("const SESSION=")
  );
  assert.ok(applicationScript);
  assert.doesNotMatch(applicationScript, /searchParams\.get\(['"]token['"]\)/);
  assert.match(applicationScript, /history\.replaceState/);
  assert.match(
    applicationScript,
    /stored\('locwarp-requested-role','readonly'\)/
  );
  new vm.Script(applicationScript);
});

test("viewer approval gates a complete host command round trip", async t => {
  const httpServer = startServer(0);
  await once(httpServer, "listening");
  const address = httpServer.address();
  const base = `ws://127.0.0.1:${address.port}`;
  const httpBase = `http://127.0.0.1:${address.port}`;
  const origin = `http://127.0.0.1:${address.port}`;
  const session = "session-e2e-001";
  const hostToken = "host-token-e2e-001-secret";
  const viewerToken = "viewer-token-e2e-001-secret";
  const viewerVerifier = verifier(viewerToken);
  const e2eNonce = "viewer-nonce-e2e-001";
  const e2eProof = verifier("proof-e2e-001");
  const grant = opaqueEnvelope("G");
  const commandEnvelope = opaqueEnvelope("C");
  const resultEnvelope = opaqueEnvelope("R");
  const readonlyGrant = opaqueEnvelope("L");
  let host;
  let viewer;
  let attacker;
  let unauthorized;
  let forged;
  let replay;
  t.after(() => {
    if (host) host.terminate();
    if (viewer) viewer.terminate();
    if (attacker) attacker.terminate();
    if (unauthorized) unauthorized.terminate();
    if (forged) forged.terminate();
    if (replay) replay.terminate();
    return new Promise(resolve => httpServer.close(resolve));
  });

  unauthorized = new WebSocket(`${base}/ws/host`);
  await once(unauthorized, "open");
  unauthorized.send(JSON.stringify({
    type: "host_init",
    session,
    deviceKeyId: "A".repeat(43)
  }));
  const [unauthorizedCloseCode] = await once(unauthorized, "close");
  assert.equal(unauthorizedCloseCode, 1008);

  forged = new WebSocket(`${base}/ws/host`);
  const nextForged = jsonInbox(forged);
  await once(forged, "open");
  const forgedChallenge = await beginHostAuthentication(
    forged,
    nextForged,
    session
  );
  const forgedKeyPair = crypto.generateKeyPairSync("ec", {
    namedCurve: "prime256v1"
  });
  forged.send(JSON.stringify(hostAuthMessage(
    session,
    hostToken,
    viewerVerifier,
    forgedChallenge,
    forgedKeyPair.privateKey
  )));
  const [forgedCloseCode] = await once(forged, "close");
  assert.equal(forgedCloseCode, 1008);

  host = new WebSocket(`${base}/ws/host`);
  const nextHost = jsonInbox(host);
  await once(host, "open");
  const hostChallenge = await beginHostAuthentication(
    host,
    nextHost,
    session
  );
  const signedHostAuth = hostAuthMessage(
    session,
    hostToken,
    viewerVerifier,
    hostChallenge
  );
  host.send(JSON.stringify(signedHostAuth));
  assert.deepEqual(await nextHost(), {
    type: "hello",
    role: "host",
    session
  });
  assert.deepEqual(await nextHost(), { type: "viewer_count", count: 0 });
  assert.deepEqual(await nextHost(), { type: "viewer_list", viewers: [] });

  replay = new WebSocket(`${base}/ws/host`);
  const nextReplay = jsonInbox(replay);
  await once(replay, "open");
  await beginHostAuthentication(replay, nextReplay, session);
  replay.send(JSON.stringify(signedHostAuth));
  const [replayCloseCode] = await once(replay, "close");
  assert.equal(replayCloseCode, 1008);

  const pagePromise = fetch(`${httpBase}/r/${session}/control?lang=zh-Hant`);
  const httpRequest = await nextHost();
  assert.equal(httpRequest.type, "http_request");
  assert.equal(httpRequest.method, "GET");
  assert.equal(httpRequest.path, `/r/${session}/control?lang=zh-Hant`);
  assert.equal(httpRequest.bodyBase64, "");
  host.send(JSON.stringify({
    type: "http_response",
    id: httpRequest.id,
    status: 200,
    headers: {
      "content-type": "text/html; charset=utf-8",
      "content-security-policy": "default-src 'none'",
      "set-cookie": "must-not-pass=true"
    },
    body: "<!doctype html><title>Phone-owned page</title>"
  }));
  const pageResponse = await pagePromise;
  assert.equal(pageResponse.status, 200);
  assert.equal(pageResponse.headers.get("content-type"), "text/html; charset=utf-8");
  assert.equal(pageResponse.headers.get("content-security-policy"), "default-src 'none'");
  assert.equal(pageResponse.headers.get("set-cookie"), null);
  assert.equal(
    pageResponse.headers.get("strict-transport-security"),
    "max-age=31536000"
  );
  assert.equal(await pageResponse.text(), "<!doctype html><title>Phone-owned page</title>");

  const redirectResponse = await fetch(`${httpBase}/health?probe=1`, {
    redirect: "manual",
    headers: {
      "cf-visitor": JSON.stringify({ scheme: "http" })
    }
  });
  assert.equal(redirectResponse.status, 308);
  assert.equal(
    redirectResponse.headers.get("location"),
    `https://127.0.0.1:${address.port}/health?probe=1`
  );

  attacker = new WebSocket(`${base}/ws/host`);
  const nextAttacker = jsonInbox(attacker);
  await once(attacker, "open");
  const attackerChallenge = await beginHostAuthentication(
    attacker,
    nextAttacker,
    session
  );
  attacker.send(JSON.stringify(hostAuthMessage(
    session,
    viewerVerifier,
    viewerVerifier,
    attackerChallenge
  )));
  const [attackerCloseCode] = await once(attacker, "close");
  assert.equal(attackerCloseCode, 1008);

  viewer = new WebSocket(`${base}/ws/viewer`, { origin });
  const nextViewer = jsonInbox(viewer);
  await once(viewer, "open");
  viewer.send(JSON.stringify({
    type: "auth",
    session,
    token: viewerVerifier,
    name: "QA browser",
    deviceId: "device-e2e-001",
    requestedRole: "controller",
    e2eNonce,
    e2eProof
  }));
  const approvalPending = await nextViewer();
  const viewerRequest = await nextHost();
  assert.equal(approvalPending.type, "approval_pending");
  assert.equal(viewerRequest.type, "viewer_request");
  assert.equal(viewerRequest.viewerId, approvalPending.viewerId);
  assert.equal(viewerRequest.name, "QA browser");
  assert.equal(viewerRequest.deviceId, "device-e2e-001");
  assert.equal(viewerRequest.requestedRole, "controller");
  assert.equal(viewerRequest.e2eNonce, e2eNonce);
  assert.equal(viewerRequest.e2eProof, e2eProof);

  host.send(JSON.stringify({
    type: "viewer_decision",
    viewerId: viewerRequest.viewerId,
    approved: true,
    accessRole: "controller",
    grant
  }));
  assert.deepEqual(await nextViewer(), {
    type: "hello",
    role: "viewer",
    hostOnline: true,
    accessRole: "controller",
    viewerId: viewerRequest.viewerId,
    deviceId: "device-e2e-001",
    e2eNonce,
    grant
  });
  assert.deepEqual(await nextHost(), { type: "viewer_count", count: 1 });
  const viewerList = await nextHost();
  assert.equal(viewerList.type, "viewer_list");
  assert.deepEqual(viewerList.viewers.map(item => ({
    id: item.id,
    name: item.name,
    deviceId: item.deviceId,
    e2eNonce: item.e2eNonce,
    accessRole: item.accessRole
  })), [{
    id: viewerRequest.viewerId,
    name: "QA browser",
    deviceId: "device-e2e-001",
    e2eNonce,
    accessRole: "controller"
  }]);

  viewer.send(JSON.stringify({ type: "status" }));
  assert.deepEqual(await nextHost(), {
    type: "status_request",
    viewerId: viewerRequest.viewerId
  });

  viewer.send(JSON.stringify({
    type: "command",
    id: "browser-42",
    envelope: commandEnvelope
  }));
  const hostCommand = await nextHost();
  assert.equal(hostCommand.type, "command");
  assert.equal(hostCommand.envelope, commandEnvelope);
  assert.equal(hostCommand.command, undefined);
  assert.equal(hostCommand.payload, undefined);
  assert.notEqual(hostCommand.id, "browser-42");
  assert.equal(hostCommand.viewer.name, "QA browser");
  assert.equal(hostCommand.viewer.accessRole, "controller");

  host.send(JSON.stringify({
    type: "result",
    id: hostCommand.id,
    envelope: resultEnvelope
  }));
  assert.deepEqual(await nextViewer(), {
    type: "result",
    id: "browser-42",
    envelope: resultEnvelope
  });

  host.send(JSON.stringify({
    type: "viewer_role",
    viewerId: viewerRequest.viewerId,
    accessRole: "readonly",
    grant: readonlyGrant
  }));
  assert.deepEqual(await nextViewer(), {
    type: "access",
    accessRole: "readonly",
    grant: readonlyGrant
  });
  assert.deepEqual(await nextHost(), { type: "viewer_count", count: 1 });
  const readonlyList = await nextHost();
  assert.equal(readonlyList.viewers[0].accessRole, "readonly");

  viewer.send(JSON.stringify({
    type: "command",
    id: "browser-readonly",
    envelope: commandEnvelope
  }));
  assert.deepEqual(await nextViewer(), {
    type: "relay_error",
    id: "browser-readonly",
    message: "This viewer is read-only"
  });

  host.send(JSON.stringify({
    type: "viewer_disconnect",
    viewerId: viewerRequest.viewerId
  }));
  const [viewerCloseCode] = await once(viewer, "close");
  assert.equal(viewerCloseCode, 1008);
});
