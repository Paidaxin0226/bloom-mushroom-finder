const http = require("node:http");
const crypto = require("node:crypto");
const { WebSocketServer } = require("ws");

const port = Number(process.env.PORT || 8787);
const defaultSessionTtlMs = 6 * 60 * 60 * 1000;
const configuredSessionTtlMs = Number(
  process.env.SESSION_TTL_MS || defaultSessionTtlMs
);
const sessionTtlMs = Math.max(
  60_000,
  Math.min(
    7 * 24 * 60 * 60 * 1000,
    Number.isFinite(configuredSessionTtlMs)
      ? configuredSessionTtlMs
      : defaultSessionTtlMs
  )
);
const maxSessions = Math.max(
  1,
  Math.min(100_000, Number(process.env.MAX_SESSIONS) || 1_000)
);
const maxSessionsPerHostDevice = Math.max(
  1,
  Math.min(
    100,
    Number(process.env.MAX_SESSIONS_PER_HOST_DEVICE) || 3
  )
);
const maxHttpRequestBytes = Math.max(
  1_024,
  Math.min(
    256 * 1_024,
    Number(process.env.MAX_HTTP_REQUEST_BYTES) || 64 * 1_024
  )
);
const maxHttpResponseBytes = Math.max(
  8 * 1_024,
  Math.min(
    1024 * 1_024,
    Number(process.env.MAX_HTTP_RESPONSE_BYTES) || 256 * 1_024
  )
);
const maxPendingHttp = Math.max(
  1,
  Math.min(100, Number(process.env.MAX_PENDING_HTTP) || 32)
);
const httpResponseTimeoutMs = Math.max(
  1_000,
  Math.min(
    60_000,
    Number(process.env.HTTP_RESPONSE_TIMEOUT_MS) || 10_000
  )
);
const websocketMaxPayload = Math.max(
  64 * 1_024,
  Math.min(
    2 * 1024 * 1024,
    Number(process.env.WEBSOCKET_MAX_PAYLOAD_BYTES) || 512 * 1_024
  )
);
const maxConnections = Math.max(
  10,
  Math.min(20_000, Number(process.env.MAX_CONNECTIONS) || 2_000)
);
const maxConnectionsPerIp = Math.max(
  2,
  Math.min(500, Number(process.env.MAX_CONNECTIONS_PER_IP) || 30)
);
const maxUpgradesPerIpPerMinute = Math.max(
  5,
  Math.min(
    10_000,
    Number(process.env.MAX_UPGRADES_PER_IP_PER_MINUTE) || 120
  )
);
const maxNewSessionsPerIpPerMinute = Math.max(
  1,
  Math.min(
    1_000,
    Number(process.env.MAX_NEW_SESSIONS_PER_IP_PER_MINUTE) || 20
  )
);
const maxHttpPerIpPerMinute = Math.max(
  10,
  Math.min(
    100_000,
    Number(process.env.MAX_HTTP_PER_IP_PER_MINUTE) || 300
  )
);
const maxApprovalRequestsPerMinute = Math.max(
  1,
  Math.min(
    60,
    Number(process.env.MAX_APPROVAL_REQUESTS_PER_MINUTE) || 5
  )
);
const maxBufferedAmount = Math.max(
  64 * 1_024,
  Math.min(
    8 * 1024 * 1024,
    Number(process.env.MAX_WS_BUFFERED_BYTES) || 1024 * 1024
  )
);
const trustProxy = process.env.TRUST_PROXY === "true";
const strictTransportSecurity = "max-age=31536000";
const hostChallengeTtlMs = 30_000;
const allowedHostDeviceKeys = parseHostDevicePublicKeys(
  process.env.HOST_DEVICE_PUBLIC_KEYS || ""
);
if (allowedHostDeviceKeys.size === 0) {
  throw new Error(
    "HOST_DEVICE_PUBLIC_KEYS must contain at least one trusted host device"
  );
}

const sessions = new Map();
const activeConnectionsByIp = new Map();
const upgradeAttemptsByIp = new Map();
const newSessionAttemptsByIp = new Map();
const httpAttemptsByIp = new Map();
let activeConnections = 0;
const responseHeaderAllowlist = new Set([
  "cache-control",
  "content-language",
  "content-security-policy",
  "content-type",
  "cross-origin-embedder-policy",
  "cross-origin-opener-policy",
  "cross-origin-resource-policy",
  "location",
  "permissions-policy",
  "referrer-policy",
  "x-content-type-options",
  "x-frame-options"
]);
const requestHeaderAllowlist = new Set([
  "accept",
  "accept-encoding",
  "accept-language",
  "content-type",
  "origin",
  "referer",
  "user-agent"
]);

function text(res, status, body, contentType = "text/plain; charset=utf-8") {
  if (res.writableEnded || res.destroyed) return;
  res.writeHead(status, {
    "content-type": contentType,
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
    "strict-transport-security": strictTransportSecurity
  });
  res.end(body);
}

function parseUrl(req) {
  return new URL(req.url, `http://${req.headers.host || "localhost"}`);
}

function clientIp(req) {
  if (trustProxy) {
    const cloudflare = cleanHeaderValue(req.headers["cf-connecting-ip"], 80);
    if (cloudflare) return cloudflare;
    const forwarded = cleanHeaderValue(req.headers["x-forwarded-for"], 512);
    if (forwarded) return forwarded.split(",")[0].trim().slice(0, 80);
  }
  return String(req.socket.remoteAddress || "unknown").slice(0, 80);
}

function forwardedScheme(req) {
  if (req.socket && req.socket.encrypted) return "https";
  if (!trustProxy) return "";
  const visitor = cleanHeaderValue(req.headers["cf-visitor"], 200);
  if (visitor) {
    const parsed = safeJson(visitor);
    if (parsed && (parsed.scheme === "https" || parsed.scheme === "http")) {
      return parsed.scheme;
    }
  }
  const forwarded = cleanHeaderValue(req.headers["x-forwarded-proto"], 80)
    .split(",")[0]
    .trim()
    .toLowerCase();
  return forwarded === "https" || forwarded === "http" ? forwarded : "";
}

function httpsLocation(req, url) {
  const configured = String(process.env.PUBLIC_ORIGIN || "")
    .trim()
    .replace(/\/+$/, "");
  if (/^https:\/\/[A-Za-z0-9.-]+(?::\d+)?$/.test(configured)) {
    return configured + url.pathname + url.search;
  }
  const host = String(req.headers.host || "");
  if (!/^[A-Za-z0-9.-]+(?::\d+)?$/.test(host)) return "";
  return `https://${host}${url.pathname}${url.search}`;
}

function redirectToHttps(req, res, url) {
  const location = httpsLocation(req, url);
  if (!location) {
    text(res, 400, "Invalid host");
    return;
  }
  res.writeHead(308, {
    location,
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
    "strict-transport-security": strictTransportSecurity
  });
  res.end();
}

function rejectUpgrade(socket, status, reason) {
  try {
    socket.end(
      `HTTP/1.1 ${status} ${reason}\r\n`
      + "Connection: close\r\n"
      + "Cache-Control: no-store\r\n"
      + "Content-Length: 0\r\n\r\n"
    );
  } catch {
    socket.destroy();
  }
}

function safeJson(raw) {
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

function send(ws, message) {
  if (!ws || ws.readyState !== ws.OPEN) return false;
  if (ws.bufferedAmount > maxBufferedAmount) {
    closeWs(ws, 1013, "backpressure limit");
    return false;
  }
  ws.send(JSON.stringify(message));
  return true;
}

function sessionIsExpired(session) {
  return Date.now() - session.createdAt > sessionTtlMs;
}

function sameSecret(left, right) {
  const a = Buffer.from(String(left || ""), "utf8");
  const b = Buffer.from(String(right || ""), "utf8");
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

function parseHostDevicePublicKeys(rawValue) {
  const result = new Map();
  const value = String(rawValue || "").trim();
  if (!value) return result;
  for (const rawEntry of value.split(",")) {
    const entry = rawEntry.trim();
    const separator = entry.indexOf(":");
    if (separator <= 0) {
      throw new Error("Invalid HOST_DEVICE_PUBLIC_KEYS entry");
    }
    const keyId = entry.slice(0, separator);
    const encoded = entry.slice(separator + 1);
    if (!/^[A-Za-z0-9_-]{43}$/.test(keyId)
        || !/^[A-Za-z0-9_-]{100,500}$/.test(encoded)) {
      throw new Error("Invalid host device key format");
    }
    const der = Buffer.from(encoded, "base64url");
    const computedKeyId = crypto.createHash("sha256")
      .update(der)
      .digest("base64url");
    if (!sameSecret(keyId, computedKeyId)) {
      throw new Error("Host device key fingerprint mismatch");
    }
    const publicKey = crypto.createPublicKey({
      key: der,
      format: "der",
      type: "spki"
    });
    if (publicKey.asymmetricKeyType !== "ec"
        || publicKey.asymmetricKeyDetails.namedCurve !== "prime256v1") {
      throw new Error("Host device key must use P-256");
    }
    result.set(keyId, publicKey);
  }
  return result;
}

function hostAdmissionPayload(
  sessionId,
  deviceKeyId,
  challenge,
  issuedAtMs,
  hostToken,
  viewerVerifier
) {
  return [
    "locwarp-host-admission-v1",
    sessionId,
    deviceKeyId,
    challenge,
    String(issuedAtMs),
    hostToken,
    viewerVerifier
  ].join("|");
}

function verifyHostAdmission(message, expected) {
  if (!message || message.type !== "auth") return false;
  const sessionId = String(message.session || "");
  const deviceKeyId = String(message.deviceKeyId || "");
  const challenge = String(message.challenge || "");
  const hostToken = String(message.token || "");
  const viewerVerifier = String(message.viewerVerifier || "");
  const signature = String(message.signature || "");
  const issuedAtMs = Number(message.issuedAtMs);
  if (!/^[A-Za-z0-9_-]{8,80}$/.test(sessionId)
      || !/^[A-Za-z0-9_-]{43}$/.test(deviceKeyId)
      || !/^[A-Za-z0-9_-]{43}$/.test(challenge)
      || !/^[A-Za-z0-9_-]{12,160}$/.test(hostToken)
      || !/^[A-Za-z0-9_-]{43}$/.test(viewerVerifier)
      || !/^[A-Za-z0-9_-]{80,160}$/.test(signature)
      || !Number.isSafeInteger(issuedAtMs)
      || !sameSecret(sessionId, expected.sessionId)
      || !sameSecret(deviceKeyId, expected.deviceKeyId)
      || !sameSecret(challenge, expected.challenge)
      || issuedAtMs !== expected.issuedAtMs
      || Math.abs(Date.now() - issuedAtMs) > hostChallengeTtlMs) {
    return false;
  }
  const publicKey = allowedHostDeviceKeys.get(deviceKeyId);
  if (!publicKey) return false;
  try {
    return crypto.verify(
      "sha256",
      Buffer.from(hostAdmissionPayload(
        sessionId,
        deviceKeyId,
        challenge,
        issuedAtMs,
        hostToken,
        viewerVerifier
      ), "utf8"),
      publicKey,
      Buffer.from(signature, "base64url")
    );
  } catch {
    return false;
  }
}

function ensureSession(
  sessionId,
  hostToken,
  viewerVerifier,
  hostDeviceKeyId
) {
  let session = sessions.get(sessionId);
  if (session && (
    !sameSecret(session.hostToken, hostToken)
    || !sameSecret(session.viewerVerifier, viewerVerifier)
    || !sameSecret(session.hostDeviceKeyId, hostDeviceKeyId)
  )) return null;
  if (!session) {
    if (sessions.size >= maxSessions) return null;
    let deviceSessionCount = 0;
    for (const active of sessions.values()) {
      if (active.hostDeviceKeyId === hostDeviceKeyId) {
        deviceSessionCount += 1;
      }
    }
    if (deviceSessionCount >= maxSessionsPerHostDevice) return null;
    session = {
      id: sessionId,
      hostToken,
      viewerVerifier,
      hostDeviceKeyId,
      createdAt: Date.now(),
      host: null,
      viewers: new Set(),
      pendingViewers: new Map(),
      pendingCommands: new Map(),
      pendingHttp: new Map(),
      approvalTimes: []
    };
    sessions.set(sessionId, session);
  }
  return session;
}

function getViewerSession(sessionId, viewerVerifier) {
  const session = sessions.get(sessionId);
  if (!session
      || !sameSecret(session.viewerVerifier, viewerVerifier)
      || sessionIsExpired(session)) {
    return null;
  }
  return session;
}

function cleanupSession(session) {
  if (session.host && session.host.readyState === session.host.OPEN) return;
  if (session.viewers.size > 0) return;
  if (session.pendingViewers.size > 0) return;
  if (session.pendingHttp.size > 0) return;
  sessions.delete(session.id);
}

function closeWs(ws, code, reason) {
  try {
    ws.close(code, reason);
  } catch {
  }
}

function allowMessage(ws, limit = 120, windowMs = 60_000) {
  const now = Date.now();
  ws.messageTimes = (ws.messageTimes || []).filter(time => now - time < windowMs);
  if (ws.messageTimes.length >= limit) {
    closeWs(ws, 1008, "rate limit");
    return false;
  }
  ws.messageTimes.push(now);
  return true;
}

function allowBucket(store, key, limit, windowMs = 60_000) {
  const now = Date.now();
  const recent = (store.get(key) || []).filter(time => now - time < windowMs);
  if (recent.length >= limit) {
    store.set(key, recent);
    return false;
  }
  recent.push(now);
  store.set(key, recent);
  return true;
}

function pruneBucketStore(store, now, windowMs = 60_000) {
  for (const [key, times] of store) {
    const recent = times.filter(time => now - time < windowMs);
    if (recent.length > 0) store.set(key, recent);
    else store.delete(key);
  }
}

function createPendingRequest(viewer, message) {
  const clientId = String(message.id || crypto.randomUUID()).slice(0, 120);
  return {
    id: crypto.randomUUID(),
    pending: {
      viewer,
      viewerId: viewer.viewerId,
      clientId,
      createdAt: Date.now()
    }
  };
}

function cleanViewerName(value) {
  const name = String(value || "").trim().replace(/\s+/g, " ");
  return (name || "Browser").slice(0, 60);
}

function cleanDeviceId(value) {
  const id = String(value || "").trim();
  return /^[A-Za-z0-9_-]{8,100}$/.test(id) ? id : "";
}

function cleanOpaque(value, minimum = 16, maximum = 160) {
  const item = String(value || "").trim();
  return new RegExp(`^[A-Za-z0-9_-]{${minimum},${maximum}}$`).test(item)
    ? item
    : "";
}

function cleanEnvelope(value) {
  const envelope = String(value || "");
  return envelope.length <= websocketMaxPayload
      && /^v2\.[A-Za-z0-9_-]{16}\.[A-Za-z0-9_-]{20,}$/.test(envelope)
    ? envelope
    : "";
}

function accessRole(value) {
  return value === "controller" ? "controller" : "readonly";
}

function cleanHeaderValue(value, maxLength = 4096) {
  const out = String(value == null ? "" : value);
  if (!out || out.length > maxLength || /[\r\n]/.test(out)) return "";
  return out;
}

function sanitizeResponseHeaders(raw) {
  const output = {};
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return output;
  for (const [rawName, rawValue] of Object.entries(raw)) {
    const name = String(rawName || "").toLowerCase();
    if (!responseHeaderAllowlist.has(name)) continue;
    const value = cleanHeaderValue(rawValue);
    if (value) output[name] = value;
  }
  return output;
}

function sanitizeRequestHeaders(raw) {
  const output = {};
  if (!raw || typeof raw !== "object") return output;
  for (const [rawName, rawValue] of Object.entries(raw)) {
    const name = String(rawName || "").toLowerCase();
    if (!requestHeaderAllowlist.has(name)) continue;
    const value = Array.isArray(rawValue)
      ? cleanHeaderValue(rawValue.join(", "))
      : cleanHeaderValue(rawValue);
    if (value) output[name] = value;
  }
  return output;
}

function collectRequestBody(req) {
  if (req.method === "GET" || req.method === "HEAD") {
    return Promise.resolve(Buffer.alloc(0));
  }
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    let settled = false;
    req.on("data", chunk => {
      if (settled) return;
      size += chunk.length;
      if (size > maxHttpRequestBytes) {
        settled = true;
        req.resume();
        reject(new Error("request too large"));
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => {
      if (settled) return;
      settled = true;
      resolve(Buffer.concat(chunks));
    });
    req.on("error", error => {
      if (settled) return;
      settled = true;
      reject(error);
    });
  });
}

function failPendingHttp(session, status, message) {
  for (const [id, pending] of session.pendingHttp) {
    clearTimeout(pending.timer);
    session.pendingHttp.delete(id);
    text(pending.res, status, message);
  }
}

function completeHttpResponse(session, message) {
  const id = String(message.id || "");
  const pending = session.pendingHttp.get(id);
  if (!pending) return;
  session.pendingHttp.delete(id);
  clearTimeout(pending.timer);
  if (pending.res.writableEnded || pending.res.destroyed) return;

  let status = Number(message.status);
  if (!Number.isInteger(status) || status < 200 || status > 599) status = 502;
  let body = typeof message.body === "string" ? message.body : "";
  if (Buffer.byteLength(body, "utf8") > maxHttpResponseBytes) {
    text(pending.res, 502, "Host response exceeded relay limit");
    return;
  }
  const headers = sanitizeResponseHeaders(message.headers);
  if (!headers["cache-control"]) headers["cache-control"] = "no-store";
  if (!headers["x-content-type-options"]) {
    headers["x-content-type-options"] = "nosniff";
  }
  headers["strict-transport-security"] = strictTransportSecurity;
  if (!headers["content-type"]) {
    headers["content-type"] = "application/octet-stream";
  }
  pending.res.writeHead(status, headers);
  if (pending.method === "HEAD" || status === 204 || status === 304) {
    pending.res.end();
  } else {
    pending.res.end(body);
  }
}

async function forwardHttpRequest(req, res, url, sessionId) {
  const session = sessions.get(sessionId);
  if (!session || sessionIsExpired(session)) {
    text(res, 404, "Session unavailable");
    return;
  }
  if (!session.host || session.host.readyState !== session.host.OPEN) {
    text(res, 503, "Host offline");
    return;
  }
  if (session.pendingHttp.size >= maxPendingHttp) {
    text(res, 429, "Too many pending HTTP requests");
    return;
  }

  let body;
  try {
    body = await collectRequestBody(req);
  } catch {
    text(res, 413, "Request body exceeded relay limit");
    return;
  }
  if (!session.host || session.host.readyState !== session.host.OPEN) {
    text(res, 503, "Host offline");
    return;
  }

  const id = crypto.randomUUID();
  const timer = setTimeout(() => {
    const pending = session.pendingHttp.get(id);
    if (!pending) return;
    session.pendingHttp.delete(id);
    text(pending.res, 504, "Host response timed out");
    cleanupSession(session);
  }, httpResponseTimeoutMs);
  timer.unref();
  session.pendingHttp.set(id, {
    res,
    method: req.method || "GET",
    timer
  });
  res.once("close", () => {
    const pending = session.pendingHttp.get(id);
    if (!pending) return;
    clearTimeout(pending.timer);
    session.pendingHttp.delete(id);
    cleanupSession(session);
  });
  const delivered = send(session.host, {
    type: "http_request",
    id,
    method: String(req.method || "GET").slice(0, 16),
    path: `${url.pathname}${url.search}`.slice(0, 4096),
    headers: sanitizeRequestHeaders(req.headers),
    bodyBase64: body.toString("base64")
  });
  if (!delivered) {
    const pending = session.pendingHttp.get(id);
    if (pending) {
      clearTimeout(pending.timer);
      session.pendingHttp.delete(id);
    }
    text(res, 503, "Host offline");
  }
}

async function handleHttp(req, res) {
  let url;
  try {
    url = parseUrl(req);
  } catch {
    text(res, 400, "Bad request");
    return;
  }
  if (forwardedScheme(req) === "http") {
    redirectToHttps(req, res, url);
    return;
  }
  if (url.pathname === "/health") {
    text(
      res,
      200,
      JSON.stringify({ ok: true }),
      "application/json; charset=utf-8"
    );
    return;
  }
  if (url.pathname === "/") {
    text(res, 200, "Generic session relay");
    return;
  }
  const match = url.pathname.match(
    /^\/r\/([A-Za-z0-9_-]{8,80})(?:\/.*)?$/
  );
  if (match) {
    await forwardHttpRequest(req, res, url, match[1]);
    return;
  }
  text(res, 404, "Not found");
}

const server = http.createServer((req, res) => {
  const ip = clientIp(req);
  if (!allowBucket(httpAttemptsByIp, ip, maxHttpPerIpPerMinute)) {
    text(res, 429, "Too many requests");
    return;
  }
  handleHttp(req, res).catch(() => text(res, 500, "Relay error"));
});

const wss = new WebSocketServer({
  noServer: true,
  maxPayload: websocketMaxPayload,
  perMessageDeflate: false
});

server.on("upgrade", (req, socket, head) => {
  let url;
  try {
    url = parseUrl(req);
  } catch {
    socket.destroy();
    return;
  }
  const ip = clientIp(req);
  if (!allowBucket(
    upgradeAttemptsByIp,
    ip,
    maxUpgradesPerIpPerMinute
  )) {
    rejectUpgrade(socket, 429, "Too Many Requests");
    return;
  }
  if (activeConnections >= maxConnections
      || (activeConnectionsByIp.get(ip) || 0) >= maxConnectionsPerIp) {
    rejectUpgrade(socket, 503, "Service Unavailable");
    return;
  }
  const scheme = forwardedScheme(req);
  if (scheme === "http") {
    rejectUpgrade(socket, 426, "Upgrade Required");
    return;
  }
  if (url.pathname !== "/ws/host" && url.pathname !== "/ws/viewer") {
    socket.destroy();
    return;
  }
  if (url.pathname === "/ws/viewer") {
    const origin = req.headers.origin || "";
    const host = req.headers.host || "";
    const secureOrigin = origin === `https://${host}`;
    const localDevelopmentOrigin = !scheme && origin === `http://${host}`;
    if (!secureOrigin && !localDevelopmentOrigin) {
      socket.destroy();
      return;
    }
  }
  wss.handleUpgrade(req, socket, head, ws => {
    ws.clientIp = ip;
    wss.emit("connection", ws, req, url);
  });
});

wss.on("connection", (ws, req, url) => {
  activeConnections += 1;
  activeConnectionsByIp.set(
    ws.clientIp,
    (activeConnectionsByIp.get(ws.clientIp) || 0) + 1
  );
  ws.isAlive = true;
  ws.on("pong", () => {
    ws.isAlive = true;
  });
  ws.once("close", () => {
    activeConnections = Math.max(0, activeConnections - 1);
    const remaining = Math.max(
      0,
      (activeConnectionsByIp.get(ws.clientIp) || 1) - 1
    );
    if (remaining > 0) activeConnectionsByIp.set(ws.clientIp, remaining);
    else activeConnectionsByIp.delete(ws.clientIp);
  });
  const role = url.pathname === "/ws/host" ? "host" : "viewer";
  const authTimer = setTimeout(
    () => closeWs(ws, 1008, "auth timeout"),
    15_000
  );
  if (role === "host") {
    authenticateHost(ws, authTimer);
  } else {
    authenticateViewer(ws, authTimer);
  }
  ws.once("close", () => clearTimeout(authTimer));
});

function authenticateHost(ws, authTimer) {
  ws.once("message", raw => {
    if (!allowMessage(ws, 10, 10_000)) return;
    const message = safeJson(String(raw));
    const sessionId = message && message.type === "host_init"
      ? message.session || ""
      : "";
    const deviceKeyId = message && message.type === "host_init"
      ? message.deviceKeyId || ""
      : "";
    if (!/^[A-Za-z0-9_-]{8,80}$/.test(sessionId)
        || !/^[A-Za-z0-9_-]{43}$/.test(deviceKeyId)
        || !allowedHostDeviceKeys.has(deviceKeyId)) {
      clearTimeout(authTimer);
      closeWs(ws, 1008, "host device not allowed");
      return;
    }
    const expected = {
      sessionId,
      deviceKeyId,
      challenge: crypto.randomBytes(32).toString("base64url"),
      issuedAtMs: Date.now()
    };
    if (!send(ws, {
      type: "host_challenge",
      challenge: expected.challenge,
      issuedAtMs: expected.issuedAtMs
    })) {
      clearTimeout(authTimer);
      return;
    }
    ws.once("message", authRaw => {
      clearTimeout(authTimer);
      if (!allowMessage(ws, 10, 10_000)) return;
      const auth = safeJson(String(authRaw));
      if (!verifyHostAdmission(auth, expected)) {
        closeWs(ws, 1008, "host signature invalid");
        return;
      }
      attachHost(
        ws,
        expected.sessionId,
        String(auth.token),
        String(auth.viewerVerifier),
        expected.deviceKeyId
      );
    });
  });
}

function authenticateViewer(ws, authTimer) {
  ws.once("message", raw => {
    clearTimeout(authTimer);
    if (!allowMessage(ws, 10, 10_000)) return;
    const message = safeJson(String(raw));
    const sessionId = message && message.type === "auth"
      ? message.session || ""
      : "";
    const token = message && message.type === "auth"
      ? message.token || ""
      : "";
    if (!/^[A-Za-z0-9_-]{8,80}$/.test(sessionId)
        || !/^[A-Za-z0-9_-]{43}$/.test(token)) {
      closeWs(ws, 1008, "bad session");
      return;
    }
    attachViewer(ws, sessionId, token, message);
  });
}

setInterval(() => {
  for (const ws of wss.clients) {
    if (ws.isAlive === false) {
      ws.terminate();
      continue;
    }
    ws.isAlive = false;
    try {
      ws.ping();
    } catch {
      ws.terminate();
    }
  }
}, 30_000).unref();

function attachHost(
  ws,
  sessionId,
  hostToken,
  viewerVerifier,
  hostDeviceKeyId
) {
  if (!sessions.has(sessionId)
      && !allowBucket(
        newSessionAttemptsByIp,
        ws.clientIp,
        maxNewSessionsPerIpPerMinute
      )) {
    closeWs(ws, 1013, "new session rate limit");
    return;
  }
  const session = ensureSession(
    sessionId,
    hostToken,
    viewerVerifier,
    hostDeviceKeyId
  );
  if (!session) {
    closeWs(ws, 1008, "bad token");
    return;
  }
  if (session.host && session.host !== ws) {
    closeWs(session.host, 1012, "replaced");
  }
  session.host = ws;
  ws.session = session;
  ws.role = "host";
  send(ws, { type: "hello", role: "host", session: sessionId });
  notifyViewers(session);
  for (const viewer of session.viewers) {
    send(viewer, { type: "host", online: true });
  }

  ws.on("message", raw => {
    if (!allowMessage(ws)) return;
    const message = safeJson(String(raw));
    if (!message || typeof message.type !== "string") return;
    if (message.type === "http_response") {
      completeHttpResponse(session, message);
      return;
    }
    if (message.type === "viewer_decision") {
      const viewerId = String(message.viewerId || "");
      const viewer = session.pendingViewers.get(viewerId);
      if (!viewer) return;
      session.pendingViewers.delete(viewerId);
      clearTimeout(viewer.approvalTimer);
      if (message.approved === true) {
        const grant = cleanEnvelope(message.grant);
        if (!grant) {
          closeWs(viewer, 1008, "invalid viewer grant");
          return;
        }
        activateViewer(
          session,
          viewer,
          accessRole(message.accessRole),
          grant
        );
      } else {
        closeWs(viewer, 1008, "viewer rejected");
      }
      return;
    }
    if (message.type === "viewer_disconnect") {
      disconnectViewer(session, String(message.viewerId || ""));
      return;
    }
    if (message.type === "viewer_role") {
      const viewer = findViewer(session, String(message.viewerId || ""));
      if (!viewer) return;
      const grant = cleanEnvelope(message.grant);
      if (!grant) return;
      viewer.accessRole = accessRole(message.accessRole);
      send(viewer, {
        type: "access",
        accessRole: viewer.accessRole,
        grant
      });
      notifyViewers(session);
      return;
    }
    if (message.type === "viewer_status") {
      const viewer = findViewer(session, String(message.viewerId || ""));
      const envelope = cleanEnvelope(message.envelope);
      if (viewer && envelope) {
        send(viewer, { type: "status", envelope });
      }
      return;
    }
    if (message.type === "result") {
      const pending = session.pendingCommands.get(message.id);
      const envelope = cleanEnvelope(message.envelope);
      if (pending) {
        session.pendingCommands.delete(message.id);
        if (envelope) {
          send(pending.viewer, {
            type: "result",
            id: pending.clientId,
            envelope
          });
        } else {
          send(pending.viewer, {
            type: "relay_error",
            id: pending.clientId,
            message: "Invalid encrypted result"
          });
        }
      }
    }
  });

  ws.on("close", () => {
    if (session.host !== ws) return;
    session.host = null;
    for (const viewer of session.viewers) {
      send(viewer, { type: "host", online: false });
    }
    for (const viewer of session.pendingViewers.values()) {
      closeWs(viewer, 1012, "host offline");
    }
    session.pendingViewers.clear();
    for (const [id, pending] of session.pendingCommands) {
      send(pending.viewer, {
        type: "relay_error",
        id: pending.clientId,
        message: "Host disconnected"
      });
    }
    session.pendingCommands.clear();
    failPendingHttp(session, 503, "Host disconnected");
    cleanupSession(session);
  });
}

function attachViewer(ws, sessionId, token, auth) {
  const session = getViewerSession(sessionId, token);
  if (!session) {
    closeWs(ws, 1008, "session unavailable");
    return;
  }
  if (!session.host || session.host.readyState !== session.host.OPEN) {
    closeWs(ws, 1012, "host offline");
    return;
  }
  if (session.viewers.size + session.pendingViewers.size >= 5) {
    closeWs(ws, 1013, "viewer limit");
    return;
  }
  const now = Date.now();
  session.approvalTimes = session.approvalTimes.filter(
    time => now - time < 60_000
  );
  if (session.approvalTimes.length >= maxApprovalRequestsPerMinute) {
    closeWs(ws, 1013, "approval rate limit");
    return;
  }
  const deviceId = cleanDeviceId(auth && auth.deviceId);
  const e2eNonce = cleanOpaque(auth && auth.e2eNonce, 16, 100);
  const e2eProof = cleanOpaque(auth && auth.e2eProof, 43, 100);
  if (!deviceId || !e2eNonce || !e2eProof) {
    closeWs(ws, 1008, "invalid viewer proof");
    return;
  }
  session.approvalTimes.push(now);
  ws.session = session;
  ws.role = "pending_viewer";
  ws.viewerId = crypto.randomUUID();
  ws.viewerName = cleanViewerName(auth && auth.name);
  ws.deviceId = deviceId;
  ws.e2eNonce = e2eNonce;
  ws.e2eProof = e2eProof;
  ws.requestedRole = accessRole(auth && auth.requestedRole);
  ws.connectedAt = Date.now();
  session.pendingViewers.set(ws.viewerId, ws);
  send(ws, { type: "approval_pending", viewerId: ws.viewerId });
  send(session.host, {
    type: "viewer_request",
    viewerId: ws.viewerId,
    name: ws.viewerName,
    deviceId: ws.deviceId,
    requestedRole: ws.requestedRole,
    e2eNonce: ws.e2eNonce,
    e2eProof: ws.e2eProof
  });
  ws.approvalTimer = setTimeout(() => {
    cancelPendingViewer(session, ws);
    closeWs(ws, 1008, "approval timeout");
  }, 30_000);

  ws.on("close", () => {
    clearTimeout(ws.approvalTimer);
    cancelPendingViewer(session, ws);
    session.viewers.delete(ws);
    for (const [id, pending] of session.pendingCommands) {
      if (pending.viewer === ws) session.pendingCommands.delete(id);
    }
    notifyViewers(session);
    cleanupSession(session);
  });
}

function activateViewer(session, ws, role, grant) {
  if (ws.readyState !== ws.OPEN) return;
  session.pendingViewers.delete(ws.viewerId);
  session.viewers.add(ws);
  ws.role = "viewer";
  ws.accessRole = accessRole(role);
  send(ws, {
    type: "hello",
    role: "viewer",
    hostOnline: true,
    accessRole: ws.accessRole,
    viewerId: ws.viewerId,
    deviceId: ws.deviceId,
    e2eNonce: ws.e2eNonce,
    grant
  });
  notifyViewers(session);

  ws.on("message", raw => {
    if (!allowMessage(ws)) return;
    const message = safeJson(String(raw));
    if (!message || typeof message.type !== "string") return;
    if (message.type === "status") {
      if (session.host && session.host.readyState === session.host.OPEN) {
        send(session.host, {
          type: "status_request",
          viewerId: ws.viewerId
        });
      }
      return;
    }
    if (message.type !== "command") return;
    if (ws.accessRole !== "controller") {
      send(ws, {
        type: "relay_error",
        id: message.id,
        message: "This viewer is read-only"
      });
      return;
    }
    if (!session.host || session.host.readyState !== session.host.OPEN) {
      send(ws, {
        type: "relay_error",
        id: message.id,
        message: "Host is offline"
      });
      return;
    }
    if (session.pendingCommands.size >= 100) {
      send(ws, {
        type: "relay_error",
        id: message.id,
        message: "Too many pending commands"
      });
      return;
    }
    const envelope = cleanEnvelope(message.envelope);
    if (!envelope) {
      send(ws, {
        type: "relay_error",
        id: message.id,
        message: "Invalid encrypted command"
      });
      return;
    }
    const request = createPendingRequest(ws, message);
    session.pendingCommands.set(request.id, request.pending);
    send(session.host, {
      type: "command",
      id: request.id,
      envelope,
      viewer: {
        id: ws.viewerId,
        name: ws.viewerName,
        deviceId: ws.deviceId,
        accessRole: ws.accessRole,
        e2eNonce: ws.e2eNonce
      }
    });
  });
}

function cancelPendingViewer(session, ws) {
  if (!session.pendingViewers.delete(ws.viewerId)) return;
  if (session.host && session.host.readyState === session.host.OPEN) {
    send(session.host, {
      type: "viewer_cancelled",
      viewerId: ws.viewerId
    });
  }
}

function notifyViewerCount(session) {
  send(session.host, {
    type: "viewer_count",
    count: session.viewers.size
  });
}

function notifyViewers(session) {
  notifyViewerCount(session);
  send(session.host, {
    type: "viewer_list",
    viewers: Array.from(session.viewers, viewer => ({
      id: viewer.viewerId,
      name: viewer.viewerName,
      deviceId: viewer.deviceId,
      e2eNonce: viewer.e2eNonce,
      accessRole: viewer.accessRole,
      connectedAt: viewer.connectedAt
    }))
  });
}

function findViewer(session, viewerId) {
  for (const viewer of session.viewers) {
    if (viewer.viewerId === viewerId) return viewer;
  }
  return null;
}

function disconnectViewer(session, viewerId) {
  const pending = session.pendingViewers.get(viewerId);
  if (pending) {
    session.pendingViewers.delete(viewerId);
    clearTimeout(pending.approvalTimer);
    closeWs(pending, 1008, "disconnected by host");
    return;
  }
  const viewer = findViewer(session, viewerId);
  if (viewer) closeWs(viewer, 1008, "disconnected by host");
}

setInterval(() => {
  const now = Date.now();
  for (const session of sessions.values()) {
    for (const [id, pending] of session.pendingCommands) {
      if (now - pending.createdAt <= 45_000) continue;
      send(pending.viewer, {
        type: "relay_error",
        id: pending.clientId,
        message: "Command timed out"
      });
      session.pendingCommands.delete(id);
    }
    if (now - session.createdAt <= sessionTtlMs) continue;
    if (session.host) closeWs(session.host, 1000, "expired");
    for (const viewer of session.viewers) {
      closeWs(viewer, 1000, "expired");
    }
    for (const viewer of session.pendingViewers.values()) {
      closeWs(viewer, 1000, "expired");
    }
    failPendingHttp(session, 410, "Session expired");
    sessions.delete(session.id);
  }
  pruneBucketStore(upgradeAttemptsByIp, now);
  pruneBucketStore(newSessionAttemptsByIp, now);
  pruneBucketStore(httpAttemptsByIp, now);
}, 60_000).unref();

function startServer(listenPort = port) {
  return server.listen(listenPort, () => {
    console.log(`Generic session relay listening on ${listenPort}`);
  });
}

if (require.main === module) startServer();

module.exports = {
  allowMessage,
  createPendingRequest,
  sameSecret,
  sanitizeResponseHeaders,
  startServer
};
