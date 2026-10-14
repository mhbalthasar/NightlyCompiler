#!/usr/bin/env bash
# verify_ollvm_loongarch.sh - prove the packaged clang is (a) an OLLVM fork at all,
# (b) built with the LoongArch backend, and (c) able to link a LoongArch binary the
# way linuxloong64 actually does.
#
# A tree that merely contains bin/clang proves the install ran. It does not prove
# -DLLVM_TARGETS_TO_BUILD survived the build, which is the whole reason this package
# exists (the prebuilt clang23 release has no LoongArch backend), nor that the
# obfuscation passes are registered.
#
# The probe replicates Daisium_Predict/CMakeLists.txt dp_probe_ollvm() verbatim
# (-enable-cffobf -> hikari, -fla -> classic) and then uses that repo's NON-WINDOWS
# level set, because level 2 there is deliberately reduced to -enable-cffobf
# -enable-strcry: sub/split crash Hikari's AsmPrinter off Windows. Probing with the
# full Windows set would fail on a toolchain that the consumer builds fine with.
#
#   usage: verify_ollvm_loongarch.sh <tree>
set -euo pipefail
PKG="${1:-${PKG_DIR:?PKG_DIR or \$1 must be the unpacked tree}}"
SYSROOT="${LA_SYSROOT:-$HOME/tools/sysroots/loongarch64/root}"
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
export PATH="$PKG/bin:$PATH"
clang --version | head -1

# (a) LoongArch + X86 backends.
TARGETS="$(clang -print-targets)"
for t in loongarch64 x86-64; do
  grep -q "^[[:space:]]*$t" <<<"$TARGETS" || { echo "[verify] clang has no '$t' target" >&2; exit 1; }
done
echo "[verify] targets ok (loongarch64, x86-64)"

# (b) OLLVM flavour probe, same shape as dp_probe_ollvm().
printf 'int main(){return 0;}\n' > "$W/probe.cpp"
FLAVOR=none
if clang++ -c "$W/probe.cpp" "-o$W/probe.o" -mllvm -enable-cffobf >/dev/null 2>&1; then
  FLAVOR=hikari
elif clang++ -c "$W/probe.cpp" "-o$W/probe.o" -mllvm -fla >/dev/null 2>&1; then
  FLAVOR=classic
fi
[ "$FLAVOR" != none ] || { echo "[verify] no OLLVM passes accepted - not an obfuscating clang" >&2; exit 1; }
echo "[verify] OLLVM flavour: $FLAVOR"

# (c) real LoongArch sysroot, fetched with Daisium_Predict's own prep script rather
# than a copy, so the pairing proven here is the one the consumer job uses.
[ -d "$SYSROOT/usr/lib" ] || bash dp/compile_scripts/tools/prepare_loongarch64_sysroot.sh > /tmp/la-sysroot.log 2>&1 \
  || { echo "[verify] could not prepare a loongarch64 sysroot to link against" >&2; tail -20 /tmp/la-sysroot.log >&2; exit 1; }
printf '#include <stdio.h>\nint main(void){puts("envpkg");return 0;}\n' > "$W/t.c"
X=(--target=loongarch64-unknown-linux-gnu --sysroot="$SYSROOT" -fuse-ld=lld)
clang "${X[@]}" -O2 -o "$W/t" "$W/t.c"
MACHINE="$(readelf -h "$W/t" | sed -n 's/.*Machine:[[:space:]]*//p')"
grep -q LoongArch <<<"$MACHINE" || { echo "[verify] linked binary is '$MACHINE', not LoongArch" >&2; exit 1; }
echo "[verify] loongarch64 link ok (Machine='$MACHINE')"

# (d) the flags linuxloong64 builds with, end to end - obfuscated cross link.
if [ "$FLAVOR" = hikari ]; then
  OBF=(-O2 -mllvm -enable-cffobf -mllvm -enable-strcry)
else
  OBF=(-mllvm -fla -mllvm -sub -mllvm -sub_loop=3 -mllvm -sobf)
fi
clang "${X[@]}" "${OBF[@]}" -o "$W/t-obf" "$W/t.c"
readelf -h "$W/t-obf" | grep -q LoongArch || { echo "[verify] obfuscated cross link failed" >&2; exit 1; }
echo "[verify] obfuscated loongarch64 link ok (${OBF[*]})"
echo "[verify] OK"
