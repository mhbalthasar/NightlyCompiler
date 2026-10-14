#!/usr/bin/env python3
"""
envpkgs.py - build, verify and publish the prebuilt build-environment packages.

The three downstream repos (Daisium, Daisium_Predict, libOpenCyan) each spend
part of every build run compiling the *toolchains* they build with: a
source-built Hikari OLLVM for LoongArch, osxcross + an Apple SDK, and Debian
sysroots. Those trees do not depend on the product sources, so rebuilding them
per run buys nothing - and the only place they could live was actions/cache,
which evicts after 7 days, is scoped per repo, and only saves on job success.

This script turns that cache into a GitHub Release on this repo:

  plan     : envpkgs.conf.json -> matrix JSON (one job per package)
  clone    : fetch the consumer repos a package's prepare step shells into
  stage    : run the package's prepare step, tar the tree, then EXTRACT THE
             TARBALL into a clean directory and verify it there
  publish  : upsert the tarball as a release asset  <family>-v<N>.tar.xz
  prune    : keep the highest version per family, drop the rest, refresh the
             release body

Verification runs against the extracted tarball and not against the build
directory: the thing consumers restore is the tarball, and a tree that only
works where it was built (absolute symlinks, wrappers that baked in their
install path) is exactly the failure this job exists to prevent.

Naming is by family, and a family name carries everything that changes the
bytes (SDK version, target list, which repo's prep script produced the tree).
Consumers ask for a family by name; they never compute keys. That is the same
contract actions/cache gave them, with durable storage and no per-repo scope.

Modes read their secrets/config from env only, never argv, so a token cannot
end up in a process list or in the step name GitHub echoes to the log.
"""
import argparse
import base64
import datetime
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(HERE, "envpkgs.conf.json")


def conf():
    with open(CONF, "r", encoding="utf-8") as fh:
        return json.load(fh)


def template_vars():
    """Placeholders available inside conf strings. Only the knobs a workflow
    input can move; anything else belongs in the conf file itself."""
    return {"macos_sdk_ver": os.environ.get("MACOS_SDK_VER", "11.3")}


def fill(s):
    try:
        return s.format(**template_vars())
    except KeyError as e:
        sys.exit("[envpkgs] conf string %r references unknown placeholder %s" % (s, e))


def resolve_dest(expr):
    """Conf dests are shell expressions ($HOME/...); expand with bash so the
    value matches what the prepare step actually wrote. The %%s is bash's, not
    python's - writing it unescaped makes this line raise before any job runs."""
    out = subprocess.run(["bash", "-c", 'printf "%%s" "%s"' % expr],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def package_rows(only):
    rows = []
    for p in conf()["packages"]:
        missing = [k for k in ("family", "runner", "dest", "prepare", "verify", "markers")
                   if not p.get(k)]
        if missing:
            sys.exit("[envpkgs] package row %r is missing %s" % (p.get("family", "?"), missing))
        family = fill(p["family"])
        if only and not any(o in family for o in only):
            continue
        row = dict(p)
        row["family"] = family
        row["markers"] = [fill(m) for m in p["markers"]]
        rows.append(row)
    return rows


def emit(outfile, key, value):
    with open(outfile, "a", encoding="utf-8") as fh:
        fh.write("%s=%s\n" % (key, value))


def sh(cmd, cwd=None, env=None, echo=True):
    """Stream to the log: an OLLVM build runs for hours and a buffered run
    looks like a hang."""
    printable = cmd if isinstance(cmd, str) else " ".join(cmd)
    if echo:
        print("+ %s" % printable, flush=True)
    p = subprocess.run(cmd, cwd=cwd, env=env, shell=isinstance(cmd, str))
    if p.returncode != 0:
        raise SystemExit("[envpkgs] command failed (%d): %s" % (p.returncode, printable))
    return p


def _redact(token, text):
    return text.replace(token, "***") if token else text


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------
def cmd_plan(a):
    only = a.only.split() if a.only else []
    rows = package_rows(only)
    if not rows:
        sys.exit("[envpkgs] no package matched %r" % a.only)
    matrix = json.dumps([{
        "id": r["family"],
        "family": r["family"],
        "desc": r["desc"],
        "runner": r["runner"],
        # Flattened because jobs.<id>.timeout-minutes cannot reach into a nested
        # matrix value; the rest of the row travels as one JSON blob (see the
        # UNIT_JSON trick in daisium_predict_compiler.yaml).
        "timeout": int(r.get("timeout_minutes", 120)),
        "pkg": r,
    } for r in rows], separators=(",", ":"))
    if "\n" in matrix:
        sys.exit("[envpkgs] matrix JSON contains a newline - GITHUB_OUTPUT cannot carry it")
    out = a.outputs or os.environ.get("GITHUB_OUTPUT", "")
    if not out:
        print(matrix)
        return
    emit(out, "matrix", matrix)
    print("planned %d package(s): %s" % (len(rows), ", ".join(r["family"] for r in rows)))


# --------------------------------------------------------------------------
# clone
# --------------------------------------------------------------------------
def refs():
    d = {}
    for kv in os.environ.get("ENVPKG_REFS", "").split(","):
        k, _, v = kv.partition("=")
        if k:
            d[k] = v or "main"
    return d


def clone_env(pat):
    """Env for a private clone. Two things the first CI run learned the hard way:

    * GitHub's git-over-HTTPS endpoint does not accept `Authorization: Bearer
      <PAT>` - it answers 401, git falls back to asking for a username, and a
      runner has no terminal, so the step dies with "could not read Username".
      Basic with any username and the token as password is what works, which is
      exactly what actions/checkout sets up (its post-job cleanup unsets that
      same extraheader).
    * The header goes through GIT_CONFIG_* rather than `-c` on the command line,
      so the credential is not in the process arguments that a traceback or a
      `ps` snapshot can echo. base64 of a secret is not masked by Actions
      either, so keeping it out of the visible command line matters.
    """
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if pat:
        blob = base64.b64encode(("x-access-token:" + pat).encode()).decode()
        env.update({"GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "http.extraHeader",
                    "GIT_CONFIG_VALUE_0": "Authorization: Basic " + blob})
    return env


def clone_status_path():
    return os.path.join("dist", "clone-status.json")


def record_clone(name, state):
    """Remember why a checkout is or is not here. `stage` and the table test read
    this to tell "the token cannot reach that repo" (skip, say so) from "the clone
    broke for some other reason" (fail) - and to keep a repo that was never
    fetched from turning a contract check into a silent no-op."""
    p = clone_status_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    d = {}
    if os.path.isfile(p):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except ValueError:
            d = {}
    d[name] = state
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=2, sort_keys=True)


def cmd_clone(a):
    c = conf()
    names = list(c["repos"]) if a.all else json.loads(os.environ["PKG_JSON"]).get("repos", [])
    pat = os.environ.get("CI_PAT_STRING", "").strip()
    blocked = []
    for name in names:
        spec = c["repos"][name]
        dest = spec["dir"]
        ref = refs().get(name, "main")
        if os.path.isdir(dest):
            print("[envpkgs] %s already present, skipping clone" % dest)
            record_clone(name, "ok")
            continue
        url = "https://github.com/%s.git" % spec["full_name"]
        cmd = ["git", "clone", "--depth", "1", "--branch", ref, url, dest]
        print("[envpkgs] cloning %s @ %s -> %s" % (spec["full_name"], ref, dest))
        p = subprocess.run(cmd, env=clone_env(pat), capture_output=True, text=True)
        if p.returncode != 0:
            # 403 is a scope answer, not a build failure: a fine-grained PAT lists
            # the repositories it may read, and one left out answers 403. Treat it
            # as "this package cannot be produced here", warn, and let stage skip
            # the packages that need it - the ones the token CAN reach still build
            # and publish in the same run.
            #
            # 401 and "could not read Username" are NOT the same case: a rejected
            # or missing credential means this repo's setup is broken, and every
            # package would silently "skip" to a green run that publishes nothing.
            err = p.stderr
            if re.search(r"error: 403|permission denied", err, re.I):
                print("::warning::[envpkgs] no read access to %s - packages built "
                      "from its scripts will be skipped" % spec["full_name"])
                record_clone(name, "no-access")
                blocked.append(spec["full_name"])
                continue
            raise SystemExit("[envpkgs] clone failed: %s\n%s" % (
                spec["full_name"], _redact(pat, err)))
        record_clone(name, "ok")
        sha = subprocess.run(["git", "-C", dest, "rev-parse", "HEAD"],
                             capture_output=True, text=True).stdout.strip()
        print("[envpkgs] %s @ %s" % (dest, sha[:12]))
    if blocked:
        print("[envpkgs] unreachable repos: %s" % ", ".join(blocked))


# --------------------------------------------------------------------------
# staging
# --------------------------------------------------------------------------
def check_markers(root, markers, label):
    missing = []
    for m in markers:
        if not glob.glob(os.path.join(root, m)):
            missing.append(m)
    if missing:
        listing = "\n".join(sorted(os.listdir(root))[:40])
        raise SystemExit("[envpkgs] %s: markers absent %s\n%s contains: %s" % (
            label, missing, root, listing))
    print("[envpkgs] %s: %d marker(s) ok" % (label, len(markers)))


def provenance(row):
    c = conf()
    pins = {}
    path = os.environ.get("ENVPKG_PINS", "")
    if path and os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                k, _, v = line.strip().partition("=")
                if k:
                    pins[k] = v
    return {
        "family": row["family"],
        "desc": row["desc"],
        "built_at": datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="seconds"),
        "nightlycompiler_sha": os.environ.get("GITHUB_SHA", "")[:12],
        "runner": os.environ.get("RUNNER_OS", "") + "/" + os.environ.get("ImageOS", ""),
        "source_repos": {
            n: {"full_name": c["repos"][n]["full_name"],
                "ref": refs().get(n, "main"),
                "sha": subprocess.run(["git", "-C", c["repos"][n]["dir"], "rev-parse", "HEAD"],
                                      capture_output=True, text=True).stdout.strip()[:12]
                if os.path.isdir(c["repos"][n]["dir"]) else ""}
            for n in row.get("repos", [])
        },
        "prepare": row["prepare"],
        # What the prepare step says it produced: resolved upstream commit, SDK
        # tarball digest, glibc floor, prep-script hash. This is the only record of
        # "which tree is this", because a family name cannot carry a sid snapshot.
        "pins": pins,
        "env": {k: os.environ.get(k, "") for k in
                ("OLLVM_REV", "OSXCROSS_REV", "MACOS_SDK_VER", "DEBIAN_MIRROR")},
    }


def cmd_stage(a):
    row = json.loads(os.environ["PKG_JSON"])
    out = a.outputs or os.environ.get("GITHUB_OUTPUT", "")
    missing = [n for n in row.get("repos", [])
               if not os.path.isdir(conf()["repos"][n]["dir"])]
    if missing:
        # The tree is produced by that repo's own prep script, and the script is
        # not copied here on purpose - so without the checkout there is nothing
        # honest to build. Say which repo and which package, and let the run go on.
        print("::warning::[envpkgs] skipping %s: needs the %s checkout (not readable "
              "with this token)" % (row["family"], ", ".join(missing)))
        if out:
            emit(out, "asset", "")
            emit(out, "skipped", ",".join(missing))
        return
    dest = resolve_dest(row["dest"])
    os.makedirs("dist", exist_ok=True)
    # Hand the prepare steps their paths instead of letting each script re-derive
    # them: conf's `dest` is the one authority on where the tree must be.
    os.environ["ENVPKG_DEST"] = dest
    os.environ["ENVPKG_PINS"] = os.path.abspath(os.path.join("dist", "pins.txt"))
    print("::group::prepare %s" % row["family"])
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    sh(row["prepare"])
    print("::endgroup::")
    if not os.path.isdir(dest) or not os.listdir(dest):
        raise SystemExit("[envpkgs] %s left nothing at %s" % (row["family"], dest))
    check_markers(dest, row["markers"], "built tree")

    version = next_version(row["family"])
    asset = "%s-v%d.tar.xz" % (row["family"], version)
    path = os.path.join("dist", asset)
    meta = provenance(row)
    meta["asset"] = asset
    with open(os.path.join(dest, ".envpkg.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, sort_keys=True)

    print("::group::pack %s" % asset)
    # -T0: a 1.5 GB toolchain tree takes ~10 minutes to xz single-threaded.
    # Preserves symlinks as symlinks, which is what makes the tree relocatable;
    # the verification step below is what proves it.
    sh("tar -C %s --format=pax -cf - . | xz -T0 -6 > %s"
       % (shlex.quote(dest), shlex.quote(path)))
    print("::endgroup::")

    print("::group::verify extracted %s" % asset)
    w = tempfile.mkdtemp(prefix="envpkg-verify-")
    try:
        tree = os.path.join(w, "pkg")
        os.makedirs(tree)
        sh("xz -dc %s | tar -xf - -C %s" % (shlex.quote(path), shlex.quote(tree)))
        check_markers(tree, row["markers"], "extracted tree")
        env = dict(os.environ, PKG_DIR=tree)
        env.update(row.get("verify_env", {}))
        printable = "%s %s" % (row["verify"], tree)
        if row.get("verify_env"):
            printable += "  (env %s)" % row["verify_env"]
        print("+ %s" % printable)
        p = subprocess.run(["bash", "-e", row["verify"], tree], env=env)
        if p.returncode != 0:
            raise SystemExit("[envpkgs] verify failed for %s - not publishing a tree "
                             "we cannot prove is usable" % row["family"])
    finally:
        shutil.rmtree(w, ignore_errors=True)
    print("::endgroup::")

    size = os.path.getsize(path)
    digest = sha256(path)
    if out:
        emit(out, "asset", asset)
        emit(out, "path", path)
        emit(out, "sha256", digest)
    print("[envpkgs] staged %s (%.1f MiB, sha256 %s)" % (asset, size / 1048576.0, digest[:12]))


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for blk in iter(lambda: fh.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def gh(*args):
    sh(["gh"] + list(args))


def next_version(family):
    """Highest existing -vN for this family plus one. Unknown release (first
    ever) or an unparseable name both mean 1 rather than an error: the producer
    must still work on a cold repo."""
    tag = conf()["release"]["tag"]
    p = subprocess.run(["gh", "release", "view", tag, "--json", "assets",
                        "-q", ".assets[].name"], capture_output=True, text=True)
    names = p.stdout.split() if p.returncode == 0 else []
    prefix = family + "-v"
    vs = [int(n[len(prefix):].split(".")[0]) for n in names
          if n.startswith(prefix) and n[len(prefix):].split(".")[0].isdigit()]
    return (max(vs) + 1) if vs else 1


# --------------------------------------------------------------------------
# publish / prune
# --------------------------------------------------------------------------
def ensure_release():
    """Create the rolling release if it is not there yet, and return its tag.
    publish needs it to upload into, and prune needs it because `gh release edit`
    on a cold repo fails with "release not found" - which is precisely the state
    of the very first run, where prune executes after every build job."""
    r = conf()["release"]
    tag = r["tag"]
    p = subprocess.run(["gh", "release", "view", tag], capture_output=True, text=True)
    if p.returncode != 0:
        print("[envpkgs] release %s does not exist yet - creating" % tag)
        gh("release", "create", tag, "--target", r["target"], "--title", r["title"],
           "--notes", r["notes"])
    return tag


def cmd_publish(a):
    tag = ensure_release()
    path = os.environ["PKG_PATH"]
    asset = os.path.basename(path)
    if not os.path.isfile(path):
        raise SystemExit("[envpkgs] nothing to publish at %s" % path)
    # --clobber replaces a same-named asset; distinct version names mean a
    # rebuild never destroys the tree some other job just restored.
    gh("release", "upload", tag, path, "--clobber")
    print("[envpkgs] published %s" % asset)
    out = a.outputs or os.environ.get("GITHUB_OUTPUT", "")
    if out:
        emit(out, "published", asset)


def families():
    return [fill(p["family"]) for p in conf()["packages"]]


def split_asset(name):
    """-> (family, version) for a managed asset, else (None, None). Anything
    not matching `<known family>-v<N>.tar.xz` is left alone by prune."""
    for f in families():
        prefix = f + "-v"
        if name.startswith(prefix):
            stem = name[len(prefix):]
            if stem.endswith(".tar.xz"):
                num = stem[:-len(".tar.xz")]
                if num.isdigit():
                    return f, int(num)
    return None, None


def cmd_prune(a):
    tag = ensure_release()
    keep, superseded = select_survivors(assets_of(tag))
    for name, asset_id, _size, _updated in superseded:
        print("[envpkgs] pruning superseded asset %s" % name)
        gh("api", "-X", "DELETE", "repos/{owner}/{repo}/releases/assets/%s" % asset_id)
    # Re-read what is actually on the release instead of reconstructing it: the
    # body is what a human trusts, so it must come from the same list a
    # consumer's curl sees.
    write_body(tag)
    print("[envpkgs] %d managed package(s) published, %d superseded asset(s) removed"
          % (len([r for r in keep if split_asset(r[0])[0]]), len(superseded)))


def assets_of(tag):
    p = subprocess.run(["gh", "release", "view", tag, "--json", "assets",
                        "-q", "[.assets[] | {name, id, size, updatedAt}] | .[] | "
                        "[.name, .id, .size, .updatedAt] | @tsv"],
                       capture_output=True, text=True)
    if p.returncode != 0:
        return []
    rows = []
    for line in p.stdout.splitlines():
        if not line.strip():
            continue
        name, asset_id, size, updated = line.split("\t")
        rows.append((name, int(asset_id), int(size), updated))
    return rows


def select_survivors(rows):
    """Split a release's assets into (surviving, superseded). Per family the
    highest -vN survives. Assets this conf does not describe are survivors too:
    prune only ever deletes names it can parse, and leaving them out of the return
    value would misreport what is still on the release."""
    best = {}
    unmanaged = []
    superseded = []
    for r in rows:
        fam, ver = split_asset(r[0])
        if fam is None:
            unmanaged.append(r)
            continue
        cur = best.get(fam)
        if cur is None or ver > cur[1]:
            if cur is not None:
                superseded.append(cur[0])
            best[fam] = (r, ver)
        else:
            superseded.append(r)
    return [v[0] for v in best.values()] + unmanaged, superseded


def write_body(tag):
    rows = assets_of(tag)
    lines = [conf()["release"]["notes"], "",
             "| package | asset | size |", "|---|---|---|"]
    for r in sorted(rows, key=lambda x: x[0]):
        fam, ver = split_asset(r[0])
        label = fam if fam else "(unmanaged)"
        lines.append("| %s | %s | %.1f MiB |" % (label, r[0], r[2] / 1048576.0))
    lines += ["", "Regenerated by `.github/workflows/envpkgs_compiler.yaml`."]
    fd, path = tempfile.mkstemp(prefix="envpkg-body-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        gh("release", "edit", tag, "--notes-file", path)
    finally:
        os.unlink(path)


def main():
    ap = argparse.ArgumentParser(prog="envpkgs.py")
    sub = ap.add_subparsers(dest="mode", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--only", default="")
    p.add_argument("--outputs", default=os.environ.get("GITHUB_OUTPUT", ""))
    p.set_defaults(fn=cmd_plan)
    p = sub.add_parser("clone")
    p.add_argument("--all", action="store_true",
                   help="clone every repo in the table, not just the row's (the "
                        "selftest job needs all three to check the contract)")
    p.set_defaults(fn=cmd_clone)
    p = sub.add_parser("stage")
    p.add_argument("--outputs", default=os.environ.get("GITHUB_OUTPUT", ""))
    p.set_defaults(fn=cmd_stage)
    p = sub.add_parser("publish")
    p.add_argument("--outputs", default=os.environ.get("GITHUB_OUTPUT", ""))
    p.set_defaults(fn=cmd_publish)
    p = sub.add_parser("prune")
    p.set_defaults(fn=cmd_prune)
    p = sub.add_parser("families")
    p.set_defaults(fn=lambda a: print("\n".join(families())))
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
