#!/usr/bin/env python3
"""
daisium_ci_runner.py - parse Daisium_Predict's .github/workflows/ci.yml and
execute its build steps locally (inside NightlyCompiler's Actions runners).

Modes:
  plan      : read ci.yml -> expand matrices -> emit normalized "units" (each
              unit = one runnable job/matrix combination with a list of
              ordered blocks: run / uses-mappings / upload meta) as JSON to
              $GITHUB_OUTPUT (layer0/layer1/layer2 = matrix fan-out arrays).
  exec-unit : read the UNIT_JSON env var and execute its blocks in order on
              the current runner.

Actions mapping (executed locally, no remote dispatch):
  actions/checkout           -> no-op (source already cloned into <repo-dir>)
  gha-setup-ninja            -> pip install ninja (target dir, PATH prepend)
  ilammy/msvc-dev-cmd        -> VsDevCmd.bat env import (arch from `with`)
  actions/setup-dotnet       -> verify preinstalled dotnet
  actions/cache              -> no-op (always cache-miss; dependent `if:` is
                                resolved at plan time -> consumer step runs)
  actions/download-artifact  -> same-run artifacts via REST (GITHUB_TOKEN)
  actions/upload-artifact    -> recorded as unit.upload (real upload happens
                                as a matrix-driven step in the workflow)
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import zipfile

SUPPORTED_USES = {
    "checkout": ["actions/checkout"],
    "ninja": ["seanmiddleditch/gha-setup-ninja"],
    "msvc": ["ilammy/msvc-dev-cmd"],
    "dotnet": ["actions/setup-dotnet"],
    "cache": ["actions/cache"],
    "download": ["actions/download-artifact"],
    "upload": ["actions/upload-artifact"],
}


def classify(uses):
    repo = uses.split("@")[0]
    for kind, prefixes in SUPPORTED_USES.items():
        if repo in prefixes:
            return kind
    return None


# --------------------------------------------------------------------------
# GitHub expression mini-interpreter: ${{ ... }} subset used by ci.yml
# --------------------------------------------------------------------------
def _truthy(x):
    if isinstance(x, str):
        return x != "" and x != "0" and x.lower() != "false"
    return bool(x)


def _ctx_get(ctx, path):
    cur = ctx
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return ""
    return "" if cur is None else cur


_F = {
    "startsWith": lambda a, b: str(a).startswith(str(b)),
    "endsWith": lambda a, b: str(a).endswith(str(b)),
    "contains": lambda a, b: (str(b) in a) if isinstance(a, (list, dict)) else (str(b).lower() in str(a).lower()),
    "format": lambda fmt, *a: re.sub(r"\{(\d+)\}", lambda m: str(a[int(m.group(1))] if int(m.group(1)) < len(a) else ""), fmt),
    "always": lambda: True,
    "success": lambda: True,
    "cancelled": lambda: False,
    "failure": lambda: False,
    "toJSON": lambda v: json.dumps(v, separators=(",", ":")),
    "fromJSON": lambda s: json.loads(s) if s else None,
}
_KW = {"true": True, "false": False, "null": None}


def eval_expr(expr, ctx):
    e = expr.strip()
    lits = []

    def stash(m):
        lits.append(m.group(0)[1:-1].replace("''", "'"))
        return "\x00%d\x00" % (len(lits) - 1)

    e = re.sub(r"'(?:[^']|'')*'", stash, e)
    e = e.replace("&&", " and ").replace("||", " or ")
    e = re.sub(r"(?<![<>!=])!(?!=)", " not ", e)
    e = re.sub(r"(?<![<>=!])=(?!=)", "==", e)

    def path_repl(m):
        tok = m.group(0)
        low = tok.lower()
        if low in _KW:
            return repr(_KW[low])
        if tok in _F:
            return "__fn['%s']" % tok
        if tok in ("and", "or", "not", "in", "is"):
            return tok
        return "__get(%r)" % tok

    e = re.sub(r"[A-Za-z_][A-Za-z0-9_.\-]*", path_repl, e)

    def unstash(m):
        return repr(lits[int(m.group(1))])

    e = re.sub("\x00(\\d+)\x00", unstash, e)
    try:
        val = eval(e, {"__get": lambda p: _ctx_get(ctx, p), "__fn": _F}, {})
    except Exception:
        sys.stderr.write("[warn] cannot resolve expr %r -> ''\n" % expr)
        return ""
    return val


def gh_str(v):
    if v is True:
        return "true"
    if v is False:
        return "false"
    if v is None:
        return ""
    return str(v)


def resolve(value, ctx):
    if isinstance(value, dict):
        return {k: resolve(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, ctx) for v in value]
    if not isinstance(value, str):
        return value
    whole = re.fullmatch(r"\s*\$\{\{(.+)\}\}\s*", value)
    if whole:
        return eval_expr(whole.group(1), ctx)
    return re.sub(r"\$\{\{(.+?)\}\}", lambda m: gh_str(eval_expr(m.group(1), ctx)), value)


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------
def expand_matrix(job):
    mx = (job.get("strategy") or {}).get("matrix") or {}
    base = {k: v for k, v in mx.items() if k != "include"}
    if base:
        combos = [{}]
        for k, vals in base.items():
            combos = [dict(c, **{k: v}) for c in combos for v in vals]
    else:
        combos = []
    for inc in mx.get("include", []) or []:
        merged = False
        if base:
            for c in combos:
                if all(k not in c or c[k] == v for k, v in inc.items() if k in base):
                    c.update(inc)
                    merged = True
                    break
        if not merged:
            combos.append(dict(inc))
    return combos or [{}]


def collect_steps(job):
    steps = list(job.get("steps") or [])
    return steps


def _cond(raw, ctx):
    raw = raw.strip()
    if raw.startswith("${{"):
        raw = raw[3:]
        if raw.endswith("}}"):
            raw = raw[:-2]
    return _truthy(eval_expr(raw, ctx))


def parse(path, inputs, github, repo_dir, outputs_file, summary_file):
    import yaml
    with open(path, "r", encoding="utf-8") as fh:
        wf = yaml.safe_load(fh)

    top_env = {}
    ctx0 = {"github": github, "inputs": inputs, "env": {}, "vars": {}, "needs": {}}
    for k, v in (wf.get("env") or {}).items():
        top_env[k] = gh_str(resolve(v, ctx0))

    units = []
    buildable_jobs = set()
    for job_id, job in (wf.get("jobs") or {}).items():
        if not any((s.get("uses") or "").split("@")[0] == "actions/upload-artifact" for s in collect_steps(job)):
            continue
        buildable_jobs.add(job_id)

    for job_id, job in (wf.get("jobs") or {}).items():
        if job_id not in buildable_jobs:
            continue
        if "if" in job and not _cond(job["if"], {"github": github, "inputs": inputs, "env": top_env, "needs": {}}):
            continue
        for m in expand_matrix(job):
            ctx = {
                "github": github, "inputs": inputs, "env": dict(top_env),
                "matrix": m, "vars": {}, "needs": {},
                "strategy": (job.get("strategy") or {}),
                "steps": {"ollvm-cache": {"outputs": {"cache-hit": "false"}},
                          "osxcross-cache": {"outputs": {"cache-hit": "false"}}},
                "runner": {"os": "", "arch": "x64"},
            }
            runs_on = gh_str(resolve(job.get("runs-on", "ubuntu-latest"), ctx))
            osname = "Windows" if "windows" in runs_on else ("macOS" if "macos" in runs_on or "macos" in str(m) else "Linux")
            ctx["runner"]["os"] = osname
            tag = gh_str(resolve(job.get("name") or job_id, ctx))
            unit_id = re.sub(r"[^A-Za-z0-9]+", "-", tag).strip("-").lower() or job_id
            blocks = []
            upload = None
            for i, st in enumerate(collect_steps(job)):
                sctx = dict(ctx)
                sctx["steps"] = ctx["steps"]
                if "if" in st:
                    if not _cond(st["if"], sctx):
                        continue
                uses = st.get("uses")
                if uses:
                    kind = classify(uses)
                    with_ = resolve(st.get("with") or {}, sctx)
                    if kind == "upload":
                        if upload is not None:
                            raise SystemExit("[plan] job %r has >1 upload step - unsupported" % job_id)
                        raw_paths = gh_str(with_.get("path", ""))
                        paths = [(repo_dir + "/" + p.strip().lstrip("./")) if repo_dir else p.strip()
                                 for p in re.split(r"\n+", raw_paths) if p.strip()]
                        upload = {
                            "name": gh_str(with_.get("name", "")),
                            "paths": paths,
                            "paths_joined": "\n".join(paths),
                            "retention": gh_str(with_.get("retention-days", "")),
                            "if_no_files": gh_str(with_.get("if-no-files-found", "warn")) or "warn",
                        }
                    elif kind == "download":
                        blocks.append({"kind": "download", "name": gh_str(resolve(st.get("name", uses), sctx)),
                                       "target": gh_str(with_.get("path", "artifacts")),
                                       "artifact": gh_str(with_.get("name", ""))})
                    elif kind in ("checkout", "cache"):
                        blocks.append({"kind": kind, "name": gh_str(resolve(st.get("name", kind), sctx))})
                    elif kind in ("ninja", "msvc", "dotnet"):
                        blocks.append({"kind": kind, "name": gh_str(resolve(st.get("name", kind), sctx)),
                                       "arch": gh_str(with_.get("arch", "x64")),
                                       "version": gh_str(with_.get("dotnet-version", ""))})
                    else:
                        raise SystemExit("[plan] unsupported uses: %r in job %r" % (uses, job_id))
                    continue
                if "run" in st:
                    rdef = (job.get("defaults") or {}).get("run") or {}
                    job_shell = rdef.get("shell", "")
                    job_shell_wd = rdef.get("working-directory", "")
                    shell = gh_str(resolve(st.get("shell") or job_shell or "", sctx)).strip()
                    if not shell:
                        shell = "pwsh" if osname == "Windows" else "bash"
                    wd = gh_str(resolve(job_shell_wd, sctx))
                    blocks.append({"kind": "run", "name": gh_str(resolve(st.get("name", "run"), sctx)),
                                   "shell": shell.lower(), "code": resolve(st["run"], sctx),
                                   "workdir": wd})
            if upload is None:
                continue
            needs = job.get("needs") or []
            needs = [needs] if isinstance(needs, str) else list(needs)
            deps = sorted(set(n for n in needs if n in buildable_jobs))
            units.append({"id": unit_id, "name": tag, "job": job_id, "runs_on": runs_on,
                          "layer": 0, "deps": deps, "upload": upload, "blocks": blocks})

    # topological layers (max 3)
    by_job = {}
    for u in units:
        by_job.setdefault(u["job"], []).append(u)
    changed = True
    while changed:
        changed = False
        for u in units:
            for jid in u["deps"]:
                for d in by_job.get(jid, []):
                    if d is not u and u["layer"] <= d["layer"]:
                        u["layer"] = d["layer"] + 1
                        changed = True
    if any(u["layer"] > 2 for u in units):
        raise SystemExit("[plan] dependency depth > 3 layers unsupported")

    layers = [[u for u in units if u["layer"] == n] for n in (0, 1, 2)]
    with open(outputs_file, "a", encoding="utf-8") as fh:
        for n, arr in enumerate(layers):
            payload = json.dumps(arr, separators=(",", ":"))
            fh.write("layer%d=%s\n" % (n, payload))
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as fh:
            fh.write("## Daisium_Predict ci.yml plan\n\n")
            for n, arr in enumerate(layers):
                for u in arr:
                    fh.write("- L%d `%s` -> `%s` -> artifact `%s`\n" % (n, u["job"], u["runs_on"], u["upload"]["name"]))
    print("planned %d units (L0=%d L1=%d L2=%d)" % (len(units), *[len(a) for a in layers]))


# --------------------------------------------------------------------------
# exec-unit
# --------------------------------------------------------------------------
def _which(prog):
    from shutil import which
    return which(prog)


def _run(cmd, env, cwd=None):
    sys.stdout.flush()
    return subprocess.run(cmd, env=env, cwd=cwd).returncode


def _apply_gh_files(env, files):
    for name, path in files.items():
        if not path or not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as fh:
            data = fh.read()
        if name == "GITHUB_ENV":
            for line in data.splitlines():
                if "=" in line and not line.startswith("::"):
                    k, v = line.split("=", 1)
                    env[k] = v
        elif name == "GITHUB_PATH":
            add = [l for l in data.splitlines() if l.strip()]
            if add:
                env["PATH"] = os.pathsep.join(add) + os.pathsep + env.get("PATH", "")
        os.remove(path)


def _download_artifacts(block, env):
    import urllib.request
    token = env.get("GITHUB_TOKEN") or ""
    api = env.get("GITHUB_API_URL", "https://api.github.com")
    repo = env["GITHUB_REPOSITORY"]
    run = env["GITHUB_RUN_ID"]
    want = block.get("artifact") or ""

    def get(url, raw=False):
        req = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "User-Agent": "daisium-ci-runner",
            "X-GitHub-Api-Version": "2022-11-28"})
        with urllib.request.urlopen(req) as resp:
            return resp.read() if raw else json.loads(resp.read())

    base = "%s/repos/%s/actions/runs/%s" % (api, repo, run)
    page = 1
    picked = []
    while True:
        data = get("%s/artifacts?per_page=100&page=%d" % (base, page))
        for a in data.get("artifacts", []):
            if want and a["name"] != want:
                continue
            picked.append(a)
        if page * 100 >= data.get("total_count", 0):
            break
        page += 1
    if want and not picked:
        raise SystemExit("[exec] artifact %r not found in this run" % want)
    for a in picked:
        dest = os.path.join(block["target"], a["name"])
        os.makedirs(dest, exist_ok=True)
        print("downloading artifact %s -> %s" % (a["name"], dest))
        blob = get(a["archive_download_url"], raw=True)
        zf = os.path.join(tempfile.gettempdir(), "art-%s.zip" % a["id"])
        with open(zf, "wb") as fh:
            fh.write(blob)
        with zipfile.ZipFile(zf) as z:
            z.extractall(dest)
        os.remove(zf)


def _setup_ninja(env):
    if _which("ninja"):
        return 0
    tgt = os.path.join(tempfile.gettempdir(), "dp-ninja")
    rc = _run([sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
               "--target", tgt, "ninja"], env)
    if rc:
        return rc
    for root, _d, fs in os.walk(tgt):
        for f in fs:
            if f in ("ninja", "ninja.exe"):
                env["PATH"] = root + os.pathsep + env["PATH"]
                print("ninja at", os.path.join(root, f))
                return 0
    return 1


def _msvc_dev_cmd(env, arch):
    pf = env.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    vswhere = os.path.join(pf, "Microsoft Visual Studio", "Installer", "vswhere.exe")
    if not os.path.isfile(vswhere):
        return 1
    vs = subprocess.run([vswhere, "-latest", "-products", "*", "-property", "installationPath"],
                        capture_output=True, text=True, env=env).stdout.strip().splitlines()
    if not vs:
        return 1
    devcmd = os.path.join(vs[0], "Common7", "Tools", "VsDevCmd.bat")
    out = subprocess.run(["cmd", "/c", '"%s" -arch=%s && set' % (devcmd, arch)],
                         capture_output=True, text=True, env=env).stdout
    n = 0
    for line in out.splitlines():
        m = re.match(r"([^=]+)=(.*)", line)
        if m and not m.group(1).startswith("="):
            env[m.group(1)] = m.group(2)
            n += 1
    print("imported %d vars from VsDevCmd (%s)" % (n, arch))
    return 0


def exec_unit():
    unit = json.loads(os.environ["UNIT_JSON"])
    repo_dir = os.environ.get("DP_REPO_DIR", "dp")
    base_env = dict(os.environ)
    rc_final = 0
    for i, b in enumerate(unit["blocks"]):
        kind = b["kind"]
        rc = 0
        print("::group::[%d/%d] %s" % (i + 1, len(unit["blocks"]), b.get("name", kind)))
        try:
            if kind in ("checkout", "cache", "dotnet"):
                if kind == "cache":
                    print("(cache emulated: always miss)")
                elif kind == "dotnet" and not _which("dotnet"):
                    raise SystemExit("dotnet missing on runner")
            elif kind == "ninja":
                rc = _setup_ninja(base_env)
            elif kind == "msvc":
                rc = _msvc_dev_cmd(base_env, b.get("arch") or "x64")
            elif kind == "download":
                b2 = dict(b)
                if not os.path.isabs(b2["target"]):
                    b2["target"] = os.path.join(repo_dir, b2["target"])
                rc = 0
                _download_artifacts(b2, base_env)
            elif kind == "run":
                shell = b.get("shell", "bash")
                code = b["code"]
                suffix = {"bash": ".sh", "sh": ".sh", "cmd": ".bat",
                          "pwsh": ".ps1", "powershell": ".ps1",
                          "python": ".py", "python3": ".py"}.get(shell, ".sh")
                tf = tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False, encoding="utf-8", newline="\n")
                cmd = None
                if shell in ("bash", "sh"):
                    tf.write(code)
                    tf.close()
                    exe0 = "bash" if shell == "bash" else "sh"
                    if not _which(exe0):
                        raise SystemExit(shell + " not available on this runner")
                    cmd = (["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", tf.name]
                           if shell == "bash" else ["sh", tf.name])
                elif shell in ("pwsh", "powershell"):
                    exe = _which("pwsh") or _which("powershell")
                    if not exe:
                        raise SystemExit("no powershell on runner")
                    tf.close()
                    ps = tf.name + ".ps1"
                    with open(ps, "w", encoding="utf-8", newline="\r\n") as fh:
                        fh.write("$ErrorActionPreference='Stop'\n" + code +
                                 "\nif ($LASTEXITCODE -ne $null -and $LASTEXITCODE -ne 0) { exit $LASTEXITCODE }\n")
                    cmd = [exe, "-NoProfile", "-NonInteractive", "-File", ps]
                    tf.name = ps
                elif shell == "cmd":
                    tf.write(code)
                    tf.close()
                    cmd = ["cmd", "/c", tf.name]
                elif shell in ("python", "python3"):
                    tf.write(code)
                    tf.close()
                    cmd = [sys.executable, tf.name]
                else:
                    raise SystemExit("unsupported shell %r for block %r" % (shell, b.get("name")))
                cwd = os.path.join(repo_dir, b.get("workdir", "")) if b.get("workdir", "") else repo_dir
                fds = {}
                step_env = dict(base_env)
                for nm in ("GITHUB_ENV", "GITHUB_PATH", "GITHUB_OUTPUT"):
                    fd, p = tempfile.mkstemp(prefix="ghenv_")
                    os.close(fd)
                    step_env[nm] = p
                    fds[nm] = p
                rc = _run(cmd, step_env, cwd=cwd)
                _apply_gh_files(base_env, fds)
                if tf.name and os.path.exists(tf.name):
                    os.unlink(tf.name)
            else:
                raise SystemExit("unknown block kind " + kind)
        except SystemExit as e:
            print("::error::%s" % e)
            rc = 1
        except Exception as e:
            import traceback
            traceback.print_exc()
            print("::error::%s" % e)
            rc = 1
        finally:
            print("::endgroup::")
        if rc != 0:
            print("[fail] block %r exit %d" % (b.get("name", kind), rc))
            rc_final = rc
            break
    # record upload meta for the workflow's real upload step (already static),
    # nothing else to do.
    if rc_final == 0:
        print("unit %r executed OK (artifact: %s)" % (unit["id"], unit["upload"]["name"]))
    sys.exit(rc_final)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--ci", required=True)
    p.add_argument("--input", action="append", default=[], help="k=v workflow input forwarded from NightlyCompiler")
    p.add_argument("--ref", default="main")
    p.add_argument("--github-ref", default="")
    p.add_argument("--repo-dir", default="dp")
    p.add_argument("--outputs", default=os.environ.get("GITHUB_OUTPUT", "/tmp/gho"))
    p.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY", ""))
    e = sub.add_parser("exec-unit")
    e.add_argument("--unused", default="")
    a = ap.parse_args()

    if a.mode == "plan":
        inputs = {}
        for kv in a.input:
            k, _, v = kv.partition("=")
            inputs[k] = v
        gref = a.github_ref
        if not gref:
            gref = "refs/heads/" + a.ref
        github = {"ref": gref, "head_ref": a.ref, "event_name": "workflow_dispatch",
                  "event": {"inputs": inputs, "ref": a.ref}, "run_id": os.environ.get("GITHUB_RUN_ID", "0")}
        parse(a.ci, inputs, github, a.repo_dir, a.outputs, a.summary)
    else:
        exec_unit()


if __name__ == "__main__":
    main()
