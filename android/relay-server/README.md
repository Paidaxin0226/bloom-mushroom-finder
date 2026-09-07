# Generic Session Relay

This Docker service is a product-neutral, in-memory relay. It contains no
application page, product UI, command definitions, or business logic.

An authenticated host owns each session. HTTP requests under `/r/{session}/`
are forwarded to that host as opaque `http_request` messages, and the host
returns an `http_response`. Approved WebSocket viewers exchange versioned,
end-to-end encrypted envelopes with the host. The relay does not receive the
viewer secret, command payloads, results, status, routes, bookmarks, or
coordinates.

## Deploy

Copy the environment file, select either the tunnel or direct profile, and
build the container:

```bash
cp .env.example .env
docker compose up -d --build
```

Before starting Docker, open LocWarp's Relay page and copy
`伺服器白名單公鑰` into `HOST_DEVICE_PUBLIC_KEYS` in `.env`. Multiple trusted
phones can be separated with commas. The value is a public P-256 key, not a
secret; the matching non-exportable private key stays in Android Keystore.
Docker intentionally fails closed when no trusted key is configured.

Health check:

```bash
curl https://relay.example.com/health
```

The relay stores sessions only in memory. Restarting the container disconnects
active sessions; hosts may reconnect and recreate them.

## Cloudflare Tunnel

For a host behind NAT:

```dotenv
RELAY_DOMAIN=relay.example.com
COMPOSE_PROFILES=tunnel
CLOUDFLARED_CREDENTIALS_FILE=C:/Users/you/.cloudflared/TUNNEL_ID.json
```

Copy `cloudflared.yml.example` to the Git-ignored `cloudflared.yml`, set the
tunnel ID and hostname there, provision the DNS route once, and run
`docker compose up -d --build`.

For direct HTTPS mode, use `COMPOSE_PROFILES=direct`; Caddy terminates TLS.

## Protocol

The host connects to `/ws/host` and first identifies its allowlisted device:

```json
{"type":"host_init","session":"...","deviceKeyId":"SHA-256 of P-256 SPKI"}
```

The Relay returns a 32-byte random, short-lived challenge:

```json
{"type":"host_challenge","challenge":"...","issuedAtMs":1234567890}
```

The phone signs the domain-separated canonical payload with its Android
Keystore P-256 private key and answers:

```json
{
  "type":"auth",
  "session":"...",
  "token":"...",
  "viewerVerifier":"SHA-256 verifier",
  "deviceKeyId":"...",
  "challenge":"...",
  "issuedAtMs":1234567890,
  "signature":"base64url ECDSA signature"
}
```

Unknown devices are rejected before a session is created. Challenges expire
after 30 seconds and cannot be reused on another connection because every
connection receives a fresh challenge.

For an HTTP request to `/r/{session}/...`, the relay sends:

```json
{
  "type":"http_request",
  "id":"...",
  "method":"GET",
  "path":"/r/{session}/...",
  "headers":{"accept":"text/html"},
  "bodyBase64":""
}
```

The host answers with:

```json
{
  "type":"http_response",
  "id":"...",
  "status":200,
  "headers":{"content-type":"text/html; charset=utf-8"},
  "body":"..."
}
```

Response headers are allowlisted and all request, response, WebSocket,
connection, per-IP, pending request, backpressure, and timeout limits are
bounded. Viewer secrets remain in the phone and browser; only a
domain-separated verifier reaches the relay. Viewers remain pending until the
host validates their proof, the user checks a six-digit pairing code, and the
phone grants either a controller or read-only capability. Capabilities and
application messages are encrypted with AES-256-GCM. Commands are freshness
checked and replay protected by the phone.

## Configuration

- `SESSION_TTL_MS` (default `21600000`, six hours)
- `HOST_DEVICE_PUBLIC_KEYS` (required comma-separated
  `keyId:base64urlP256Spki` allowlist)
- `MAX_SESSIONS` (default `1000`)
- `MAX_SESSIONS_PER_HOST_DEVICE` (default `3`)
- `MAX_CONNECTIONS` (default `2000`)
- `MAX_CONNECTIONS_PER_IP` (default `30`)
- `MAX_UPGRADES_PER_IP_PER_MINUTE` (default `120`)
- `MAX_NEW_SESSIONS_PER_IP_PER_MINUTE` (default `20`)
- `MAX_HTTP_PER_IP_PER_MINUTE` (default `300`)
- `MAX_APPROVAL_REQUESTS_PER_MINUTE` (default `5` per session)
- `MAX_WS_BUFFERED_BYTES` (default `1048576`)
- `MAX_HTTP_REQUEST_BYTES` (default `65536`)
- `MAX_HTTP_RESPONSE_BYTES` (default `262144`)
- `MAX_PENDING_HTTP` (default `32` per session)
- `HTTP_RESPONSE_TIMEOUT_MS` (default `10000`)
- `WEBSOCKET_MAX_PAYLOAD_BYTES` (default `524288`)

## Test

```bash
npm test
```

The suite exercises device allowlisting, signed host authentication, HTTP host
forwarding, response sanitization, viewer approval, roles, and a complete
command-result round trip.

After deployment, verify the public health route and confirm that a random,
non-allowlisted host is rejected:

```bash
node public-security-smoke.js https://relay.example.com
```

## Threat model

The encrypted viewer protocol prevents a normally operating or
honest-but-curious Relay from reading application data or manufacturing a valid
command. TLS, hash-fragment secrets, phone approval, pairing codes, rotating
capabilities, freshness windows, and replay checks also limit network and
credential-leak attacks.

Possessing the Relay URL or the Android APK is not enough to create a host
session. A host must prove possession of a private key whose public key is
explicitly allowlisted by the operator. This blocks other phones from using the
Relay even if they install the same APK.

The browser document is delivered under the Relay's web origin. A Relay that is
already fully compromised before the first page load can replace that document
and attempt to steal the fragment secret. Preventing that active bootstrap
attack requires serving the browser client from a separately trusted,
independently deployed origin; it cannot be solved by application-layer
encryption inside a document supplied by the same compromised origin.
