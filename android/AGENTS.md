# Android release packaging

Never distribute `app-debug.apk`. Its Android debug certificate cannot update
existing LocWarp installations.

Always build the companion app with:

```powershell
.\tools\build-android-release.ps1
```

The script builds the release variant from the repository root, verifies
package/version metadata, and requires the certificate SHA-256 fingerprint
documented in `signing/README.md`. A release build must fail if the local
signing setup is missing or incorrect.
