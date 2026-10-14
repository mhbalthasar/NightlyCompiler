#!/usr/bin/env bash
# build_ollvm_loongarch.sh - source-build Hikari OLLVM 23 with the LoongArch
# backend, the one target set the prebuilt clang23 release does not ship.
#
# Verbatim the recipe Daisium_Predict's ci.yml and Daisium's build.yml run inline
# today (same projects, same target list, same -Off switches): this file exists so
# the build happens once per release instead of once per cache eviction, and so the
# result can be checked rather than trusted. The resolved upstream commit is
# recorded as a pin - clang23 is a branch, so "what did we ship" is only answerable
# after the clone.
set -euo pipefail
UPSTREAM="${OLLVM_GIT:-https://github.com/ollvm-adaplite/ollvm-clang}"
REV="${OLLVM_REV:-clang23}"
DEST="${OLLVM_INSTALL:-$HOME/tools/ollvm-built}"
WORK="${ENVPKG_WORK:-$HOME/envpkg-work}"
PINS="${ENVPKG_PINS:-$HOME/envpkg-pins.txt}"
SRC="$WORK/ollvm-src"
BUILD="$WORK/ollvm-build"
mkdir -p "$WORK" "$(dirname "$PINS")"; : > "$PINS"
rm -rf "$DEST"

[ -d "$SRC" ] || git clone --depth 1 -b "$REV" "$UPSTREAM" "$SRC"
SHA="$(git -C "$SRC" rev-parse HEAD)"
echo "[envpkg] building OLLVM $UPSTREAM @ $REV ($SHA)"
cmake -S "$SRC/llvm" -B "$BUILD" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX="$DEST" \
  -DLLVM_ENABLE_PROJECTS="clang;lld" \
  -DLLVM_TARGETS_TO_BUILD="X86;LoongArch" \
  -DLLVM_INCLUDE_TESTS=OFF -DLLVM_INCLUDE_EXAMPLES=OFF \
  -DLLVM_INCLUDE_BENCHMARKS=OFF -DLLVM_ENABLE_ASSERTIONS=OFF
# Ninja already parallelises; -j here would only fight it.
cmake --build "$BUILD" --target install

[ -x "$DEST/bin/clang" ] || { echo "[envpkg] no clang at $DEST/bin" >&2; exit 1; }
{
  printf 'ollvm_git=%s\n'      "$UPSTREAM"
  printf 'ollvm_rev=%s\n'      "$REV"
  printf 'ollvm_sha=%s\n'      "$SHA"
  printf 'llvm_targets=X86;LoongArch\n'
  printf 'llvm_projects=clang;lld\n'
  printf 'clang_version=%s\n'  "$("$DEST/bin/clang" --version | head -1 | tr -s ' ')"
  printf 'tree_bytes=%s\n'     "$(du -sb "$DEST" | cut -f1)"
} >> "$PINS"
echo "[envpkg] installed at $DEST"; cat "$PINS"
