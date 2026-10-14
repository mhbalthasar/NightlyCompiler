#!/usr/bin/env python3
"""
test_envpkgs_table.py - check the package table and the version/pruning logic.

These are the parts that can be proven without spending CI minutes: a row that
names a script that does not exist fails two hours into a runner, and a pruning
mistake silently deletes the package some consumer just started depending on.

Run:  python3 scripts/test_envpkgs_table.py
"""
import hashlib
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import envpkgs  # noqa: E402

CONF_PATH = os.path.join(HERE, "envpkgs.conf.json")
fails = []


def ck(label, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + label)
    if not cond:
        fails.append(label)
        if detail:
            print("      " + str(detail).replace("\n", "\n      ")[:500])


def main():
    with open(CONF_PATH, encoding="utf-8") as fh:
        c = json.load(fh)
    rows = c["packages"]

    ck("release tag is the one consumers ask for", c["release"]["tag"] == "envpkgs")
    ck("families unique", len({r["family"] for r in rows}) == len(rows),
       sorted(r["family"] for r in rows))

    # A row is only runnable if the scripts it names are in this repo (or the
    # consumer checkouts it declares) and the repos it names exist in the table.
    for r in rows:
        prep = r["prepare"].split()
        for arg in prep:
            if arg.endswith(".sh"):
                ck("%s prepare exists: %s" % (r["family"], arg), os.path.isfile(os.path.join(HERE, os.pardir, arg)))
        ck("%s verify exists" % r["family"],
           os.path.isfile(os.path.join(HERE, os.pardir, r["verify"])))
        ck("%s repos declared" % r["family"],
           all(n in c["repos"] for n in r.get("repos", [])), r.get("repos"))
        ck("%s dest is absolute-after-expansion" % r["family"], r["dest"].startswith("$HOME"))
        ck("%s timeout given" % r["family"], isinstance(r.get("timeout_minutes"), int))

    # --- the contract between the table and the CI steps -------------------
    # Both directions matter: a family nobody asks for is dead weight that still
    # costs a build, and a name a CI step asks for that the table does not publish
    # is a step that silently always falls back to compiling - which looks like
    # success and saves nothing. Read from the workflows rather than a hardcoded
    # list, so the two cannot drift unnoticed.
    #
    # Fail closed: an unreadable workflow means the contract was NOT checked, and
    # a vacuous pass here is worse than no test at all.
    ci_files = {
        "daisium": ["Daisium/.github/workflows/build.yml"],
        "dp": ["Daisium_Predict/.github/workflows/ci.yml"],
        "opencyan": ["libOpenCyan/.github/workflows/build.yml",
                     "libOpenCyan/.github/workflows/build-one.yml"],
    }
    # Two layouts: a dev box keeps the repos as siblings of this one; the CI
    # selftest job clones them into this repo's workspace root.
    roots = [os.path.normpath(os.path.join(HERE, os.pardir)),
             os.path.normpath(os.path.join(HERE, os.pardir, os.pardir))]
    requests, unreadable = set(), []
    for repo, files in sorted(ci_files.items()):
        found = False
        for f in files:
            path = next((os.path.join(r, f) for r in roots
                         if os.path.isfile(os.path.join(r, f))), None)
            if not path:
                # The conf spells some checkouts differently from the repo name.
                alt = os.path.join(c["repos"][repo]["dir"], *f.split("/")[1:])
                path = next((os.path.join(r, alt) for r in roots
                             if os.path.isfile(os.path.join(r, alt))), None)
            if not path:
                continue
            found = True
            body = open(path, encoding="utf-8").read()
            # Some steps pass the family in a shell variable (the matrix branches
            # set it per row). Harvest `fam=<literal>` occurrences - all of them,
            # since one variable name carries one value per branch - so an
            # indirected call site is still checked. An empty prefix would match
            # every family and turn this test back into a pass-no-matter-what.
            assigns = {}
            for k, v in re.findall(r"^\s*(fam\w*)=([A-Za-z0-9_.-]+)", body, re.M):
                assigns.setdefault(k, set()).add(v)
            for m in re.findall(r"envpkg\.sh\s+restore\s+\"?([A-Za-z0-9_.$/-]+)", body):
                tok = m.split("$")[0].rstrip('"')
                var = re.sub(r"[\"$]", "", m.split("{")[0]).strip()
                if var in assigns:
                    requests |= assigns[var]
                    continue
                if tok:
                    requests.add(tok)
        if not found:
            unreadable.append(repo)
    # A repo the token cannot reach is a recorded fact from the clone step, not a
    # hole in this test: skip exactly those, and still fail if something else makes
    # a workflow unreadable (that is how a vacuous pass starts).
    status = {}
    sp = os.path.join(HERE, os.pardir, "dist", "clone-status.json")
    if os.path.isfile(sp):
        status = json.load(open(sp, encoding="utf-8"))
    blocked = sorted(r for r in unreadable if status.get(r) == "no-access")
    if blocked:
        print("NOTE  no read access to %s with this token; its contract is not "
              "checked here" % ", ".join(blocked))
    ck("reachable repos' workflows are readable",
       not [r for r in unreadable if r not in blocked],
       "missing checkout for %s - run `python3 scripts/envpkgs.py clone --all`"
       % [r for r in unreadable if r not in blocked])
    published = envpkgs.families()
    # A family whose prepare step shells into a repo this token cannot reach is
    # not "published but unrequested" - it is unpublished-by-necessity, and its
    # requester lives in the checkout we could not fetch. Derive that from the row
    # (which names its repos) rather than from the family's spelling.
    unbuildable = {envpkgs.fill(p["family"]) for p in rows
                   if any(status.get(r) == "no-access" for r in p.get("repos", []))}
    if requests:
        ck("every CI request resolves to a published family",
           all(any(f.startswith(r) for f in published) for r in requests),
           "requests=%s" % sorted(requests))
        orphans = [f for f in published
                   if f not in unbuildable and not any(f.startswith(r) for r in requests)]
        ck("every buildable family is requested by some CI step", not orphans, orphans)
    else:
        # Rollout order: this repo's CI can be published before the consumer repos
        # are wired to it, and a first run with zero call sites is not a contract
        # violation - it is the state before step two. Once any consumer asks for a
        # family, both directions above become fatal again.
        print("NOTE  no envpkg.sh call sites in the reachable consumer workflows; "
              "%d family(s) published, request checks skipped" % len(published))

    # --- vendored recipes must still be the file they were taken from ----------
    # Vendoring is what lets this CI build a tree without read access to the
    # upstream repo, so the pin is the only thing keeping the copy from becoming a
    # second, quietly different recipe. The copy is always checked against the
    # pinned hash; upstream is checked too whenever its checkout is reachable (a
    # dev box with the repos as siblings, or CI once the PAT covers that repo).
    for p in rows:
        vf = p.get("vendored_from")
        if not vf:
            continue
        copy = os.path.normpath(os.path.join(HERE, os.pardir, vf["copy"]))
        here = hashlib.sha256(open(copy, "rb").read()).hexdigest() if os.path.isfile(copy) else ""
        ck("%s: vendored copy matches its pinned hash" % p["family"],
           here == vf["sha256"],
           "pinned=%s local=%s" % (vf["sha256"][:12], here[:12] or "MISSING"))
        dirnames = [v["dir"] for v in c["repos"].values() if v["full_name"] == vf["repo"]]
        # The dev box spells the sibling as <repo name>, CI clones it as the conf's
        # `dir`, so both spellings are candidates at both root levels.
        names = [vf["repo"].split("/")[-1]] + dirnames
        candidates = [os.path.normpath(os.path.join(r, n, vf["path"]))
                      for r in roots for n in names]
        up = next((x for x in candidates if os.path.isfile(x)), None)
        if up:
            up_sha = hashlib.sha256(open(up, "rb").read()).hexdigest()
            ck("%s: vendored copy still matches upstream" % p["family"],
               up_sha == vf["sha256"],
               "upstream=%s pinned=%s  (%s)" % (up_sha[:12], vf["sha256"][:12], up))
        else:
            print("NOTE  %s not readable here; upstream drift check skipped for %s"
                  % (vf["repo"], p["family"]))

    # --- family templating -------------------------------------------------
    os.environ["MACOS_SDK_VER"] = "12.0"
    fams = envpkgs.families()
    ck("MACOS_SDK_VER moves the opencyan family",
       "osxcross-macosx12.0-opencyan" in fams, fams)
    ck("MACOS_SDK_VER leaves the dp family pinned",
       "osxcross-macosx11.3-dp" in fams, fams)
    row = [p for p in envpkgs.package_rows([]) if p["family"] == "osxcross-macosx12.0-opencyan"][0]
    ck("its SDK marker follows the version",
       "SDK/MacOSX12.0.sdk" in row["markers"], row["markers"])
    os.environ.pop("MACOS_SDK_VER")

    # --- --only filtering --------------------------------------------------
    ck("--only matches substrings",
       {r["family"] for r in envpkgs.package_rows(["ollvm"])} ==
       {f for f in published if f.startswith("ollvm")})
    ck("--only with several terms unions",
       len(envpkgs.package_rows(["ollvm", "osxcross-macosx11.3-dp"])) == 2)

    # --- version selection and pruning -------------------------------------
    def asset(name, i=1, size=100):
        return (name, i, size, "2026-10-14T00:00:00Z")

    fam = "ollvm23-x86-loongarch-linux"
    keep, sup = envpkgs.select_survivors([
        asset(fam + "-v1.tar.xz"), asset(fam + "-v2.tar.xz"), asset(fam + "-v10.tar.xz"),
    ])
    ck("highest version survives", [k[0] for k in keep] == [fam + "-v10.tar.xz"], keep)
    ck("-v10 outranks -v2 (not lexicographic)",
       sorted(k[0] for k in sup) == [fam + "-v1.tar.xz", fam + "-v2.tar.xz"], sup)

    keep, sup = envpkgs.select_survivors([
        asset(fam + "-v1.tar.xz"), asset("osxcross-macosx11.3-dp-v3.tar.xz"),
        # A family the table no longer describes (sysroots were dropped as
        # unshareable): its assets stay on the release, they are just not ours to
        # prune. Deleting what we cannot describe is how a rename erases history.
        asset("dp-sysroot-arm64-v4.tar.xz"), asset("someone-elses-thing.zip"),
        asset(fam + "-vX.tar.xz"),
    ])
    names = sorted(k[0] for k in keep)
    ck("other families untouched", fam + "-v1.tar.xz" in names, names)
    ck("assets this table does not describe are never pruned",
       "someone-elses-thing.zip" in names and "dp-sysroot-arm64-v4.tar.xz" in names
       and fam + "-vX.tar.xz" in names, names)
    ck("nothing superseded here", sup == [], sup)

    # `split_asset` is what decides whether prune dares to DELETE something.
    ck("split recognises managed name",
       envpkgs.split_asset(fam + "-v12.tar.xz") == (fam, 12))
    ck("split ignores a near-miss prefix",
       envpkgs.split_asset(fam + "-linux-v2.tar.xz") == (None, None),
       envpkgs.split_asset(fam + "-linux-v2.tar.xz"))
    ck("split ignores a retired family",
       envpkgs.split_asset("dp-sysroot-arm64-v4.tar.xz") == (None, None))
    ck("split ignores non-numeric version", envpkgs.split_asset(fam + "-vX.tar.xz") == (None, None))

    # --- plan output shape (what the matrix consumes) ----------------------
    import tempfile
    fd, out = tempfile.mkstemp(prefix="envpkgs-plan-")
    os.close(fd)
    try:
        subprocess.run([sys.executable, os.path.join(HERE, "envpkgs.py"), "plan",
                        "--only", "ollvm", "--outputs", out],
                       check=True, capture_output=True, text=True, env=dict(
                           os.environ, MACOS_SDK_VER="11.3"))
        line = [l for l in open(out, encoding="utf-8").read().splitlines()
                if l.startswith("matrix=")][0][len("matrix="):]
        m = json.loads(line)
        ck("plan emits one row per package", len(m) == 1, m)
        ck("plan row carries runner and timeout",
           m[0]["runner"].startswith("ubuntu") and m[0]["timeout"] == 480, m[0])
        ck("plan row nests the full package", m[0]["pkg"]["family"] == m[0]["family"])
        ck("plan JSON is single-line (GITHUB_OUTPUT cannot carry a newline)",
           "\n" not in line)
    finally:
        os.remove(out)

    print("\n%d check(s) failed" % len(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
