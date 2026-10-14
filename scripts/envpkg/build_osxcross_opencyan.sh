#!/usr/bin/env bash
# build_osxcross_opencyan.sh - build osxcross through libOpenCyan's own
# prepare_osxcross.sh, which drives TARGET_DIR/SDK_VERSION and installs the tool
# tree directly at the prefix (that repo's OSXCROSS_ROOT points straight at it).
#
# The recipe is vendored, not rewritten: scripts/envpkg/prepare_osxcross.sh is a
# byte-for-byte copy of libOpenCyan's tools/prepare_osxcross.sh, pinned by sha256
# in the package table and re-checked by test_envpkgs_table.py. Vendoring is what
# lets this CI build the tree without read access to that repo; the pin is what
# keeps the copy from silently becoming a second, different recipe. When upstream
# changes, re-copy and re-dispatch - do not edit the copy here.
set -euo pipefail
SDK_VER="${MACOS_SDK_VER:-11.3}"
DEST="${ENVPKG_DEST:-$HOME/osxcross-target}"
# The recipe is a verbatim copy of libOpenCyan's tools/prepare_osxcross.sh, held
# here as scripts/envpkg/prepare_osxcross.sh and pinned by sha256 in the package
# table: that repo is private and this CI's token cannot read it, and a recipe
# rewrite here would drift from theirs within a change. scripts/
# test_envpkgs_table.py re-checks both hashes on every run.
PREP="${OSXCROSS_PREP:-scripts/envpkg/prepare_osxcross.sh}"
PINS="${ENVPKG_PINS:-$HOME/envpkg-pins.txt}"
mkdir -p "$(dirname "$PINS")"; : > "$PINS"
rm -rf "$DEST"

[ -f "$PREP" ] || { echo "[envpkg] $PREP missing - the vendored osxcross prep script is not in this repo" >&2; exit 1; }
chmod +x "$PREP" 2>/dev/null || true
# Host clang/lzma/ssl dev headers are what osxcross itself compiles cctools+ld64
# against; the workflow's install step provides them, this only fails loudly if
# they are not there, because the resulting make errors name nothing useful.
command -v clang >/dev/null || { echo "[envpkg] host clang missing" >&2; exit 1; }
MACOS_SDK_VER="$SDK_VER" "$PREP" "$DEST"
# The same script's own gate, run against the tree we are about to publish.
"$PREP" "$DEST" --verify-only

{
  printf 'prep_script=%s\n'    "$PREP"
  printf 'prep_sha256=%s\n'    "$(sha256sum "$PREP" | cut -d' ' -f1)"
  printf 'macos_sdk_ver=%s\n'  "$SDK_VER"
  printf 'osxcross_rev=%s\n'   "${OSXCROSS_REV:-master}"
  printf 'tree_bytes=%s\n'     "$(du -sb "$DEST" | cut -f1)"
} >> "$PINS"
echo "[envpkg] osxcross (libOpenCyan layout) at $DEST"; cat "$PINS"
