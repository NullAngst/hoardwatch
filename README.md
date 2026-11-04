<p align="center"><img src="assets/icon.png" width="128" alt="Hoardwatch icon"></p>

<h1 align="center">Hoardwatch</h1>

<p align="center">A bulk media archiver that remembers what it has seen.</p>

Hoardwatch downloads everything from accounts, channels, playlists and single posts, and keeps a ledger of every item each source has ever listed. Press **Check** and it lists each source again, downloads whatever you do not have yet, and records anything that has disappeared upstream since the last check.

The original use case: archiving accounts that post and delete often, such as government or agency social media, so the archive stays an accurate record of what was published.

![Hoardwatch items view](docs/screenshot.png)

yt-dlp does the listing and downloading for most sites, running inside the app. gallery-dl is an optional second backend for image-first sites (Instagram, X, Reddit and others) and for profiles yt-dlp cannot list.

## Features

- **Archives with memory.** Each archive is a folder with a SQLite ledger inside it (`.hoardwatch/archive.db`). Move the folder to another machine, open it, and the full history comes with it.
- **Deletion tracking.** Items that vanish from a source are marked "deleted upstream" with a timestamp. If you saved them first, you keep the file and the record. If they were deleted before you got them, they show as "missed".
- **Content-aware listing.** Give it a channel, profile, playlist or post URL. Hoardwatch walks nested listings (for example a YouTube channel's Videos, Shorts and Live tabs), turns them into individual items, and downloads them one at a time.
- **Full yt-dlp options.** Common settings have fields. Anything else goes in "Extra yt-dlp arguments" as normal command-line flags, parsed by yt-dlp's own option parser, so every yt-dlp option is available.
- **Rate limits.** Randomized delay between items (shared across parallel downloads), delay between sources, delay between requests, bandwidth cap, a per-check download cap, and an automatic pause after HTTP 429 or bot checks.
- **SOCKS5 proxies.** A list of proxies with round-robin, random or per-source sticky rotation, failover to the next proxy, a kill switch that refuses direct connections, and a tester that shows each proxy's exit IP.
- **Accounts.** Reusable account profiles with a cookies.txt file, cookies read from a browser, or username and password. Assign one per archive or per source.
- **Link grabber.** Paste any text and it extracts every link and shows which backend handles it. "Scan pages" reads web pages and finds linked or embedded media. Optional clipboard watching.
- **Scheduling.** Per-archive auto-check interval while the app is open, plus a headless `--check` mode for cron or systemd timers.
- **Built-in tool manager.** Installs or updates ffmpeg, deno and gallery-dl, and updates yt-dlp itself without waiting for a new Hoardwatch release.
- **Exportable record.** Export the ledger to CSV. Every check also writes a plain-text log into the archive folder.

## Download

Get the latest build from the [Releases](../../releases) page.

| Platform | File | Notes |
|---|---|---|
| Windows 10/11 x64 | `Hoardwatch-windows-x64.exe` | Unsigned, so SmartScreen will warn. Click "More info", then "Run anyway". |
| Linux x64 | `Hoardwatch-linux-x64.AppImage` | Built on Ubuntu 22.04, needs glibc 2.35 or newer. |
| Linux arm64 | `Hoardwatch-linux-arm64.AppImage` | Same glibc requirement. |
| macOS Apple silicon | `Hoardwatch-macos-arm64.zip` | Unsigned. Right-click the app, choose Open, then Open again. Or run `xattr -dr com.apple.quarantine Hoardwatch.app`. |

Linux:

```bash
chmod +x Hoardwatch-linux-x64.AppImage
./Hoardwatch-linux-x64.AppImage
```

I keep mine in `~/Applications`, since that's where AppImageLauncher and Gear Lever look. Put it wherever you like. Either of those tools also adds a menu entry with the icon.

AppImages need FUSE 2 to mount themselves. Most desktops have it. If you get a "dlopen(): error loading libfuse.so.2" message, install it (`sudo apt install libfuse2t64` on Ubuntu 24.04 and newer, `libfuse2` on older Debian and Ubuntu, `fuse-libs` on Fedora, `fuse2` on Arch, or whichever your distribution calls it), or run it without FUSE:

```bash
./Hoardwatch-linux-x64.AppImage --appimage-extract-and-run
```

Each Linux release also has a `.zsync` file, so AppImageUpdate and Gear Lever can update the AppImage by downloading only the parts that changed.

Intel Macs and older Linux distributions can run it from source (see below). `SHA256SUMS.txt` in each release lists checksums for every file.

## First run: tools

Open **Tools** and install what you need. Each tool is downloaded from its official release page into Hoardwatch's data folder. You can also point to your own copies, or let Hoardwatch find them on your PATH.

| Tool | Needed for | Without it |
|---|---|---|
| ffmpeg | Merging separate video and audio streams, embedding metadata | yt-dlp falls back to lower-quality single-file formats |
| deno | Full YouTube support (yt-dlp requires a JavaScript runtime since late 2025) | Many YouTube formats are missing or downloads fail |
| gallery-dl | Instagram, X, Reddit, Tumblr, Pinterest, Bluesky and similar sites | Those profiles usually cannot be listed |

On macOS, the Tools button can install deno but not ffmpeg or gallery-dl, since neither publishes a standalone macOS build. Use `brew install ffmpeg gallery-dl`. On Linux arm64, install gallery-dl with `pip install -U gallery-dl` or your package manager.

The **Update** button next to yt-dlp installs the newest yt-dlp and yt-dlp-ejs from PyPI into the data folder. Restart to use it. "Use bundled" removes the update. Site extractors break often, so this is the first thing to try when a site stops working.

## Quick start

1. **New** (left panel): name the archive and pick an empty folder.
2. **Sources > Add**: paste one or more URLs, one per line.
3. **Check archive**: Hoardwatch lists each source, records every item, and downloads what is missing.
4. Run **Check archive** again whenever you want, or set an auto-check interval with **Edit**. Each check downloads only what is new or missing and records anything that disappeared.

"Scan selected only" and "Scan whole archive" update the ledger without downloading anything. That is a cheap way to track deletions frequently while downloading less often.

## How tracking works

Every item in the ledger has a download status and an upstream status.

| Status | Meaning |
|---|---|
| pending | Listed by the source, not downloaded yet |
| done | Downloaded; the file path is recorded |
| failed | Last attempt failed with a retryable error. Retried on every check. |
| unavailable | Private, removed, members-only or similar. Not retried unless you enable it in Settings > Tracking. |
| skipped | yt-dlp downloaded nothing, usually because a filter you set excluded it |
| missed | Deleted upstream before it was downloaded |

| Online column | Meaning |
|---|---|
| yes | Present in the most recent complete listing |
| deleted | Absent from the required number of consecutive complete listings |

Items are identified the same way yt-dlp's download archive identifies them (`youtube dQw4w9WgXcQ`), so the same video under two sources is only downloaded once.

Deletion tracking is deliberately conservative, because a wrong "deleted" mark is worse than a late one:

- A listing that hit any error is treated as incomplete and marks nothing deleted.
- A listing that suddenly shrinks by more than 50% (adjustable) compared with the previous one is treated as a partial listing and marks nothing deleted. Rate limits and login walls often produce short listings without raising an error.
- "Mark deleted after" (default 1) sets how many complete listings an item must be missing from before it is marked. Raise it to 2 or 3 for sites with flaky pagination.
- If a single-post source starts returning "removed", "private" or 404, that post is marked deleted.
- If an item reappears, it goes back to "yes".
- At the start of each check, files that were downloaded but are no longer on disk are queued again (Settings > Tracking).

## Sources and backends

Each source uses a backend: **Automatic** (default), **yt-dlp** or **gallery-dl**.

Automatic uses gallery-dl for image-first sites when gallery-dl is installed. For everything else it uses yt-dlp, and falls back to gallery-dl only when yt-dlp does not support the URL. It never falls back on a 404, removal notice, rate limit or network error, because those say something about the content.

What to expect, as of this release. Site support changes constantly, so treat this as a starting point:

| Site | Works well with | Notes |
|---|---|---|
| YouTube channels, playlists, videos | yt-dlp | Needs deno. Heavy use from one IP triggers "confirm you're not a bot"; use cookies and conservative delays. |
| TikTok profiles and videos | yt-dlp | Profile listing has broken and been fixed repeatedly upstream. Update yt-dlp first if it stops working. |
| Instagram profiles and posts | gallery-dl | Requires a logged-in account's cookies. Use a throwaway account and slow request delays. |
| X / Twitter profiles and posts | gallery-dl | Requires cookies from a logged-in session. |
| Reddit, Tumblr, Bluesky, Pinterest, Flickr, Imgur, DeviantArt | gallery-dl | Mostly works without login. |
| Twitch, Vimeo, Rumble, SoundCloud, Bandcamp and over a thousand other sites | yt-dlp | Run `yt-dlp --list-extractors` for the full list. |
| Arbitrary web pages | yt-dlp generic, or Link grabber > Scan pages | Scan pages finds links on a page that a real extractor supports. |

gallery-dl files are matched to ledger entries through the `.json` metadata gallery-dl writes next to each file. Leave "Write gallery-dl metadata" on or those items will stay listed as pending even after they download.

## Rate limits

Settings > Rate limits. Defaults are moderate; tune them per site.

- **Delay between items**: a random delay between min and max seconds before each download starts. The spacing is shared across all parallel downloads, so 2 parallel downloads with a 4 to 8 second delay still start at most one item every 4 to 8 seconds.
- **Delay between sources**: seconds between one source and the next.
- **Delay between requests**: passed to yt-dlp (`--sleep-requests`) and gallery-dl (`--sleep-request`). This is the setting that matters most for Instagram and TikTok.
- **Bandwidth limit per download**: for example `2M`. With parallel downloads the total is this times the parallel count.
- **Max downloads per check**: 0 is unlimited. The rest wait for the next check.
- **Pause after rate limiting**: after HTTP 429 or a bot check, no new requests are made for this many minutes and the current proxy is rotated out.

## Proxies (SOCKS5, Mullvad)

Settings > Proxies. One proxy per line.

```
socks5h://10.64.0.1:1080
socks5h://se-got-wg-socks5-001.relays.mullvad.net:1080
socks5h://de-fra-wg-socks5-003.relays.mullvad.net:1080
# lines starting with # are ignored
socks5h://user:pass@proxy.example.net:1080
```

Use `socks5h://` rather than `socks5://`. The `h` sends DNS lookups through the proxy; plain `socks5://` resolves names locally, which leaks the sites you visit to your DNS resolver.

For Mullvad, connect the Mullvad app first. `10.64.0.1:1080` is the proxy on the server you are connected to. The `*.relays.mullvad.net` proxies route through other Mullvad servers, which gives you several exit IPs from one tunnel. Current relay hostnames are listed on Mullvad's server page.

Rotation modes: `round_robin` changes proxy for each item, `random` picks randomly, `sticky` keeps one proxy per source (better for sites that tie sessions to an IP, such as Instagram), and `off` ignores the list.

A proxy that fails is benched for 5 minutes and the item is retried on the next one. If every proxy is benched, the check stops. It does not fall back to your real connection unless you turn on "Fall back to a direct connection".

What goes through the proxy: all yt-dlp listing and downloading, all gallery-dl traffic, page scans, and tool and yt-dlp updates (through the first proxy in the list). What does not: downloads yt-dlp hands off to ffmpeg, which happens for some live streams and a few HLS sources, because ffmpeg has no SOCKS support. If that matters, run Hoardwatch inside the VPN tunnel itself rather than relying on the proxy list.

**Test proxies** fetches the test URL through each proxy and shows the exit IP and location. The default test URL is Mullvad's connection check, which also reports whether the exit is a Mullvad server.

## Accounts, cookies and logins

Settings > Accounts. Logins are optional; most public content needs none.

An account profile can use:

- **Cookies file**: a Netscape-format `cookies.txt` exported from a logged-in browser with an extension such as "Get cookies.txt LOCALLY". The most reliable option.
- **Browser cookies**: read directly from a browser profile, for example `firefox`, `chrome:Profile 1` or `firefox:/path/to/profile`. On Windows, Chromium-based browsers often need to be closed first.
- **Username and password**: only a handful of sites accept these in yt-dlp. gallery-dl accepts them for more sites.

Pick a default account per archive (Edit archive) and override it per source.

Passwords are stored in plain text in `config.json` in the data folder. On Linux and macOS the file is restricted to your user (mode 600). Prefer cookies, and use separate accounts for scraping; sites ban accounts that pull whole profiles.

## Using any yt-dlp option

Settings > Downloads > Extra yt-dlp arguments takes normal yt-dlp flags. They are applied after all other settings, so they win. Examples:

```
--match-filters "!is_live & duration < 7200"
--no-mtime --restrict-filenames
--extractor-args "youtube:player_client=default,-tv"
--impersonate chrome
--dateafter 20240101
```

Each archive can add its own extra arguments and its own output template (Edit archive). Your normal yt-dlp config file is ignored unless you enable "Read my yt-dlp config files", so archives behave the same on every machine.

The default output template is `%(upload_date>%Y-%m-%d|undated)s %(title).150B [%(id)s].%(ext)s`, placed inside each source's subfolder. By default, metadata JSON, descriptions, thumbnails and subtitles are saved next to each video, which is what you want for an archival record.

## Link grabber

Paste anything into the Link grabber tab: a list of URLs, an email, a web page's source. **Find links** extracts every URL and labels it with the extractor that will handle it. **Scan pages** downloads each pasted page and pulls out links and embeds that a real extractor supports. Select rows and add them to an archive as sources, optionally all into one shared subfolder.

With **Watch clipboard** on, any link you copy anywhere appears in the grabber.

## Unattended checks

Auto-check (Edit archive) runs while the app is open. For a server or a machine that is not always running the GUI, use headless mode:

```bash
Hoardwatch --list                      # archive names and folders
Hoardwatch --check "Agency archive"    # by name
Hoardwatch --check /srv/archives/agency
Hoardwatch --check all
Hoardwatch --check all --scan-only     # update the ledger, download nothing
```

The exit code is 0 when everything succeeded and 1 when any item or source failed. Headless mode does not need a display. On Linux, `Hoardwatch` in these examples means the path to your AppImage, for example `~/Applications/Hoardwatch-linux-x64.AppImage`.

A systemd user timer that checks every archive hourly:

```ini
# ~/.config/systemd/user/hoardwatch.service
[Unit]
Description=Hoardwatch archive check

[Service]
Type=oneshot
ExecStart=%h/Applications/Hoardwatch-linux-x64.AppImage --check all
```

```ini
# ~/.config/systemd/user/hoardwatch.timer
[Unit]
Description=Run Hoardwatch hourly

[Timer]
OnCalendar=hourly
RandomizedDelaySec=10m
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now hoardwatch.timer
loginctl enable-linger "$USER"   # keep it running while logged out
```

The GUI and a headless run should not check the same archive at the same time. SQLite will keep the ledger consistent, but both will download the same missing items.

## Where things are stored

Inside each archive folder:

```
Agency archive/
  agency YouTube/                  one subfolder per source (configurable)
    2026-01-14 Press briefing [abc123].mp4
    2026-01-14 Press briefing [abc123].info.json
  agency Instagram/instagram/...   gallery-dl's own layout
  .hoardwatch/
    archive.db                     the ledger
    logs/check-20260114-0900.log   one log per check
```

App data (settings, accounts, installed tools, yt-dlp updates):

| OS | Location |
|---|---|
| Windows | `%APPDATA%\Hoardwatch` |
| macOS | `~/Library/Application Support/Hoardwatch` |
| Linux | `$XDG_DATA_HOME/Hoardwatch`, usually `~/.local/share/Hoardwatch` |

**Portable mode**: create a folder named `hoardwatch-data` next to the executable (or next to the `.AppImage` file) and Hoardwatch keeps everything there instead.

## Interface

The window has the archive list on the left and, for the selected archive, a header with its folder, totals and last check, then four tabs: Sources, Items, Link grabber and Activity. Right-click an archive, a source or an item for everything you can do with it. Click any column heading to sort by it, click again to reverse.

The Items tab filters as you type. "missed" in the status filter shows items deleted upstream before they were saved.

View > Theme switches between Hoardwatch (the default), Light, Dark, Verdigris, Kawaii Light and Kawaii Dark. The Kawaii themes use a rounded font (M PLUS Rounded 1c, Comfortaa, Nunito, Quicksand or Varela Round) when one is installed and the normal font when not. The window size, the sidebar width and the theme are remembered.

| Shortcut | Action |
|---|---|
| Ctrl+N | New archive |
| Ctrl+O | Open an archive folder |
| Ctrl+R | Check the selected archive |
| Ctrl+Shift+S | Scan the selected archive without downloading |
| Ctrl+Shift+R | Check all archives |
| Ctrl+Shift+A | Add sources |
| Ctrl+F | Search items |
| Ctrl+1 to Ctrl+4 | Switch tabs |
| Ctrl+, | Settings |
| F5 | Refresh |
| Ctrl+Q | Quit |

## Run from source

Python 3.10 or newer with Tk.

```bash
git clone https://github.com/NullAngst/Hoardwatch
cd Hoardwatch
python3 -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python hoardwatch.py
```

If Tk is missing: `sudo zypper install python3-tk` (openSUSE), `sudo apt install python3-tk` (Debian and Ubuntu), `sudo dnf install python3-tkinter` (Fedora).

## Building and releasing

`.github/workflows/build.yml` builds with PyInstaller for Windows x64, Linux x64, Linux arm64 and macOS Apple silicon, runs a smoke test, and publishes a release with checksums. On Linux the PyInstaller folder build is wrapped into an AppImage by `packaging/build-appimage.sh`, which uses [appimagetool](https://github.com/AppImage/appimagetool).

From the GitHub website: **Actions > Build and release > Run workflow**, enter a version such as `v1.0.0`, and run. The workflow creates the tag and the release. Leaving the version blank builds the files as downloadable workflow artifacts without publishing a release. Pushing a tag that starts with `v` also triggers a release.

The version number shown in the app is stamped from the tag at build time.

To build locally:

```bash
pip install -r requirements.txt pyinstaller pillow
pyinstaller --onefile --windowed --name Hoardwatch --icon assets/icon.png \
  --add-data "assets/icon.png:assets" --add-data "assets/icon.ico:assets" \
  --collect-all yt_dlp_ejs hoardwatch.py
```

On Windows use `;` instead of `:` in `--add-data` and `assets/icon.ico` for `--icon`.

To build the AppImage locally, build a folder instead of a single file, then wrap it:

```bash
pyinstaller --noconfirm --clean --onedir --windowed --name Hoardwatch --icon assets/icon.png \
  --add-data "assets/icon.png:assets" --add-data "assets/icon.ico:assets" \
  --collect-all yt_dlp_ejs hoardwatch.py
packaging/build-appimage.sh
```

The script uses `appimagetool` from your PATH, or downloads the current build into `~/.cache/hoardwatch-build` if it is not installed. The AppImage lands in the current folder as `Hoardwatch-linux-x64.AppImage` (or `-arm64`). Run `packaging/build-appimage.sh -h` for the options.

## Limitations

- Hoardwatch can only record what a site's listing shows. If a platform hides older posts from its listing, those posts can look deleted. The partial-listing guard and the "Mark deleted after" threshold reduce false marks but cannot eliminate them. Treat "deleted upstream" as strong evidence to verify, not proof.
- Deletion is only detected at check time. Something posted and deleted between two checks is never seen. Check more often (scan-only is cheap) for accounts that delete quickly.
- Scraping whole profiles is exactly what rate limiters and bot detection look for. Expect to tune delays, use cookies and rotate proxies for Instagram, X and TikTok.
- Windows and macOS builds are unsigned. The AppImage is not GPG-signed either; check it against `SHA256SUMS.txt`.
- A yt-dlp update installed through the Tools button uses the Python libraries bundled into the Hoardwatch build. That works unless a future yt-dlp needs a new compiled dependency; in that case, update Hoardwatch itself.
- Only one check runs at a time. Parallelism happens inside a check.

## Legal

Respect the terms of service of the sites you archive and the laws where you live. Downloading content you do not have rights to may be illegal where you are. Hoardwatch is intended for preserving public records and content you are entitled to keep.

## License


It uses [yt-dlp](https://github.com/yt-dlp/yt-dlp) (Unlicense) and bundles its Python dependencies under their own licenses. [gallery-dl](https://github.com/mikf/gallery-dl) (GPL-2.0), [FFmpeg](https://ffmpeg.org) (GPL build from [yt-dlp/FFmpeg-Builds](https://github.com/yt-dlp/FFmpeg-Builds)) and [deno](https://github.com/denoland/deno) (MIT) are separate programs that Hoardwatch downloads on request and runs as external processes; they are not part of the Hoardwatch executable.
