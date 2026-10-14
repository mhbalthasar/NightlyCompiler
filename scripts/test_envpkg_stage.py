#!/usr/bin/env python3
"""
test_envpkg_stage.py - run scripts/envpkgs.py stage end to end on a fake package.

stage is the producer's core: it expands the dest, runs the prepare step, checks
markers, writes provenance, packs with the exact tar|xz pipeline the consumers
unpack, extracts into a FRESH directory and runs the verify step there. A mistake
in any of that either publishes nothing or publishes a tree that then fails in nine
consumer jobs, and today it would only show up hours into a real build.

Only `gh` is stubbed (the version lookup). The filesystem, the packer, the unpacker
and the verifier are the real ones.

POSIX paths are the norm here; the test keeps every path forward-slash so it also
runs on a Windows dev box (bash + GNU tar + xz), and the workflow's selftest job
runs it on ubuntu where the consumers will.

Run:  python3 scripts/test_envpkg_stage.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ENVPKG = os.path.join(HERE, "envpkgs.py")

PREPARE = r"""#!/usr/bin/env bash
set -euo pipefail
# A prepare step that behaves like the real ones: builds the tree at the dest the
# table declared (which stage must hand over through ENVPKG_DEST), leaves a
# RELATIVE symlink in it, and records pins for the provenance file.
test -n "${ENVPKG_DEST:?stage did not export ENVPKG_DEST}"
test -n "${ENVPKG_PINS:?stage did not export ENVPKG_PINS}"
mkdir -p "$ENVPKG_DEST/bin" "$ENVPKG_DEST/usr/lib/triplet"
printf '#!/bin/sh\necho clang-stub\n' > "$ENVPKG_DEST/bin/clang"
chmod +x "$ENVPKG_DEST/bin/clang"
echo libc > "$ENVPKG_DEST/usr/lib/triplet/libc.so.6"
echo startfile > "$ENVPKG_DEST/usr/lib/triplet/crt1.o"
ln -s usr/lib "$ENVPKG_DEST/lib"
printf 'glibc_floor=GLIBC_2.28\nollvm_sha=deadbeefcafe\n' > "$ENVPKG_PINS"
"""

VERIFY = r"""#!/usr/bin/env bash
set -euo pipefail
# Must run against the EXTRACTED tree. Record where it was pointed, prove the
# env the table declared was forwarded, and refuse to run in the build directory.
echo "dir=$PKG_DIR" > "$VERIFY_LOG"
echo "first=$1" >> "$VERIFY_LOG"
echo "forwarded=${FAKE_VAR:-unset}" >> "$VERIFY_LOG"
[ "$PKG_DIR" != "$BUILD_DIR" ] || { echo "verify ran in the build dir" >&2; exit 1; }
test -x "$PKG_DIR/bin/clang" || { echo "no clang after unpack" >&2; exit 1; }
test -e "$PKG_DIR/lib/triplet/libc.so.6" || { echo "symlink did not survive" >&2; exit 1; }
"""

ROW = {
    "family": "selftest-package",
    "desc": "fake package for the stage test",
    "runner": "ubuntu-latest",
    "timeout_minutes": 5,
    "repos": [],
    "dest": "$HOME/selftest-tree",
    "prepare": "bash ./prepare.sh",
    "verify": "./verify.sh",
    "verify_env": {"FAKE_VAR": "forwarded"},
    "markers": ["bin/clang", "usr/lib/*/libc.so.6", "usr/lib/*/crt1.o", "lib"],
}

GH_STUB = """#!/usr/bin/env bash
# stand-in for gh: behave as a repo with no release yet.
if [ "${1:-}" = "release" ] && [ "${2:-}" = "view" ]; then exit 1; fi
if [ "${1:-}" = "release" ] && [ "${2:-}" = "upload" ]; then echo "$*" >> "${GH_UPLOADS:-/dev/null}"; exit 0; fi
echo "unexpected gh call: $*" >&2
exit 9
"""


def main():
    # envpkgs.py stage mixes bash-expanded paths ($HOME/...) with python file
    # operations, and the two only agree on a POSIX box: on Windows, bash rewrites
    # a C:/... HOME into /tmp/... and python then cannot see the tree the prepare
    # step just wrote. The workflow's selftest job runs this on ubuntu.
    if os.name != "posix":
        print("SKIP: stage mixes bash paths with python file calls; needs POSIX")
        return 0
    for t in ("bash", "tar", "xz"):
        if not shutil.which(t):
            print("SKIP: needs %s" % t)
            return 0

    # Forward slashes throughout: stage hands bash-expanded paths to python, and
    # `C:\users\...` from bash and `C:/users/...` from python are the same place
    # only when nothing has to escape a backslash.
    w = tempfile.mkdtemp(prefix="envpkg-stage-test-").replace("\\", "/")
    home, ws = os.path.join(w, "home"), os.path.join(w, "ws")
    os.makedirs(home); os.makedirs(ws)
    fails = []

    def ck(label, cond, detail=""):
        print(("PASS  " if cond else "FAIL  ") + label)
        if not cond:
            fails.append(label)
            if detail:
                print("      " + str(detail).replace("\n", "\n      ")[:900])

    for name, body, mode in (("prepare.sh", PREPARE, 0o755), ("verify.sh", VERIFY, 0o755),
                             ("gh", GH_STUB, 0o755), ("verify-fail.sh",
                             "#!/usr/bin/env bash\necho 'verify says no' >&2\nexit 1\n", 0o755)):
        p = os.path.join(ws, name)
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
        os.chmod(p, mode)

    outs = os.path.join(w, "outputs.txt")
    vlog = os.path.join(w, "verify-log.txt")
    uploads = os.path.join(w, "uploads.txt")
    base = os.path.join(home, "selftest-tree")
    env = dict(os.environ, HOME=home, PATH=ws + os.pathsep + os.environ["PATH"],
               GITHUB_OUTPUT=outs, GH_UPLOADS=uploads, VERIFY_LOG=vlog,
               BUILD_DIR=base, MACOS_SDK_VER="11.3")

    def stage(row, tag=""):
        e = dict(env, PKG_JSON=json.dumps(row))
        return subprocess.run([sys.executable, ENVPKG, "stage", "--outputs", outs],
                              cwd=ws, env=e, capture_output=True, text=True)

    p = stage(ROW)
    log = p.stdout + p.stderr
    ck("stage succeeds on a good package (rc=%d)" % p.returncode, p.returncode == 0, log)

    dist = os.path.join(ws, "dist", "selftest-package-v1.tar.xz")
    ck("tarball named <family>-v1.tar.xz (no release -> version 1)",
       os.path.isfile(dist), os.listdir(os.path.join(ws, "dist")) if os.path.isdir(os.path.join(ws, "dist")) else "no dist/")
    got = open(outs, encoding="utf-8").read() if os.path.isfile(outs) else ""
    ck("stage emitted the asset name", "asset=selftest-package-v1.tar.xz" in got, got)
    ck("stage emitted a path and a sha256",
       "path=" in got and "sha256=" in got, got)

    # --- the verify ran on the unpacked copy, not the tree it just built ----
    if os.path.isfile(vlog):
        rec = dict(l.split("=", 1) for l in open(vlog, encoding="utf-8").read().splitlines())
        ck("verify pointed at a fresh extraction dir",
           rec.get("dir") and rec["dir"] != base, rec)
        ck("verify_env reached the verify step", rec.get("forwarded") == "forwarded", rec)
        ck("verify got the tree path as its argument",
           rec.get("first", "").startswith(rec["dir"][:3]), rec)
    else:
        ck("verify step ran at all", False, log)

    # --- provenance travels inside the tree --------------------------------
    if os.path.isfile(dist):
        lst = subprocess.run(["tar", "-tJf", dist], capture_output=True, text=True).stdout.split()
        ck("package carries .envpkg.json", "./.envpkg.json" in lst, lst[:10])
        ck("package carries the symlink as a symlink",
           any(n.endswith("/lib") for n in lst), lst[:10])
        ex = os.path.join(w, "peek")
        os.makedirs(ex)
        subprocess.run("xz -dc %s | tar -xf - -C %s" % (dist, ex), shell=True, check=True)
        # Checked against THIS extraction: stage removes its own verify directory
        # when it finishes, so asking whether a path under it is a symlink would
        # just be asking whether a deleted directory is a symlink.
        ck("symlink survived pack -> unpack -> relocate",
           os.path.islink(os.path.join(ex, "lib")) and
           os.path.realpath(os.path.join(ex, "lib", "triplet", "libc.so.6")) ==
           os.path.realpath(os.path.join(ex, "usr", "lib", "triplet", "libc.so.6")),
           "lib -> %s" % (os.readlink(os.path.join(ex, "lib"))
                          if os.path.islink(os.path.join(ex, "lib")) else "NOT A SYMLINK"))
        meta = json.load(open(os.path.join(ex, ".envpkg.json"), encoding="utf-8"))
        ck("pins from ENVPKG_PINS merged into provenance",
           meta.get("pins", {}).get("glibc_floor") == "GLIBC_2.28", meta)
        ck("family + desc recorded", meta.get("family") == "selftest-package", meta)

    # --- failure paths: never publish --------------------------------------
    bad = dict(ROW, family="selftest-verifyfail", verify="./verify-fail.sh")
    p = stage(bad)
    ck("failing verify aborts stage", p.returncode != 0, p.stdout + p.stderr)
    ck("nothing uploaded when verify fails",
       not os.path.isfile(uploads) or "selftest-verifyfail" not in open(uploads).read(),
       open(uploads).read() if os.path.isfile(uploads) else "")

    miss = dict(ROW, family="selftest-markermiss", markers=["bin/not-there"])
    p = stage(miss)
    ck("missing marker aborts stage before packing", p.returncode != 0, p.stderr)
    ck("missing marker produced no tarball",
       not os.path.exists(os.path.join(ws, "dist", "selftest-markermiss-v1.tar.xz")))

    noprep = dict(ROW, family="selftest-emptydir", dest="$HOME/nothing-here",
                  prepare="mkdir -p $HOME/nothing-here")
    p = stage(noprep)
    ck("an empty build tree aborts stage", p.returncode != 0, p.stderr)

    shutil.rmtree(w, ignore_errors=True)
    print("\n%d check(s) failed" % len(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
