# LocWarp Android

LocWarp Android is the standalone Android mock-location controller.

The old Windows desktop app, Electron frontend, Python backend, tunnel service,
and installer code are not part of this repo.

## Features

- Local mock-location enable/restore flow
- Four-page bottom navigation for location, step injection, per-app LSPosed
  controls, and Relay
- Built-in map and coordinate input
- Teleport, navigation, loop, multi-stop, random walk, and joystick controls
- Floating joystick and floating map hint overlays
- Local bookmarks, saved routes, waypoint reordering/reversing, distance/ETA preview
- Route import/export for coordinates, GPX, KML, and GeoJSON
- Visited-tile map cache with online, custom-source, and policy-aware offline modes
- Portable JSON backup/restore for bookmarks, routes, Hook settings, history, and UI settings
- Movement safety timeout, optional automatic location restore, and restart recovery
- Foreground notification controls for pause/resume, speed changes, and stop-and-restore
- Local operation history, CSV export, and a copyable diagnostics report
- Built-in LSPosed hooks for step-sensor injection and mock-location flag hiding
- Apps-page Step Hook selection and one global walking multiplier
- Dedicated Relay page for server settings, connection state, viewer approval,
  session rotation, sharing, and QR access
- User-enabled, six-digit-PIN-paired USB control for the Windows control center
- ADB local-abstract command tunnel that reuses the same validated movement,
  step, status, bookmark, and route command executor as Relay
- Named Relay viewers, controller/read-only roles, per-viewer disconnect and role changes,
  and command audit entries
- Relay control: B phone shows one relay QR, A scans it and controls B
  from a browser over the internet after B explicitly approves the viewer

## Requirements

- Android Studio or Gradle with Android Gradle Plugin support
- Android SDK Platform 36 / Build Tools 35.0.0
- JDK 17 for release packaging
- A local signing setup for release builds

For local builds, set `sdk.dir` in `local.properties` to your own Android SDK
directory, for example `C:/Android/Sdk`. Do not commit `local.properties`.

## Relay Docker Server

Configure your own relay domain, for example `relay.example.com`.

On the Docker host:

```bash
cd relay-server
cp .env.example .env
docker compose up -d --build
```

In DNS, point your relay domain to the Docker host public IP.
The included deployment uses `COMPOSE_PROFILES=tunnel` to run Relay behind the
existing Cloudflare Tunnel without exposing local ports. Hosts with direct
public ports can instead select the `direct` profile to run Caddy on `80/443`.

The Android app owns the complete browser page, including Traditional Chinese
copy, App-matched layout, step controls, JavaScript, and security headers.
Docker contains no LocWarp page or product command definitions: it forwards
bounded HTTP requests to the authenticated phone and returns the phone's opaque
response, so the same relay can serve other host applications.

WebSocket credentials are sent in the first authenticated frame instead of the
WebSocket URL. Host and viewer use separate secrets; the Host secret never
enters the QR, and only a domain-separated verifier of the viewer secret reaches
the Relay. Android persists both secrets through Android Keystore encryption.
Before the Relay accepts a phone, it also requires a fresh ECDSA challenge
signed by that phone's non-exportable Android Keystore P-256 key. Copy the
phone's `伺服器白名單公鑰` from the Relay page into
`HOST_DEVICE_PUBLIC_KEYS` in `relay-server/.env`; an empty allowlist prevents
the server from starting. Installing the same APK on another phone does not
grant access because each phone generates a different private key.
Status, coordinates, routes, bookmarks, commands, capabilities, and results use
AES-256-GCM envelopes between the browser and phone. A newly connected browser
remains pending until B verifies the same six-digit pairing code and approves it
as a controller or read-only viewer. B can inspect, change, or disconnect each
active viewer, or rotate the QR/session immediately. Fresh nonces, expiring
messages, capability rotation, and sequence/replay checks reject stale traffic.
The Relay container is read-only, unprivileged, health-checked,
payload-limited, connection-limited, rate-limited, backpressure-protected, and
keeps only short-lived in-memory session data.

Health check:

```bash
curl https://relay.example.com/health
```

The Windows control center mirrors all four Android pages over the local USB
tunnel. It can open or close the Android floating joystick and map overlays,
change their sizes, and use a desktop joystick; Android still enforces the
system overlay permission before a phone overlay is shown. The Android map's
long-press quick actions are exposed as a desktop right-click menu.

## Windows USB control

1. Install the signed Android release and connect the phone with USB debugging.
2. Open **連線** in the Android app.
3. Under **電腦 USB 控制**, tap **啟動 USB 控制**.
4. In Bloom Mushroom Finder Control Center, open **裝置控制** and select the authorized
   USB-debugging device. The desktop uses Android's existing ADB authorization;
   no separate pairing code is required.

The service is non-exported and does not listen on a Wi-Fi/LAN TCP port. The
desktop reaches its Android local-abstract socket through a dynamically allocated
ADB forward. Stopping the service clears the PIN and disconnects the desktop.

Mushroom recognition uses three short-lived USB commands:
`beginCoordinateCapture`, `coordinateCaptureStatus`, and
`endCoordinateCapture`. While this lease is active, map intents are read-only:
LocWarp returns the parsed coordinate but does not teleport, navigate, add a
flower target, copy to the clipboard, or forward the link. The persisted expiry
restores normal map-link behavior if the desktop disconnects unexpectedly.

## LSPosed hooks

The signed LocWarp APK is also an LSPosed module. Enable LocWarp in LSPosed,
add each target app to the module scope, then force-stop and reopen those apps.

The module provides two hooks:

- Step counter/detector injection. The step page can send an exact one-time
  total, run a continuous loop, or follow LocWarp's simulated walking speed.
- Mock-location flag hiding. Scoped apps see both
  `Location.isFromMockProvider()` and `Location.isMock()` as `false`.

The LSPosed apps page reads the framework's selected module scope when the
service is available. Older frameworks use an observed-hook fallback plus an
installed-app picker for scoped apps that have not launched yet. Each scoped
app can enable Location Hook, Step Hook, or both independently.

Specified-step, continuous-loop, and simulated-walking jobs are scheduled by
LocWarp itself. They wait a random 0.2–0.6 second between broadcasts and send a
random 1–3 steps per batch. The last specified-step batch is reduced when needed
so the requested total is exact. Jobs do not wait for target-app progress
callbacks, can be paused or terminated locally, and broadcast each batch to all
running apps enabled through the Apps page. A global 1–100x real-walking
multiplier uses the same random batching while adding the missing
`multiplier - 1` steps for each real step.

Each running target process accumulates the received step offset even when it
does not currently have a step-sensor listener. Existing listeners receive each
batch immediately; step-counter listeners registered later receive the
accumulated offset with their next sensor value. Commands use LocWarp's
signature-level permission. There is no exported step-progress receiver.
Health Connect and `WRITE_STEPS` are no longer used.

## Reliability and limits

- Route requests use a bounded 15-minute LRU cache, cancellable in-flight calls,
  and a 2 MB response limit.
- Map tiles requested by the visible viewport use a bounded 100 MB on-device cache.
  The OpenStreetMap preset does not expose bulk download or offline mode; custom
  HTTPS sources can enable cache-only mode when their provider permits it.
- Remote control rejects excessive speed, radius, pause, lap, name, and route
  point values before they reach the simulation engine.
- High-frequency mock-location state remains in memory and is persisted at
  controlled checkpoints instead of writing preferences every update.
- Movement sessions can be restored after process restart when enabled; safety
  timeout and automatic real-location restore are configured on the location
  page.

## Verification

Android release tests and Lint:

```powershell
.\gradlew.bat :app:testReleaseUnitTest :app:lintRelease --no-daemon
```

Relay unit and WebSocket approval/command round-trip tests:

```powershell
cd relay-server
npm test
```

## Debug Build

```powershell
gradle :app:assembleDebug --no-daemon
```

## Release Build

Configure signing files under `signing/`:

- `signing/locwarp-release.keystore`
- `signing/signing.properties`

Use the release script so package metadata and signing certificate are verified:

```powershell
.\tools\build-android-release.ps1
```

The signed APK is copied to `release-assets/`.
