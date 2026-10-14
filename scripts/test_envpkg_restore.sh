#!/usr/bin/env bash
# test_envpkg_restore.sh - run scripts/envpkg.sh against a replayed release
# listing and locally packed packages.
#
# Only the network is faked: a curl stand-in replays a fixture for the release
# listing and serves one local tarball for the download. The code under test is
# the shipped script itself - the URL pick, the version ordering, the
# extract-aside-then-move, and every path that has to fall back to building from
# source - because re-implementing that parsing in a test would prove nothing
# about the parsing consumers actually run.
#
#   usage: bash scripts/test_envpkg_restore.sh
# Needs: bash, tar, xz, curl(stand-in only), coreutils.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="$HERE/envpkg.sh"
W="$(mktemp -d)"
trap 'rm -rf "$W"' EXIT
FAM=dp-sysroot-x86_64
fails=0

pass() { echo "PASS  $1"; }
fail() { echo "FAIL  $1"; [ -n "${2:-}" ] && printf '%s\n' "$2" | sed 's/^/      /' | head -20; fails=$((fails + 1)); }
ck() { # ck <label> <rc> [log]      - expects SUCCESS
  if [ "$2" = "0" ]; then pass "$1"; else fail "$1" "${3:-}"; fi
}
ckn() { # ckn <label> <rc> [log]    - expects FAILURE (a fallback path)
  if [ "$2" != "0" ]; then pass "$1"; else fail "$1" "${3:-}"; fi
}

# ---------------------------------------------------------------- fixtures --
api_json() { # api_json <name>... -> a release listing in api.github.com's shape
  { echo '{ "tag_name": "envpkgs", "assets": ['
    i=0
    for n in "$@"; do
      i=$((i + 1))
      printf '    { "id": %d, "name": "%s", "size": 1234,\n      "browser_download_url": "https://release-a.example.invalid/%s" }%s\n' \
        "$((999 + i))" "$n" "$n" "$([ "$i" -lt $# ] && echo , )"
    done
    echo '  ] }'; }
}

make_package() { # make_package <out> <family> <version> [no-meta] [truncate]
  local out="$1" fam="$2" ver="$3" nometa="${4:-}" trunc="${5:-}"
  local tree; tree="$(mktemp -d)"
  mkdir -p "$tree/usr/lib"
  echo "not really libc" > "$tree/usr/lib/libc.so.6"
  # A RELATIVE symlink, which is what a sysroot tree actually contains
  # (lib -> usr/lib). Absolute links are the failure relocation exposes.
  ln -s usr/lib "$tree/lib" 2>/dev/null || true
  if [ -z "$nometa" ]; then
    printf '{\n  "family": "%s",\n  "asset": "%s-v%s.tar.xz",\n  "glibc_floor": "GLIBC_2.28",\n  "multiarch": "x86_64-linux-gnu"\n}\n' \
      "$fam" "$fam" "$ver" > "$tree/.envpkg.json"
  fi
  # Same pack command as scripts/envpkgs.py, so the archive the test serves is
  # the archive a consumer would download.
  ( cd "$tree" && tar --format=pax -cf - . ) | xz -T0 -6 > "$out"
  rm -rf "$tree"
  [ -n "$trunc" ] && truncate -s "$(( $(stat -c%s "$out") / 2 ))" "$out"
}

PKG="$W/pkg.tar.xz";          make_package "$PKG" "$FAM" 10
PKG_OLD="$W/old.tar.xz";      make_package "$PKG_OLD" "$FAM" 1
PKG_TRUNC="$W/trunc.tar.xz";  make_package "$PKG_TRUNC" "$FAM" 11 "" trunc
PKG_NOMETA="$W/nometa.tar.xz"; make_package "$PKG_NOMETA" "dp-sysroot-i386" 1 nometa
make_package "$W/ollvm.tar.xz" ollvm23-x86-loongarch-linux 4

FIX="$W/api.json"
api_json "$FAM-v1.tar.xz" "$FAM-v10.tar.xz" "ollvm23-x86-loongarch-linux-v4.tar.xz" \
         "unrelated-thing.zip" "envpkgs-manifest.txt" > "$FIX"
FIX_I386="$W/api2.json"; api_json "dp-sysroot-i386-v1.tar.xz" > "$FIX_I386"

# The stand-in decides, from its own argv, whether it is being asked for the
# release listing (stdout) or for an asset (-o file). FAIL_API/FAIL_DL reproduce
# a 404/rate limit and a broken CDN.
BIN="$W/bin"; mkdir -p "$BIN"
cat > "$BIN/curl" <<'SHIM'
#!/usr/bin/env bash
set -uo pipefail
mode=""; out=""; prev=""
for a in "$@"; do
  if [ "$prev" = "-o" ]; then out="$a"; mode=dl; fi
  if [ "$a" = "-o" ]; then prev="-o"; continue; fi
  case "$a" in *releases/tags*) mode=api ;; esac
  prev="$a"
done
if [ "$mode" = api ]; then [ -n "${FAIL_API:-}" ] && exit 22; cat "$API_FIXTURE"; exit 0; fi
if [ "$mode" = dl ]; then [ -n "${FAIL_DL:-}" ] && exit 22; cp "${PKG_FILE:?}" "$out"; exit 0; fi
exit 22
SHIM
chmod +x "$BIN/curl"

run() { # run <fixture> <pkg> <fail_api> <fail_dl> <args...>  -> "<rc>\n<log>"
  local fx="$1" pk="$2" fa="$3" fd="$4"; shift 4
  local out rc
  # The environment goes on the command, not on an assignment: `VAR=1 out=$(cmd)`
  # is a bare assignment, and the exit status the test then reads is the
  # assignment's, always 0 - which makes every fallback look like a pass.
  out="$(PATH="$BIN:$PATH" API_FIXTURE="$fx" PKG_FILE="$pk" FAIL_API="$fa" FAIL_DL="$fd" \
         ENVPKG_REPO="mhbalthasar/NightlyCompiler" bash "$HELPER" "$@" 2>&1)"; rc=$?
  printf '%s\n%s' "$rc" "$out"
}
rc_of() { printf '%s\n' "$1" | head -1; }
log_of() { printf '%s\n' "$1" | tail -n +2; }

has() { printf '%s' "$1" | grep -q "$2"; }

echo "=== envpkg.sh ==="

# --- version selection: -v10 must beat -v1 ------------------------------------
# Lexicographic order puts "-v10" ahead of "-v2", so a plain `sort` would serve
# the OLDEST tree and report success.
I="$(run "$FIX" "$PKG" "" "" info "$FAM")"
ck "info exits 0" "$(rc_of "$I")" "$(log_of "$I")"
ck "info URL names -v10, not -v1" "$(has "$(log_of "$I")" '\-v10\.tar\.xz' && echo 0 || echo 1)" "$(log_of "$I")"

# --- happy path ---------------------------------------------------------------
DEST_DIR="$W/nested/deep"
DEST="$DEST_DIR/x86_64/root"
R="$(run "$FIX" "$PKG" "" "" restore "$FAM" "$DEST")"
ck "restore exits 0" "$(rc_of "$R")" "$(log_of "$R")"
ck "tree placed at dest" "$([ -f "$DEST/usr/lib/libc.so.6" ] && echo 0 || echo 1)"
ck "provenance file carried" "$([ -f "$DEST/.envpkg.json" ] && echo 0 || echo 1)"
ck "provenance echoed into log" "$(has "$(log_of "$R")" glibc_floor && echo 0 || echo 1)" "$(log_of "$R")"
ck "missing parent dirs created" "$([ -d "$(dirname "$DEST")" ] && echo 0 || echo 1)"
if [ -L "$DEST/lib" ]; then
  tgt="$(readlink "$DEST/lib")"
  ck "relative symlink survived pack -> unpack -> relocate" \
     "$([ "$tgt" = "usr/lib" ] && [ -d "$DEST/$tgt" ] && echo 0 || echo 1)" "lib -> ${tgt:-none}"
else
  echo "SKIP  symlink check (this platform will not create symlinks)"
fi

R="$(run "$FIX" "$PKG" "" "" restore "$FAM" "$DEST")"
ck "re-restore over a populated dest succeeds" "$(rc_of "$R")" "$(log_of "$R")"

# --- fallbacks: non-zero, no crash, no side effects ---------------------------
R="$(run "$FIX" "$PKG" "" "" restore "no-such-family" "$W/d2")"
ckn "unknown family -> non-zero" "$(rc_of "$R")" "$(log_of "$R")"
ck "unknown family says 'building from source'" \
   "$(has "$(log_of "$R")" "building from source" && echo 0 || echo 1)" "$(log_of "$R")"
ck "unknown family leaves nothing behind" "$([ ! -e "$W/d2" ] && echo 0 || echo 1)"

R="$(run "$FIX" "$PKG" 1 "" restore "$FAM" "$W/d3")"
ckn "API failure -> non-zero (falls back)" "$(rc_of "$R")" "$(log_of "$R")"
R="$(run "$FIX" "$PKG" "" 1 restore "$FAM" "$W/d4")"
ckn "download failure -> non-zero (falls back)" "$(rc_of "$R")" "$(log_of "$R")"

# A half-downloaded archive must not replace a directory the caller might still
# build against - that is why extraction goes to a sibling path first.
KEEP="$W/keep"; mkdir -p "$KEEP"; echo "must survive" > "$KEEP/sentinel"
R="$(run "$FIX" "$PKG_TRUNC" "" "" restore "$FAM" "$KEEP")"
ckn "truncated archive -> non-zero" "$(rc_of "$R")" "$(log_of "$R")"
ck "truncated archive left the old dir intact" \
   "$([ -f "$KEEP/sentinel" ] && echo 0 || echo 1)"

R="$(run "$FIX_I386" "$PKG_NOMETA" "" "" restore "dp-sysroot-i386" "$W/d6")"
ckn "package without .envpkg.json -> non-zero" "$(rc_of "$R")" "$(log_of "$R")"

R="$(run "$FIX" "$PKG" "" "" restore "$FAM" "$W/d7")"   # sanity before the switch
ck "restore works (control for ENVPKG_DISABLE)" "$(rc_of "$R")" "$(log_of "$R")"
rm -rf "$W/d7"
export ENVPKG_DISABLE=1
R="$(run "$FIX" "$PKG" "" "" restore "$FAM" "$W/d7")"
unset ENVPKG_DISABLE
ckn "ENVPKG_DISABLE=1 -> non-zero" "$(rc_of "$R")" "$(log_of "$R")"
ck "ENVPKG_DISABLE=1 says so" "$(has "$(log_of "$R")" ENVPKG_DISABLE && echo 0 || echo 1)" "$(log_of "$R")"
ck "ENVPKG_DISABLE=1 creates nothing" "$([ ! -e "$W/d7" ] && echo 0 || echo 1)"

usage_rc() { bash "$HELPER" "$@" >/dev/null 2>&1; printf '%s' "$([ $? = 2 ] && echo 0 || echo 1)"; }
ck "no args -> usage exit 2"          "$(usage_rc)"
ck "info without family -> exit 2"    "$(usage_rc info)"
ck "restore without dest -> exit 2"   "$(usage_rc restore "$FAM")"

# --- the copies in the consumer repos must not have drifted ------------------
echo "=== copies in consumer repos ==="
want="$(sha256sum "$HELPER" | cut -d' ' -f1)"
for rel in ../../Daisium ../../Daisium_Predict ../../libOpenCyan; do
  p="$(cd "$HERE/$rel" 2>/dev/null && pwd)/compile_scripts/tools/envpkg.sh"
  if [ -f "$p" ]; then
    got="$(sha256sum "$p" | cut -d' ' -f1)"
    ck "$(basename "$rel") copy identical" "$([ "$got" = "$want" ] && echo 0 || echo 1)" "$p"
  else
    fail "$(basename "$rel") copy missing" "$p"
  fi
done

echo
if [ "$fails" = 0 ]; then echo "all checks passed"; exit 0; fi
echo "$fails check(s) failed"; exit 1
