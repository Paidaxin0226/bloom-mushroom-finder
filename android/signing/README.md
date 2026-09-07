# LocWarp Android signing

This directory is the canonical local location for Android release signing.

Local, Git-ignored files:

- `locwarp-release.keystore`
- `signing.properties`

Tracked files describe the required setup without committing secrets.

Release identity:

- Package: `com.locwarp.mock`
- Key alias: `locwarpdebug`
- Certificate SHA-256:
  `0832b67396143e7bd420dce24aa172143e38512c3beb93ffeedf9fb3a19585ec`

Packaging rule:

```powershell
.\tools\build-android-release.ps1
```

The release script also selects JDK 17 and uses the repository's Gradle Wrapper.
The release build runs unit tests and release lint before assembling the APK.

Do not provide `app-debug.apk` to users. Only the APK produced by the release
script can update LocWarp installations carrying the certificate above,
including releases 0.2.95 through 0.2.159.
