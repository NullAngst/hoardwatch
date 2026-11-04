# Changes

2026.11.04 - Linux AppImage / UX overhaul

* Linux builds are now AppImages (`Hoardwatch-linux-x64.AppImage`, `Hoardwatch-linux-arm64.AppImage`) in place of the tar.gz archives, built by GitHub Actions with appimagetool.
* Each Linux release ships a `.zsync` file with update information embedded in the AppImage, so AppImageUpdate and Gear Lever can update it in place.
* Added `packaging/` with the AppImage build script, AppRun and the desktop entry. The same script builds an AppImage locally.
* Linux PyInstaller builds switched from onefile to onedir, which also removes the extract-to-/tmp step at every launch.
* Child programs (ffmpeg, deno, gallery-dl, xdg-open, the file manager) no longer inherit the bundled library path, which could crash them against the wrong libssl or libstdc++.
* Portable mode works from an AppImage: put `hoardwatch-data` next to the .AppImage file.
* New menu bar (File, Archive, View, Tools, Help) and keyboard shortcuts for the common actions.
* Header for the selected archive with its folder, item totals, last check time and auto-check interval.
* Welcome screen when there are no archives, and empty-state hints in the tables.
* Right-click menus for archives and sources, not only items. Sources can be included or excluded from checks, opened, copied and jumped to in the Items tab.
* Click any column heading to sort; the sort survives refreshes. Table rows are striped.
* Items search filters as you type. Added a "missed" status filter, a Reset button and a per-status count.
* Progress bar and a disabled Stop button while idle. The Activity tab shows the number of running downloads.
* Tooltips on the main buttons. Settings tabs scroll on small screens. Number fields are spin boxes.
* Themes: Hoardwatch (the original look), Light, Dark, Verdigris, Kawaii Light and Kawaii Dark, switched live from View > Theme.
* The window size, sidebar width and theme are remembered between runs.
* New archive with a blank folder creates one under ~/Hoardwatch, then goes straight to adding sources. Adding sources offers to check them right away.
* Settings > Save keeps an account that was filled in but not saved with "Save account".
* Log is monospaced, has a horizontal scrollbar, Wrap lines and Copy all, and stops jumping to the bottom while you scroll back.
* Enter submits dialogs, Escape cancels.
* Fixed the checksum step's glob in the release job (shellcheck SC2035).

Reasons for changes: Linux users get a single file that runs on any distribution with glibc 2.35 or newer and integrates with AppImage tools. The UI needed less hunting: actions were only reachable from one place, empty tables gave no hint what to do next, and there was no way to sort or see what an archive's state was at a glance.
