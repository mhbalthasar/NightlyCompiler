#!/usr/bin/env bash
# build_osxcross_opencyan.sh - build osxcross through libOpenCyan's own
# prepare_osxcross.sh, which drives TARGET_DIR/SDK_VERSION and installs the tool
# tree directly at the prefix (that repo's OSXCROSS_ROOT points straight at it).
#
# The recipe stays owned by libOpenCyan: this runs its file, so the packaged tree
# is whatever its current master produces, and a change there shows up as a new
# prep_sha256 pin rather than as a silent divergence between two copies.
set -euo pipefail
SDK_VER="${MACOS_SDK_VER:-11.3}"
DEST="${ENVPKG_DEST:-$HOME/osxcross-target}"
PREP="${OSXCROSS_PREP:-opencyan/compile_scripts/tools/prepare_osxcross.sh}"
PINS="${ENVPKG_PINS:-$HOME/envpkg-pins.txt}"
mkdir -p "$(dirname "$PINS")"; : > "$PINS"
rm -rf "$DEST"

[ -f "$PREP" ] || { echo "[envpkg] $PREP not found - was the libOpenCyan checkout cloned?" >&2; exit 1; }
chmod +x "$PREP" 2>/dev/null || true
# Host clang/lzma/ssl dev headers are what osxcross itself compiles cctools+ld64
# against; the workflow's install step provides them, this only fails loudly if
# they are not there, because the resulting make errors name nothing useful.
command -v clang >/dev/null || { echo "[envpkg] host clang missing" >&2; exit 1; }
MACOS_SDK_VER="$SDK_VER" "$PREP" "$DEST"
# The same script's own gate, run against the tree we are about to publish.
"$PREP" "$DEST" --verify-only

SHA="$(git -C "$(dirname "$PREP")/../.." rev-parse HEAD 2>/dev/null || echo unknown)"
{
  printf 'prep_script=%s\n'    "$PREP"
  printf 'prep_sha256=%s\n'    "$(sha256sum "$PREP" | cut -d' ' -f1)"
  printf 'libopencyan_sha=%s\n' "$SHA"
  printf 'macos_sdk_ver=%s\n'  "$SDK_VER"
  printf 'osxcross_rev=%s\n'   "${OSXCROSS_REV:-master}"
  printf 'tree_bytes=%s\n'     "$(du -sb "$DEST" | cut -f1)"
} >> "$PINS"
echo "[envpkg] osxcross (libOpenCyan layout) at $DEST"; cat "$PINS"
