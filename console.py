#!/usr/bin/env python3
"""
console.py — a local control panel for mockd.

Starts and stops the mock server, lists every operation it is serving, and lets
you fire requests at it and read the response, without leaving the browser.

    pip install flask flask-cors pyyaml jsonschema
    python console.py                    # then open http://localhost:4100

It has to run locally and serve its own page: the UI needs to spawn a process on
this machine and call http://localhost:<mock port>, and every request it makes
to the mock goes through this server, so the browser never has to care about
CORS or mixed origins.

Endpoints (all under /api, all same-origin):

    GET  /api/state      is the mock running, on what port, with what flags
    POST /api/start      spawn mockd with the given options
    POST /api/stop       terminate it
    GET  /api/stdout     tail of the mock's own console output
    GET  /api/routes     /_mock/routes from the running mock
    GET  /api/drift      /_mock/drift
    GET  /api/requests   /_mock/log
    POST /api/send       proxy one request to the mock and return the result
    GET  /api/sample     a request body generated from an operation's schema
    POST /api/reset      /_mock/reset
    POST /api/reload     /_mock/reload
    POST /api/verify     run verify.py against the running mock
    POST /api/overlay    run build_overlay.py
"""
import json
import os
import re
import yaml
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import project
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

HERE = Path(__file__).resolve().parent
LOG_DIR = HERE / "logs"
LOG_DIR.mkdir(exist_ok=True)
STDOUT_LOG = LOG_DIR / "mockd_console.log"

app = Flask("mockd-console", static_folder=None)

# ----------------------------------------------------------------------------
# Process control
# ----------------------------------------------------------------------------


class MockProcess:
    def __init__(self):
        self.proc = None
        self.options = {}
        self.started_at = None
        self.lock = threading.Lock()

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    @property
    def port(self):
        return int(self.options.get("port") or 4010)

    def base_url(self):
        return f"http://127.0.0.1:{self.port}"

    @staticmethod
    def port_taken(port):
        """Is something already answering as a mock on this port?

        Without this check, starting over a leftover process looks like success:
        the health probe gets a 200 from the OLD mock, and the tester spends the
        next hour wondering why their spec change had no effect."""
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/_mock/routes", timeout=1) as resp:
                json.loads(resp.read())
            return True
        except Exception:
            return False

    def start(self, options):
        with self.lock:
            if self.running:
                return False, "already running"
            port = int(options.get("port") or 4010)
            if self.port_taken(port):
                return False, (f"port {port} is already serving a mock this console did "
                               f"not start — stop it first (lsof -ti:{port} | xargs kill), "
                               f"or pick another port")
            cmd = [sys.executable, str(HERE / "mockd.py"),
                   "--spec", options["spec"],
                   "--port", str(options.get("port") or 4010),
                   "--host", "127.0.0.1"]
            if options.get("overlay"):
                cmd += ["--overlay", options["overlay"]]
            if options.get("stateful"):
                cmd += ["--stateful"]
            if options.get("require_auth"):
                cmd += ["--require-auth"]
            if options.get("validation_mode"):
                cmd += ["--validation-mode", options["validation_mode"]]
            if options.get("allow_undocumented"):
                cmd += ["--allow-undocumented-status"]
            if options.get("no_watch"):
                cmd += ["--no-watch"]
            if options.get("array_items"):
                cmd += ["--array-items", str(options["array_items"])]
            if options.get("poll"):
                cmd += ["--poll", str(options["poll"])]
            for raw in (options.get("headers") or "").splitlines():
                if raw.strip():
                    cmd += ["--header", raw.strip()]

            STDOUT_LOG.write_text(f"$ {' '.join(cmd)}\n\n")
            handle = open(STDOUT_LOG, "a")
            try:
                self.proc = subprocess.Popen(
                    cmd, cwd=str(HERE), stdout=handle, stderr=subprocess.STDOUT,
                    start_new_session=True)
            except Exception as exc:
                return False, f"{type(exc).__name__}: {exc}"
            self.options = options
            self.started_at = time.time()

        for _ in range(60):                       # wait for it to answer
            if not self.running:
                return False, "process exited on startup — see the server output"
            try:
                urllib.request.urlopen(self.base_url() + "/_mock/routes", timeout=1).read()
                return True, "started"
            except Exception:
                time.sleep(0.25)
        return False, "started but did not answer on /_mock/routes"

    def stop(self):
        with self.lock:
            if not self.running:
                self.proc = None
                return False, "not running"
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except Exception:
                self.proc.terminate()
            try:
                self.proc.wait(timeout=6)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                except Exception:
                    self.proc.kill()
            self.proc = None
            return True, "stopped"


mock = MockProcess()


def mock_get(path, timeout=10):
    url = mock.base_url() + path
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read() or b"null")


def target_base(name):
    """Just the address — no login performed.

    Writing a curl is producing a recipe, not making a call: it must work for an
    environment that is unreachable from this machine, or whose credentials the
    person copying does not hold."""
    name = (name or "mock").strip()
    if name in ("", "mock", "__mock__"):
        if not mock.running:
            return None, None, "the mock server is not running"
        return "mock", mock.base_url().replace("127.0.0.1", "localhost"), None
    try:
        import environments as envmod
        env = envmod.get(name)
        return name, envmod.resolve(env.get("base_url", ""), []).rstrip("/"), None
    except SystemExit as exc:
        return None, None, str(exc)
    except Exception as exc:
        return None, None, f"{type(exc).__name__}: {exc}"


def resolve_target(name):
    """Where an Explore request should go.

    Everything in this view used to be hardwired to the mock, so "Copy as cURL"
    handed you a localhost command even when you meant to hit dev. One target,
    honoured by Send, by the curl, and by what the response is labelled with."""
    name = (name or "mock").strip()
    if name in ("", "mock", "__mock__"):
        if not mock.running:
            return None, None, None, "the mock server is not running"
        return ("mock", mock.base_url().replace("127.0.0.1", "localhost"), {}, None)
    try:
        import environments as envmod
        env = envmod.get(name)
        base = envmod.resolve(env.get("base_url", ""), [])
        headers, _ = envmod.authenticate(env)
        return name, base.rstrip("/"), headers, None
    except SystemExit as exc:
        return None, None, None, str(exc)
    except Exception as exc:
        return None, None, None, f"{type(exc).__name__}: {exc}"


def require_running():
    if not mock.running:
        return jsonify({"error": "the mock server is not running"}), 409
    return None


# ----------------------------------------------------------------------------
# API
# ----------------------------------------------------------------------------


@app.get("/api/state")
def state():
    info = {"running": mock.running, "options": mock.options,
            "base_url": mock.base_url() if mock.running else None,
            "uptime_s": round(time.time() - mock.started_at) if mock.started_at
                        and mock.running else None,
            "console_cwd": str(HERE)}
    if mock.running:
        try:
            info["drift"] = mock_get("/_mock/drift", timeout=4)
        except Exception as exc:
            info["drift_error"] = str(exc)
    return jsonify(info)


# What you last ran is what you almost certainly want next time. Kept out of
# git: it is one person's working choice, not a decision the team shares —
# that decision is spec.lock.json.
PREFS_FILE = HERE / ".clavis-console.json"
# Deliberately NOT "spec": which document this project is about lives in
# clavis.json, and two competing defaults is how you end up staring at
# coverage for a document you thought you had replaced.
REMEMBERED = ("overlay", "port", "array_items", "validation_mode",
              "stateful", "require_auth", "allow_undocumented", "headers")


def load_preferences():
    try:
        saved = json.loads(PREFS_FILE.read_text())
    except (OSError, ValueError):
        return {}
    keep = {k: v for k, v in saved.items() if k in REMEMBERED} if isinstance(saved, dict) else {}
    keep["spec"] = project.active_spec(None, "mock")      # always the project's answer
    return keep


def save_preferences(options):
    keep = {k: v for k, v in (options or {}).items() if k in REMEMBERED}
    if not keep:
        return
    try:
        PREFS_FILE.write_text(json.dumps(keep, indent=2) + "\n")
    except OSError:
        pass


# A file next to this script is not a spec just because it is JSON —
# environments.json and spec.lock.json are not documents anyone can serve.
# Parsing is the only honest test — a text search misses a minified document,
# and spec.lock.json carries an `openapi` field without being a spec. But
# PyYAML is pure Python and ~200x slower than json on the same bytes, and this
# runs on every page load, so JSON goes to the C parser and the result is cached
# against the file's mtime and size.
_SPEC_SNIFF = {}


def is_spec_file(path):
    try:
        stat = path.stat()
    except OSError:
        return False
    if stat.st_size > 40 * 1024 * 1024:
        return False
    key = str(path)
    cached = _SPEC_SNIFF.get(key)
    stamp = (stat.st_mtime, stat.st_size)
    if cached and cached[0] == stamp:
        return cached[1]

    verdict = False
    try:
        text = path.read_text(errors="ignore")
        if path.suffix.lower() == ".json":
            doc = json.loads(text)
        else:
            doc = yaml.safe_load(text)
        verdict = (isinstance(doc, dict)
                   and ("openapi" in doc or "swagger" in doc)
                   and isinstance(doc.get("paths"), dict))
    except (OSError, ValueError, yaml.YAMLError, RecursionError):
        verdict = False
    _SPEC_SNIFF[key] = (stamp, verdict)
    return verdict


def known_specs():
    """Every document that could be served, wherever it landed.

    Fetched and uploaded candidates go to specs/, so a list of the project root
    alone makes a saved document look like it was never saved."""
    found = []
    for base, prefix in ((HERE, ""), (SPEC_DIR, "specs/")):
        if not base.is_dir():
            continue
        for item in sorted(base.iterdir()):
            if item.suffix.lower() not in (".json", ".yaml", ".yml"):
                continue
            if "overlay" in item.name or "postman" in item.name.lower():
                continue
            if is_spec_file(item):
                found.append(prefix + item.name)
    return found


@app.post("/api/spec/compare")
def spec_compare():
    """Compare two documents by shape, not by text — see specdiff.py."""
    import specdiff
    payload = request.get_json(silent=True) or {}
    left, right = (payload.get("from") or "").strip(), (payload.get("to") or "").strip()
    if not left or not right:
        return jsonify({"ok": False, "error": "pick two documents"}), 400
    if left == right:
        return jsonify({"ok": False, "error": "those are the same document"}), 400
    try:
        result = specdiff.compare(specdiff._read(left), specdiff._read(right), left, right)
    except SystemExit as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400
    return jsonify({"ok": True, **result})


@app.post("/api/spec/compare/export")
def spec_compare_export():
    """The same comparison, in something you can paste into a pull request."""
    import specdiff
    payload = request.get_json(silent=True) or {}
    left, right = (payload.get("from") or "").strip(), (payload.get("to") or "").strip()
    fmt = payload.get("format") or "markdown"
    if fmt not in specdiff.RENDERERS:
        return jsonify({"ok": False, "error": f"unknown format {fmt}"}), 400
    if not left or not right or left == right:
        return jsonify({"ok": False, "error": "pick two different documents"}), 400
    try:
        result = specdiff.compare(specdiff._read(left), specdiff._read(right), left, right)
    except SystemExit as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400
    text = specdiff.RENDERERS[fmt](result, bool(payload.get("breaking_only")))
    stem = f"contract-changes-{time.strftime('%Y%m%d')}"
    suffix = {"markdown": "md", "html": "html", "csv": "csv"}[fmt]
    return jsonify({"ok": True, "text": text, "filename": f"{stem}.{suffix}",
                    "counts": result["counts"]})


@app.get("/api/project")
def project_get():
    """The document this project is about, and what each module resolves to."""
    return jsonify({"spec": project.active_spec(), "from": project.source(),
                    "overlay": project.active_overlay(),
                    "modules": project.report(), "specs": known_specs()})


@app.post("/api/project")
def project_set():
    """Choose the project spec, or pin one module to a different document."""
    payload = request.get_json(silent=True) or {}
    module = payload.get("module") or None
    if module and module not in project.MODULES:
        return jsonify({"ok": False, "error": f"unknown module {module}"}), 400
    if payload.get("clear") and module:
        project.clear_module(module)
        return jsonify({"ok": True, "message": f"{module} follows the project spec again",
                        "spec": project.active_spec(), "modules": project.report()})
    spec = (payload.get("spec") or "").strip()
    if not spec:
        return jsonify({"ok": False, "error": "which spec?"}), 400
    known = spec.lower().startswith(("http://", "https://")) or (HERE / spec).exists()
    if not known:
        return jsonify({"ok": False, "error": f"{spec} does not exist"}), 400
    project.set_active(spec=spec, overlay=payload.get("overlay"), module=module)

    # A setting the running mock ignores is not a project-wide setting. If the
    # mock is up on a different document, move it — otherwise coverage, the
    # explorer and every saved test keep answering from the old one.
    moved = ""
    wanted = project.active_spec(None, "mock")
    if mock.running and mock.options.get("spec") != wanted:
        options = dict(mock.options)
        options["spec"] = wanted
        mock.stop()
        ok, detail = mock.start(options)
        moved = (f" The mock was restarted on it."
                 if ok else f" The mock could NOT be restarted: {detail}")

    where = f"module {module}" if module else "the whole project"
    return jsonify({"ok": True, "spec": project.active_spec(), "modules": project.report(),
                    "mock_spec": mock.options.get("spec") if mock.running else None,
                    "message": f"{where} now uses {spec} — commit clavis.json "
                               f"so the team shares it.{moved}"})


@app.get("/api/defaults")
def defaults():
    """Offer every spec and overlay this project holds, plus the remembered choice."""
    overlays = sorted(p.name for p in HERE.glob("*overlay*.json"))
    return jsonify({"specs": known_specs(), "overlays": overlays,
                    "remembered": load_preferences()})


@app.post("/api/start")
def start():
    options = request.get_json(silent=True) or {}
    if not options.get("spec"):
        return jsonify({"error": "spec is required"}), 400
    ok, message = mock.start(options)
    if ok:
        save_preferences(options)     # only a start that worked becomes the default
    return jsonify({"ok": ok, "message": message,
                    "stdout": tail_log()}), (200 if ok else 500)


@app.post("/api/stop")
def stop():
    ok, message = mock.stop()
    return jsonify({"ok": ok, "message": message})


def tail_log(lines=200):
    if not STDOUT_LOG.exists():
        return ""
    return "\n".join(STDOUT_LOG.read_text(errors="replace").splitlines()[-lines:])


@app.get("/api/stdout")
def stdout():
    return jsonify({"stdout": tail_log(), "running": mock.running})


@app.get("/api/routes")
def routes():
    guard = require_running()
    if guard:
        return guard
    return jsonify(mock_get("/_mock/routes"))


@app.get("/api/postman")
def postman_collection():
    """The whole spec as a Postman collection, aimed at the running mock.

    QA imports it today and runs the same file with newman tomorrow — the
    requests and the spec-derived assertions do not change, only `baseUrl`."""
    spec_path = project.active_spec(request.args.get("spec") or mock.options.get("spec"))
    base = request.args.get("base_url") or (
        mock.base_url().replace("127.0.0.1", "localhost") if mock.running
        else "http://localhost:4010")
    try:
        import postman as pm
        from mockd import Source, Spec
        src = Source(spec_path, poll=0, cache_dir=str(LOG_DIR))
        text, _ = src.read(force=True)
        if text is None:
            return jsonify({"error": src.error or "could not read spec"}), 400
        spec = Spec(text=text, origin=spec_path)
        collection = pm.build(spec, base)
    except Exception as exc:
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 400

    count = sum(len(f["item"]) for f in collection["item"])
    if request.args.get("download"):
        name = re.sub(r"[^A-Za-z0-9]+", "_",
                      collection["info"]["name"]).strip("_") or "collection"
        return Response(
            json.dumps(collection, indent=2, ensure_ascii=False),
            mimetype="application/json",
            headers={"Content-Disposition":
                     f'attachment; filename="{name}.postman_collection.json"'})
    return jsonify({"collection": collection, "requests": count,
                    "folders": len(collection["item"]), "base_url": base})


def _placeholder(var_name, resolved):
    """A blank that says what belongs in it.

    `$DEV_COOKIE` is safe but makes the reader go and set a shell variable first.
    A visible placeholder is self-explanatory — and where the real value has a
    recognisable shape (a cookie is `name=value`) the name is kept so only the
    secret half is blanked."""
    if resolved and "=" in resolved and " " not in resolved.split("=")[0]:
        return f"{resolved.split('=')[0]}=<use_your_token>"
    return f"<use_your_{var_name.lower()}>" if var_name else "<use_your_token>"


def _auth_curl(target, reveal=False):
    """How this environment authenticates, expressed as curl.

    A real credential is never written into the clipboard. A ${VAR} in the
    environment stays a shell variable in the command, so the copied text is
    safe to paste into a ticket while still working in your own terminal.

    Three shapes, and an environment may combine them:
      static headers   whatever `headers` the environment declares — this is how
                       a cookie-authenticated API is usually driven
      token            a bearer, sent as a header
      login            call the login endpoint first and keep the cookie jar
    """
    if not target or target == "mock":
        return "", ""
    try:
        import environments as envmod
        env = envmod.get(target)
    except BaseException:            # get() raises SystemExit for an unknown name
        return "", ""

    prelude, flags, exports = [], [], []

    def as_shell(name, raw):
        var = re.fullmatch(r"\$\{(\w+)\}", str(raw or "").strip())
        resolved = envmod.resolve(str(raw or ""), [])
        if reveal and resolved:
            return f"-H '{name}: {resolved}'"
        if var:
            return f"-H '{name}: {_placeholder(var.group(1), resolved)}'"
        if _is_secret_name(name):
            return f"-H '{name}: {_placeholder(name, resolved)}'"
        return f"-H '{name}: {resolved or raw}'"

    # 1. static headers, declared straight on the environment
    for name, raw in (env.get("headers") or {}).items():
        flags.append(as_shell(name, raw))

    auth = env.get("auth") or {}
    mode = auth.get("mode", "none")

    if mode == "token":
        header = auth.get("header", "Authorization")
        prefix = auth.get("prefix", "Bearer ")
        raw = auth.get("token", "")
        var = re.fullmatch(r"\$\{(\w+)\}", str(raw).strip())
        resolved = envmod.resolve(str(raw), [])
        value = resolved if (reveal and resolved) else \
            f"<use_your_{(var.group(1) if var else 'token').lower()}>"
        flags.append(f"-H '{header}: {prefix}{value}'")

    elif mode == "login":
        base = envmod.resolve(env.get("base_url", ""), []).rstrip("/")
        path = auth.get("path", "/")
        uf = auth.get("username_field", "username")
        pf = auth.get("password_field", "password")
        where = (auth.get("send") or "query").lower()
        u_var = re.fullmatch(r"\$\{(\w+)\}", str(auth.get("username", "")).strip())
        p_var = re.fullmatch(r"\$\{(\w+)\}", str(auth.get("password", "")).strip())
        u_res = envmod.resolve(str(auth.get("username", "")), [])
        p_res = envmod.resolve(str(auth.get("password", "")), [])
        u = u_res if (reveal and u_res) else "<use_your_username>"
        p_name = p_res if (reveal and p_res) else "<use_your_password>"
        if where == "query":
            # double quotes on purpose: the shell must expand the two variables
            login = (f"curl -s -c jar.txt -X {auth.get('method', 'POST')} "
                     f"'{base}{path}?{uf}={u}&{pf}={p_name}' > /dev/null")
        else:
            login = (f"curl -s -c jar.txt -X {auth.get('method', 'POST')} '{base}{path}' "
                     f"-H 'Content-Type: application/json' "
                     f"-d '{{\"{uf}\": \"{u}\", \"{pf}\": \"{p_name}\"}}'")
        prelude.append("# 1. log in once — this API authenticates by cookie")
        prelude.append(login)
        prelude.append("")
        prelude.append("# 2. the call, reusing the jar")
        flags.append("-b jar.txt")

    if not reveal and any("<use_your_" in f for f in flags + prelude):
        prelude.insert(0, "# replace every <use_your_...> below with your own value")
    head = ("\n".join(prelude) + "\n") if prelude else ""
    return head, " \\\n  ".join(flags)


@app.get("/api/curl/auth")
def curl_auth():
    """Just the authentication half of a curl, so a command composed in the
    browser gets the same credentials as one built from the spec."""
    label, _base, problem = target_base(request.args.get("target"))
    if problem:
        return jsonify({"error": problem}), 409
    prelude, extra = _auth_curl(label, reveal=request.args.get("reveal") == "1")
    return jsonify({"prelude": prelude, "extra": extra, "target": label})


@app.get("/api/curl")
def curl():
    """A ready-to-paste curl for one operation, with a valid body filled in."""
    method = (request.args.get("method") or "GET").upper()
    path = request.args.get("path") or "/"
    spec_path = project.active_spec(mock.options.get("spec") or request.args.get("spec"))
    label, base, problem = target_base(request.args.get("target"))
    if problem:
        return jsonify({"error": problem}), 409
    headers = {"Accept": "application/json"}
    body = None
    try:
        import random

        import postman as pm
        from generator import generate_from_schema
        from mockd import Source, Spec
        src = Source(spec_path, poll=0, cache_dir=str(LOG_DIR))
        text, _ = src.read(force=True)
        spec = Spec(text=text, origin=spec_path)
        route = next((r for r in spec.routes
                      if r["method"] == method and r["path"] == path), None)
        if route is None:
            return jsonify({"error": f"{method} {path} is not in the spec"}), 404
        rng = random.Random(route["key"])
        url = base + path
        query = []
        for prm in route["parameters"]:
            schema = prm.get("schema") or {}
            if prm.get("in") == "path":
                url = url.replace("{%s}" % prm["name"], pm.sample_value(schema, rng))
            elif prm.get("in") == "query" and prm.get("required"):
                query.append(f"{prm['name']}={pm.sample_value(schema, rng, '')}")
        if query:
            url += "?" + "&".join(query)
        media = (route["request_body"].get("content") or {}).get("application/json")
        if media and media.get("schema"):
            body = generate_from_schema(media["schema"], rng, array_items=1)
            if body is not None:
                headers["Content-Type"] = "application/json"
        prelude, extra = _auth_curl(label, reveal=request.args.get("reveal") == "1")
        command = pm.to_curl(method, url, headers, body)
        if extra:
            command += " \\\n  " + extra
        return jsonify({"curl": (prelude + command) if prelude else command,
                        "operation": route["key"], "target": label, "base_url": base})
    except Exception as exc:
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 400


@app.post("/api/agree")
def agree():
    """Move one operation up the trust ladder and write it into the overlay.

    This is the point of the overlay: it is where UI, QA and backend record what
    a payload IS, for the 65 operations the spec does not say. Marking it here
    writes to the file, so the decision lands in git next to the code instead of
    in a chat thread."""
    payload = request.get_json(silent=True) or {}
    operation = payload.get("operation")
    status = payload.get("status")
    if status not in ("guess", "proposed", "agreed", "verified", "spec"):
        return jsonify({"error": f"unknown status {status!r}"}), 400
    overlay_path = HERE / (mock.options.get("overlay") or "mock_overlay.json")
    if not overlay_path.exists():
        return jsonify({"error": f"{overlay_path.name} does not exist — build it first"}), 400

    doc = json.loads(overlay_path.read_text())
    ops = doc.get("operations")
    if ops is None or operation not in ops:
        return jsonify({"error": f"{operation} is not in the overlay"}), 404
    ops[operation]["status"] = status
    ops[operation]["status_set_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    overlay_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")

    # the running mock watches the file, so the change is live on the next request
    return jsonify({"ok": True, "operation": operation, "status": status,
                    "file": str(overlay_path)})


@app.get("/api/integration")
def integration():
    """Everything a dev team needs to point their frontend at this mock."""
    if not mock.running:
        return jsonify({"running": False})
    port = mock.port
    lan = None
    try:
        import socket
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        lan = probe.getsockname()[0]
        probe.close()
    except Exception:
        pass

    opts = mock.options
    sample = None
    try:
        routes = mock_get("/_mock/routes", timeout=5)
        sample = next((r for r in routes if r["method"] == "GET"
                       and "{" not in r["path"]), routes[0] if routes else None)
    except Exception:
        pass

    return jsonify({
        "running": True,
        "base_url": f"http://localhost:{port}",
        "lan_url": f"http://{lan}:{port}" if lan else None,
        "port": port,
        "spec": opts.get("spec"),
        "sample_path": (sample or {}).get("path"),
        "validation_mode": opts.get("validation_mode") or "spec",
        "stateful": bool(opts.get("stateful")),
        "require_auth": bool(opts.get("require_auth")),
        "allow_undocumented": bool(opts.get("allow_undocumented")),
    })


SPEC_DIR = HERE / "specs"


@app.get("/api/spec/status")
def spec_status():
    """What document is loaded, and does it match the pinned one?"""
    import speclock
    from mockd import Source
    path = project.active_spec(request.args.get("spec") or mock.options.get("spec"))
    lock = speclock.load()
    out = {"source": path, "lock": lock, "running": mock.running}
    try:
        src = Source(path, poll=0, cache_dir=str(LOG_DIR))
        text, _ = src.read(force=True)
        if text is None:
            out["error"] = src.error or "could not read the spec"
            return jsonify(out)
        out["summary"] = {k: v for k, v in speclock.summarise(text).items()
                          if k != "operation_ids"}
        out["state"] = speclock.compare(text, lock)
        out["is_url"] = src.is_url
        out["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                          time.gmtime(src.fetched_at)) if src.fetched_at else None
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return jsonify(out)


@app.post("/api/spec/fetch")
def spec_fetch():
    """Pull a spec from a Swagger/OpenAPI URL and keep it as a candidate.

    A fetch never silently becomes the source of truth: it is written to
    specs/, compared against the lock, and the caller decides what to do."""
    import speclock
    from mockd import Source, Spec
    payload = request.get_json(silent=True) or {}
    url = (payload.get("url") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return jsonify({"ok": False, "error": "give an http(s) URL"}), 400
    headers = {}
    for raw in (payload.get("headers") or "").splitlines():
        if ":" in raw:
            name, value = raw.split(":", 1)
            headers[name.strip()] = value.strip()
    src = Source(url, headers=headers, poll=0, cache_dir=str(LOG_DIR))
    text, _ = src.read(force=True)
    if text is None:
        return jsonify({"ok": False, "error": src.error or "fetch failed"}), 400
    try:
        Spec(text=text, origin=url)                  # reject junk before saving
    except Exception as exc:
        return jsonify({"ok": False, "error": f"not a usable OpenAPI document: {exc}"}), 400

    SPEC_DIR.mkdir(exist_ok=True)
    name = payload.get("save_as") or "fetched.json"
    if not re.fullmatch(r"[A-Za-z0-9._-]+\.(json|ya?ml)", name):
        return jsonify({"ok": False, "error": "save_as must be a simple file name"}), 400
    path = SPEC_DIR / name
    path.write_text(text)
    return jsonify({"ok": True, "file": str(path.relative_to(HERE)),
                    "url": src.location,          # what was actually read
                    "resolved_from": src.resolved_from,
                    "summary": {k: v for k, v in speclock.summarise(text).items()
                                if k != "operation_ids"},
                    "state": speclock.compare(text)})


@app.post("/api/spec/upload")
def spec_upload():
    """Accept a pasted or uploaded document as a CANDIDATE, never as the truth."""
    import speclock
    from mockd import Spec
    payload = request.get_json(silent=True) or {}
    text = payload.get("content") or ""
    name = payload.get("name") or "uploaded.json"
    if not text.strip():
        return jsonify({"ok": False, "error": "nothing to import"}), 400
    if not re.fullmatch(r"[A-Za-z0-9._-]+\.(json|ya?ml)", name):
        return jsonify({"ok": False, "error": "name must be a simple file name"}), 400
    try:
        Spec(text=text, origin=name)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"not a usable OpenAPI document: {exc}"}), 400
    SPEC_DIR.mkdir(exist_ok=True)
    path = SPEC_DIR / name
    path.write_text(text)
    return jsonify({"ok": True, "file": str(path.relative_to(HERE)),
                    "summary": {k: v for k, v in speclock.summarise(text).items()
                                if k != "operation_ids"},
                    "state": speclock.compare(text)})


@app.post("/api/spec/diff")
def spec_diff():
    """Operation-level difference between a candidate and the pinned source."""
    import speclock
    payload = request.get_json(silent=True) or {}
    try:
        text = speclock._read(payload.get("spec"))
        against = payload.get("against") or (speclock.load() or {}).get("source")
        if not against:
            return jsonify({"error": "nothing to compare against"}), 400
        return jsonify(speclock.diff(text, speclock._read(against)))
    except SystemExit as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 400


@app.post("/api/spec/lock")
def spec_lock_write():
    """Adopt a document deliberately. Writes spec.lock.json, which is committed —
    so the decision shows up in review rather than in somebody's working copy."""
    import speclock
    payload = request.get_json(silent=True) or {}
    path = payload.get("spec")
    if not path:
        return jsonify({"ok": False, "error": "which spec?"}), 400
    try:
        lock = speclock.write(speclock._read(path), path, payload.get("note"))
    except SystemExit as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "lock": lock,
                    "message": "pinned — commit spec.lock.json so the team shares it"})


@app.get("/api/spec-report")
def spec_report_endpoint():
    """How the loaded document scores against SPEC_GUIDE.md."""
    if mock.running:
        return jsonify(mock_get("/_mock/spec-report", timeout=20))
    spec_path = project.active_spec(request.args.get("spec") or mock.options.get("spec"))
    if not spec_path:
        return jsonify({"error": "no spec"}), 400
    try:
        from mockd import Source, Spec, spec_report
        src = Source(spec_path, poll=0, cache_dir=str(LOG_DIR))
        text, _ = src.read(force=True)
        if text is None:
            return jsonify({"error": src.error or "could not read spec"}), 400
        return jsonify(spec_report(Spec(text=text, origin=spec_path)))
    except Exception as exc:
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 400


@app.get("/api/spec-guide")
def spec_guide():
    """The authoring standard itself, so the console and the repo cannot drift."""
    path = HERE / "SPEC_GUIDE.md"
    if not path.exists():
        return jsonify({"error": "SPEC_GUIDE.md is missing"}), 404
    return jsonify({"markdown": path.read_text(), "file": str(path)})


@app.get("/api/coverage")
def coverage():
    """Documentation coverage. Works whether or not the mock is running: if it
    is not, the spec is read directly, so you can see the gaps before starting
    anything."""
    override = request.args.get("spec")
    if mock.running and (not override or override == mock.options.get("spec")):
        return jsonify(mock_get("/_mock/coverage", timeout=20))
    spec_path = override or mock.options.get("spec") or project.active_spec(None, "coverage")
    if not spec_path:
        return jsonify({"error": "no spec"}), 400
    try:
        from mockd import Overlay, Source, Spec, coverage as compute
        src = Source(spec_path, poll=0, cache_dir=str(LOG_DIR))
        text, _ = src.read(force=True)
        if text is None:
            return jsonify({"error": src.error or "could not read spec"}), 400
        overlay_path = request.args.get("overlay")
        overlay = Overlay(overlay_path) if overlay_path else Overlay()
        return jsonify(compute(Spec(text=text, origin=spec_path), overlay))
    except Exception as exc:
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 400


@app.get("/api/drift")
def drift():
    guard = require_running()
    if guard:
        return guard
    return jsonify(mock_get("/_mock/drift"))


@app.get("/api/requests")
def requests_log():
    guard = require_running()
    if guard:
        return guard
    return jsonify(mock_get("/_mock/log"))


@app.post("/api/reset")
def reset():
    guard = require_running()
    if guard:
        return guard
    req = urllib.request.Request(mock.base_url() + "/_mock/reset", method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return jsonify(json.loads(resp.read()))


@app.post("/api/reload")
def reload_spec():
    guard = require_running()
    if guard:
        return guard
    req = urllib.request.Request(mock.base_url() + "/_mock/reload", method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return jsonify(json.loads(resp.read()))


@app.get("/api/sample")
def sample():
    """Generate a request body for an operation, so the tester starts from a
    valid payload instead of a blank box."""
    method = (request.args.get("method") or "GET").upper()
    path = request.args.get("path") or ""
    spec_path = project.active_spec(mock.options.get("spec") or request.args.get("spec"))
    if not spec_path:
        return jsonify({"body": None})
    try:
        import random

        from generator import generate_from_schema
        from mockd import Source, Spec
        src = Source(spec_path, poll=0, cache_dir=str(LOG_DIR))
        text, _ = src.read(force=True)
        spec = Spec(text=text, origin=spec_path)
        for route in spec.routes:
            if route["method"] == method and route["path"] == path:
                media = (route["request_body"].get("content") or {}).get("application/json")
                if not media or not media.get("schema"):
                    return jsonify({"body": None})
                body = generate_from_schema(media["schema"], random.Random(route["key"]),
                                            array_items=1)
                return jsonify({"body": body})
    except Exception as exc:
        return jsonify({"body": None, "error": f"{type(exc).__name__}: {exc}"})
    return jsonify({"body": None})


@app.post("/api/send")
def send():
    """Proxy one request to the mock. Everything the tester types goes through
    here, so the browser makes only same-origin calls."""
    payload = request.get_json(silent=True) or {}
    label, base, auth_headers, problem = resolve_target(payload.get("target"))
    if problem:
        return jsonify({"error": problem}), 409
    method = (payload.get("method") or "GET").upper()
    path = payload.get("path") or "/"
    if not path.startswith("/"):
        path = "/" + path
    query = payload.get("query") or ""
    headers = dict(auth_headers or {})      # the environment's own auth first
    for raw in (payload.get("headers") or "").splitlines():
        if ":" in raw:
            name, value = raw.split(":", 1)
            if name.strip():
                headers[name.strip()] = value.strip()

    body_text = (payload.get("body") or "").strip()
    data = None
    if body_text and method in ("POST", "PUT", "PATCH", "DELETE"):
        data = body_text.encode()
        headers.setdefault("Content-Type", "application/json")

    url = base + path + (("?" + query.lstrip("?")) if query else "")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw, status, hdrs = resp.read(), resp.status, dict(resp.headers)
    except urllib.error.HTTPError as exc:
        raw, status, hdrs = exc.read(), exc.code, dict(exc.headers or {})
    except Exception as exc:
        return jsonify({"error": f"{type(exc).__name__}: {exc}",
                        "url": url, "ms": round((time.time() - started) * 1000)}), 200

    ms = round((time.time() - started) * 1000)
    text = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text)
        pretty = json.dumps(parsed, indent=2, ensure_ascii=False)
    except ValueError:
        parsed, pretty = None, text

    # kept so "Save as test" can propose assertions from what actually came back
    record = {
        "request": {"method": method, "path": path, "query": query,
                    "headers": payload.get("headers") or "", "body": body_text},
        "result": {"status": status, "headers": hdrs, "text": text,
                   "json": parsed, "ms": ms},
    }
    LAST_RESPONSE["_last"] = record
    LAST_RESPONSE[f"{method} {path}"] = record

    return jsonify({"status": status, "headers": hdrs, "body": pretty,
                    "ms": ms, "url": url, "bytes": len(raw), "target": label})


JOBS = {}
JOBS_LOCK = threading.Lock()


def start_job(cmd, env_extra=None, timeout=900):
    """Run a child process in the background, streaming its output.

    A sweep of a remote environment takes as long as it takes. Blocking until it
    ends makes a slow run and a hung run look identical — which is exactly how a
    stuck request turned into three minutes of staring at "checking…"."""
    job_id = uuid.uuid4().hex[:12]
    job = {"id": job_id, "cmd": cmd, "lines": [], "done": False, "ok": None,
           "started": time.time(), "proc": None, "cancelled": False}
    with JOBS_LOCK:
        JOBS[job_id] = job

    def pump():
        env = dict(os.environ)
        env.update(env_extra or {})
        try:
            # -u: without it Python block-buffers stdout to a pipe, so progress
            # arrives in 8KB lumps and a live run looks like a frozen one
            argv = list(cmd)
            if argv and argv[0] == sys.executable:
                argv.insert(1, "-u")
            proc = subprocess.Popen(argv, cwd=str(HERE), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1,
                                    env={**env, "PYTHONUNBUFFERED": "1"},
                                    start_new_session=True)
        except Exception as exc:
            job["lines"].append(f"{type(exc).__name__}: {exc}")
            job["done"], job["ok"] = True, False
            return
        job["proc"] = proc
        for line in proc.stdout:
            job["lines"].append(line.rstrip("\n"))
            del job["lines"][:-2000]
            if time.time() - job["started"] > timeout:
                job["lines"].append(f"-- stopped after {timeout}s --")
                _kill(proc)
                break
        proc.wait()
        job["done"] = True
        job["ok"] = proc.returncode == 0 and not job["cancelled"]

    threading.Thread(target=pump, daemon=True).start()
    return job_id


def _kill(proc):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass


@app.get("/api/job/<job_id>/report")
def job_report(job_id):
    """The JSON report a finished test run wrote."""
    try:
        return jsonify(json.loads((LOG_DIR / "last_test_run.json").read_text()))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 404


@app.get("/api/job/<job_id>")
def job_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "no such job"}), 404
    return jsonify({"id": job_id, "done": job["done"], "ok": job["ok"],
                    "cancelled": job["cancelled"],
                    "seconds": round(time.time() - job["started"]),
                    "output": "\n".join(job["lines"])})


@app.post("/api/job/<job_id>/cancel")
def job_cancel(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "no such job"}), 404
    job["cancelled"] = True
    if job.get("proc"):
        _kill(job["proc"])
    return jsonify({"ok": True})


def _run(cmd, timeout=600, env_extra=None):
    """Run a child process.

    `env_extra` exists because anything passed in argv is visible to every user
    on the machine via `ps`. A bearer token or a session cookie must not travel
    that way, so credentials go through the environment instead."""
    try:
        env = dict(os.environ)
        env.update(env_extra or {})
        done = subprocess.run(cmd, cwd=str(HERE), capture_output=True, text=True,
                              timeout=timeout, env=env)
        return {"ok": done.returncode == 0, "code": done.returncode,
                "output": (done.stdout or "") + (done.stderr or "")}
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": -1, "output": "timed out"}
    except Exception as exc:
        return {"ok": False, "code": -1, "output": f"{type(exc).__name__}: {exc}"}


@app.post("/api/verify")
def verify():
    """Self-check: does the MOCK answer its own spec?

    Every operation runs, writes included — the mock's store is in memory, so
    there is nothing to protect. This checks the mock and the overlay, and says
    nothing about the real backend."""
    guard = require_running()
    if guard:
        return guard
    payload = request.get_json(silent=True) or {}
    cmd = [sys.executable, str(HERE / "verify.py"),
           "--spec", mock.options.get("spec"),
           "--base-url", mock.base_url(), "--target", "mock",
           "--quiet", "--fail-on", "never"]
    if payload.get("only"):
        cmd += ["--only", payload["only"]]
    if payload.get("negative"):
        cmd += ["--negative"]
    if payload.get("no_writes"):
        cmd += ["--no-writes"]
    return jsonify(_run(cmd))


# ----------------------------------------------------------------------------
# Saved tests
# ----------------------------------------------------------------------------

LAST_RESPONSE = {}


def _find_suite(name, stage=None, ident=None):
    """A suite name is not unique — the same module exists as a draft and as a
    shared file. (name, stage) is the key; when the caller does not know the
    stage, the one that actually contains the test wins."""
    import tests as t
    matches = [s for s in t.load_suites(include_drafts=True) if s["name"] == name]
    if not matches:
        return None
    if stage:
        exact = [s for s in matches if s.get("_stage") == stage]
        if exact:
            return exact[0]
    if ident:
        holding = [s for s in matches if t.find_test(s, ident)[1] is not None]
        if holding:
            return holding[0]
    return matches[0]


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")


def _spec_for_tests():
    spec_path = project.active_spec(mock.options.get("spec"))
    try:
        from mockd import Source, Spec
        src = Source(spec_path, poll=0, cache_dir=str(LOG_DIR))
        text, _ = src.read(force=True)
        return Spec(text=text, origin=spec_path) if text else None
    except Exception:
        return None


@app.get("/api/tests")
def list_tests():
    import tests as t
    suites = t.load_suites(include_drafts=True)
    history = t.load_history()

    def hist(suite, ident):
        return history.get(f"{suite}/{ident}", {})

    return jsonify({"suites": [{
        "name": s["name"], "description": s.get("description", ""),
        "path": s.get("_path"), "stage": s.get("_stage", "shared"),
        "data": s.get("data") or {},
        "cases": [{"id": c.get("id"), "name": c.get("name"), "tags": c.get("tags", []),
                   "method": (c.get("request") or {}).get("method", "GET"),
                   "path": (c.get("request") or {}).get("path", ""),
                   "assertions": len(c.get("assertions") or []),
                   "history": hist(s["name"], c.get("id"))}
                  for c in (s.get("cases") or [])],
        "scenarios": [{"id": sc.get("id"), "name": sc.get("name"),
                       "kind": sc.get("kind", "api"), "tags": sc.get("tags", []),
                       "history": hist(s["name"], sc.get("id")),
                       "steps": [{"name": st.get("name"),
                                  "role": st.get("role"),
                                  "method": (st.get("request") or {}).get("method", "GET"),
                                  "path": (st.get("request") or {}).get("path", ""),
                                  "captures": list((st.get("capture") or {}).keys()),
                                  "assertions": len(st.get("assertions") or [])}
                                 for st in (sc.get("steps") or [])]}
                      for sc in (s.get("scenarios") or [])],
    } for s in suites]})


@app.post("/api/tests/suggest")
def suggest_assertions():
    """Turn the response QA just looked at into a draft test.

    Authoring from a blank page is why suites do not get written; ticking lines
    off a real response is a different job entirely."""
    import tests as t
    payload = request.get_json(silent=True) or {}
    key = payload.get("key") or ""
    saved = LAST_RESPONSE.get(key) or LAST_RESPONSE.get("_last")
    if not saved:
        return jsonify({"error": "send the request first — there is no response to read"}), 400
    spec = _spec_for_tests()
    suite_hint, summary, op_key = None, None, None
    if spec is not None:
        route, _ = spec.match(saved["request"]["method"], saved["request"]["path"])
        if route:
            op_key = route["key"]
            summary = route["summary"]
            # a test belongs with the module it exercises; the spec already
            # groups operations by tag, so reuse that rather than inventing one
            suite_hint = (route["tags"] or [None])[0]
    return jsonify({
        "request": saved["request"],
        "status": saved["result"]["status"],
        "suite_hint": _slug(suite_hint) if suite_hint else None,
        "operation": op_key,
        "summary": summary,
        "suites": sorted({s["name"] for s in __import__("tests").load_suites(include_drafts=True)}),
        "assertions": t.suggest(saved["result"], spec,
                                saved["request"]["method"], saved["request"]["path"]),
        "capture": t.suggest_captures(saved["result"]),
    })


@app.post("/api/tests/try")
def try_test():
    """Run a test definition that has not been saved yet.

    Authoring assertions without running them is guesswork — you find out whether
    `data.total gte 1` is even true when CI tells you tomorrow. This runs exactly
    what is in the editor, right now, and reports each assertion separately."""
    import tests as t
    payload = request.get_json(silent=True) or {}
    body = payload.get("test")
    if not isinstance(body, dict):
        return jsonify({"error": "no test to run"}), 400

    # the section's own variables are part of the test's world; without them a
    # perfectly good {{departmentTitle}} looks like an undefined variable
    data = dict(payload.get("data") or {})
    suite = _find_suite(payload.get("suite"), payload.get("stage"), body.get("id"))
    if suite:
        merged = dict(suite.get("data") or {})
        merged.update(data)
        data = merged
    data.update(body.get("data") or {})

    problems = t.validate_test({**body, "data": data})
    if problems:
        return jsonify({"ok": False, "errors": problems}), 200

    label, base, auth_headers, problem = resolve_target(payload.get("target"))
    if problem:
        return jsonify({"error": problem}), 409

    spec = _spec_for_tests()
    runner = t.Runner(base, auth_headers or {}, spec, timeout=30)
    data = t.interpolate(data, {}, strict=False)
    try:
        if body.get("steps"):
            outcome = runner.run_scenario(body, data)
        else:
            outcome = runner.run_case(body, data)
    except Exception as exc:
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 200
    outcome["target"] = label
    outcome["base_url"] = base
    return jsonify({"ok": True, "result": outcome})


@app.post("/api/tests/save")
def save_test():
    """Append a case, or a scenario step, to a suite file on disk."""
    import tests as t
    payload = request.get_json(silent=True) or {}
    suite_name = (payload.get("suite") or "").strip()
    if not suite_name or not re.fullmatch(r"[A-Za-z0-9._-]+", suite_name):
        return jsonify({"error": "suite must be a simple file name"}), 400

    stage = payload.get("stage") or "draft"
    suites = {s["name"]: s for s in t.load_suites(include_drafts=True)
              if s.get("_stage") == stage}
    suite = suites.get(suite_name) or {"name": suite_name, "data": {}, "cases": [],
                                       "scenarios": [], "_stage": stage}
    suite.setdefault("cases", [])
    suite.setdefault("scenarios", [])

    entry = payload.get("case") or {}
    if payload.get("scenario"):
        target = payload["scenario"]
        existing = next((sc for sc in suite["scenarios"] if sc.get("id") == target.get("id")),
                        None)
        if existing is None:
            existing = {"id": target.get("id"), "name": target.get("name") or target.get("id"),
                        "kind": target.get("kind", "api"), "steps": []}
            suite["scenarios"].append(existing)
        if target.get("kind"):
            existing["kind"] = target["kind"]
        existing["steps"].append(entry)
    else:
        suite["cases"] = [c for c in suite["cases"] if c.get("id") != entry.get("id")]
        suite["cases"].append(entry)

    path = t.save_suite(suite, stage=stage)
    return jsonify({"ok": True, "file": str(path), "suite": suite_name, "stage": stage,
                    "cases": len(suite["cases"]), "scenarios": len(suite["scenarios"])})


@app.get("/api/tests/one")
def get_test():
    """The full definition, for editing."""
    import tests as t
    suite_name, ident = request.args.get("suite"), request.args.get("id")
    suite = _find_suite(suite_name, request.args.get("stage"), ident)
    if not suite:
        return jsonify({"error": "no such suite"}), 404
    kind, test = t.find_test(suite, ident)
    if test is None:
        return jsonify({"error": "no such test"}), 404
    return jsonify({"suite": suite_name, "stage": suite.get("_stage", "shared"),
                    "kind": "case" if kind == "cases" else "scenario",
                    "test": test, "data": suite.get("data") or {}})


@app.post("/api/tests/update")
def update_test():
    """Replace a test's definition — assertions edited, steps added or removed,
    renamed. A saved test that cannot be changed gets abandoned."""
    import tests as t
    payload = request.get_json(silent=True) or {}
    suite_name, ident = payload.get("suite"), payload.get("id")
    body = payload.get("test")
    if not isinstance(body, dict):
        return jsonify({"error": "test must be an object"}), 400
    suite = _find_suite(suite_name, payload.get("stage"), ident)
    if not suite:
        return jsonify({"error": "no such suite"}), 404
    kind, existing = t.find_test(suite, ident)
    if existing is None:
        return jsonify({"error": "no such test"}), 404
    suite[kind] = [body if x.get("id") == ident else x for x in suite[kind]]
    if payload.get("data") is not None:
        suite["data"] = payload["data"]
    path = t.save_suite(suite, stage=suite.get("_stage"))
    return jsonify({"ok": True, "file": str(path)})


@app.post("/api/tests/import")
def import_tests_endpoint():
    """Paste in one test, a list of them, or a whole suite.

    This is the extension point for generated tests: whatever writes the JSON —
    a teammate, a script, an assistant — comes through this one validated door,
    and lands in the draft workspace, where it still has to pass before it can
    be promoted into the shared repo."""
    import tests as t
    payload = request.get_json(silent=True) or {}
    raw = payload.get("tests")
    suite_name = (payload.get("suite") or "imported").strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", suite_name):
        return jsonify({"ok": False, "errors": ["suite must be a simple file name"]}), 400
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError as exc:
        return jsonify({"ok": False, "errors": [f"not valid JSON: {exc}"]}), 400
    if parsed is None:
        return jsonify({"ok": False, "errors": ["nothing to import"]}), 400
    outcome = t.import_tests(parsed, suite_name,
                             payload.get("stage") or "draft",
                             payload.get("data"))
    return jsonify(outcome), (200 if outcome.get("ok") else 400)


@app.get("/api/tests/prompt")
def test_prompt():
    """The brief an assistant needs to write tests for one operation: the
    grammar, that operation's contract, a real response, and what is already
    covered so it does not duplicate."""
    import tests as t
    operation = request.args.get("operation")
    spec = _spec_for_tests()
    sample = None
    if operation and mock.running:
        method, _, path = operation.partition(" ")
        route = next((r for r in (spec.routes if spec else [])
                      if r["method"] == method.upper() and r["path"] == path), None)
        body = None
        if route:
            import random as _r

            from generator import generate_from_schema
            media = (route["request_body"].get("content") or {}).get("application/json")
            if media and media.get("schema"):
                body = generate_from_schema(media["schema"], _r.Random(route["key"]),
                                            array_items=1)
            for prm in route["parameters"]:
                if prm.get("in") == "path":
                    value = generate_from_schema(prm.get("schema") or {"type": "string"},
                                                 _r.Random(prm["name"]))
                    path = path.replace("{%s}" % prm["name"], str(value))
        result = t.send(method.upper(), mock.base_url() + path, {}, body)
        sample = result.get("json")

    covered = []
    for suite in t.load_suites(include_drafts=True):
        covered += [c.get("id") for c in (suite.get("cases") or [])]
        covered += [s.get("id") for s in (suite.get("scenarios") or [])]
    return jsonify({"prompt": t.context_pack(spec, operation, sample, covered),
                    "operation": operation})


@app.post("/api/tests/promote")
def promote_test():
    """Draft -> shared. Refuses a test that has not gone green, because a red
    assertion in the common repo costs everyone else time."""
    import tests as t
    payload = request.get_json(silent=True) or {}
    try:
        path = t.promote(payload.get("suite"), payload.get("id"),
                         bool(payload.get("force")))
    except SystemExit as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "file": str(path),
                    "message": "moved into the shared suite — commit it to share it"})


@app.post("/api/tests/delete")
def delete_test():
    import tests as t
    payload = request.get_json(silent=True) or {}
    ident = payload.get("id")
    suite = _find_suite(payload.get("suite"), payload.get("stage"), ident)
    if not suite:
        return jsonify({"error": "no such suite"}), 404
    suite["cases"] = [c for c in (suite.get("cases") or []) if c.get("id") != ident]
    suite["scenarios"] = [s for s in (suite.get("scenarios") or []) if s.get("id") != ident]
    if not (suite["cases"] or suite["scenarios"]) and suite.get("_path"):
        Path(suite["_path"]).unlink(missing_ok=True)
    else:
        t.save_suite(suite, stage=suite.get("_stage"))
    return jsonify({"ok": True})


@app.post("/api/tests/run")
def run_tests():
    """Anyone can press this, against any environment, with the same test data."""
    payload = request.get_json(silent=True) or {}
    cmd = [sys.executable, str(HERE / "tests.py"), "run"]
    if payload.get("env"):
        cmd += ["--env", payload["env"]]
    elif payload.get("base_url"):
        cmd += ["--base-url", payload["base_url"]]
    elif mock.running:
        cmd += ["--base-url", mock.base_url()]
    else:
        return jsonify({"error": "pick an environment, or start the mock"}), 400
    for key, flag in (("suite", "--suite"), ("only", "--only")):
        if payload.get(key):
            cmd += [flag, payload[key]]
    if payload.get("drafts"):
        cmd += ["--drafts"]
    for kind in (payload.get("kinds") or []):
        cmd += ["--kind", kind]
    for tag in (payload.get("tags") or []):
        cmd += ["--tag", tag]
    if payload.get("verbose"):
        cmd += ["--verbose"]
    cmd += ["--json", str(LOG_DIR / "last_test_run.json")]
    return jsonify({"job": start_job(cmd, timeout=900)})


@app.get("/api/ci")
def ci_snippet():
    """The pipeline, ready to paste. Minimal integration is the point: a company
    adds one file and gets drift, self-check and the team's own suites."""
    files = {"github": HERE / ".github/workflows/api-contract.yml",
             "gitlab": HERE / "ci/gitlab-ci.yml"}
    which = request.args.get("flavour", "github")
    path = files.get(which, files["github"])
    if not path.exists():
        return jsonify({"error": f"{path.name} has not been generated"}), 404
    return jsonify({"flavour": which, "file": str(path), "snippet": path.read_text()})


@app.get("/api/environments")
def list_environments():
    """Named targets the team shares. Secrets are never returned — only whether
    they resolve, so a tester can see 'dev is missing DEV_PASSWORD' without the
    console ever holding the password."""
    try:
        import environments as envmod
        envs = envmod.load()
    except Exception as exc:
        return jsonify({"error": f"{type(exc).__name__}: {exc}", "environments": []})
    out = []
    for name, env in sorted(envs.items()):
        missing = []
        envmod.resolve(env, missing)
        out.append({
            "name": name,
            "description": env.get("description", ""),
            "base_url": envmod.resolve(env.get("base_url", ""), []),
            "auth_mode": (env.get("auth") or {}).get("mode", "none"),
            "unresolved": sorted(set(missing)),
            "ready": not missing,
        })
    return jsonify({"environments": out})


@app.get("/api/environments/<name>/vars")
def environment_vars(name):
    """What this environment still needs, and where it is set.

    Values already set are never sent back to the browser — only whether they
    resolve, and a masked hint for the ones that do."""
    try:
        import environments as envmod
        env = envmod.get(name)
    except SystemExit as exc:
        return jsonify({"error": str(exc)}), 404

    fields = []
    for var in envmod.variables_in(env):
        current = envmod.resolve("${%s}" % var, [])
        fields.append({
            "name": var,
            "set": bool(current),
            "hint": envmod.mask(current) if current and _is_secret_name(var) else current,
            "secret": _is_secret_name(var),
            "purpose": _purpose(var),
        })
    return jsonify({"environment": name,
                    "description": env.get("description", ""),
                    "auth_mode": (env.get("auth") or {}).get("mode", "none"),
                    "fields": fields,
                    "writes_to": str((HERE / ".env").relative_to(HERE))})


def _is_secret_name(var):
    return any(w in var.lower() for w in ("password", "token", "secret", "cookie", "key"))


def _purpose(var):
    low = var.lower()
    if low.endswith("base_url"):
        return "the root of the API, e.g. https://api.dev.example.com"
    if "username" in low:
        return "the account the tests log in as"
    if "password" in low:
        return "its password — stored only in .env, which is gitignored"
    if "cookie" in low:
        return "a session cookie from your browser, e.g. access_token=..."
    if "token" in low:
        return "a bearer token issued out of band"
    if low.endswith("_id"):
        return "the id of a row that exists on that server, for tests that read one"
    return ""


@app.post("/api/environments/vars")
def set_environment_vars():
    """Write values into .env. Never into the committed environments.json."""
    import environments as envmod
    payload = request.get_json(silent=True) or {}
    values = {k: v for k, v in (payload.get("values") or {}).items()
              if isinstance(k, str) and re.fullmatch(r"\w+", k) and str(v) != ""}
    if not values:
        return jsonify({"ok": False, "error": "nothing to set"}), 400
    path = envmod.write_dotenv(values)
    return jsonify({"ok": True, "file": str(path), "count": len(values),
                    "message": f"{len(values)} value(s) written to {path.name} — "
                               f"gitignored, and read live, so no restart is needed"})


@app.post("/api/env-login")
def env_login():
    """Prove an environment's credentials before running anything against it."""
    payload = request.get_json(silent=True) or {}
    name = payload.get("name")
    try:
        import environments as envmod
        env = envmod.get(name)
        headers, note = envmod.authenticate(env)
    except SystemExit as exc:
        return jsonify({"ok": False, "error": str(exc)})
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    return jsonify({"ok": True, "note": note,
                    "base_url": envmod.resolve(env.get("base_url", ""), []),
                    "headers": {k: envmod.mask(v) if envmod.is_secret(k, v) else v
                                for k, v in headers.items()}})


@app.post("/api/verify-live")
def verify_live():
    """Live check: does the REAL backend answer the spec?

    A different question from the self-check, against a different machine, with
    different stakes — writes here create and delete real rows, so they stay
    opt-in and the caller has to say so explicitly."""
    payload = request.get_json(silent=True) or {}
    env_name = (payload.get("env") or "").strip()
    base_url = (payload.get("base_url") or "").strip()
    if not env_name and not base_url:
        return jsonify({"error": "pick an environment, or type a base URL"}), 400
    spec_path = project.active_spec(payload.get("spec") or mock.options.get("spec"))

    token = (payload.get("token") or "").strip()
    raw_headers = [r.strip() for r in (payload.get("headers") or "").splitlines() if r.strip()]
    carries_auth = bool(token) or any(
        r.split(":", 1)[0].strip().lower() in ("authorization", "cookie") for r in raw_headers)

    cmd = [sys.executable, str(HERE / "verify.py"),
           "--spec", spec_path, "--target", "live", "--quiet", "--fail-on", "never"]

    secret_env = {}
    if carries_auth:
        # A token typed into the form IS the credential. Passing --env as well
        # would make verify.py also run that environment's login and fail on a
        # password the person deliberately did not supply.
        if not base_url and env_name:
            try:
                import environments as envmod
                base_url = envmod.base_url_for(env_name)
            except Exception as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400
        if token:
            value = token if token.lower().startswith(("bearer ", "basic ")) \
                else f"Bearer {token}"
            secret_env["CLAVIS_HEADER_1"] = f"Authorization: {value}"
    elif env_name:
        # no token given: let the environment authenticate however it is defined
        cmd += ["--env", env_name]

    if base_url:
        cmd += ["--base-url", base_url]
    for i, raw in enumerate(raw_headers, start=2):
        secret_env[f"CLAVIS_HEADER_{i}"] = raw
    if payload.get("only"):
        cmd += ["--only", payload["only"]]
    for key in (payload.get("skip") or []):
        cmd += ["--skip", key]
    if payload.get("skip_streaming"):
        cmd += ["--skip-streaming"]
    if payload.get("negative"):
        cmd += ["--negative"]
    if payload.get("allow_writes"):
        cmd += ["--allow-writes"]
    if payload.get("learn_overlay"):
        cmd += ["--learn-overlay", "learned_overlay.json"]
    job = start_job(cmd, secret_env, timeout=int(payload.get("timeout") or 600))
    return jsonify({"job": job, "target": base_url or env_name,
                    "auth": ("bearer token from the form" if token else
                             "headers from the form" if carries_auth else
                             f"environment {env_name}" if env_name else "none")})


@app.post("/api/overlay")
def overlay():
    payload = request.get_json(silent=True) or {}
    spec_path = project.active_spec(mock.options.get("spec") or payload.get("spec"))
    out = payload.get("out") or "mock_overlay.json"
    cmd = [sys.executable, str(HERE / "build_overlay.py"), "--spec", spec_path, "--out", out]
    if payload.get("check"):
        cmd += ["--check"]
    if payload.get("report"):
        cmd += ["--report"]
    return jsonify(_run(cmd))


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------


@app.get("/")
def index():
    return send_from_directory(HERE, "console.html")


@app.get("/console.js")
def console_js():
    return send_from_directory(HERE, "console.js", mimetype="application/javascript")


@app.after_request
def no_cache(resp: Response):
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _shutdown(signum, _frame):
    """A console killed with SIGTERM must take its mock with it — otherwise the
    next start finds the port occupied by a process nobody remembers owning."""
    if mock.running:
        mock.stop()
        print("mockd console: stopped the mock server")
    raise SystemExit(0)


def main():
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, _shutdown)
        except (ValueError, OSError):
            pass
    port = int(os.environ.get("CONSOLE_PORT", 4100))
    print(f"mockd console: http://localhost:{port}")
    print(f"  working directory: {HERE}")
    print("  Ctrl-C stops the console; it also stops the mock it started.")
    try:
        app.run(host="127.0.0.1", port=port, threaded=True)
    finally:
        if mock.running:
            mock.stop()
            print("mockd console: stopped the mock server")


if __name__ == "__main__":
    main()
