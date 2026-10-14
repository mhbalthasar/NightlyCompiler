#!/usr/bin/env bash
# envpkg.sh - fetch a prebuilt shared toolchain from NightlyCompiler's release.
#
# Build scripts here spend real time compiling the toolchains they build with:
# a LoongArch-capable Hikari OLLVM, osxcross plus an Apple SDK. Those trees are
# the same regardless of which repo consumes them, so they live in
# NightlyCompiler's `envpkgs` release, published by its envpkgs_compiler workflow
# after verifying each one by extracting the tree and linking with it.
#
# Debian sysroot trees are NOT in that release: every repo wants a different
# package set in its sysroot, so those stay built by each repo's own
# prepare_*_sysroot.sh against its own requirements.
#
# The order is always actions/cache -> this -> build from source. The cache stays
# first: a hit is local and free, and this script only runs when it missed. Every
# failure path exits non-zero so the caller's `|| <build from source>` still runs:
# no network, rate-limited API, unknown family, truncated archive - all of them
# mean "build it yourself", which is exactly the behaviour this file replaced.
#
#   envpkg.sh restore <family> <dest-dir>   0 = tree placed at <dest-dir>
#   envpkg.sh info    <family>              print the URL that would be fetched
#
# Env: ENVPKG_REPO (default mhbalthasar/NightlyCompiler - that repo is public, so
# this needs no token and no new secret in the consuming repo); ENVPKG_DISABLE=1
# forces the from-source path, e.g. when comparing a suspect toolchain against a
# freshly built one.
#
# This file is a copy in Daisium, Daisium_Predict and libOpenCyan rather than a
# shared dependency: the three repos are private and unrelated, and pulling a
# fourth repo into every build is a worse versioning problem than keeping ~70 lines
# in triplicate. Keep the three byte-identical.
set -euo pipefail

REPO="${ENVPKG_REPO:-mhbalthasar/NightlyCompiler}"
TAG="envpkgs"
API="https://api.github.com/repos/$REPO/releases/tags/$TAG"

usage() { echo "usage: envpkg.sh restore <family> <dest-dir> | info <family>" >&2; exit 2; }

# Newest download URL for a family, or empty. Quoted-field 4 of the API's
# "browser_download_url": "..." lines: split on quotes with awk rather than
# parsing JSON, because jq is not guaranteed on every runner image and python is
# not something a compiler bootstrap should depend on. `sort -V` orders
# -v9 before -v10, which a plain sort gets wrong.
asset_url() {
  curl -fsSL --connect-timeout 15 -m 60 \
       -H 'Accept: application/vnd.github+json' "$API" 2>/dev/null \
    | awk -F'"' '/"browser_download_url"/ {print $4}' \
    | grep -E "/$1-v[0-9]+\.tar\.xz$" \
    | sort -V | tail -1
}

cmd="${1:-}"; fam="${2:-}"
case "$cmd" in
  info)    [ -n "$fam" ] || usage; url="$(asset_url "$fam")" || true
           [ -n "$url" ] || { echo "[envpkg] no $fam package on $REPO@$TAG" >&2; exit 1; }
           echo "$url"; exit 0 ;;
  restore) [ -n "$fam" ] && [ -n "${3:-}" ] || usage; dest="$3" ;;
  *)       usage ;;
esac

if [ "${ENVPKG_DISABLE:-0}" = "1" ]; then
  echo "[envpkg] ENVPKG_DISABLE=1 - skipping the release"
  exit 1
fi

# `|| true`: a 404/rate-limit/timeout here must fall through to the source build
# instead of tripping errexit and failing the job for a cache-shaped miss.
url="$(asset_url "$fam" || true)"
if [ -z "$url" ]; then
  echo "[envpkg] no $fam package on $REPO@$TAG yet - building from source"
  exit 1
fi

w="$(mktemp -d)"; trap 'rm -rf "$w"' EXIT
echo "[envpkg] fetching $(basename "$url")"
# 100 MB to 500 MB over the usual CDN: -m is sized for the largest package on a
# slow runner so a stalled transfer cannot eat the job's whole timeout, and -C -
# makes a retry cheap.
curl -fL --retry 3 --retry-delay 5 --connect-timeout 20 -m 1800 -o "$w/pkg.tar.xz" "$url"

mkdir -p "$w/partial"
# Extract aside, then move into place: a truncated archive (xz reports it here)
# must never leave a half-written directory that the caller would then build
# against as if it were the real toolchain.
if ! xz -dc "$w/pkg.tar.xz" | tar -xf - -C "$w/partial"; then
  echo "[envpkg] $fam archive did not unpack cleanly" >&2
  exit 1
fi
[ -f "$w/partial/.envpkg.json" ] || { echo "[envpkg] $fam package has no .envpkg.json" >&2; exit 1; }

mkdir -p "$(dirname "$dest")"
rm -rf "$dest"
mv "$w/partial" "$dest"

# Provenance into the CI log, so "which tree was this" is answerable without
# re-running the producer to find out.
echo "[envpkg] $fam restored to $dest"
sed 's/^/[envpkg]   /' "$dest/.envpkg.json"
