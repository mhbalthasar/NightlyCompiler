#!/usr/bin/env python3
"""Regression test for ci.yml cache handling in daisium_ci_runner.py.

This is the contract that makes the loongarch64 OLLVM rebuild (88 minutes of a
91-minute job) avoidable:

  * `plan` cannot decide `if: steps.<id>.outputs.cache-hit != 'true'` - it runs
    on its own runner, before any cache has been restored.  So it must forward
    the cache step (path / key / id) as unit["cache"] and carry such conditions
    into the block as `deferred_if` instead of resolving them.
  * `exec-unit` seeds steps.<id>.outputs.cache-hit from DP_CACHE_STEP_ID /
    DP_CACHE_HIT (the workflow's real actions/cache@v4 step) and skips the
    deferred block on a hit.

Uses `shell: python3` blocks so it runs anywhere without a POSIX shell.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
RUNNER = os.path.join(HERE, "daisium_ci_runner.py")

CI_YML = """\
name: ci
on: [workflow_dispatch]
jobs:
  build:
    name: loong64
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4

      - name: Cache toolchain
        id: tc-cache
        uses: actions/cache@v4
        with:
          path: ~/tools/tc
          key: dp-tc-ubuntu-v1

      - name: Build toolchain from source
        if: steps.tc-cache.outputs.cache-hit != 'true'
        shell: python3
        run: |
          import pathlib
          pathlib.Path("toolchain-built.txt").write_text("built")

      - name: Real build
        shell: python3
        run: |
          import pathlib
          pathlib.Path("real-build.txt").write_text("ok")

      - uses: actions/upload-artifact@v4
        with:
          name: build-loong64
          path: out/
"""


def run_runner(args, env_extra=None, cwd=None):
    env = {k: v for k, v in os.environ.items()
           if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
    env.update(env_extra or {})
    return subprocess.run([sys.executable, RUNNER] + args, env=env, cwd=cwd,
                          capture_output=True, text=True)


def main():
    try:
        import yaml  # noqa: F401
    except ImportError:
        print("[skip] PyYAML not available")
        return 0

    tmp = tempfile.mkdtemp(prefix="cachetest_")
    ci = os.path.join(tmp, "ci.yml")
    outs = os.path.join(tmp, "plan_out.txt")
    repodir = os.path.join(tmp, "dp")
    os.makedirs(repodir, exist_ok=True)
    with open(ci, "w", encoding="utf-8") as fh:
        fh.write(CI_YML)

    failures = []

    # ---- plan: cache spec forwarded, step-output `if:` deferred ----
    p = run_runner(["plan", "--ci", ci, "--ref", "main", "--repo-dir", repodir,
                    "--outputs", outs])
    if p.returncode != 0:
        print(p.stdout, p.stderr)
        print("[FAIL] plan exited %d" % p.returncode)
        return 1
    unit = None
    with open(outs, encoding="utf-8") as fh:
        for line in fh:
            k, v = line.split("=", 1)
            if k == "layer0":
                unit = json.loads(v)[0]
    if unit is None:
        print("[FAIL] plan produced no layer0 unit")
        return 1

    want_cache = {"step_id": "tc-cache", "key": "dp-tc-ubuntu-v1",
                  "paths_joined": "~/tools/tc"}
    got_cache = {k: unit.get("cache", {}).get(k) for k in want_cache}
    if got_cache != want_cache:
        failures.append("plan: unit['cache'] = %r, want %r" % (got_cache, want_cache))
    else:
        print("[ok]   plan: cache forwarded -> %r" % (got_cache,))

    gated = [b for b in unit["blocks"] if b.get("deferred_if")]
    if len(gated) != 1 or gated[0]["name"] != "Build toolchain from source":
        failures.append("plan: expected exactly 1 deferred block, got %r"
                        % [(b["name"], b.get("deferred_if")) for b in unit["blocks"]])
    elif gated[0]["deferred_if"] != "steps.tc-cache.outputs.cache-hit != 'true'":
        failures.append("plan: deferred_if = %r" % gated[0]["deferred_if"])
    else:
        print("[ok]   plan: step-output `if:` deferred -> %s" % gated[0]["deferred_if"])

    # upload is recorded as unit["upload"], not as a block
    if len(unit["blocks"]) != 4:
        failures.append("plan: expected 4 blocks (checkout/cache/gated/real), got %d"
                        % len(unit["blocks"]))
    else:
        print("[ok]   plan: 4 blocks emitted (gated block kept for exec-unit to decide)")

    unit_json = json.dumps(unit)

    # ---- exec-unit on a MISS: the gated block must run ----
    for f in ("toolchain-built.txt", "real-build.txt"):
        fp = os.path.join(repodir, f)
        if os.path.exists(fp):
            os.remove(fp)
    p = run_runner(["exec-unit"], {"UNIT_JSON": unit_json, "DP_REPO_DIR": repodir,
                                   "DP_CACHE_STEP_ID": "tc-cache", "DP_CACHE_HIT": ""})
    built = os.path.exists(os.path.join(repodir, "toolchain-built.txt"))
    real = os.path.exists(os.path.join(repodir, "real-build.txt"))
    if p.returncode != 0:
        failures.append("exec-unit(miss) exit %d\n%s%s" % (p.returncode, p.stdout, p.stderr))
    elif not built or not real:
        failures.append("exec-unit(miss): built=%s real=%s (both must run)" % (built, real))
    elif "(cache tc-cache: MISS)" not in p.stdout:
        failures.append("exec-unit(miss): cache status not reported as MISS")
    else:
        print("[ok]   exec-unit MISS: cache reported MISS, gated build ran, real build ran")

    # ---- exec-unit on a HIT: the gated block must be skipped ----
    for f in ("toolchain-built.txt", "real-build.txt"):
        fp = os.path.join(repodir, f)
        if os.path.exists(fp):
            os.remove(fp)
    p = run_runner(["exec-unit"], {"UNIT_JSON": unit_json, "DP_REPO_DIR": repodir,
                                   "DP_CACHE_STEP_ID": "tc-cache", "DP_CACHE_HIT": "true"})
    built = os.path.exists(os.path.join(repodir, "toolchain-built.txt"))
    real = os.path.exists(os.path.join(repodir, "real-build.txt"))
    if p.returncode != 0:
        failures.append("exec-unit(hit) exit %d\n%s%s" % (p.returncode, p.stdout, p.stderr))
    elif built:
        failures.append("exec-unit(hit): gated block ran despite cache hit")
    elif not real:
        failures.append("exec-unit(hit): unrelated block was skipped too")
    elif "(cache tc-cache: HIT)" not in p.stdout:
        failures.append("exec-unit(hit): cache status not reported as HIT")
    else:
        print("[ok]   exec-unit HIT: cache reported HIT, gated build SKIPPED, rest ran")

    # ---- a hit must not be believed for a step the workflow never restored ----
    for f in ("toolchain-built.txt",):
        fp = os.path.join(repodir, f)
        if os.path.exists(fp):
            os.remove(fp)
    p = run_runner(["exec-unit"], {"UNIT_JSON": unit_json, "DP_REPO_DIR": repodir,
                                   "DP_CACHE_STEP_ID": "some-other-step",
                                   "DP_CACHE_HIT": "true"})
    built = os.path.exists(os.path.join(repodir, "toolchain-built.txt"))
    if p.returncode != 0 or not built:
        failures.append("exec-unit: mismatched DP_CACHE_STEP_ID must be ignored (built=%s)" % built)
    else:
        print("[ok]   exec-unit: DP_CACHE_STEP_ID for another step ignored (build still ran)")

    shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print()
        for f in failures:
            print("[FAIL] %s" % f)
        return 1
    print()
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
