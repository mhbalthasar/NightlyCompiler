#!/usr/bin/env python3
"""Regression test for the NightlyCompiler artifact-download 401.

Reproduces the exact production request chain:

    client --(Bearer token)--> api.github.com /repos/.../artifacts   -> 200 JSON
    client --(Bearer token)--> api.github.com /zip/<id>              -> 302
    client --(Bearer token!)--> *.blob.core.windows.net /signed/<id> -> 401

Two loopback servers on *different ports* (different netloc => "cross host"):

  * api  : /repos/o/r/actions/runs/1/artifacts -> JSON whose
           archive_download_url points back at the api host (as GitHub does)
           /zip/<id>  -> 302 to the blob host's SAS URL
  * blob : /signed/<id>?sig=... -> 401 if an Authorization header is present
           (mimics the Azure Blob bearer challenge), else 200 + zip

The control case runs the OLD code path (plain urlopen, no redirect handler)
and asserts it 401s, proving the fixture really exercises the bug.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.abspath(__file__))
RUNNER = os.path.join(REPO, "daisium_ci_runner.py")

AZURE_401_BODY = (
    "<?xml version=\"1.0\" encoding=\"utf-8\"?><Error>"
    "<Code>NoAuthenticationInformation</Code>"
    "<Message>Server failed to authenticate the request. Please refer to the "
    "information in the www-authenticate header.\n"
    "RequestId:ec4f02d7-1003-0006-21f9-c55bc8000000</Message></Error>"
)
AZURE_401_HDR = ("Bearer authorization_uri=https://login.microsoftonline.com/"
                 "oauth2/authorize resource_id=https://storage.azure.com")


def make_zip(name, payload):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, payload)
    return buf.getvalue()


def load_runner():
    import importlib.util
    spec = importlib.util.spec_from_file_location("daisium_ci_runner", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _respond(h, code, body=b"", ctype="text/plain", extra=()):
    h.send_response(code)
    for k, v in extra:
        h.send_header(k, v)
    if body:
        h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    if body:
        h.wfile.write(body)


class BlobHandler(BaseHTTPRequestHandler):
    """Mimics the Azure Blob endpoint behind GitHub's artifact 302."""
    zip_bytes = b""
    seen = []                      # [(path, had_authorization)]

    def log_message(self, *a):
        pass

    def do_GET(self):
        auth = self.headers.get("Authorization")
        BlobHandler.seen.append((self.path, auth))
        if auth:
            _respond(self, 401, AZURE_401_BODY.encode(), "application/xml",
                     [("WWW-Authenticate", AZURE_401_HDR)])
            return
        _respond(self, 200, self.zip_bytes, "application/zip")


class ApiHandler(BaseHTTPRequestHandler):
    """Mimics api.github.com for the artifacts list + archive redirect."""
    blob_port = 0

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer fake-token":
            _respond(self, 401)
            return
        if self.path.startswith("/repos/o/r/actions/runs/1/artifacts"):
            body = json.dumps({
                "total_count": 1,
                "artifacts": [{
                    "id": 7,
                    "name": "build-x",
                    "archive_download_url":
                        "http://127.0.0.1:%d/zip/7" % self.server.server_address[1],
                }],
            }).encode()
            _respond(self, 200, body, "application/json")
            return
        if self.path.startswith("/zip/"):
            _respond(self, 302, b"", extra=[
                ("Location", "http://127.0.0.1:%d/signed/%s?sig=abc"
                 % (ApiHandler.blob_port, self.path.rsplit("/", 1)[-1]))])
            return
        _respond(self, 404)


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def main():
    BlobHandler.zip_bytes = make_zip("hello.txt", "compiled output")

    blob_srv, blob_port = serve(BlobHandler)
    ApiHandler.blob_port = blob_port
    api_srv, api_port = serve(ApiHandler)

    mod = load_runner()
    failures = []
    env = {"GITHUB_TOKEN": "fake-token", "GITHUB_REPOSITORY": "o/r",
           "GITHUB_RUN_ID": "1",
           "GITHUB_API_URL": "http://127.0.0.1:%d" % api_port}
    block = {"kind": "download", "name": "Download all build artifacts",
             "target": "", "artifact": "build-x"}

    # ---- control: OLD code path (plain urlopen) must reproduce the 401 ----
    old_req = urllib.request.Request(
        "http://127.0.0.1:%d/zip/7" % api_port,
        headers={"Authorization": "Bearer fake-token",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "daisium-ci-runner"})
    try:
        urllib.request.urlopen(old_req)
        failures.append("control: old urlopen path unexpectedly succeeded")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print("[ok]   control: old path reproduces HTTP 401  (Azure: %s)"
                  % (e.headers.get("www-authenticate") or "")[:46])
        else:
            failures.append("control: got HTTP %s, expected 401" % e.code)
    except Exception as e:
        failures.append("control raised %r" % (e,))

    # ---- fixed: module's _download_artifacts must succeed ----
    BlobHandler.seen = []
    tmp = tempfile.mkdtemp(prefix="fix_")
    b = dict(block, target=tmp)
    try:
        mod._download_artifacts(b, env)
        got = os.path.join(tmp, "build-x", "hello.txt")
        if not os.path.isfile(got):
            failures.append("fixed: extracted file missing at %s" % got)
        elif open(got, "rb").read() != b"compiled output":
            failures.append("fixed: extracted content mismatch")
        else:
            print("[ok]   fixed: artifact downloaded + extracted -> %s" % got)
    except Exception as e:
        failures.append("fixed: raised %r" % (e,))

    hops = [(p, bool(a)) for p, a in BlobHandler.seen]
    print("       blob host saw: %s" % hops)
    if not hops:
        failures.append("blob host received no requests")
    elif any(a for _p, a in hops):
        failures.append("Authorization still leaked to the blob host")

    # ---- end-to-end: the real exec-unit CLI path ----
    BlobHandler.seen = []
    tmp2 = tempfile.mkdtemp(prefix="e2e_")
    unit = {"id": "bundle", "name": "bundle", "job": "bundle",
            "runs_on": "windows-2022", "layer": 1, "deps": [],
            "upload": {"name": "native-bundle"},
            "blocks": [{"kind": "download",
                        "name": "Download all build artifacts",
                        "target": "artifacts", "artifact": ""}]}
    sub_env = {k: v for k, v in os.environ.items()
               if k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
    sub_env.update(env)
    sub_env["UNIT_JSON"] = json.dumps(unit)
    sub_env["DP_REPO_DIR"] = tmp2
    p = subprocess.run([sys.executable, RUNNER, "exec-unit"],
                       env=sub_env, capture_output=True, text=True)
    got = os.path.join(tmp2, "artifacts", "build-x", "hello.txt")
    if p.returncode != 0:
        failures.append("exec-unit exit %d\n%s%s"
                        % (p.returncode, p.stdout, p.stderr))
    elif not os.path.isfile(got):
        failures.append("exec-unit: extracted file missing at %s" % got)
    else:
        print("[ok]   exec-unit CLI: exit 0, artifact extracted")

    # ---- same-host redirects must KEEP the credential (no over-stripping) ----
    kept = mod._StripAuthOnCrossHostRedirect().redirect_request(
        urllib.request.Request("https://api.github.com/zip/7", headers={
            "Authorization": "Bearer t"}), None, 302, "Found", {}, 
        "https://api.github.com/signed/7")
    if kept is None or kept.headers.get("Authorization") != "Bearer t":
        failures.append("same-host redirect dropped the credential")
    else:
        print("[ok]   same-host redirect keeps Authorization (no over-stripping)")

    api_srv.shutdown()
    blob_srv.shutdown()

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
