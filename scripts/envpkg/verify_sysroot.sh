#!/usr/bin/env bash
# verify_sysroot.sh - prove a packaged sysroot TREE can actually link a binary for
# its arch, using the runner's own clang+lld and nothing else.
#
# Existence checks are not enough here. A sysroot is a set of .deb payloads
# unpacked side by side, and the classic breakage is invisible to `test -f`: libc.so
# is a linker script naming /lib/ld-linux-*.so.2 absolutely, so the tree needs the
# usrmerge symlink inside it, and a sid pair can ship a *.so dev symlink pointing at
# a soname no runtime package provides. Both fail only at link time, in a downstream
# job, forty minutes after the cache was saved.
#
#   usage: verify_sysroot.sh <tree>        (TARGET_TRIPLE=<arch>-linux-gnu)
set -euo pipefail
PKG="${1:-${PKG_DIR:?PKG_DIR or \$1 must be the unpacked tree}}"
TRIPLE="${TARGET_TRIPLE:?verify_sysroot.sh needs TARGET_TRIPLE (e.g. aarch64-linux-gnu)}"
ARCH="${TRIPLE%%-*}"
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT

command -v clang >/dev/null && command -v ld.lld >/dev/null || {
  echo "[verify] need host clang and ld.lld to prove the tree links" >&2; exit 1; }

printf '#include <stdio.h>\n#include <string.h>\nint main(void){char b[16];memset(b,0,sizeof b);strcpy(b,"envpkg");puts(b);return 0;}\n' > "$W/t.c"

# -nostdlibinc is deliberately absent: the point is that the sysroot supplies
# headers, start files, libc and libgcc on its own.
clang --target="$TRIPLE" --sysroot="$PKG" -fuse-ld=lld -O0 -o "$W/t" "$W/t.c" 2>&1 | tee "$W/link.log"
[ -x "$W/t" ] || { echo "[verify] link produced no binary for $TRIPLE" >&2; sed -n '1,40p' "$W/link.log" >&2; exit 1; }

MACHINE="$(readelf -h "$W/t" | sed -n 's/.*Machine:\s*//p')"
case "$ARCH:$MACHINE" in
  x86_64:*X86-64*|i386:*80386*|aarch64:*AArch64*|loongarch64:*LoongArch*)
    echo "[verify] $TRIPLE linked: Machine='$MACHINE'" ;;
  *) echo "[verify] wrong machine for $ARCH: '$MACHINE'" >&2; exit 1 ;;
esac

# Which glibc the produced binary demands, i.e. the floor this tree implies.
FLOOR="$(readelf -V "$W/t" | grep -o 'GLIBC_2\.[0-9]*' | sort -Vu | tail -1)"
echo "[verify] required glibc symbol: ${FLOOR:-none}"

# C++ too: the packaging exists to build libdaisium/libdaisium_predict, and
# libstdc++ is a separate .deb that can be missing while libc looks fine.
cat > "$W/t.cpp" <<'EOF'
#include <string>
#include <cstdio>
int main(){std::string s="envpkg";printf("%s\n",s.c_str());return 0;}
EOF
if clang++ --target="$TRIPLE" --sysroot="$PKG" -fuse-ld=lld -O0 -o "$W/tc" "$W/t.cpp" 2>"$W/cxx.log"; then
  echo "[verify] $TRIPLE C++ link ok (libstdc++ present)"
else
  echo "[verify] C++ link FAILED for $TRIPLE - is libstdc++-dev in the tree?" >&2
  sed -n '1,25p' "$W/cxx.log" >&2
  exit 1
fi
echo "[verify] OK"
