# Keep the native-to-Java bridge used by the bundled Leaflet WebView.
-keepclassmembers class * {
    @android.webkit.JavascriptInterface <methods>;
}

# LSPosed resolves this class by the literal name stored in assets/xposed_init.
-keep class com.locwarp.mock.LocWarpXposedHook { *; }
