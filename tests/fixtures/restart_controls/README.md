# Optional local Pikmin map controls

These private PNGs are excluded from Git because even cropped map roads can
reveal a location. The five tests that use them skip when they are absent;
the synthetic image tests remain available in a fresh checkout.

Local fixtures retain the lower 20% (468 × 1080 px) of phone screenshots.
Tests place them at the bottom of a neutral 2340 × 1080 px frame. Keep any
locally supplied captures in this ignored directory.

- `map_3d.png`: a 3D map capture.
  The pale green tabs defeated the previous broad white-pixel comparison.
  Flowers and a road also satisfy the old loading-progress heuristic.
- `map_2d.png`: cropped from an existing 2D map capture to check that the same
  selected-tab detector identifies the other view correctly.

The retained band includes both the loading detector's region and the bottom
controls; the rest of the screen, status bar and player avatars are omitted.
