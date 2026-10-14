#!/usr/bin/env bash
# verify_osxcross.sh - prove an unpacked osxcross tree compiles AND links Mach-O
# for both arches, from wherever it was unpacked.
#
# Relocation is the point. osxcross wrappers resolve the SDK relative to their own
# location, and the tree we publish was built at a fixed path in the producer job -
# so the only meaningful check is to extract it somewhere else and build through the
# wrappers there. A tree that only worked where it was built would otherwise show up
# as nine confusing "SDK not found" failures in consumer jobs.
#
# EXPECT_DARWIN pins the tool-name suffix the consumer hardcodes: Daisium and
# Daisium_Predict's build.osx*.sh look for exactly x86_64-apple-darwin20.4-clang
# (that is SDK 11.3's mapping), so a tree whose wrappers came out as darwin21 would
# satisfy every glob here and still die in their jobs. libOpenCyan globs instead, so
# its row leaves EXPECT_DARWIN empty.
#
#   usage: verify_osxcross.sh <tree>   [EXPECT_DARWIN=20.4]
set -euo pipefail
PKG="${1:-${PKG_DIR:?PKG_DIR or \$1 must be the unpacked tree}}"
EXPECT_DARWIN="${EXPECT_DARWIN:-}"
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
SDK="$(find "$PKG/SDK" -maxdepth 1 -type d -name 'MacOSX*.sdk' -print -quit 2>/dev/null || true)"
[ -n "$SDK" ] || { echo "[verify] no MacOSX*.sdk under $PKG/SDK" >&2; ls "$PKG" >&2; exit 1; }
echo "[verify] SDK: $(basename "$SDK")"

# *-cmake-clang is a cmake driver, not a compiler (it forwards arguments to cmake,
# and using it to link dies on "-nodefaultlibs"), so it is excluded everywhere.
wrap() { # wrap <arch> <tool>
  local a="$1" t="$2" c
  for c in "$PKG/bin/$a-apple-darwin"*$t; do
    case "${c##*/}" in *-cmake-clang) continue ;; esac
    [ -x "$c" ] && { printf '%s\n' "$c"; return 0; }
  done
  echo "[verify] missing $a-apple-darwin*$t in $PKG/bin" >&2
  ls "$PKG/bin" >&2 | sed 's/^/    /'
  return 1
}

if [ -n "$EXPECT_DARWIN" ]; then
  for a in x86_64 arm64; do
    p="$PKG/bin/$a-apple-darwin$EXPECT_DARWIN-clang"
    [ -x "$p" ] || { echo "[verify] consumer hardcodes $(basename "$p"), tree lacks it" >&2; exit 1; }
  done
  echo "[verify] darwin$EXPECT_DARWIN wrappers present for x86_64 + arm64"
fi

printf '#include <stdio.h>\nint main(void){puts("envpkg");return 0;}\n' > "$W/t.c"
cat > "$W/t.cpp" <<'EOF'
#include <string>
#include <cstdio>
int main(){std::string s="envpkg";printf("%s\n",s.c_str());return 0;}
EOF

for a in x86_64 arm64; do
  CC="$(wrap "$a" -clang)"; CXX="$(wrap "$a" -clang++)"
  LD="$(wrap "$a" -ld)"; NM="$(wrap "$a" -nm)"; STRIP="$(wrap "$a" -strip)"
  OT="$(wrap "$a" -otool)"
  # Deploy target 11.0 is what build.osx{64,arm64}.sh passes to CMake; mirror it so
  # the availability of symbols in the shipped SDK is proven at the same floor.
  # -fuse-ld=<wrapper>-ld is the other half of that: the consumers set it in
  # CMAKE_{EXE,SHARED}_LINKER_FLAGS, so the linker selection is part of the
  # contract this tree has to satisfy, not just a file that happens to exist.
  "$CC" -mmacosx-version-min=11.0 -isysroot "$SDK" -fuse-ld="$LD" -O2 -o "$W/t-$a" "$W/t.c"
  "$CXX" -mmacosx-version-min=11.0 -isysroot "$SDK" -fuse-ld="$LD" -std=c++17 -O2 -o "$W/tc-$a" "$W/t.cpp"
  "$OT" -l "$W/t-$a" >/dev/null
  "$NM" "$W/t-$a" | grep -q '_main' || { echo "[verify] $a binary has no _main symbol" >&2; exit 1; }
  "$STRIP" -u "$W/t-$a"
  file "$W/t-$a" | grep -q "Mach-O.*$a" || { echo "[verify] $a output is not Mach-O/$a" >&2; exit 1; }
  echo "[verify] $a: C + C++ link ok via $(basename "$LD"), nm/otool/strip ok"
done
echo "[verify] OK"
