#!/usr/bin/env bash
# 装一套 osxcross（Linux 上交叉编译 macOS 产物），产出 targets.conf 里
# linker-kind=osxcross 的两档所需环境：OSXCROSS_ROOT=<前缀>。
#
#   前置：宿主要有 clang(+liblto)、llvm、make、cmake、patch、python3、zlib 头文件。
#   用法：prepare_osxcross.sh <安装前缀>
#   SDK 来源（三选一，优先级从上到下；都不给才走内置默认源）：
#     SDK_TARBALL   本地 MacOSX<ver>.sdk.tar.xz 路径
#     SDK_URL       该 tar.xz 的单个下载 URL
#     SDK_URLS      逗号分隔的候选 URL 列表，按顺序试，第一个成的即采用
#   可选：
#     OSXCROSS_GIT   默认 https://github.com/tpoechtrager/osxcross
#     OSXCROSS_REV   默认 master（要复现就钉 commit）
#     MACOS_SDK_VER  默认 11.3；决定下哪个 tarball，也传给 osxcross 的 build.sh
#
# 默认源是 GitHub Releases 上的 macosx-sdks（tag 即 SDK 版本），CI 无需任何 secret
# 即可自举。它是社区再分发的 Apple SDK，属授权物：**不要**用于正式产品的商业分发，
# 自用/内部构建没问题；要合规分发就设 SDK_TARBALL 指向你自己取的 SDK。
set -euo pipefail

[ $# -ge 1 ] && [ $# -le 2 ] || { echo "用法：$0 <安装前缀> [--verify-only]" >&2; exit 2; }
PREFIX="$1"
# --verify-only：只跑第 4 步的 wrapper 校验，不下载也不编译。
# CI 里 osxcross 由单独的 prepare job 编好并落进缓存，build job 恢复后调这个模式
# 来确认树是好的 —— 走正常路径会把 17 分钟的编译再触发一遍。
VERIFY_ONLY=0
[ "${2:-}" = "--verify-only" ] && VERIFY_ONLY=1
GIT="${OSXCROSS_GIT:-https://github.com/tpoechtrager/osxcross}"
REV="${OSXCROSS_REV:-master}"
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT

if [ "$VERIFY_ONLY" = 1 ]; then
  [ -d "$PREFIX/bin" ] || { echo "校验失败：$PREFIX/bin 不存在（osxcross 没装到这里）" >&2; exit 1; }
  echo "[verify-only] 校验 $PREFIX"
else

for t in git make cmake patch curl tar clang; do
  command -v "$t" >/dev/null 2>&1 || { echo "缺工具：$t（osxcross 自身要编译）" >&2; exit 1; }
done

SDK_VER="${MACOS_SDK_VER:-11.3}"

# SDK_URLS 归一成一个候选列表；SDK_URL 等价于只有一个候选。一个都不给时用内置默认源。
sdk_candidates=()
if [ -n "${SDK_URLS:-}" ]; then
  IFS=',' read -r -a sdk_candidates <<< "$SDK_URLS"
elif [ -n "${SDK_URL:-}" ]; then
  sdk_candidates=("$SDK_URL")
else
  sdk_candidates=("https://github.com/joseluisq/macosx-sdks/releases/download/${SDK_VER}/MacOSX${SDK_VER}.sdk.tar.xz")
fi

if [ -n "${SDK_TARBALL:-}" ]; then
  [ -f "$SDK_TARBALL" ] || { echo "SDK_TARBALL 不存在：$SDK_TARBALL" >&2; exit 1; }
  SDK_BASE="$(basename "$SDK_TARBALL")"
else
  # 逐个试：CI 里常备多个 mirrors（GitHub、代理、公司 CDN），哪个能用取决于当次
  # runner 的网络，事前无法确定 —— 所以在这里试，而不是猜一个。
  SDK_BASE=""
  i=0
  for u in "${sdk_candidates[@]}"; do
    i=$((i + 1))
    u="$(printf '%s' "$u" | tr -d '[:space:]')"
    [ -n "$u" ] || continue
    base="$(basename "${u%%\?*}")"
    echo "[1/4] 下载 SDK（候选 $i/${#sdk_candidates[@]}）：${base}"
    if curl -fSL --retry 3 --connect-timeout 30 -o "$W/$base" "$u"; then
      SDK_BASE="$base"; break
    fi
    echo "  候选 $i 失败，换下一个" >&2
    rm -f "$W/$base"
  done
  [ -n "$SDK_BASE" ] || { echo "所有候选 SDK 源都下载失败（代理可用 export https_proxy）" >&2; exit 1; }
  # 文件名里的版本号才是实际拿到的版本：候选 URL 可能与 MACOS_SDK_VER 对不上。
  SDK_VER="$(printf '%s' "$SDK_BASE" | sed -n 's/^MacOSX\([0-9.]*\)\.sdk\.tar\..*/\1/p')"
  [ -n "$SDK_VER" ] || { echo "推不出 SDK 版本（文件名不是 MacOSX<ver>.sdk.tar.xz）：$SDK_BASE" >&2; exit 1; }
fi

echo "[2/4] 取 osxcross（$GIT @ $REV）"
git -c advice.detachedHead=false clone --depth 1 --branch "$REV" "$GIT" "$W/osxcross" >/dev/null 2>&1 \
  || git clone "$GIT" "$W/osxcross"
mkdir -p "$W/osxcross/tarballs"
[ -n "${SDK_TARBALL:-}" ] && cp "$SDK_TARBALL" "$W/osxcross/tarballs/$SDK_BASE" \
  || mv "$W/$SDK_BASE" "$W/osxcross/tarballs/$SDK_BASE"

echo "[3/4] 构建 osxcross（SDK $SDK_VER，安装到 $PREFIX）—— 这一步几分钟，日志 $W/build.log"
# ⚠️ 现在的 osxcross master **完全用环境变量驱动**，build.sh 里根本没有参数解析。
# 早先这里传的是老式命令行开关（--sdk-version / --no-driver / --target / --prefix），
# 那些开关被静默忽略：不报错、不生效，于是 osxcross 老老实实装进了它自己的
# $BASE_DIR/target，而不是 $PREFIX。等下一步去 $PREFIX/bin 找 wrapper 时当然找不到，
# 而报错说的是「缺 x86_64-apple-darwin*-clang」，跟真正的原因（装错地方了）
# 隔着十万八千里。
#
# 认得的变量（见 tools/tools.sh 的 import_osxcross_environment）：
#   TARGET_DIR   安装前缀；不给就是 $BASE_DIR/target
#   SDK_VERSION  同时决定目标三元组后缀（11.3 → darwin20.4）
#   JOBS         make -j 的并行度
#   UNATTENDED   1 = 不交互
( cd "$W/osxcross" \
  && UNATTENDED=1 TARGET_DIR="$PREFIX" SDK_VERSION="$SDK_VER" \
     JOBS="${OSXCROSS_JOBS:-4}" \
     CC="${CC:-clang}" CXX="${CXX:-clang++}" \
     ./build.sh ) >"$W/build.log" 2>&1 \
  || { tail -40 "$W/build.log" >&2; echo "osxcross 构建失败，完整日志：$W/build.log" >&2; exit 1; }
fi

echo "[4/4] 校验 wrapper"
# 只查我们真正会用的东西：每个目标 arch 的 clang wrapper。
# 早先这里还要求 osx-strip / osx-codesign，当前 osxcross 已经不提供这两个无前缀名了
# （工具一律带 arch 前缀，如 x86_64-apple-darwin20.4-strip），而本仓库从头到尾没用过
# 它们 —— 一个没人用的前置条件只会让门禁永远红着。
for w in 'x86_64-apple-darwin*-clang' 'arm64-apple-darwin*-clang'; do
  # osxcross 各版本给的 wrapper 名可能带 SDK 号（x86_64-apple-darwin23-clang），所以按通适找。
  # cmake-clang 有同样的形状但不是编译器（它把参数转交给 cmake），拿它链接会报
  # "CMake Error: Unknown argument -nodefaultlibs"，所以必须排除。
  found=0
  for c in "$PREFIX"/bin/$w; do
    case "${c##*/}" in *-cmake-clang) continue ;; esac
    [ -x "$c" ] && found=1
  done
  if [ "$found" -ne 1 ]; then
    echo "  缺 $PREFIX/bin/$w" >&2
    echo "  实际装出来的（$PREFIX/bin）：" >&2
    ls "$PREFIX/bin" 2>&1 | sed 's/^/    /' >&2 || true
    exit 1
  fi
done

echo
echo "完成。挂上它："
echo "  export OSXCROSS_ROOT=$PREFIX"
echo "  export PATH=\$PATH:$PREFIX/bin"
echo "然后：compile_scripts/build-target.sh x86_64-apple-darwin"
