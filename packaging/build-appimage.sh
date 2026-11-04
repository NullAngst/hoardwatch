#!/usr/bin/env bash
# Wraps a PyInstaller onedir build (dist/Hoardwatch) into an AppImage using appimagetool.
# Used by the GitHub Actions build and for local builds. Run from the repository root.

set -euo pipefail
export LC_ALL=C

usage() {
    printf '%s\n' "Usage: $0 [-d DIST_DIR] [-o OUTPUT] [-u UPDATE_INFO]" \
        "  -d  PyInstaller onedir output (default: dist/Hoardwatch)" \
        "  -o  AppImage to write (default: Hoardwatch-linux-<arch>.AppImage)" \
        "  -u  embedded update information, e.g. gh-releases-zsync|owner|repo|latest|file.zsync" >&2
}

die() {
    printf 'build-appimage: %s\n' "$1" >&2
    exit 1
}

dist="dist/Hoardwatch"
output=""
update=""
while getopts "d:o:u:h" opt; do
    case "$opt" in
        d) dist=$OPTARG ;;
        o) output=$OPTARG ;;
        u) update=$OPTARG ;;
        h) usage; exit 0 ;;
        *) usage; exit 2 ;;
    esac
done

missing=()
for c in uname cp chmod mkdir mv rm ln mktemp dirname basename; do
    command -v "$c" >/dev/null 2>&1 || missing+=("$c")
done
if [ "${#missing[@]}" -gt 0 ]; then
    die "missing required commands: ${missing[*]}"
fi

here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/.." && pwd)

case "$(uname -m)" in
    x86_64 | amd64) arch=x86_64; label=x64 ;;
    aarch64 | arm64) arch=aarch64; label=arm64 ;;
    *) die "unsupported architecture: $(uname -m)" ;;
esac
[ -n "$output" ] || output="Hoardwatch-linux-$label.AppImage"

[ -x "$dist/Hoardwatch" ] || die "no PyInstaller build at $dist (build with --onedir first)"
[ -f "$root/assets/icon.png" ] || die "assets/icon.png not found"

# Use an appimagetool on PATH if there is one, otherwise fetch the continuous build into a cache.
tool=$(command -v appimagetool || true)
if [ -z "$tool" ]; then
    cache="${XDG_CACHE_HOME:-$HOME/.cache}/hoardwatch-build"
    tool="$cache/appimagetool-$arch.AppImage"
    if [ ! -x "$tool" ]; then
        url="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-$arch.AppImage"
        mkdir -p -- "$cache"
        printf 'Downloading %s\n' "$url"
        if command -v curl >/dev/null 2>&1; then
            curl -fL --retry 3 -o "$tool.part" "$url"
        elif command -v wget >/dev/null 2>&1; then
            wget -O "$tool.part" "$url"
        else
            die "appimagetool is not installed and neither curl nor wget is available to fetch it"
        fi
        chmod +x -- "$tool.part"
        mv -- "$tool.part" "$tool"
    fi
fi

appdir=$(mktemp -d "${TMPDIR:-/tmp}/hoardwatch-appdir.XXXXXX")
trap 'rm -rf -- "$appdir"' EXIT

mkdir -p -- "$appdir/usr/lib/hoardwatch" "$appdir/usr/bin" "$appdir/usr/share/applications" \
    "$appdir/usr/share/icons/hicolor/512x512/apps"
# -P keeps symlinks inside the PyInstaller bundle as symlinks.
cp -RP -- "$dist/." "$appdir/usr/lib/hoardwatch/"
ln -s -- ../lib/hoardwatch/Hoardwatch "$appdir/usr/bin/Hoardwatch"
cp -- "$here/AppRun" "$appdir/AppRun"
chmod +x -- "$appdir/AppRun"
cp -- "$here/hoardwatch.desktop" "$appdir/hoardwatch.desktop"
cp -- "$here/hoardwatch.desktop" "$appdir/usr/share/applications/hoardwatch.desktop"
cp -- "$root/assets/icon.png" "$appdir/hoardwatch.png"
cp -- "$root/assets/icon.png" "$appdir/usr/share/icons/hicolor/512x512/apps/hoardwatch.png"
ln -s -- hoardwatch.png "$appdir/.DirIcon"
# mktemp -d creates the folder as 0700. The image has to be readable by whoever runs it.
chmod -R a+rX -- "$appdir"

mkdir -p -- "$(dirname "$output")"
args=()
if [ -n "$update" ]; then
    args+=(-u "$update")
fi
# Extract-and-run lets appimagetool (itself an AppImage) work without FUSE, as on CI runners.
APPIMAGE_EXTRACT_AND_RUN=1 ARCH="$arch" "$tool" "${args[@]}" "$appdir" "$output"
chmod +x -- "$output"
# appimagetool writes the .zsync file (only made when -u is given) into the current directory.
zs="$(basename -- "$output").zsync"
if [ -f "$zs" ] && [ "$(cd "$(dirname -- "$output")" && pwd)" != "$(pwd)" ]; then
    mv -- "$zs" "$(dirname -- "$output")/$zs"
fi
printf 'Built %s\n' "$output"
