#!/usr/bin/env bash
# build_osxcross_dp.sh - build osxcross the way Daisium_Predict's ci.yml and
# Daisium's build.yml do it: plain checkout, SDK dropped into tarballs/, then
# UNATTENDED=1 + MACOSX_DEPLOYMENT_TARGET=11.0, and the tree lands in the
# checkout's own target/.
#
# It is a separate family from libOpenCyan's osxcross on purpose. That repo builds
# through prepare_osxcross.sh with TARGET_DIR/SDK_VERSION/JOBS and does NOT set
# MACOSX_DEPLOYMENT_TARGET, and current osxcross is entirely environment-variable
# driven - so the two recipes produce trees with different wrapper defaults. One
# shared blob would quietly move whichever repo did not build it onto the other's
# deployment target.
set -euo pipefail
GIT="${OSXCROSS_GIT:-https://github.com/tpoechtrager/osxcross}"
REV="${OSXCROSS_REV:-master}"
SDK_VER="${MACOS_SDK_VER:-11.3}"
SDK_URL="${MACOS_SDK_URL:-https://github.com/joseluisq/macosx-sdks/releases/download/${SDK_VER}/MacOSX${SDK_VER}.sdk.tar.xz}"
DEPLOY_TARGET="${MACOSX_DEPLOYMENT_TARGET:-11.0}"
ROOT="${OSXCROSS_ROOT_DIR:-$HOME/tools/osxcross}"
DEST="$ROOT/target"
PINS="${ENVPKG_PINS:-$HOME/envpkg-pins.txt}"
mkdir -p "$(dirname "$PINS")"; : > "$PINS"
rm -rf "$ROOT"

command -v clang >/dev/null || { echo "[envpkg] host clang missing (osxcross builds cctools/ld64 with it)" >&2; exit 1; }
mkdir -p "$(dirname "$ROOT")"
git -c advice.detachedHead=false clone --depth 1 --branch "$REV" "$GIT" "$ROOT"
SHA="$(git -C "$ROOT" rev-parse HEAD)"
echo "[envpkg] osxcross $GIT @ $REV ($SHA), SDK $SDK_VER, deployment target $DEPLOY_TARGET"

mkdir -p "$ROOT/tarballs"
curl -fL --retry 3 --connect-timeout 30 -o "$ROOT/tarballs/$(basename "$SDK_URL")" "$SDK_URL"
# find -print -quit, not `ls | head`: the runner shell is `bash -e -o pipefail`,
# and head exiting early makes ls die on SIGPIPE and fails the step over a listing.
TARBALL="$(find "$ROOT/tarballs" -maxdepth 1 -name 'MacOSX*.sdk.tar.*' -print -quit)"
# The version that matters is the one in the file name, not the one we asked for:
# a mirror that redirects still has to give a tree matching the family name.
GOT_VER="$(printf '%s' "$(basename "$TARBALL")" | sed -n 's/^MacOSX\([0-9.]*\)\.sdk\.tar\..*/\1/p')"
[ "$GOT_VER" = "$SDK_VER" ] || { echo "[envpkg] asked SDK $SDK_VER, got '${GOT_VER:-unparsable}' - family name would lie" >&2; exit 1; }

( cd "$ROOT" && UNATTENDED=1 MACOSX_DEPLOYMENT_TARGET="$DEPLOY_TARGET" \
    CC="$(command -v clang)" CXX="$(command -v clang++)" \
    ./build.sh ) > /tmp/osxcross_build.log 2>&1 || { tail -40 /tmp/osxcross_build.log >&2; exit 1; }
[ -d "$DEST/bin" ] || { echo "[envpkg] build.sh left no target/bin under $ROOT" >&2; ls "$ROOT" >&2; exit 1; }

{
  printf 'osxcross_git=%s\n'   "$GIT"
  printf 'osxcross_rev=%s\n'   "$REV"
  printf 'osxcross_sha=%s\n'   "$SHA"
  printf 'macos_sdk_ver=%s\n'  "$SDK_VER"
  printf 'sdk_tarball_sha256=%s\n' "$(sha256sum "$TARBALL" | cut -d' ' -f1)"
  printf 'deployment_target=%s\n' "$DEPLOY_TARGET"
  printf 'tree_bytes=%s\n'     "$(du -sb "$DEST" | cut -f1)"
} >> "$PINS"
echo "[envpkg] osxcross (dp layout) at $DEST"; cat "$PINS"
