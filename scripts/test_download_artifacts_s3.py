#!/usr/bin/env python3
"""Regression test for the S3 download branch of daisium_ci_runner.

There is no live bucket here, so this pins the two things that actually matter:

  1. DISPATCH - with DP_S3_PREFIX absent/empty the artifacts REST path must run
     unchanged. That is the whole "original route byte-for-byte" guarantee.
  2. ARGUMENT SHAPING - in S3 mode the aws CLI must be addressed with the exact
     key layout the upload side writes and that ci.yml's bundle job reads back
     (<prefix>/<artifact-name>/ -> <target>/<artifact-name>/).

Plus the guards that replace actions/upload-artifact's if-no-files-found: error.
"""
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.abspath(__file__))
RUNNER = os.path.join(REPO, "daisium_ci_runner.py")
failures = []


def load_runner():
    import importlib.util
    spec = importlib.util.spec_from_file_location("daisium_ci_runner", RUNNER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def check(cond, label, detail=""):
    if cond:
        print("[ok]   %s" % label)
    else:
        failures.append("%s%s" % (label, (" | " + detail) if detail else ""))
        print("[FAIL] %s%s" % (label, (" | " + detail) if detail else ""))


class FakeCompleted:
    def __init__(self, rc=0, out=""):
        self.returncode, self.stdout, self.stderr = rc, out, ""


def make_fake_aws(calls, ls_output):
    """Records every argv and answers `aws s3 ls` from a canned string."""
    def fake_run(cmd, *a, **kw):
        calls.append(list(cmd))
        if len(cmd) > 2 and cmd[2] == "ls":
            return FakeCompleted(0, ls_output)
        return FakeCompleted(0, "")
    return fake_run


# AWS CLI v2 right-aligns the PRE marker. The fixture must carry that padding:
# an unpadded "PRE name/" line is what let a ^PRE regex pass locally while the
# real listing matched nothing.
PADDED_LS = (" " * 27) + "PRE build-linux64/\n" \
            + (" " * 27) + "PRE build-win64_msvc/\n" \
            + (" " * 27) + "PRE build-wrapper/\n"


def test_common_prefix_parser(mod, failures):
    """Direct unit test on the parser, both real and unpadded shapes."""
    got = mod._s3_common_prefixes(PADDED_LS)
    if sorted(got) != ["build-linux64", "build-win64_msvc", "build-wrapper"]:
        failures.append("parser: padded PRE lines -> %s" % got)
    else:
        print("[ok]   parser: AWS CLI v2 padded `PRE` lines parse")
    unp = mod._s3_common_prefixes("PRE a/\nPRE b/\n2026-01-01 00:00:00 12 f.txt\n")
    if sorted(unp) != ["a", "b"]:
        failures.append("parser: unpadded PRE lines -> %s" % unp)
    else:
        print("[ok]   parser: unpadded PRE lines still parse, object lines ignored")


def main():
    import tempfile
    mod = load_runner()
    orig_rest = mod._download_artifacts_rest
    orig_s3 = mod._download_artifacts_s3
    # The aws CLI is not installed on dev machines; the S3 branch hard-guards for
    # it before touching subprocess, so stand in a path and fake the calls below.
    mod._which = lambda prog: "/usr/local/bin/aws" if prog == "aws" else None

    test_common_prefix_parser(mod, failures)

    # ---- 1. dispatch: no prefix -> REST, untouched ------------------------
    try:
        for label, env_prefix in (("absent", None), ("empty", ""), ("blank", "   ")):
            hits = {"rest": 0, "s3": 0}
            mod._download_artifacts_rest = lambda b, e: hits.__setitem__("rest", hits["rest"] + 1)
            mod._download_artifacts_s3 = lambda b, e: hits.__setitem__("s3", hits["s3"] + 1)
            env = {} if env_prefix is None else {"DP_S3_PREFIX": env_prefix}
            mod._download_artifacts({"target": "/tmp/x", "artifact": "a"}, env)
            check(hits == {"rest": 1, "s3": 0},
                  "dispatch: DP_S3_PREFIX %s -> artifacts REST path" % label, str(hits))
    finally:
        mod._download_artifacts_rest, mod._download_artifacts_s3 = orig_rest, orig_s3

    # dispatch must survive a real prefix too
    hits = {"rest": 0, "s3": 0}
    mod._download_artifacts_rest = lambda b, e: hits.__setitem__("rest", 1)
    mod._download_artifacts_s3 = lambda b, e: hits.__setitem__("s3", 1)
    try:
        mod._download_artifacts({"target": "/tmp/x", "artifact": "a"},
                                {"DP_S3_PREFIX": "Daisium_Predict/build-s3/1/1"})
        check(hits == {"rest": 0, "s3": 1}, "dispatch: prefix set -> S3 path", str(hits))
    finally:
        mod._download_artifacts_rest, mod._download_artifacts_s3 = orig_rest, orig_s3

    # ---- 2. argument shaping ---------------------------------------------
    base_env = {"DP_S3_PREFIX": "Daisium_Predict/build-s3/9/1", "BUCKET": "bkt",
                "S3_URL": "https://s3.example", "AWS_ACCESS_KEY_ID": "a",
                "AWS_SECRET_ACCESS_KEY": "s"}

    calls = []
    mod.subprocess.run = make_fake_aws(calls, PADDED_LS)
    try:
        tmp = tempfile.mkdtemp(prefix="s3_all_")
        # Two of the three "synced" dirs must look non-empty or the guard fires.
        for n in ("build-win64_msvc", "build-linux64", "build-wrapper"):
            os.makedirs(os.path.join(tmp, n), exist_ok=True)
            with open(os.path.join(tmp, n, "payload"), "w") as fh:
                fh.write("x")
        mod._download_artifacts_s3({"target": tmp, "artifact": ""}, dict(base_env))
        syncs = [c for c in calls if c[2] == "sync"]
        check(len(syncs) == 3, "all mode: one sync per PRE entry", str(len(syncs)))
        got = {c[3].rstrip("/").rsplit("/", 1)[-1]: c[4] for c in syncs}
        check(got.get("build-linux64") == os.path.join(tmp, "build-linux64"),
              "all mode: dest is <target>/<artifact-name>/", str(got))
        srcs = [c[3] for c in syncs]
        check("s3://bkt/Daisium_Predict/build-s3/9/1/build-linux64/" in srcs,
              "all mode: src carries prefix + name", str(srcs))
        ls = [c for c in calls if c[2] == "ls"]
        check(any(c[3].endswith("/9/1/") for c in ls),
              "all mode: enumerates the run prefix", str([c[3] for c in ls]))
        check(all("--endpoint-url" in c for c in calls),
              "every aws call carries --endpoint-url")
    finally:
        mod.subprocess.run = subprocess.run

    # named artifact must skip enumeration
    calls = []
    mod.subprocess.run = make_fake_aws(calls, "")
    try:
        tmp = tempfile.mkdtemp(prefix="s3_one_")
        os.makedirs(os.path.join(tmp, "build-wrapper"), exist_ok=True)
        with open(os.path.join(tmp, "build-wrapper", "f"), "w") as fh:
            fh.write("x")
        mod._download_artifacts_s3({"target": tmp, "artifact": "build-wrapper"},
                                   dict(base_env))
        check(not [c for c in calls if c[2] == "ls" and c[3].endswith("/9/1/")],
              "named mode: no enumeration of the whole prefix")
        check([c[3] for c in calls if c[2] == "sync"] ==
              ["s3://bkt/Daisium_Predict/build-s3/9/1/build-wrapper/"],
              "named mode: syncs exactly that one prefix")
    finally:
        mod.subprocess.run = subprocess.run

    # ---- 3. guards ---------------------------------------------------------
    def expect_exit(fn, needle, label):
        try:
            fn()
        except SystemExit as e:
            msg = str(e)
            check(needle in msg, label, msg)
            return
        except Exception as e:
            failures.append("%s: raised %r instead of SystemExit" % (label, e))
            print("[FAIL] %s: raised %r" % (label, e))
            return
        failures.append("%s: no error raised" % label)
        print("[FAIL] %s: nothing raised" % label)

    no_bkt = {k: v for k, v in base_env.items() if k != "BUCKET"}
    expect_exit(lambda: mod._download_artifacts_s3(
        {"target": "/tmp/z", "artifact": "a"}, no_bkt), "BUCKET",
        "guard: missing BUCKET is a named error")

    no_key = {k: v for k, v in base_env.items() if k != "AWS_SECRET_ACCESS_KEY"}
    expect_exit(lambda: mod._download_artifacts_s3(
        {"target": "/tmp/z", "artifact": "a"}, no_key), "AWS_SECRET_ACCESS_KEY",
        "guard: missing AWS key is a named error")

    # empty prefix listing -> cannot find anything to fetch
    mod.subprocess.run = make_fake_aws([], "")
    try:
        expect_exit(lambda: mod._download_artifacts_s3(
            {"target": tempfile.mkdtemp(prefix="s3_empty_"), "artifact": ""},
            dict(base_env)), "no artifact prefixes",
            "guard: empty run prefix fails loudly")
    finally:
        mod.subprocess.run = subprocess.run

    # sync exits 0 but transfers nothing -> must still fail (upload-artifact parity)
    calls = []
    mod.subprocess.run = make_fake_aws(calls, "")
    try:
        tmp = tempfile.mkdtemp(prefix="s3_zero_")
        expect_exit(lambda: mod._download_artifacts_s3(
            {"target": tmp, "artifact": "build-x"}, dict(base_env)), "0 files",
            "guard: sync that transfers nothing still fails")
    finally:
        mod.subprocess.run = subprocess.run

    print()
    if failures:
        for f in failures:
            print("[FAIL] %s" % f)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())