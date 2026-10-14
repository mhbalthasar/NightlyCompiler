#!/usr/bin/env bash
# build_sysroot.sh - run a consumer repo's OWN sysroot prep script and record
# what it produced.
#
# The recipe is not copied here. Daisium's tree carries libxml2/libicu/zlib and a
# cross gcc+binutils, Daisium_Predict's carries only the libc/libstdc++ pairs, and
# libOpenCyan's uses a third package set - three shapes that are NOT
# interchangeable. Copying one recipe into this repo would freeze a snapshot of
# someone else's package list, so this runs the file that lives in the consuming
# repo and publishes the tree it yields, under a family named after that repo.
#
#   usage: build_sysroot.sh <repo-dir> <arch-token>
#     repo-dir   daisium | dp | opencyan   (the checkout name from envpkgs.conf)
#     arch-token x86_64 | i386 | aarch64 | loongarch64 | arm64 | loong64
#
# Pins (glibc floor, triplet, prep-script hash) go to $ENVPKG_PINS so the release
# records which revision of the consuming repo's script the tree came from: the
# number a human compares against their own HEAD when deciding to re-run the
# producer.
set -euo pipefail
REPO="${1:?usage: build_sysroot.sh <repo-dir> <arch-token>}"
ARCH="${2:?usage: build_sysroot.sh <repo-dir> <arch-token>}"
TOOLS="$REPO/compile_scripts/tools"
PINS="${ENVPKG_PINS:-$HOME/envpkg-pins.txt}"
mkdir -p "$(dirname "$PINS")"; : > "$PINS"

case "$REPO" in
  daisium|dp)
    case "$ARCH" in
      x86_64|i386|aarch64|loongarch64) ;;
      *) echo "[envpkg] $REPO has no sysroot for '$ARCH'" >&2; exit 2 ;;
    esac
    SCRIPT="$TOOLS/prepare_${ARCH}_sysroot.sh"
    DEST="$HOME/tools/sysroots/$ARCH/root"
    [ -x "$SCRIPT" ] || chmod +x "$SCRIPT" 2>/dev/null || true
    bash "$SCRIPT"
    ;;
  opencyan)
    case "$ARCH" in
      i386|arm64|loong64) ;;
      *) echo "[envpkg] libOpenCyan has no sysroot for '$ARCH'" >&2; exit 2 ;;
    esac
    SCRIPT="$TOOLS/prepare_debian_sysroot.sh"
    DEST="$HOME/sysroot-$ARCH"
    bash "$SCRIPT" "$ARCH" "$DEST"
    ;;
  *)
    echo "[envpkg] unknown repo dir '$REPO' (expected daisium | dp | opencyan)" >&2
    exit 2
    ;;
esac

[ -d "$DEST" ] || { echo "[envpkg] $SCRIPT did not leave a tree at $DEST" >&2; exit 1; }

# What actually ended up in the tree, so a rebuild that silently pulled a
# different sid snapshot is visible without unpacking 200 MB.
printf 'prep_script=%s\n' "$SCRIPT" >> "$PINS"
printf 'prep_sha256=%s\n' "$(sha256sum "$SCRIPT" | cut -d' ' -f1)" >> "$PINS"
printf 'tree_bytes=%s\n' "$(du -sb "$DEST" | cut -f1)" >> "$PINS"
MA="$(find "$DEST/usr/lib" -maxdepth 1 -type d -name '*-linux-gnu*' -printf '%f\n' 2>/dev/null | sort | head -1 || true)"
printf 'multiarch=%s\n' "${MA:-none}" >> "$PINS"
LIBC="$DEST/usr/lib/${MA}/libc.so.6"
if [ -f "$LIBC" ]; then
  FLOOR="$(strings "$LIBC" | grep -o 'GLIBC_2\.[0-9]*' | sort -Vu | tail -1)"
  printf 'glibc_floor=%s\n' "${FLOOR:-unknown}" >> "$PINS"
  # The loader has to resolve inside the sysroot or nothing links. This is the
  # exact failure a packaged tree could carry and a marker check would miss.
  LOADER="$(sed -n 's/.*AS_NEEDED ( \(\/lib[0-9]*\/[^)]*\) ).*/\1/p' "$DEST/usr/lib/${MA}/libc.so" 2>/dev/null | head -1 || true)"
  if [ -n "${LOADER:-}" ]; then
    [ -e "$DEST$LOADER" ] || { echo "[envpkg] loader $LOADER missing inside the tree" >&2; exit 1; }
    printf 'loader=%s\n' "$LOADER" >> "$PINS"
  fi
fi
echo "[envpkg] sysroot tree: $DEST"; cat "$PINS"
