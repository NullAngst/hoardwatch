# Maintenance

A checklist for keeping Hoardwatch working. Run through it every couple of months, and any time a site stops working or a GitHub Actions build fails.

## 1. Check that the build still runs

1. On GitHub, open **Actions > Build and release > Run workflow**, leave the version blank, and run it. That builds every platform without publishing a release.
2. Every job should go green. The "Smoke test" step runs `--version` and `--list` on the macOS app and the Linux AppImages, so a green run means yt-dlp got bundled correctly.
3. Download the `Hoardwatch-linux-x64` artifact, `chmod +x` the AppImage, and open it. Click through the tabs, open Settings and Tools, and switch a theme or two.

If a job fails, the usual causes are below.

## 2. Things that break, and where to look

**A site stops listing or downloading.** Almost always yt-dlp or gallery-dl. In the app, Tools > Update next to yt-dlp, restart, and try again. Tools > Install or update next to gallery-dl does the same for gallery-dl. If that fixes it, cut a new release (step 4) so the bundled yt-dlp is current too.

**GitHub Actions warns that an action is deprecated.** The workflow uses major version tags (`actions/checkout@v4`, `actions/setup-python@v5`, `actions/upload-artifact@v4`, `actions/download-artifact@v4`, `softprops/action-gh-release@v2`). These follow updates within a major version on their own. When GitHub announces a new major version, bump the number in `.github/workflows/build.yml` and run the workflow once to confirm.

**A runner image is retired.** The Linux jobs run on `ubuntu-22.04` and `ubuntu-22.04-arm` on purpose: an AppImage only runs on systems with the same or newer glibc than the build machine, so the oldest supported Ubuntu gives the widest compatibility. When GitHub announces 22.04's retirement, move both to the next oldest LTS (24.04) and update the glibc line in the README's Download table (24.04 is glibc 2.39). `windows-latest` and `macos-14` follow GitHub's schedule; switch `macos-14` to a newer `macos-*` label when it is retired.

**The AppImage step fails.** `packaging/build-appimage.sh` downloads appimagetool from `https://github.com/AppImage/appimagetool/releases/download/continuous/`. If that URL moves, update it in the script. appimagetool also downloads its runtime from the AppImage/type2-runtime project at build time; if that fails, it says so in the log. To test locally: `shellcheck packaging/build-appimage.sh`, then follow "To build the AppImage locally" in the README.

**PyInstaller fails or the app crashes at start.** Check the PyInstaller changelog for the version the build installed (`pip install pyinstaller` always takes the newest). The `--collect-all yt_dlp_ejs` flag matters: without it, YouTube's JavaScript challenge solver is missing.

**Python version.** The workflow uses Python 3.12. Once a year, check that the newest Python works with yt-dlp, PyInstaller and Tk, and move up when it does.

## 3. Check the tools Hoardwatch installs

Tools > Install or update fetches these from their release pages. If a download fails, the release asset name probably changed. The matching patterns live in `install_tool()` in `hoardwatch.py`.

- ffmpeg: https://github.com/yt-dlp/FFmpeg-Builds/releases
- deno: https://github.com/denoland/deno/releases
- gallery-dl: https://github.com/mikf/gallery-dl/releases

## 4. Release

1. Add an entry to `changes.md`.
2. **Actions > Build and release > Run workflow**, enter the next version (`v1.2.0`), run it. The version is stamped into the app from the tag.
3. Check the release page lists the Windows .exe, the macOS .zip, two AppImages, two `.zsync` files and `SHA256SUMS.txt`.

## 5. Quick health check of the code

From the repository root:

```bash
python3 -m pyflakes hoardwatch.py
shellcheck packaging/build-appimage.sh
shellcheck -s sh packaging/AppRun
python3 hoardwatch.py --version
```

All four should be quiet apart from the version line. Install pyflakes with `pip install pyflakes` and shellcheck from your package manager.
