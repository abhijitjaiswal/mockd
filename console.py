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
import shlex
import shutil
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
                   "--host", os.environ.get("MOCK_HOST", "127.0.0.1")]
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
PREFS_FILE = HERE / ".mockd-console.json"
# Deliberately NOT "spec": which document this project is about lives in
# mockd.json, and two competing defaults is how you end up staring at
# coverage for a document you thought you had replaced.
REMEMBERED = ("overlay", "port", "array_items", "validation_mode",
              "stateful", "require_auth", "allow_undocumented", "headers")


def load_preferences():
    try:
        saved = json.loads(PREFS_FILE.read_text())
    except (OSError, ValueError):
        # Nothing remembered yet is the state of every new install. Returning
        # nothing at all left the page on its built-in sample, so somebody who
        # had just loaded their own document and pressed Start got the sample.
        saved = {}
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
    was = project.active_spec() if not module else None
    project.set_active(spec=spec, overlay=payload.get("overlay"), module=module)
    if was and was != project.active_spec():
        remember_document_change(was, project.active_spec())

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
        if ok and not module:
            # a new document means a new baseline; without this the tests on
            # screen went on describing the document that was just replaced
            try:
                begin_selfcheck(wanted)
            except Exception:
                pass

    where = f"module {module}" if module else "the whole project"
    return jsonify({"ok": True, "spec": project.active_spec(), "modules": project.report(),
                    "mock_running": mock.running, "mock_restarted": bool(moved),
                    "mock_spec": mock.options.get("spec") if mock.running else None,
                    "message": f"{where} now uses {spec} — commit mockd.json "
                               f"so the team shares it.{moved}"})


CHANGE_FILE = LOG_DIR / "document-change.json"


def document_impact(before, after):
    """What moving from one document to another does to the tests we have."""
    import impact
    import specdiff
    import tests as t
    return impact.assess(specdiff._read(before), specdiff._read(after),
                         t.load_suites(include_drafts=True), before, after)


def remember_document_change(before, after):
    """Keep what the last switch of document changed, so it can be pointed out
    until the tests it reaches have been run. Never gets in the way of the
    switch itself: a comparison that cannot be made is simply not remembered."""
    try:
        found = document_impact(before, after)
        if found["identical"]:
            CHANGE_FILE.unlink(missing_ok=True)
            return
        LOG_DIR.mkdir(exist_ok=True)
        CHANGE_FILE.write_text(json.dumps({
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "from": before, "to": after, "sentence": found["sentence"],
            "counts": found["counts"], "changes": found["changes"][:40],
            "tests": found["tests"]["affected"]}, indent=1))
    except Exception:
        pass


@app.post("/api/spec/impact")
def spec_impact():
    """A document that has been loaded but not yet put to use: what would it
    change, and which of our tests would it reach?"""
    payload = request.get_json(silent=True) or {}
    proposed = (payload.get("to") or "").strip()
    current = (payload.get("from") or "").strip() or project.active_spec()
    if not proposed:
        return jsonify({"ok": False, "error": "which document?"}), 400
    if proposed == current:
        return jsonify({"ok": True, "same_file": True, "identical": True,
                        "sentence": "That is the document already in use.",
                        "counts": {}, "changes": [], "tests": {"total": 0, "affected": []}})
    try:
        return jsonify({"ok": True, **document_impact(current, proposed)})
    except SystemExit as exc:
        return jsonify({"ok": False, "error": str(exc)}), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 200


@app.get("/api/noticed")
def noticed():
    """Everything mockd has noticed by itself that somebody should know about.

    Nobody asked for any of this to be checked. That is the point: a contract
    that moved, a test that cannot pass, a mock that no longer matches — each
    was already knowable from what is on disk, and was waiting to be looked
    for. One list, worst first, each with the one thing to do about it."""
    import tests as t
    out = []
    suites = t.load_suites(include_drafts=True)
    history = t.load_history()

    # 1. the document moved, and tests that use what moved have not run since
    try:
        change = json.loads(CHANGE_FILE.read_text())
    except (OSError, ValueError):
        change = None
    if change and not change.get("dismissed"):
        waiting = []
        for test in change.get("tests") or []:
            runs = (history.get(f"{test['suite']}/{test['id']}") or {}).get("by_env") or {}
            if not any((r.get("last_run") or "") > change["at"] for r in runs.values()):
                waiting.append(f"{test['suite']}|{test['stage']}|{test['id']}")
        counts = change.get("counts") or {}
        # Dealt with once every test it reaches has been run again — what those
        # runs found then shows up as failures, below. A breaking change that
        # reaches no test at all is still said once, until put away.
        if waiting or (counts.get("breaking") and not change.get("tests")):
            out.append({
                "key": "document-changed", "level": "act" if counts.get("breaking") else "look",
                "title": "The API document changed",
                "detail": change.get("sentence") or "",
                "more": [f"{c['operation']}: {c['what']}" + (f" ({c['detail']})" if c.get("detail") else "")
                         for c in (change.get("changes") or []) if c.get("severity") == "breaking"][:6],
                "tests": waiting,
                "action": ({"label": f"Run the {len(waiting)} affected test{'s' if len(waiting) != 1 else ''}",
                            "go": "tests", "only": waiting,
                            "label_for_filter": "affected by the document change"}
                           if waiting else None),
                "dismiss": True})

    # 2. tests that cannot pass until somebody gives them a value
    needing = [f"{s.get('name')}|{s.get('_stage', 'shared')}|{x.get('id')}"
               for s in suites for x in (s.get("scenarios") or []) + (s.get("cases") or [])
               if t.waiting_for(x, s.get("data"))]
    if needing:
        n = len(needing)
        out.append({"key": "needs-values", "level": "act",
                    "title": f"{n} test{'s are' if n != 1 else ' is'} waiting for a value only you know",
                    "detail": "Something nothing in the API can supply. Until it is filled in, "
                              "the test cannot pass on a real server.",
                    "action": {"label": "Show them", "go": "tests", "only": needing,
                               "label_for_filter": "waiting for a value"}})

    # 3. the mock no longer answers the way the document says
    try:
        check = json.loads((LOG_DIR / "mock-selfcheck.json").read_text())
        broken = [r for r in check.get("results") or [] if r.get("level") == "error"]
    except Exception:
        broken = []
    if broken and mock.running:
        n = len(broken)
        out.append({"key": "mock-unfaithful", "level": "act",
                    "title": f"The mock does not match the document on {n} endpoint{'s' if n != 1 else ''}",
                    "detail": "Tests that pass there may be passing on the mock's own behaviour "
                              "rather than on what was agreed.",
                    "more": [str(r.get("operation") or r.get("key") or "") for r in broken][:6],
                    "action": {"label": "See which", "go": "overview"}})

    # 4. tests whose last run on a server did not pass
    failing = {}
    for s in suites:
        for x in (s.get("scenarios") or []) + (s.get("cases") or []):
            if t.status_of(x) == "retired":
                continue
            runs = (history.get(f"{s.get('name')}/{x.get('id')}") or {}).get("by_env") or {}
            for env, seen in runs.items():
                if re.match(r"https?:", env) or seen.get("last_outcome") in (None, "pass"):
                    continue
                failing.setdefault(env, []).append(
                    f"{s.get('name')}|{s.get('_stage', 'shared')}|{x.get('id')}")
    # ...and of those, the ones that matter most: an endpoint that answered
    # somebody who had not signed in
    access = {f"{s.get('name')}|{s.get('_stage', 'shared')}|{x.get('id')}"
              for s in suites for x in (s.get("cases") or []) if "access" in (x.get("tags") or [])}
    for env in sorted(failing):
        opened = [k for k in failing[env] if k in access]
        if env == "mock" or not opened:
            continue
        failing[env] = [k for k in failing[env] if k not in access]
        n = len(opened)
        out.append({"key": f"open-{env}", "level": "act",
                    "title": f"{n} access check{'s' if n != 1 else ''} did not pass on {env}",
                    "detail": "Each calls an endpoint without signing in and expects to be "
                              "refused. One that answers anyway is open to anyone.",
                    "action": {"label": "Show them", "go": "tests", "only": opened, "server": env,
                               "label_for_filter": f"access checks that did not pass on {env}"}})
    for env, keys in sorted(failing.items(), key=lambda kv: (kv[0] == "mock", kv[0])):
        if not keys:
            continue
        n = len(keys)
        out.append({"key": f"failing-{env}", "level": "look",
                    "title": f"{n} test{'s' if n != 1 else ''} did not pass on {env} last time",
                    "detail": "A failure on a real server is usually the API; on the mock it is "
                              "usually the test." if env != "mock" else
                              "On the mock a failure is usually the test, or a rule the mock "
                              "cannot know.",
                    "action": {"label": "Show them", "go": "tests", "only": keys, "server": env,
                               "label_for_filter": f"not passing on {env}"}})

    # 5. real traffic that does not match the document
    try:
        seen = _recorded_findings()
    except Exception:
        seen = None
    if seen and seen.get("disagreements"):
        n = seen["disagreements"]
        where = (seen.get("meta") or {}).get("server") or "the real API"
        out.append({"key": "real-api-differs", "level": "act",
                    "title": f"{where} and the document disagree in {n} place{'s' if n != 1 else ''}",
                    "detail": seen["sentence"],
                    "action": {"label": "See them", "go": "environments"}})

    # 6. endpoints that are slow, or slower than they themselves used to be —
    #    read from the timings every run has been recording all along
    try:
        import perf
        speed = perf.findings(load_project_spec())
    except Exception:
        speed = {}
    for env, found in sorted(speed.items()):
        if found["slower"]:
            n = len(found["slower"])
            out.append({"key": f"slower-{env}", "level": "look",
                        "title": f"{n} endpoint{'s' if n != 1 else ''} on {env} got slower",
                        "detail": "Compared with how long the same endpoint took in earlier runs.",
                        "more": [f"{x['operation']}: {x['was']} ms, now {x['now']} ms"
                                 for x in found["slower"][:6]]})
        if found["slow"]:
            n = len(found["slow"])
            out.append({"key": f"slow-{env}", "level": "look",
                        "title": f"{n} endpoint{'s' if n != 1 else ''} on {env} usually take"
                                 f"{'s' if n == 1 else ''} over a second",
                        "detail": "From the timings of ordinary test runs, not a load test.",
                        "more": [f"{x['operation']}: about {x['ms']} ms" for x in found["slow"][:6]]})

    order = {"act": 0, "look": 1}
    out.sort(key=lambda f: order.get(f["level"], 9))
    return jsonify({"noticed": out})


@app.post("/api/noticed/dismiss")
def noticed_dismiss():
    key = (request.get_json(silent=True) or {}).get("key")
    if key == "document-changed":
        try:
            change = json.loads(CHANGE_FILE.read_text())
            change["dismissed"] = True
            CHANGE_FILE.write_text(json.dumps(change, indent=1))
        except (OSError, ValueError):
            pass
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "that cannot be put away"}), 200


@app.get("/api/defaults")
def defaults():
    """Offer every spec and overlay this project holds, plus the remembered choice."""
    overlays = sorted(p.name for p in HERE.glob("*overlay*.json"))
    return jsonify({"specs": known_specs(), "overlays": overlays,
                    "remembered": load_preferences()})


SELFCHECK = {"job": None, "started": None, "spec": None}


BASELINE = {"last": None}


def _some_server_signs_in():
    """Is any real server here signed in to? If one is, the API is locked, and
    a document that never says which endpoints need signing in is silent rather
    than saying none do."""
    try:
        import environments as envmod
        for name, env in envmod.load().items():
            if name == "mock" or name.startswith("mock-"):
                continue
            if ((env.get("auth") or {}).get("mode") or "none") != "none":
                return True
            if any(str(k).lower() in ("cookie", "authorization", "x-api-key")
                   for k in (env.get("headers") or {})):
                return True
    except Exception:
        pass
    return False


def _only_what_the_mock_can_show(cases):
    """Keep a not-found check only where the mock itself answers 404.

    The baseline is first of all proof that the mock is faithful, so it has to
    be able to pass there. For a collection the mock holds, an id nothing has
    is a 404, as documented. For one it cannot hold — a read served from a
    hand-written sample, say — every id gets that sample, and a check that can
    never pass on the mock would sit red for ever and teach people to ignore
    red. Asked of the running mock rather than guessed, because whether it
    holds a collection depends on what it was seeded with."""
    if not mock.running:
        return cases, []
    import urllib.error
    import urllib.request
    kept, left_out = [], []
    for case in cases:
        if ((case.get("generated") or {}).get("kind")) != "missing":
            kept.append(case)
            continue
        path = str((case.get("request") or {}).get("path") or "")
        url = mock.base_url() + path.replace("{{$uuid}}", str(uuid.uuid4()))
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                status = resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        except Exception:
            status = 404                  # could not ask; keep the check
        (kept if status == 404 else left_out).append(case if status == 404 else case.get("id"))
    return kept, left_out


def refresh_baseline(reason="asked"):
    """Derive the baseline suite from the project spec and merge it into what
    is saved — so the tests that prove each integration point exist before
    anybody has written one.

    Only for the PROJECT spec. A mock started "just this once" on some other
    document must not rewrite the team's baseline to match it."""
    import blueprint as bp
    import hashlib
    import tests as t
    spec_path = project.active_spec(None)
    if mock.running and mock.options.get("spec") \
            and project.active_spec(mock.options.get("spec")) != spec_path:
        return {"ok": False, "skipped": "the mock is not on the project spec"}
    spec = load_project_spec() if not mock.running else None
    try:
        from mockd import Source, Spec
        text, _ = Source(spec_path, poll=0, cache_dir=str(LOG_DIR)).read(force=True)
        spec = Spec(text=text, origin=spec_path)
    except Exception as exc:
        return {"ok": False, "error": f"could not read {spec_path}: {exc}"}

    fresh, skipped = bp.build(spec, name="baseline", index=id_index("mock"),
                              presume_protected=_some_server_signs_in())
    fresh["cases"], left_out = _only_what_the_mock_can_show(fresh.get("cases") or [])
    existing = next((su for su in t.load_suites(include_drafts=True)
                     if su.get("name") == "baseline" and su.get("_stage") == "draft"), None)
    merged, done = bp.merge(existing, fresh)
    merged["generated_for"] = {
        "spec": spec_path,
        "digest": hashlib.sha256((text or "").encode()).hexdigest()[:16],
        "operations": len(spec.routes)}

    problems = [e for test in (merged.get("scenarios") or [])
                for e in t.validate_test({**test, "data": {**(merged.get("data") or {}),
                                                           **(test.get("data") or {})}},
                                         "scenario")]
    problems += [e for test in (merged.get("cases") or [])
                 for e in t.validate_test(test, "case")]
    if problems:
        return {"ok": False, "errors": problems[:10]}

    changed = bool(done["added"] or done["updated"] or done["removed"]) or existing is None
    if changed:
        if existing is not None and existing.get("_path"):
            merged["_path"] = existing["_path"]
        t.save_suite(merged, stage="draft")
    result = {"ok": True, "reason": reason, "spec": spec_path, "written": changed,
              "tests": len(merged.get("cases") or []) + len(merged.get("scenarios") or []),
              "flows": len(merged.get("scenarios") or []),
              "cases": len(merged.get("cases") or []),
              "skipped_resources": [{"resource": n, "why": w} for n, w in skipped],
              "left_out": left_out,
              "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **done}
    BASELINE["last"] = result
    return result


@app.get("/api/tests/baseline")
def tests_baseline():
    """The tests that exist without anyone writing them, and how they are."""
    import tests as t
    suite = next((su for su in t.load_suites(include_drafts=True)
                  if su.get("name") == "baseline" and su.get("_stage") == "draft"), None)
    if suite is None:
        return jsonify({"exists": False, "last": BASELINE.get("last")})
    items = (suite.get("cases") or []) + (suite.get("scenarios") or [])
    kinds = {}
    for item in items:
        kind = (item.get("generated") or {}).get("kind") or (
            "lifecycle" if "steps" in item else "yours")
        kinds[kind] = kinds.get(kind, 0) + 1
    history = t.load_history()
    by_env = {}
    for item in items:
        for env, seen in ((history.get(f"baseline/{item.get('id')}") or {})
                          .get("by_env") or {}).items():
            slot = by_env.setdefault(env, {"pass": 0, "other": 0})
            slot["pass" if seen.get("last_outcome") == "pass" else "other"] += 1
    return jsonify({"exists": True, "tests": len(items), "kinds": kinds,
                    "generated_for": suite.get("generated_for"),
                    "needs_values": sorted({name for item in items for name in
                                            ((item.get("generated") or {})
                                             .get("needs_values") or [])}),
                    "by_env": by_env, "last": BASELINE.get("last")})


@app.post("/api/tests/baseline")
def tests_baseline_refresh():
    return jsonify(refresh_baseline("asked"))


def begin_selfcheck(spec_path):
    """Ask the mock, the moment it comes up, whether it answers its own spec.

    Everything downstream is built on the assumption that the mock is faithful.
    When it is not — a stored object served through an operation documenting a
    different shape, a status the document never mentions — every test written
    against it is measuring the mock's imagination. That is worth three seconds
    at startup rather than an afternoon of confusion later."""
    cmd = [sys.executable, str(HERE / "verify.py"),
           "--spec", spec_path, "--target", "mock",
           "--base-url", mock.base_url(), "--allow-writes",
           "--quiet", "--fail-on", "never",
           "--report", str(LOG_DIR / "mock-selfcheck.json")]
    job = start_job(cmd, timeout=300)
    SELFCHECK.update(job=job, started=time.time(), spec=spec_path)

    def then_baseline():
        # The self-check is what tells us which endpoint returns which id, and
        # the baseline needs that to read its foreign keys — so it follows it.
        for _ in range(600):
            if (JOBS.get(job) or {}).get("done"):
                break
            time.sleep(0.5)
        try:
            refresh_baseline("the mock started")
        except Exception as exc:
            BASELINE["last"] = {"ok": False, "error": str(exc)}

    threading.Thread(target=then_baseline, daemon=True).start()
    return job


@app.get("/api/mock/selfcheck")
def mock_selfcheck():
    """How the mock did against its own document, from the run at startup."""
    job_id = SELFCHECK.get("job")
    if not job_id:
        return jsonify({"state": "none"})
    job = JOBS.get(job_id) or {}
    if not job.get("done"):
        return jsonify({"state": "running", "job": job_id})
    try:
        report = json.loads((LOG_DIR / "mock-selfcheck.json").read_text())
    except Exception as exc:
        return jsonify({"state": "unreadable", "error": str(exc)})
    summary = report.get("summary") or {}
    results = report.get("results") or []
    broken = [r["operation"] for r in results if r.get("level") == "error"]
    return jsonify({"state": "done", "job": job_id, "spec": SELFCHECK.get("spec"),
                    "summary": summary, "failed": broken[:20],
                    "failed_count": len(broken),
                    "ran_at": report.get("ran_at")})


@app.post("/api/start")
def start():
    options = request.get_json(silent=True) or {}
    if not options.get("spec"):
        return jsonify({"error": "spec is required"}), 400
    ok, message = mock.start(options)
    job = None
    if ok:
        save_preferences(options)     # only a start that worked becomes the default
        if options.get("selfcheck") is not False:
            try:
                job = begin_selfcheck(project.active_spec(options.get("spec")))
            except Exception:
                job = None            # a self-check that cannot start is not a failed start
    return jsonify({"ok": ok, "message": message, "selfcheck": job,
                    "stdout": tail_log()}), (200 if ok else 500)


@app.post("/api/stop")
def stop():
    ok, message = mock.stop()
    return jsonify({"ok": ok, "message": message})


def tail_log(lines=200):
    """The mock's output: how it started, then the most recent traffic.

    The start-up lines say which spec was loaded and how many routes it has, and
    they are the first thing anyone opens this pane to read. The self-check now
    sends a request per operation the moment the mock is up, which pushed them
    out of a plain tail within seconds."""
    if not STDOUT_LOG.exists():
        return ""
    every = STDOUT_LOG.read_text(errors="replace").splitlines()
    if len(every) <= lines:
        return "\n".join(every)
    head = []
    for line in every[:40]:
        head.append(line)
        if "Press CTRL+C" in line:
            break
    else:
        head = every[:12]
    skipped = len(every) - len(head) - (lines - len(head))
    return "\n".join(head + [f"  … {skipped} earlier request line(s) not shown …"]
                     + every[-(lines - len(head)):])


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


def sample_path_and_query(route):
    """A path with its {params} filled by TYPE, and every required query
    parameter present.

    The explorer used to substitute one hardcoded uuid into every path
    parameter and send no query string at all, so an integer id got a uuid and
    a required filter simply went missing — the request was invalid before it
    left the browser, and the API's complaint looked like the API's fault."""
    import random

    import postman as pm
    rng = random.Random(route["key"])
    path = route["path"]
    query = []
    for prm in route.get("parameters") or []:
        schema = prm.get("schema") or {}
        if prm.get("in") == "path":
            path = path.replace("{%s}" % prm["name"], pm.sample_value(schema, rng))
        elif prm.get("in") == "query" and prm.get("required"):
            query.append(f"{prm['name']}={pm.sample_value(schema, rng, '')}")
    return path, "&".join(query)


@app.get("/api/sample-request")
def sample_request():
    """What a valid call to this operation looks like, before you edit it."""
    method = (request.args.get("method") or "GET").upper()
    wanted = request.args.get("path") or "/"
    spec_path = project.active_spec(mock.options.get("spec"))
    try:
        from mockd import Source, Spec
        src = Source(spec_path, poll=0, cache_dir=str(LOG_DIR))
        text, _ = src.read(force=True)
        spec = Spec(text=text, origin=spec_path)
    except Exception as exc:
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 400
    route = next((r for r in spec.routes
                  if r["method"] == method and r["path"] == wanted), None)
    if route is None:
        return jsonify({"error": f"{method} {wanted} is not in the spec"}), 404
    path, query = sample_path_and_query(route)
    required = [prm["name"] for prm in (route.get("parameters") or [])
                if prm.get("in") == "query" and prm.get("required")]

    # the body belongs with the rest of the sample: a caller filling a form
    # wants one answer, not a path from here and a body from somewhere else
    body = None
    media = (route["request_body"].get("content") or {}).get("application/json")
    if media and media.get("schema"):
        import random

        from generator import generate_from_schema
        body = generate_from_schema(media["schema"], random.Random(route["key"]),
                                    array_items=1)

    return jsonify({"path": path, "query": query, "required_query": required,
                    "body": body, "summary": route.get("summary") or "",
                    "statuses": sorted(str(c) for c in route["responses"])})


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
        sampled, query = sample_path_and_query(route)
        url = base + sampled + ("?" + query if query else "")
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
    if payload.get("name_from_title"):
        # called after the document, not after the machine it was fetched from:
        # "localhost.json" says nothing a week later
        title = speclock.summarise(text).get("title") or ""
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48]
        name = f"{slug or 'document'}.json"
        if project.active_spec() == f"specs/{name}":
            name = "new-" + name          # never write over the one in use unasked
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
        # "Running" is true the instant the process exists; it may not be
        # listening yet. Asked in that gap — a restart on a new spec — this
        # raised and the page got an HTML 500 it then tried to read as coverage.
        # Fall through to reading the spec directly, as if the mock were down.
        try:
            return jsonify(mock_get("/_mock/coverage", timeout=20))
        except Exception:
            pass
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
    """The JSON report THIS run wrote.

    It used to read a single shared last_test_run.json and ignore job_id
    altogether, so the panel showed whatever had last finished — or, if nothing
    had, a file left over from another day. That is how running one test came
    back reporting sixteen, and how a run against dev was read as a run against
    the mock. Every run already writes run-<id>.json; resolve it and say so
    plainly when it is not there rather than serving somebody else's results."""
    name = RUN_REPORTS.get(job_id, job_id)
    path = LOG_DIR / f"run-{name}.json"
    if not path.exists():
        return jsonify({"error": "that run left no report — it may still be "
                                 "running, or it failed before finishing"}), 404
    try:
        return jsonify(json.loads(path.read_text()))
    except Exception as exc:
        return jsonify({"error": f"{path.name} is unreadable: {exc}"}), 404


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
                   "levels": t.levels_of(c), **t.record_of(c),
                   "method": (c.get("request") or {}).get("method", "GET"),
                   "path": (c.get("request") or {}).get("path", ""),
                   "assertions": len(c.get("assertions") or []),
                   "needs": t.waiting_for(c, s.get("data")),
                   "history": hist(s["name"], c.get("id"))}
                  for c in (s.get("cases") or [])],
        "scenarios": [{"id": sc.get("id"), "name": sc.get("name"),
                       "kind": sc.get("kind", "api"), "tags": sc.get("tags", []),
                       "levels": t.levels_of(sc), **t.record_of(sc),
                       "needs": t.waiting_for(sc, s.get("data")),
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


def _latest_failure(suite, ident, env):
    """(title, markdown, ran_at, passed_since) for a test's most recent failed
    run, or None when there is none on record."""
    import tests as t
    runs = sorted(LOG_DIR.glob("run-*.json"), key=lambda f: f.stat().st_mtime,
                  reverse=True)[:80]
    passed_since = False
    for path in runs:
        try:
            report = json.loads(path.read_text())
        except Exception:
            continue
        if env and (report.get("env") or report.get("base_url")) != env:
            continue
        for block in report.get("suites") or []:
            if block.get("name") != suite:
                continue
            for item in block.get("results") or []:
                if item.get("id") != ident:
                    continue
                if item.get("outcome") == "pass":
                    passed_since = True
                    continue
                title, markdown = t.bug_report(item, suite, report)
                return title, markdown, report.get("ran_at"), passed_since
    return None


@app.get("/api/tests/bug")
def tests_bug():
    """The most recent failure of one test, written up for somebody else.

    Looks back through the saved runs rather than taking a job id, so the button
    works from the list long after the run that failed has scrolled away."""
    suite, ident = request.args.get("suite") or "", request.args.get("id") or ""
    env = request.args.get("env") or ""
    found = _latest_failure(suite, ident, env)
    if found is None:
        return jsonify({"ok": False, "error":
                        "No failed run of that test was found"
                        + (f" on {env}" if env else "") + ". Run it first."}), 200
    title, markdown, ran_at, stale = found
    return jsonify({"ok": True, "title": title, "markdown": markdown,
                    "ran_at": ran_at, "stale": stale})


# ---------------------------------------------------------------------------
# Where bug reports go
# ---------------------------------------------------------------------------
@app.get("/api/tracker")
def tracker_get():
    import trackers
    return jsonify({"connection": trackers.describe(trackers.current())})


@app.post("/api/tracker/detect")
def tracker_detect():
    """Somebody pasted an address. Say what it is and what else is needed."""
    import trackers
    payload = request.get_json(silent=True) or {}
    try:
        found = (trackers.as_kind(payload.get("address"), payload["kind"])
                 if payload.get("kind") else trackers.detect(payload.get("address")))
    except trackers.Problem as exc:
        return jsonify({"ok": False, "error": str(exc)}), 200
    return jsonify({"ok": True, "kind": found["kind"], "name": found["name"],
                    "what": trackers.KINDS[found["kind"]], "sure": bool(found.get("sure")),
                    "kinds": trackers.KINDS,
                    "needs": [{"field": f, "label": label, "secret": secret}
                              for f, label, secret in trackers.needs_for(found)],
                    "project_missing": found["kind"] == "jira" and not found.get("project")})


@app.post("/api/tracker/connect")
def tracker_connect():
    import trackers
    payload = request.get_json(silent=True) or {}
    try:
        found = (trackers.as_kind(payload.get("address"), payload["kind"])
                 if payload.get("kind") else trackers.detect(payload.get("address")))
    except trackers.Problem as exc:
        return jsonify({"ok": False, "error": str(exc)}), 200
    secrets = payload.get("secrets") or {}
    lacking = [label for f, label, _ in trackers.needs_for(found) if not secrets.get(f)]
    if lacking:
        return jsonify({"ok": False, "error": "It also needs: " + ", ".join(lacking) + "."}), 200
    if found["kind"] == "jira" and not found.get("project"):
        return jsonify({"ok": False, "error": "Which Jira project? Paste an address that has the "
                        "project in it, like …/browse/ABC-1 or …/projects/ABC."}), 200
    saved = trackers.save(found, secrets)
    return jsonify({"ok": True, "connection": trackers.describe(saved)})


@app.post("/api/tracker/forget")
def tracker_forget():
    import trackers
    trackers.forget()
    return jsonify({"ok": True})


@app.post("/api/tests/bug/send")
def tests_bug_send():
    """Send a test's latest failure to the connected tracker, and keep the link
    it hands back on the test — so the next person sees it was already raised."""
    import tests as t
    import trackers
    payload = request.get_json(silent=True) or {}
    suite_name, ident = payload.get("suite") or "", payload.get("id") or ""
    stage, env = payload.get("stage") or "draft", payload.get("env") or ""
    connection = trackers.current()
    if connection is None:
        return jsonify({"ok": False, "error": "Nothing is connected yet.", "connect": True}), 200
    found = _latest_failure(suite_name, ident, env)
    if found is None:
        return jsonify({"ok": False, "error": "No failed run of that test was found"
                        + (f" on {env}" if env else "") + ". Run it first."}), 200
    title, markdown, _, _ = found

    suite = next((su for su in t.load_suites(include_drafts=True)
                  if su.get("name") == suite_name and su.get("_stage", "shared") == stage), None)
    test = t.find_test(suite, ident)[1] if suite else None
    sent_before = [link for link in t.links_of(test or {})
                   if (connection.get("kind") == "jira" and re.fullmatch(r"[A-Z][A-Z0-9_]+-\d+", link))
                   or (connection.get("kind") in ("github", "gitlab") and "/issues/" in link)]
    if sent_before and not payload.get("again"):
        return jsonify({"ok": False, "already": sent_before[-1],
                        "error": f"This test is already linked to {sent_before[-1]}."}), 200
    try:
        result = trackers.send(connection, title, markdown,
                               {"suite": suite_name, "test": ident, "server": env})
    except trackers.Problem as exc:
        return jsonify({"ok": False, "error": str(exc)}), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": f"It could not be sent: {type(exc).__name__}: {exc}"}), 200

    link = (result.get("key") if connection.get("kind") == "jira" else result.get("url")) \
        or result.get("url") or result.get("key")
    if link and test is not None and link not in t.links_of(test):
        test["links"] = [*t.links_of(test), str(link)]
        t.save_suite(suite, stage=stage)
    described = trackers.describe(connection)
    return jsonify({"ok": True, "url": result.get("url"), "key": result.get("key"),
                    "linked": bool(link), "to": described["name"],
                    "message": (f"Sent to {described['name']}"
                                + (f" as {result['key']}" if result.get("key") else "") + ".")})


@app.post("/api/tests/record")
def tests_record():
    """Change how a test is managed — priority, status, owner, links, what it is
    for — without touching what it does.

    Kept apart from editing steps on purpose: re-prioritising fifty tests before
    a release should not require opening fifty tests."""
    import tests as t
    payload = request.get_json(silent=True) or {}
    name, ident = payload.get("suite"), payload.get("id")
    stage = payload.get("stage") or "draft"
    suites = [su for su in t.load_suites(include_drafts=True)
              if su.get("name") == name and su.get("_stage", "shared") == stage]
    if not suites:
        return jsonify({"ok": False, "error": f"no {stage} suite called {name!r}"}), 200
    suite = suites[0]
    _, test = t.find_test(suite, ident)
    if test is None:
        return jsonify({"ok": False, "error": f"no test {ident!r} in {name}"}), 200

    fields = payload.get("fields") or {}
    changed = {}
    for key in ("priority", "status", "owner", "description", "links", "levels"):
        if key not in fields:
            continue
        value = fields[key]
        if key == "priority":
            value = str(value or "").upper()
        if key == "status":
            value = str(value or "").lower()
        if key == "links":
            value = t.links_of({"links": value if isinstance(value, list)
                                else re.split(r"[,\s]+", str(value or ""))})
        if key == "levels":
            value = [str(x).lower() for x in (value or [])]
        if value in ("", [], None):
            test.pop(key, None)
        else:
            test[key] = value
        changed[key] = value
    kind = "scenario" if "steps" in test else "case"
    problems = t.validate_test({**test, "data": {**(suite.get("data") or {}),
                                                 **(test.get("data") or {})}}, kind)
    if problems:
        return jsonify({"ok": False, "errors": problems}), 200
    t.save_suite(suite, stage=stage)
    return jsonify({"ok": True, "changed": changed, "record": t.record_of(test),
                    "levels": t.levels_of(test)})


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


@app.get("/api/tests/taxonomy")
def tests_taxonomy():
    """What can be selected to run: modules, levels, kinds, with counts —
    and the types an assertion may name."""
    import tests as t
    suites = t.load_suites(include_drafts=request.args.get("drafts") != "0")
    return jsonify({**t.taxonomy(suites), "types": t.known_types(suites)})


def _readonly_target(label):
    """The mock is always writable; a named environment says for itself."""
    if not label or label == "mock":
        return False
    try:
        import environments as envmod
        return envmod.is_readonly(label)
    except Exception:
        return False


def live_samples(spec, routes, limit=6):
    """Real responses for a few operations, to put in a brief.

    A schema says what a field is called; a response shows what it looks like.
    Generated tests are markedly better with both, and the mock can answer for
    every operation without touching anybody's real server."""
    if not mock.running:
        return {}
    import urllib.request
    out = {}
    for route in routes[:limit]:
        if route["method"] != "GET" or route["path_params"]:
            continue                       # a read with no ids needed: safe and cheap
        try:
            url = mock.base_url().rstrip("/") + route["path"]
            with urllib.request.urlopen(url, timeout=4) as resp:
                out[route["key"]] = json.loads(resp.read().decode())
        except Exception:
            continue                       # a sample is a bonus, never a blocker
    return out


@app.post("/api/tests/blueprint")
def tests_blueprint():
    """Lifecycle flows derived from the spec — no model, no story, no typing.

    A contract sweep proves each operation answers correctly on its own. It
    cannot prove that the thing you created can then be read, changed and
    removed, because that is a sequence, not an operation. This derives those
    sequences from the shape of the paths, which every spec already carries.

    Preview by default; `import` writes them into the draft workspace, where
    they are exactly as provisional as anything an assistant produced."""
    import blueprint as bp
    payload = request.get_json(silent=True) or {}
    spec_path = project.active_spec(mock.options.get("spec"))
    try:
        from mockd import Source, Spec
        text, _ = Source(spec_path, poll=0, cache_dir=str(LOG_DIR)).read(force=True)
        spec = Spec(text=text, origin=spec_path)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"could not read {spec_path}: {exc}"}), 400

    name = (payload.get("suite") or "derived").strip() or "derived"
    suite, skipped = bp.build(spec, only=(payload.get("only") or None), name=name,
                              index=id_index())
    flows = suite["scenarios"]
    summary = [{
        "id": flow["id"],
        "name": flow["name"],
        "steps": [f"{st['request']['method']} {st['request']['path']}"
                  for st in flow["steps"]],
        "from": (flow.get("generated") or {}).get("from") or [],
        "guessed_capture": not (flow.get("generated") or {}).get("capture_from_schema"),
    } for flow in flows]

    result = {"ok": True, "spec": spec_path, "flows": summary,
              "skipped": [{"resource": name, "why": why} for name, why in skipped],
              # a lifecycle reads back what it created; a stateless mock cannot
              "stateful": bool(mock.options.get("stateful")),
              "running": bool(mock.running)}

    if payload.get("import"):
        import tests as t
        written = t.import_tests(suite, name, stage="draft")
        if not written.get("ok"):
            # generated output goes through the same validator as a human's
            # paste, so a failure here is a real defect, not a formality
            return jsonify({"ok": False, "error": "generated flows did not validate",
                            "errors": written.get("errors") or []}), 400
        result["imported"] = written
        result["suite"] = name
    return jsonify(result)


# ----------------------------------------------------------------------------
# Generators: a local assistant, when there is one
# ----------------------------------------------------------------------------

GENERATED = {}            # job id -> what that generation was for

# Each is run with no tools and a working directory outside this repository.
# They are being handed a document and asked for JSON; nothing about that needs
# the ability to read or write files, and an agent loose in the project is a
# risk with no matching benefit.
GENERATORS = {
    "claude": ["claude", "-p", "--output-format", "text", "--allowedTools", ""],
    "codex": ["codex", "exec", "--skip-git-repo-check", "--sandbox", "read-only", "-"],
}


def generator_available():
    return {name: bool(shutil.which(cmd[0])) for name, cmd in GENERATORS.items()}


def generator_workdir():
    path = LOG_DIR / "generator"
    path.mkdir(parents=True, exist_ok=True)
    return path


def run_generator_now(kind, prompt, timeout=180):
    """Synchronous, for short prompts — used to sharpen a story, not to write
    tests. Returns the reply, or None when the tool is missing or fails."""
    argv = GENERATORS.get(kind)
    if not argv or not shutil.which(argv[0]):
        return None
    try:
        done = subprocess.run(argv, input=prompt, capture_output=True, text=True,
                              timeout=timeout, cwd=str(generator_workdir()))
    except Exception:
        return None
    return done.stdout.strip() if done.returncode == 0 else None


@app.get("/api/generators")
def generators():
    """Which assistants this machine can run, so the UI offers only those."""
    return jsonify({"available": generator_available(),
                    "why": "A generator is optional. Without one you get the "
                           "prompt to paste wherever your team already works."})


# Text that is trying to steer the assistant rather than describe a story. This
# is not a security boundary — the real ones are that the generator runs with no
# tools, outside this repository, and that whatever comes back must validate as
# tests before anything is written. It is here so an obvious misuse is refused
# before it costs a call.
OFF_TOPIC = re.compile(
    r"ignore (all |any )?(previous|prior|above)|disregard (the )?(above|previous)"
    r"|system prompt|you are now|act as|pretend to be"
    r"|write (me )?an? (poem|song|essay|story about|script|program|blog)"
    r"|read (the |my )?(file|\.env|secret|credential|password)"
    r"|exfiltrat|curl |wget |http://|https://|base64|ssh |api[_ ]?key",
    re.I)


def story_out_of_scope(story, spec, matched):
    """Is this a story about testing THIS API, or something else entirely?

    The grounded half matters more than the keyword half: if nothing in the
    document matches, there is nothing to generate and no reason to call
    anything."""
    if OFF_TOPIC.search(story or ""):
        return ("that reads as an instruction rather than a user story. This "
                "generates API tests for the operations in your spec and "
                "nothing else — describe what someone should be able to do.")
    if spec is None:
        return "no spec is loaded, so there is nothing to write tests against."
    import tests as t
    if not matched or not t.story_is_about_this_api(spec, story):
        return ("nothing distinctive in this document matches that story, so "
                "there is nothing to generate. Use words from your own paths, "
                "or check the spec under Source is the one you meant.")
    return None


CRITERIA_ASK = (
    "Below is a user story for API tests. In at most six short bullet points, "
    "say what the story implies must be TRUE for it to be done — the outcomes "
    "and edge cases worth asserting.\n"
    "Rules: describe behaviour only. Do NOT name endpoints, URLs, HTTP methods, "
    "field names or status codes — those come from the API document, not from "
    "you, and inventing them is worse than saying nothing. Reply with the "
    "bullets and nothing else.\n\nSTORY\n"
)


@app.post("/api/tests/generate")
def tests_generate():
    """Build the brief and hand it to a local assistant.

    The brief is still built by the engine: every operation, schema, constraint
    and id in it comes from the document. What an assistant adds is intent —
    what the story implies — and then the tests themselves, which are validated
    exactly as a paste is before anything is written."""
    import tests as t
    payload = request.get_json(silent=True) or {}
    story = (payload.get("story") or "").strip()
    if len(story) < 12:
        return jsonify({"ok": False, "error": "give a sentence or two of story"}), 400
    via = (payload.get("via") or "").strip().lower()
    if via and via not in GENERATORS:
        return jsonify({"ok": False, "error": f"no generator called {via!r}"}), 400
    if via and not shutil.which(GENERATORS[via][0]):
        return jsonify({"ok": False, "error": f"{via} is not installed"}), 400

    # Refuse before spending a call, and before sending anything anywhere.
    import tests as t
    spec = load_project_spec()
    matched = t.relevant_routes(spec, story) if spec is not None else []
    refusal = story_out_of_scope(story, spec, matched)
    if refusal:
        return jsonify({"ok": False, "error": refusal, "out_of_scope": True}), 200

    criteria = None
    if payload.get("refine"):
        criteria = run_generator_now("claude", CRITERIA_ASK + story)

    brief = build_story_brief(story, criteria=criteria)
    if not via:
        # no assistant asked for: the prompt is the deliverable, as before
        return jsonify({"ok": True, "brief": brief, "criteria": criteria,
                        "refined": bool(criteria), "via": None})

    prompt_file = generator_workdir() / f"prompt-{uuid.uuid4().hex[:8]}.txt"
    prompt_file.write_text(brief)
    out_file = prompt_file.with_suffix(".out")
    argv = GENERATORS[via]
    if via == "codex":
        argv = argv + ["-o", str(out_file)]
    shell = " ".join(shlex.quote(a) for a in argv) + f" < {shlex.quote(str(prompt_file))}"
    # Generation is a long operation — a couple of minutes is normal and five is
    # not alarming. The old ten-minute ceiling killed runs that were still going.
    job = start_job(["/bin/sh", "-c", shell], timeout=1800)
    GENERATED[job] = {"via": via, "story": story, "brief": brief,
                      "criteria": criteria,
                      "out": str(out_file) if via == "codex" else None,
                      "module": (payload.get("module") or "").strip()}
    return jsonify({"ok": True, "job": job, "via": via, "criteria": criteria,
                    "refined": bool(criteria), "brief": brief})


def _unique_suite(base):
    """A name nobody has to invent, and that does not collide with one already
    on disk — two generations from similar stories must not overwrite."""
    import tests as t
    slug = re.sub(r"[^a-z0-9]+", "-", (base or "generated").lower()).strip("-")[:32]
    slug = slug or "generated"
    taken = {s.get("name") for s in t.load_suites(include_drafts=True)}
    if slug not in taken:
        return slug
    for _ in range(50):
        candidate = f"{slug}-{uuid.uuid4().hex[:4]}"
        if candidate not in taken:
            return candidate
    return f"{slug}-{uuid.uuid4().hex[:8]}"


def _json_from(reply):
    """The JSON an assistant meant to send, past whatever it wrapped it in."""
    text = (reply or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    start = min([i for i in (text.find("["), text.find("{")) if i != -1] or [-1])
    if start == -1:
        return None, "the reply contained no JSON"
    end = max(text.rfind("]"), text.rfind("}"))
    try:
        return json.loads(text[start:end + 1]), None
    except ValueError as exc:
        return None, f"the reply is not valid JSON: {exc}"


@app.post("/api/tests/generate/import")
def tests_generate_import():
    """Take what the generator produced and put it through the ordinary door."""
    import tests as t
    payload = request.get_json(silent=True) or {}
    job_id = payload.get("job")
    meta = GENERATED.get(job_id)
    job = JOBS.get(job_id)
    if not meta or not job:
        return jsonify({"ok": False, "error": "no such generation"}), 404
    if not job.get("done"):
        return jsonify({"ok": False, "error": "still generating"}), 409

    reply = ""
    if meta.get("out") and Path(meta["out"]).exists():
        reply = Path(meta["out"]).read_text()
    if not reply.strip():
        reply = "\n".join(job.get("lines") or [])
    parsed, problem = _json_from(reply)
    if problem:
        return jsonify({"ok": False, "error": problem,
                        "reply": reply[-4000:]}), 200

    suite_name = (payload.get("module") or meta.get("module")
                  or _unique_suite(meta.get("story")))
    written = t.import_tests(parsed, suite_name, stage="draft")
    if not written.get("ok"):
        return jsonify({"ok": False, "errors": written.get("errors") or [],
                        "suite": suite_name, "reply": reply[-4000:]}), 200
    return jsonify({"ok": True, "suite": suite_name, "file": written.get("file"),
                    "counted": written.get("counted"), "via": meta.get("via"),
                    "reply": reply[-4000:]})


INDEX_SOURCE = {"env": "mock"}


def index_report_path(env):
    return LOG_DIR / ("mock-selfcheck.json" if env in (None, "", "mock")
                      else f"index-{re.sub(r'[^A-Za-z0-9_-]', '-', env)}.json")


def id_index(env=None):
    """What every endpoint actually returns, in the environment that matters.

    The mock's self-check is free and runs at startup, so it is the default.
    But the mock invents a shape wherever the document declares none — its
    dashboard answers {items: [...]} while the real one answers
    {held_orders: ...}. An index learned from the mock then sends a test
    to capture a field that only exists on the mock. Learn it from the server
    the tests will run against."""
    try:
        import bindings
        path = index_report_path(env or INDEX_SOURCE.get("env"))
        if not path.exists():
            path = LOG_DIR / "mock-selfcheck.json"
        return bindings.index_from_report(json.loads(path.read_text()))
    except Exception:
        return {}


@app.post("/api/bindings/index")
def bindings_index():
    """Sweep an environment read-only and learn what its endpoints return.

    Reads only — no --allow-writes — so this is safe to point at dev or even a
    read-only staging: it calls the GETs the document declares and records the
    shape of each reply."""
    payload = request.get_json(silent=True) or {}
    env = (payload.get("env") or "mock").strip()
    spec_path = project.active_spec(mock.options.get("spec"))
    out = index_report_path(env)

    cmd = [sys.executable, str(HERE / "verify.py"), "--spec", spec_path,
           "--quiet", "--fail-on", "never", "--report", str(out)]
    if env in ("mock", ""):
        if not mock.running:
            return jsonify({"ok": False, "error": "start the mock first"}), 200
        cmd += ["--target", "mock", "--base-url", mock.base_url()]
    else:
        label, base, auth_headers, problem = resolve_target(env)
        if problem:
            return jsonify({"ok": False, "error": problem}), 200
        cmd += ["--target", "live", "--base-url", base, "--env", env]
    job = start_job(cmd, timeout=1800)
    INDEX_SOURCE["pending"] = {"job": job, "env": env}
    return jsonify({"ok": True, "job": job, "env": env})


@app.post("/api/bindings/index/use")
def bindings_index_use():
    """Adopt a finished sweep as the source of field names."""
    payload = request.get_json(silent=True) or {}
    env = (payload.get("env") or "mock").strip()
    path = index_report_path(env)
    if not path.exists():
        return jsonify({"ok": False,
                        "error": f"no sweep of {env} yet — learn it first"}), 200
    INDEX_SOURCE["env"] = env
    index = id_index(env)
    return jsonify({"ok": True, "env": env, "fields": len(index)})


@app.get("/api/bindings")
def bindings_list():
    """Every id the document leaves unexplained, what we know, and what has
    been decided — the whole question in one place."""
    import bindings
    import tests as t
    spec = load_project_spec()
    index = id_index()
    recorded = bindings.load()

    wanted, seen = [], set()
    for route in (spec.routes if spec is not None else []):
        content = (route.get("request_body") or {}).get("content") or {}
        media = content.get("application/json") or (
            content[sorted(content)[0]] if content else {})
        for label, field in t._required_ids(t._denull((media or {}).get("schema") or {})):
            if field in seen:
                continue
            seen.add(field)
            wanted.append({"field": field, "where": label,
                           "operation": route["key"]})

    out = []
    for item in wanted:
        field = item["field"]
        entry = recorded.get(field)
        options = bindings.candidates(field, index, limit=3)
        settled = bool(entry) or (options and options[0]["strength"] in
                                  (bindings.CERTAIN, bindings.STRONG))
        out.append({**item, "recorded": entry, "candidates": options,
                    "settled": bool(settled)})
    out.sort(key=lambda r: (r["settled"], r["field"]))
    return jsonify({"ids": out, "indexed_fields": len(index),
                    "have_index": bool(index),
                    "learned_from": INDEX_SOURCE.get("env", "mock")})


@app.post("/api/tests/rebind")
def tests_rebind():
    """Replace placeholder ids in a saved suite with real captures.

    A test written before anything knew where an id came from carries
    REPLACE_WITH_REAL_... in its data and sends that string to the server. The
    source is known now, so capture it — no regeneration, and the test keeps
    working against an environment whose rows are different."""
    import tests as t
    payload = request.get_json(silent=True) or {}
    name = (payload.get("suite") or "").strip()
    stage = payload.get("stage") or "draft"
    index = id_index()
    if not index:
        return jsonify({"ok": False, "error": "no field index yet — start the mock "
                                              "so its self-check can run"}), 200
    suites = [su for su in t.load_suites(include_drafts=True)
              if su.get("name") == name and su.get("_stage") == stage]
    if not suites:
        return jsonify({"ok": False, "error": f"no {stage} suite called {name!r}"}), 200
    suite = suites[0]
    notes, touched = [], 0
    for test in (suite.get("scenarios") or []) + (suite.get("cases") or []):
        changed, said = t.rebind_placeholders(test, index)
        notes += [f"{test.get('id')}: {line}" for line in said]
        touched += 1 if changed else 0
    problems = [e for test in (suite.get("scenarios") or [])
                for e in t.validate_test(test, "scenario")]
    problems += [e for test in (suite.get("cases") or [])
                 for e in t.validate_test(test, "case")]
    if problems:
        return jsonify({"ok": False, "errors": problems, "notes": notes}), 200
    if payload.get("dry_run"):
        return jsonify({"ok": True, "changed": touched, "notes": notes,
                        "written": False})
    path = t.save_suite(suite, stage=stage)
    return jsonify({"ok": True, "changed": touched, "notes": notes,
                    "written": True, "file": str(path)})


@app.post("/api/bindings")
def bindings_save():
    """Record one decision, so nobody has to make it again."""
    import bindings
    payload = request.get_json(silent=True) or {}
    field = (payload.get("field") or "").strip()
    if not field:
        return jsonify({"ok": False, "error": "which id?"}), 400
    if payload.get("forget"):
        return jsonify({"ok": True, "ids": bindings.forget(field)})
    if payload.get("value_required"):
        entry = {"value_required": True,
                 "note": (payload.get("note") or "").strip()
                         or "no endpoint supplies this — give it a real value"}
    else:
        if not payload.get("from") or not payload.get("path"):
            return jsonify({"ok": False,
                            "error": "give the operation and the json path"}), 400
        entry = {"from": payload["from"], "path": payload["path"]}
    entry["decided_on"] = time.strftime("%Y-%m-%d", time.gmtime())
    return jsonify({"ok": True, "ids": bindings.save(field, entry)})


def load_project_spec():
    """The document this project is about, or None."""
    try:
        from mockd import Source, Spec
        spec_path = project.active_spec(mock.options.get("spec"))
        text, _ = Source(spec_path, poll=0, cache_dir=str(LOG_DIR)).read(force=True)
        return Spec(text=text, origin=spec_path) if text else None
    except Exception:
        return None


def build_story_brief(story, criteria=None):
    """The brief, built from the document. Shared by the paste-it-yourself path
    and the run-a-generator path, so both are given exactly the same facts."""
    import tests as t
    spec_path = project.active_spec(mock.options.get("spec"))
    spec = None
    try:
        from mockd import Source, Spec
        src = Source(spec_path, poll=0, cache_dir=str(LOG_DIR))
        text, _ = src.read(force=True)
        if text:
            spec = Spec(text=text, origin=spec_path)
    except Exception:
        spec = None
    covered = []
    for suite in t.load_suites(include_drafts=True):
        covered += [c.get("id") for c in (suite.get("cases") or [])]
        covered += [sc.get("id") for sc in (suite.get("scenarios") or [])]
    # Sample exactly the operations the brief will name — including the reads it
    # is about to recommend as id suppliers. Those claims are only worth making
    # if the response really carries the field, and the mock can say so.
    chosen = t.relevant_routes(spec, story) if spec is not None else []
    # Sample generously: a candidate trimmed before sampling comes back later as
    # an unconfirmed guess, which is the thing this is meant to stop.
    candidates, _ = t.prerequisite_routes(spec, chosen, limit=12) \
        if spec is not None else ([], [])
    samples = live_samples(spec, chosen + [route for route, _, _ in candidates],
                           limit=len(chosen) + len(candidates) + 2)
    return t.story_pack(spec, story, covered, samples=samples, criteria=criteria,
                        id_index=id_index())


# ----------------------------------------------------------------------------
# Create tests: one path from a sentence to tests that have been tried
# ----------------------------------------------------------------------------

PLAIN = {
    "pass": "Works on the mock.",
    "blocked": "Could not get as far as the thing being tested — an earlier step failed.",
    "error": "Could not be run as written.",
}


def _trial(suite_name, ids, env=None):
    """Run just these tests and say, in plain words, how each one did. A new
    test nobody has tried is a guess; this is what turns it into something a
    person can decide about.

    On the mock unless a server is named. The mock answers in the documented
    shape but knows none of the API's own rules — what a total comes to, what
    is refused the second time — so a test about those can only be settled on
    a real server, and the screen offers exactly that."""
    import tests as t
    on_mock = not env or env == "mock"
    if on_mock and not mock.running:
        return None, "the mock is not running, so the new tests have not been tried yet"
    out = LOG_DIR / f"trial-{uuid.uuid4().hex[:8]}.json"
    only = "^(" + "|".join(re.escape(i) for i in ids) + ")( |$)"
    target = ["--base-url", mock.base_url()] if on_mock else ["--env", env]
    cmd = [sys.executable, str(HERE / "tests.py"), "run", *target,
           "--drafts-only", "--suite", suite_name, "--only", only, "--json", str(out)]
    try:
        subprocess.run(cmd, cwd=str(HERE), capture_output=True, text=True, timeout=180)
        report = json.loads(out.read_text())
    except Exception as exc:
        return None, f"the trial run did not finish: {exc}"
    finally:
        try:
            out.unlink()
        except OSError:
            pass
    results = {}
    for suite in report.get("suites") or []:
        for item in suite.get("results") or []:
            results[item.get("id")] = item
    return results, None


def _explain(item, where_ran="mock"):
    """One test's outcome as a sentence a non-engineer can act on."""
    on_mock = where_ran == "mock"
    if item is None:
        return "not tried", "It was not run."
    outcome = item.get("outcome") or "error"
    if outcome == "pass":
        return outcome, PLAIN["pass"] if on_mock else f"Works on {where_ran}."
    first = None
    for step in item.get("steps") or []:
        for check in step.get("checks") or []:
            if not check.get("ok"):
                first = (step.get("name") or "", check.get("label") or "",
                         check.get("why") or check.get("detail") or "")
                break
        if first:
            break
    verdict = (item.get("verdict") or {}).get("headline") or PLAIN.get(outcome) or \
        "Does not pass yet."
    if on_mock and outcome == "fail":
        # Whose fault a failure is means something against a real server. The
        # mock not knowing a business rule is nobody's fault, and "the test
        # asserts something the spec never promised" was said of a 409 the
        # document does promise.
        verdict = "Does not pass on the mock"
    if item.get("error"):
        return outcome, f"{verdict} {item['error']}"
    if first:
        where = f' At "{first[0]}":' if first[0] else ""
        detail = f" {first[1]}" + (f" — {first[2]}" if first[2] else "")
        return outcome, f"{verdict}.{where}{detail}"[:420]
    return outcome, verdict


def _servers_to_try():
    """Real servers a new test could be tried on: set up, and safe to write to."""
    try:
        import environments as envmod
        out = []
        for name, env in sorted(envmod.load().items()):
            if name == "mock" or name.startswith("mock-") or env.get("readonly"):
                continue
            missing = []
            envmod.resolve({"base_url": env.get("base_url"), "auth": env.get("auth"),
                            "headers": env.get("headers")}, missing)
            if not missing:
                out.append(name)
        return out
    except Exception:
        return []


def _created_summary(suite_name, ids, notes=None, env=None):
    """Everything the Create screen shows after tests exist: fixed up, tried on
    the mock, and described in plain words."""
    import tests as t
    where_ran = env if env and env != "mock" else "mock"
    suite = next((su for su in t.load_suites(include_drafts=True)
                  if su.get("name") == suite_name and su.get("_stage") == "draft"), None)
    if suite is None:
        return {"ok": False, "error": f"the suite {suite_name!r} is not there"}
    wanted = [x for x in (suite.get("scenarios") or []) + (suite.get("cases") or [])
              if x.get("id") in set(ids)]
    results, problem = _trial(suite_name, [x.get("id") for x in wanted], env=env)
    rows, needs = [], {}
    for test in wanted:
        outcome, words = _explain((results or {}).get(test.get("id")), where_ran) \
            if results is not None else ("untried", problem)
        missing = [name for name, value in (test.get("data") or {}).items()
                   if isinstance(value, str) and t.PLACEHOLDER.match(value.strip())]
        for name in missing:
            needs.setdefault(name, []).append(test.get("id"))
        if missing and outcome != "pass":
            words = ("Needs a real value for " + ", ".join(missing)
                     + " before it can pass. " + words)
        rows.append({"id": test.get("id"), "name": test.get("name") or test.get("id"),
                     "kind": test.get("kind") or ("flow" if "steps" in test else "check"),
                     "steps": len(test.get("steps") or []) or 1,
                     **t.record_of(test), "levels": t.levels_of(test),
                     "outcome": outcome, "words": words, "needs": missing})
    passed = sum(1 for r in rows if r["outcome"] == "pass")
    return {"ok": True, "suite": suite_name, "tests": rows, "passed": passed,
            "total": len(rows), "needs": [{"name": n, "tests": ts} for n, ts in needs.items()],
            "notes": notes or [], "tried": results is not None, "untried_because": problem,
            "env": where_ran, "servers": _servers_to_try()}


@app.post("/api/create/try")
def create_try():
    """Try the tests just created again — on the mock, or on a named server."""
    payload = request.get_json(silent=True) or {}
    env = (payload.get("env") or "mock").strip()
    if env != "mock" and env not in _servers_to_try():
        return jsonify({"ok": False, "error": f"{env} is not set up, or is read only"}), 200
    return jsonify(_created_summary(payload.get("suite"), payload.get("ids") or [], env=env))


@app.post("/api/tests/value")
def tests_value():
    """Give a test the real value it was waiting for."""
    import tests as t
    payload = request.get_json(silent=True) or {}
    name, value = (payload.get("name") or "").strip(), str(payload.get("value") or "").strip()
    if not value:
        return jsonify({"ok": False, "error": "Type the value first."}), 200
    if t.PLACEHOLDER.match(value):
        return jsonify({"ok": False, "error": "That still looks like a placeholder — "
                                              "give the real value."}), 200
    stage = payload.get("stage") or "draft"
    suite = next((su for su in t.load_suites(include_drafts=True)
                  if su.get("name") == payload.get("suite")
                  and su.get("_stage", "shared") == stage), None)
    if suite is None:
        return jsonify({"ok": False, "error": "that module is not there"}), 200
    where = t.fill_value(suite, payload.get("id"), name, t_coerce(value))
    if where is None:
        return jsonify({"ok": False, "error": f"this test is not waiting for {name}"}), 200
    t.save_suite(suite, stage=stage)
    return jsonify({"ok": True, "message": f"Saved. Run the test to see how it does."})


def t_coerce(value):
    """A number typed into a box is a number."""
    return _coerce(value)


# ---------------------------------------------------------------------------
# The whole API on one screen
# ---------------------------------------------------------------------------
@app.get("/api/map")
def api_map():
    """Every endpoint, grouped by resource, with what is known about it on one
    server: proven, failing, never run there, never tested, open, slow, or
    answering differently from the document."""
    import apimap
    import environments as envmod
    import tests as t
    spec = load_project_spec()
    if spec is None:
        return jsonify({"ok": False, "error": "There is no API document yet."}), 200
    servers = []
    for name, env in sorted(envmod.load().items(), key=lambda kv: (kv[0] != "mock", kv[0])):
        if name.startswith("mock-"):
            continue
        missing = []
        envmod.resolve({"base_url": env.get("base_url"), "auth": env.get("auth"),
                        "headers": env.get("headers")}, missing)
        if not missing:
            servers.append(name)
    env = request.args.get("env") or "mock"
    if env not in servers:
        env = "mock"

    slow, differs, used = set(), set(), set()
    try:
        import perf
        found = perf.findings(spec).get(env) or {}
        slow = {x["operation"] for x in (found.get("slow") or []) + (found.get("slower") or [])}
    except Exception:
        pass
    try:
        seen = _recorded_findings()
        if seen and (seen.get("meta") or {}).get("server") == env:
            differs = ({x["operation"] for x in seen.get("fields") or []}
                       | {x["operation"] for x in seen.get("undocumented_statuses") or []})
            used = {x["operation"] for x in seen.get("used") or []}
    except Exception:
        pass
    built = apimap.build(spec, t.load_suites(include_drafts=True), env, LOG_DIR,
                         slow=slow, differs=differs, used=used)
    return jsonify({"ok": True, "servers": servers, "title": spec.title,
                    "mock_running": bool(mock.running), **built})


# ---------------------------------------------------------------------------
# How fast it answers
# ---------------------------------------------------------------------------
PERF = {"thread": None, "stop": None, "progress": None, "result": None, "server": None}


@app.get("/api/perf")
def perf_state():
    running = PERF["thread"] is not None and PERF["thread"].is_alive()
    return jsonify({"running": running, "server": PERF["server"],
                    "progress": PERF["progress"] if running else None,
                    "result": None if running else PERF["result"]})


@app.post("/api/perf/start")
def perf_start():
    """Repeat the read-only tests with several callers at once, for a while."""
    import environments as envmod
    import perf
    if PERF["thread"] is not None and PERF["thread"].is_alive():
        return jsonify({"ok": False, "error": "A load run is already going."}), 200
    payload = request.get_json(silent=True) or {}
    server = (payload.get("env") or "mock").strip()
    if server not in envmod.load():
        return jsonify({"ok": False, "error": f"There is no server named {server}."}), 200
    if server == "mock" and not mock.running:
        return jsonify({"ok": False, "error": "The mock is not running."}), 200
    try:
        users = max(1, min(int(payload.get("users") or 5), perf.MAX_USERS))
        seconds = max(1, min(int(payload.get("seconds") or 20), perf.MAX_SECONDS))
        budget = int(payload["p95"]) if payload.get("p95") not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Callers, seconds and the budget are whole numbers."}), 200
    only = set(payload["only"]) if isinstance(payload.get("only"), list) else None
    stop = threading.Event()
    PERF.update(stop=stop, result=None, server=server,
                progress={"calls": 0, "elapsed": 0, "seconds": seconds})

    def work():
        def progress(calls, elapsed):
            PERF["progress"] = {"calls": calls, "elapsed": round(elapsed, 1), "seconds": seconds}
        try:
            PERF["result"] = perf.load(server, users, seconds, only, False, budget,
                                       progress=progress, stop=stop)
        except SystemExit as exc:
            PERF["result"] = {"ok": False, "error": str(exc)}
        except Exception as exc:
            PERF["result"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    PERF["thread"] = threading.Thread(target=work, daemon=True)
    PERF["thread"].start()
    return jsonify({"ok": True, "server": server, "users": users, "seconds": seconds})


@app.post("/api/perf/stop")
def perf_stop():
    if PERF["stop"] is not None:
        PERF["stop"].set()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Learning from real traffic
# ---------------------------------------------------------------------------
RECORDED = LOG_DIR / "recorded.jsonl"
RECORDED_META = LOG_DIR / "recorded.meta.json"


class RecorderProcess:
    """recorder.py as a child process: passes an app's traffic through to a
    real server and writes down what happened."""

    def __init__(self):
        self.proc, self.server, self.port = None, None, None

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, server, port):
        if self.running:
            return False, "already watching"
        cmd = [sys.executable, str(HERE / "recorder.py"), "--env", server,
               "--port", str(port), "--out", str(RECORDED)]
        self.proc = subprocess.Popen(cmd, cwd=str(HERE), stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, text=True)
        for _ in range(40):
            if self.proc.poll() is not None:
                return False, (self.proc.stderr.read() or "it stopped at once").strip()[-300:]
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/_recorder/state", timeout=1):
                    self.server, self.port = server, port
                    return True, "watching"
            except Exception:
                time.sleep(0.25)
        self.stop()
        return False, "it did not come up"

    def stop(self):
        if self.proc is not None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        self.proc = None


recorder = RecorderProcess()


def _recording():
    import observe
    return observe.load(RECORDED)


def _recorded_findings():
    """The recording set against the project's document, or None if there is none."""
    import observe
    exchanges = _recording()
    if not exchanges:
        return None
    spec = load_project_spec()
    if spec is None:
        return None
    found = observe.analyse(exchanges, spec)
    try:
        found["meta"] = json.loads(RECORDED_META.read_text())
    except (OSError, ValueError):
        found["meta"] = {}
    return found


@app.get("/api/record")
def record_state():
    """Is traffic being watched, and what has it shown so far?"""
    import environments as envmod
    servers = []
    for name, env in sorted(envmod.load().items()):
        if name == "mock" or name.startswith("mock-"):
            continue
        missing = []
        envmod.resolve({"base_url": env.get("base_url"), "auth": env.get("auth"),
                        "headers": env.get("headers")}, missing)
        if not missing:
            servers.append({"name": name, "read_only": bool(env.get("readonly"))})
    found = _recorded_findings()
    return jsonify({"watching": recorder.running, "server": recorder.server if recorder.running else None,
                    "address": f"http://localhost:{recorder.port}" if recorder.running else None,
                    "servers": servers, "found": found,
                    "calls": (found or {}).get("calls", 0)})


@app.post("/api/record/start")
def record_start():
    import environments as envmod
    server = ((request.get_json(silent=True) or {}).get("server") or "").strip()
    if server not in envmod.load() or server == "mock" or server.startswith("mock-"):
        return jsonify({"ok": False, "error": "choose one of your servers"}), 200
    port = mock.port + 1
    ok, message = recorder.start(server, port)
    if not ok:
        return jsonify({"ok": False, "error": f"Could not start watching: {message}"}), 200
    LOG_DIR.mkdir(exist_ok=True)
    RECORDED_META.write_text(json.dumps({
        "server": server, "source": "watched",
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}))
    return jsonify({"ok": True, "address": f"http://localhost:{port}", "server": server})


@app.post("/api/record/stop")
def record_stop():
    recorder.stop()
    return jsonify({"ok": True})


@app.post("/api/record/clear")
def record_clear():
    for path in (RECORDED, RECORDED_META):
        try:
            path.unlink()
        except OSError:
            pass
    return jsonify({"ok": True})


@app.post("/api/record/har")
def record_har():
    """A recording exported from a browser, in place of watching live."""
    import observe
    payload = request.get_json(silent=True) or {}
    spec = load_project_spec()
    try:
        exchanges, host = observe.from_har(payload.get("content") or "", spec)
    except ValueError:
        return jsonify({"ok": False, "error": "That is not a browser recording. In the browser's "
                        "developer tools, Network tab, choose “Save all as HAR”."}), 200
    if not exchanges:
        return jsonify({"ok": False, "error": "Nothing in that recording is a call to this API. "
                        "Record while using the app that talks to it."}), 200
    LOG_DIR.mkdir(exist_ok=True)
    RECORDED.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in exchanges))
    RECORDED_META.write_text(json.dumps({
        "server": host, "source": "browser recording",
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}))
    return jsonify({"ok": True, "calls": len(exchanges), "host": host})


@app.post("/api/record/test")
def record_test():
    """What was recorded, saved as a test that can be run again."""
    import observe
    import tests as t
    spec = load_project_spec()
    try:
        meta = json.loads(RECORDED_META.read_text())
    except (OSError, ValueError):
        meta = {}
    module = _unique_suite("recorded")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    test, notes = observe.test_from(_recording(), spec, name=f"recorded-{stamp}",
                                    server=meta.get("server"))
    if test is None:
        return jsonify({"ok": False, "error": "There is nothing to make a test from: "
                                              + notes[0] + "."}), 200
    written = t.import_tests([test], module, stage="draft")
    if not written.get("ok"):
        return jsonify({"ok": False, "error": "The recording did not make a valid test.",
                        "errors": written.get("errors") or []}), 200
    return jsonify({"ok": True, "module": module, "id": test["id"], "steps": len(test["steps"]),
                    "notes": notes, "key": f"{module}|draft|{test['id']}"})


@app.get("/api/mcp/setup")
def mcp_setup():
    """What somebody pastes into their AI tool to connect it to this project.

    Each way is given twice: as shown, with the home folder written as ~ so a
    screen share does not carry the account's name, and as copied, with the
    full path — an AI tool's settings file does not expand ~."""
    python, server = sys.executable, str(HERE / "mcp_server.py")
    home = str(Path.home())

    def short(text):
        return text.replace(home, "~")

    def quoted(text):
        return f'"{text}"' if " " in text else text

    command = f"claude mcp add mockd -- {quoted(python)} {quoted(server)}"
    config = json.dumps({"mcpServers": {"mockd": {"command": python, "args": [server]}}},
                        indent=2)
    return jsonify({
        "available": (HERE / "mcp_server.py").exists(),
        "ways": [
            {"tool": "Claude Code", "how": "Run this once in a terminal.",
             "shown": short(command), "copy": command},
            {"tool": "Claude Desktop",
             "how": "Settings → Developer → Edit Config. Add this, save, and restart the app.",
             "shown": short(config), "copy": config},
            {"tool": "Cursor",
             "how": "Settings → MCP → Add new MCP server, and paste this.",
             "shown": short(config), "copy": config},
        ]})


@app.get("/api/create/context")
def create_context():
    """What the Create screen needs to greet somebody: what can write tests
    here, what already exists, and a few things they could ask for."""
    import tests as t
    examples = []
    try:
        import blueprint as bp
        spec = load_project_spec()
        for key, slot in sorted(bp.families(spec).items()):
            if not slot.get("create"):
                continue
            # "address" is already singular; chopping every trailing s made it
            # "addre". And it is "an integration", not "a integration".
            thing = bp._one(key[-1]).replace("-", " ")
            article = "an" if thing[:1].lower() in "aeiou" else "a"
            if slot.get("read") or slot.get("list"):
                examples.append(f"Create {article} {thing} and check it can be read back")
            if len(examples) >= 4:
                break
    except Exception:
        pass
    baseline = next((su for su in t.load_suites(include_drafts=True)
                     if su.get("name") == "baseline"), None)
    return jsonify({
        "generators": generator_available(),
        "mock_running": bool(mock.running),
        "baseline": (len((baseline or {}).get("cases") or [])
                     + len((baseline or {}).get("scenarios") or [])) if baseline else 0,
        "examples": examples[:4],
        "modules": sorted({su.get("name") for su in t.load_suites(include_drafts=True)
                           if su.get("name") != "baseline"})})


@app.post("/api/create/finish")
def create_finish():
    """Take what came back — from a generator job or pasted in — and make it
    ready: validated, saved, ids fixed up, tried once."""
    import tests as t
    payload = request.get_json(silent=True) or {}
    reply, via, story = payload.get("reply") or "", "paste", payload.get("story") or ""
    job_id = payload.get("job")
    if job_id:
        meta, job = GENERATED.get(job_id), JOBS.get(job_id)
        if not meta or not job:
            return jsonify({"ok": False, "error": "that generation is not here any more"}), 200
        if not job.get("done"):
            return jsonify({"ok": False, "error": "still writing"}), 200
        via, story = meta.get("via"), meta.get("story") or story
        if meta.get("out") and Path(meta["out"]).exists():
            reply = Path(meta["out"]).read_text()
        if not reply.strip():
            reply = "\n".join(job.get("lines") or [])
    parsed, problem = _json_from(reply)
    if problem:
        return jsonify({"ok": False, "error":
                        "That does not contain the tests as JSON. Copy the whole reply, "
                        "from the first [ to the last ].", "detail": problem}), 200

    module = (payload.get("module") or "").strip()
    if module and not re.fullmatch(r"[A-Za-z0-9._-]+", module):
        return jsonify({"ok": False, "error":
                        "A module name can only use letters, numbers, dots, dashes "
                        "and underscores."}), 200
    suite_name = module or _unique_suite(story)
    written = t.import_tests(parsed, suite_name, stage="draft")
    if not written.get("ok"):
        return jsonify({"ok": False, "error": "The tests were not saved, because some "
                        "of them are not valid.", "errors": written.get("errors") or []}), 200

    items = parsed if isinstance(parsed, list) else (
        (parsed.get("cases") or []) + (parsed.get("scenarios") or [])
        if isinstance(parsed, dict) and ("cases" in parsed or "scenarios" in parsed)
        else [parsed])
    ids = [x.get("id") for x in items if isinstance(x, dict) and x.get("id")]

    # fix up ids the way a person would have to by hand
    notes = []
    index = id_index()
    suite = next((su for su in t.load_suites(include_drafts=True)
                  if su.get("name") == suite_name and su.get("_stage") == "draft"), None)
    if suite is not None and index:
        touched = False
        for test in (suite.get("scenarios") or []) + (suite.get("cases") or []):
            if test.get("id") not in ids:
                continue
            changed, said = t.rebind_placeholders(test, index)
            touched = touched or changed
            notes += said
        still_valid = not [e for test in (suite.get("scenarios") or [])
                           for e in t.validate_test(
                               {**test, "data": {**(suite.get("data") or {}),
                                                 **(test.get("data") or {})}}, "scenario")]
        if touched and still_valid:
            t.save_suite(suite, stage="draft")
    summary = _created_summary(suite_name, ids, notes)
    summary["via"] = via
    return jsonify(summary)


@app.post("/api/create/value")
def create_value():
    """Supply a real value for something the tests could not find for
    themselves, then try them again."""
    import tests as t
    payload = request.get_json(silent=True) or {}
    suite_name, name = payload.get("suite"), (payload.get("name") or "").strip()
    value = (payload.get("value") or "").strip()
    ids = payload.get("ids") or []
    if not value:
        return jsonify({"ok": False, "error": "Type the value first."}), 200
    suite = next((su for su in t.load_suites(include_drafts=True)
                  if su.get("name") == suite_name and su.get("_stage") == "draft"), None)
    if suite is None:
        return jsonify({"ok": False, "error": "that suite is not there"}), 200
    for test in (suite.get("scenarios") or []) + (suite.get("cases") or []):
        if test.get("id") in ids and name in (test.get("data") or {}):
            test["data"][name] = value
    t.save_suite(suite, stage="draft")
    return jsonify(_created_summary(suite_name, ids))


@app.post("/api/create/discard")
def create_discard():
    """Throw away tests that were just made and are not wanted."""
    import tests as t
    payload = request.get_json(silent=True) or {}
    suite_name, ids = payload.get("suite"), set(payload.get("ids") or [])
    suite = next((su for su in t.load_suites(include_drafts=True)
                  if su.get("name") == suite_name and su.get("_stage") == "draft"), None)
    if suite is None:
        return jsonify({"ok": True, "removed": 0})
    before = len(suite.get("cases") or []) + len(suite.get("scenarios") or [])
    suite["cases"] = [c for c in (suite.get("cases") or []) if c.get("id") not in ids]
    suite["scenarios"] = [c for c in (suite.get("scenarios") or []) if c.get("id") not in ids]
    left = len(suite["cases"]) + len(suite["scenarios"])
    if left:
        t.save_suite(suite, stage="draft")
    elif suite.get("_path"):
        try:
            Path(suite["_path"]).unlink()
        except OSError:
            pass
    return jsonify({"ok": True, "removed": before - left})


@app.post("/api/tests/story")
def tests_story():
    """A brief for turning a user story into tests, to paste into an assistant.

    Calling a model is optional and lives in /api/tests/generate; this one still
    just hands you the prompt, because whatever a team already uses is the one
    they are allowed to paste into."""
    payload = request.get_json(silent=True) or {}
    story = (payload.get("story") or "").strip()
    if len(story) < 12:
        return jsonify({"ok": False, "error": "give a sentence or two of story"}), 400
    brief = build_story_brief(story)
    matched = brief.count("\n") and "NO OPERATION MATCHED" not in brief
    return jsonify({"ok": True, "brief": brief, "matched": bool(matched),
                    "spec": project.active_spec(mock.options.get("spec"))})


@app.post("/api/tests/more-like")
def tests_more_like():
    """A brief asking for more tests in the shape of the ones already written."""
    import tests as t
    payload = request.get_json(silent=True) or {}
    module = (payload.get("module") or "").strip() or None
    spec_path = project.active_spec(mock.options.get("spec"))
    spec = None
    try:
        from mockd import Source, Spec
        src = Source(spec_path, poll=0, cache_dir=str(LOG_DIR))
        text, _ = src.read(force=True)
        if text:
            spec = Spec(text=text, origin=spec_path)
    except Exception:
        spec = None
    suites = t.load_suites(include_drafts=True)
    brief = t.more_like_pack(spec, suites, module,
                             samples=live_samples(spec,
                                                  spec.routes if spec is not None else []))
    return jsonify({"ok": True, "brief": brief, "spec": spec_path,
                    "module": module or "every module"})


@app.post("/api/tests/pipeline")
def tests_pipeline():
    """Turn a selection into something CI will run.

    Every selector the console offers is already a flag on `tests.py run`, so
    this is a formatting problem rather than a second way to run tests — which
    matters, because a pipeline that runs tests differently from the way you
    ran them locally is a pipeline whose failures you cannot reproduce."""
    import tests as t
    payload = request.get_json(silent=True) or {}
    env = (payload.get("env") or "mock").strip()
    levels = [x for x in (payload.get("levels") or []) if x in t.LEVELS]
    priorities = [str(x).upper() for x in (payload.get("priorities") or [])
                  if str(x).upper() in t.PRIORITIES]
    modules = [str(x) for x in (payload.get("modules") or []) if str(x).strip()]
    kinds = [x for x in (payload.get("kinds") or []) if x in ("case", "api", "e2e")]
    only = (payload.get("only") or "").strip()
    fmt = payload.get("format") or "github"

    flags = [f"--env {shlex.quote(env)}"]
    for level in levels:
        flags.append(f"--level {level}")
    for priority in priorities:
        flags.append(f"--priority {priority}")
    for module in modules:
        flags.append(f"--module {shlex.quote(module)}")
    for kind in kinds:
        flags.append(f"--kind {kind}")
    if only:
        flags.append(f"--only {shlex.quote(only)}")
    if payload.get("drafts"):
        flags.append("--drafts")
    # three outputs, because three different readers: a CI server renders the
    # JUnit, a person opens the HTML, a script parses the JSON
    flags.append("--junit results.xml")
    flags.append("--html report.html")
    command = "python tests.py run " + " ".join(flags)

    what = (", ".join(levels) or "every level") + " on " + (", ".join(modules) or "every module")
    if priorities:
        what += ", " + "/".join(priorities) + " only"
    if fmt == "command":
        return jsonify({"ok": True, "command": command, "describes": what})

    if fmt == "gitlab":
        text = f"""# {what}, against {env}.
api-tests:
  image: python:3.12-slim
  before_script:
    - pip install --quiet -r requirements.txt
  script:
    - {command}
  artifacts:
    when: always
    paths:
      - report.html          # open this one
    reports:
      junit: results.xml     # GitLab renders this in the pipeline view
"""
    else:
        text = f"""# {what}, against {env}.
# Values the environment needs come from repository secrets; nothing is baked in.
name: API tests

on:
  pull_request:
  push:
    branches: [main]
  workflow_dispatch:

jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -r requirements.txt
      - name: {what}
        env:
          {env.upper().replace('-', '_')}_BASE_URL: ${{{{ vars.{env.upper().replace('-', '_')}_BASE_URL }}}}
        run: {command}
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: test-results
          path: |
            report.html
            results.xml
          if-no-files-found: ignore
      # the summary a reviewer sees without downloading anything
      - name: Summary
        if: always()
        run: |
          echo "### API tests — {what} against {env}" >> "$GITHUB_STEP_SUMMARY"
          echo "Full report in the *test-results* artifact." >> "$GITHUB_STEP_SUMMARY"
"""

    # how much this selection would actually run, so the answer is not a surprise
    suites = t.load_suites(include_drafts=bool(payload.get("drafts")))
    runner = t.Runner.__new__(t.Runner)
    count = 0
    for suite in suites:
        for item in (suite.get("cases") or []):
            if kinds and "case" not in kinds:
                continue
            if runner.selects(item, suite, only or None, None, levels or None,
                              modules or None, priorities or None):
                count += 1
        for item in (suite.get("scenarios") or []):
            if kinds and item.get("kind", "api") not in kinds:
                continue
            if runner.selects(item, suite, only or None, None, levels or None,
                              modules or None, priorities or None):
                count += 1

    return jsonify({"ok": True, "yaml": text, "command": command,
                    "selects": count, "describes": what,
                    "filename": ".github/workflows/api-tests.yml" if fmt == "github"
                                else ".gitlab-ci.yml"})


@app.get("/api/tests/cleanup-for")
def cleanup_for():
    """The route that undoes what a step just created.

    A flow that creates a row on a shared server and walks away leaves that row
    there for everyone, forever. The spec already knows which endpoint deletes
    the thing, so the workbench can offer the cleanup step rather than relying
    on somebody remembering to write one."""
    created = request.args.get("path") or ""
    variable = request.args.get("var") or "id"
    spec_path = project.active_spec(mock.options.get("spec"))
    try:
        from mockd import Source, Spec
        src = Source(spec_path, poll=0, cache_dir=str(LOG_DIR))
        text, _ = src.read(force=True)
        spec = Spec(text=text, origin=spec_path)
    except Exception as exc:
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 400

    # An API may say /departments/{id} or /department/delete/{id}; the mock
    # already has to treat those as the same resource to keep a stateful store
    # coherent, so reuse that rather than guess again here.
    from mockd import _VERB_SUFFIX
    def resource(path):
        return _VERB_SUFFIX.sub("", path.split("{", 1)[0].rstrip("/")).rstrip("/")

    stem = resource(created)
    candidates = []
    for route in spec.routes:
        if route["method"] != "DELETE" or len(route["path_params"]) != 1:
            continue
        prefix = resource(route["path"])
        if prefix == stem or prefix.startswith(stem + "/"):
            candidates.append(route)
    if not candidates:
        return jsonify({"found": False,
                        "why": f"the document declares no DELETE under {stem}"})

    route = min(candidates, key=lambda r: len(r["path"]))
    name = route["path_params"][0] if isinstance(route["path_params"], list) \
        else list(route["path_params"])[0]
    return jsonify({"found": True, "step": {
        "role": "cleanup",
        "name": f"remove what this flow created",
        "request": {"method": "DELETE",
                    "path": route["path"].replace("{%s}" % name, "{{%s}}" % variable)},
        "assertions": [{"type": "status", "in": [200, 202, 204, 404]}],
    }, "operation": route["key"]})


RUN_REPORTS = {}


@app.get("/api/tests/report.<kind>")
def download_report(kind):
    """One run, in the form you need.

    html to read or send to somebody, xml for a CI server that already knows
    how to render JUnit, json for anything else. `job` picks a specific run;
    without it you get the most recent, which is only the same thing when
    nothing else has run in between."""
    mimetypes = {"html": "text/html", "xml": "application/xml",
                 "json": "application/json"}
    if kind not in mimetypes:
        return jsonify({"error": f"no such report {kind}"}), 404
    mimetype = mimetypes[kind]

    wanted = request.args.get("job")
    if wanted:
        path = LOG_DIR / f"run-{RUN_REPORTS.get(wanted, wanted)}.{kind}"
        if not path.exists():
            return jsonify({"error": "that run left no report — it may still be "
                                     "running, or it failed before finishing"}), 404
    else:
        candidates = sorted(LOG_DIR.glob(f"run-*.{kind}"),
                            key=lambda f: f.stat().st_mtime, reverse=True)
        legacy = LOG_DIR / f"last_test_run.{kind}"
        if legacy.exists():
            candidates.append(legacy)
        path = candidates[0] if candidates else None
    if path is None or not path.exists():
        return jsonify({"error": "nothing has been run yet"}), 404
    stamp = time.strftime("%Y%m%d-%H%M", time.localtime(path.stat().st_mtime))
    return Response(path.read_text(), mimetype=mimetype, headers={
        "Content-Disposition": f'attachment; filename="test-report-{stamp}.{kind}"'})


@app.get("/api/tests/values")
def dynamic_values():
    """The values that resolve themselves at run time.

    These are how a test stops colliding with its own previous run, and they
    were discoverable only by reading the source or a placeholder — so nobody
    used them, and everybody wrote constants."""
    import tests as t
    described = {
        "$uuid": "a fresh id every time it is read — for a name that must be unique",
        "$runId": "the same for the whole run, different every run — put it in what you "
                  "create so a run's leftovers can be found",
        "$timestamp": "seconds since the epoch",
        "$isoDate": "today, as 2026-09-23",
        "$isoDateTime": "now, as an ISO timestamp",
        "$randomInt": "a number between 1 and 100000",
        "$randomEmail": "an address nobody owns, on example.com",
    }
    out = []
    for name in t._BUILTINS:
        out.append({"name": name, "about": described.get(name, ""),
                    "example": str(t._BUILTINS[name]())[:48]})
    return jsonify({"values": out})


@app.post("/api/tests/compare-envs")
def compare_envs():
    """Run the same flow against two servers and say what would not survive.

    A flow built against the mock binds what the mock invented. Finding out on
    the day of a release that the real server calls it something else is the
    expensive way; running it against both while you are still writing it is
    the cheap one."""
    import tests as t
    payload = request.get_json(silent=True) or {}
    steps = payload.get("steps")
    if not isinstance(steps, list) or not steps:
        return jsonify({"ok": False, "error": "no steps to run"}), 400
    here_name = payload.get("here") or "mock"
    there_name = payload.get("there")
    if not there_name or there_name == here_name:
        return jsonify({"ok": False, "error": "choose a second, different environment"}), 400

    outcomes = {}
    for label in (here_name, there_name):
        if _readonly_target(label) and any(
                str((st.get("request") or st).get("method", "GET")).upper()
                in t.WRITE_METHODS for st in steps):
            return jsonify({"ok": False, "readonly": True,
                            "error": f"{label} is read-only and this flow writes, so it "
                                     f"cannot be compared there"}), 200
        name, base, headers, problem = resolve_target(label)
        if problem:
            return jsonify({"ok": False, "error": f"{label}: {problem}"}), 200
        runner = t.Runner(base, headers, spec=None, timeout=30,
                          readonly=_readonly_target(label), env_name=label)
        scenario = {"id": "compare", "kind": "e2e", "name": "comparison",
                    "steps": steps, "data": payload.get("data") or {}}
        try:
            outcomes[label] = runner.run_scenario(scenario, payload.get("data") or {})
        except SystemExit as exc:
            return jsonify({"ok": False, "error": f"{label}: {exc}"}), 200

    per_step = []
    for index in range(len(steps)):
        a = (outcomes[here_name]["steps"] or [])[index:index + 1]
        b = (outcomes[there_name]["steps"] or [])[index:index + 1]
        if not a or not b:
            continue
        diff = t.compare_shapes(a[0].get("response_json"), b[0].get("response_json"),
                                "a", "b")
        per_step.append({
            "step": index + 1, "name": a[0].get("name"),
            "status": {here_name: a[0].get("status"), there_name: b[0].get("status")},
            "outcome": {here_name: a[0].get("outcome"), there_name: b[0].get("outcome")},
            "same_shape": diff["same"],
            "only_here": diff["only_on_a"], "only_there": diff["only_on_b"],
            "different_type": [{"path": d["path"], here_name: d["a"], there_name: d["b"]}
                               for d in diff["different_type"]],
        })

    last = outcomes[there_name]["steps"] or []
    shape_there = {}
    for step in last:
        shape_there.update(t.shape_of(step.get("response_json")))
    at_risk = t.bindings_at_risk(steps, shape_there)

    return jsonify({"ok": True, "here": here_name, "there": there_name,
                    "steps": per_step, "at_risk": at_risk,
                    "verdict": ("nothing this flow depends on is missing"
                                if not at_risk else
                                f"{len(at_risk)} thing(s) this flow depends on are not "
                                f"in {there_name}'s responses")})


@app.post("/api/tests/chain")
def tests_chain():
    """Run a flow up to a point and hand back every step's real response.

    This is what makes the workbench a workbench rather than a form: you cannot
    bind a value you have never seen. Steps after `upto` are not run, so a
    half-built flow can be exercised without its unfinished tail failing."""
    import tests as t
    payload = request.get_json(silent=True) or {}
    steps = payload.get("steps")
    if not isinstance(steps, list) or not steps:
        return jsonify({"error": "no steps to run"}), 400
    upto = payload.get("upto")
    upto = len(steps) if upto is None else max(0, min(int(upto) + 1, len(steps)))

    # Refuse before resolving anything. Whether a read-only server's credentials
    # happen to be set is beside the point, and reporting the missing variable
    # first would hide the reason that actually matters.
    target_name = payload.get("target")
    if _readonly_target(target_name):
        writes = [st for st in steps[:upto]
                  if str((st.get("request") or st).get("method", "GET")).upper()
                  in t.WRITE_METHODS]
        if writes:
            names = ", ".join(sorted({str((w.get("request") or w).get("method")).upper()
                                      for w in writes}))
            return jsonify({"ok": False, "readonly": True, "target": target_name,
                            "error": f"{target_name} is read-only, so this flow cannot run "
                                     f"there: it uses {names}. Point it at a server where "
                                     f"writing is safe, or run only the read steps."}), 200

    label, base, auth_headers, problem = resolve_target(target_name)
    if problem:
        return jsonify({"error": problem}), 409

    scenario = {"id": payload.get("id") or "workbench", "kind": payload.get("kind", "api"),
                "name": payload.get("name") or "workbench flow",
                "steps": steps[:upto], "data": payload.get("data") or {}}
    problems = t.validate_test(scenario, kind="scenario")
    if problems:
        return jsonify({"ok": False, "errors": problems}), 200

    readonly = _readonly_target(label)
    # Give the runner the project's document, so a `schema` assertion in the
    # workbench checks the body instead of quietly having nothing to check.
    chain_spec = None
    try:
        from mockd import Source, Spec
        spec_text, _ = Source(project.active_spec(mock.options.get("spec")),
                              poll=0, cache_dir=str(LOG_DIR)).read(force=True)
        if spec_text:
            chain_spec = Spec(text=spec_text)
    except Exception:
        chain_spec = None
    runner = t.Runner(base, auth_headers, spec=chain_spec, timeout=30,
                      readonly=readonly, env_name=label)
    try:
        outcome = runner.run_scenario(scenario, payload.get("data") or {})
    except SystemExit as exc:
        return jsonify({"ok": False, "error": str(exc)}), 200
    except Exception as exc:
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 200
    # A workbench run counts as a run of the saved test only when what ran IS
    # the saved test: same flow, every step, nothing edited since it was
    # loaded. Recording an experiment as a pass would put a green badge on a
    # test nobody has actually run.
    recorded = None
    record = payload.get("record") or {}
    whole = upto >= len(steps)
    if record.get("suite") and record.get("id") and whole:
        try:
            t.record_history([(record["suite"], [{**outcome, "id": record["id"]}])],
                             base, label, None)
            recorded = f"{record['suite']}/{record['id']}"
        except Exception:
            recorded = None

    return jsonify({"ok": True, "target": label, "base_url": base,
                    "ran": upto, "of": len(steps), "result": outcome,
                    "recorded": recorded})


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
    runner = t.Runner(base, auth_headers or {}, spec, timeout=30,
                      readonly=_readonly_target(label), env_name=label)
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


@app.post("/api/tests/save-flow")
def save_flow():
    """Write a whole scenario at once.

    /api/tests/save appends one step at a time, which is right when a flow is
    grown from the explorer. The workbench holds the entire flow, so writing it
    step by step would leave a half-saved scenario on disk if anything failed."""
    import tests as t
    payload = request.get_json(silent=True) or {}
    suite_name = (payload.get("suite") or "").strip()
    if not suite_name or not re.fullmatch(r"[A-Za-z0-9._-]+", suite_name):
        return jsonify({"ok": False, "error": "module must be a simple name"}), 400
    flow = payload.get("flow")
    if not isinstance(flow, dict):
        return jsonify({"ok": False, "error": "no flow to save"}), 400

    problems = t.validate_test(flow, kind="scenario")
    if problems:
        return jsonify({"ok": False, "errors": problems}), 200

    stage = payload.get("stage") or "draft"
    suites = {s["name"]: s for s in t.load_suites(include_drafts=True)
              if s.get("_stage") == stage}
    suite = suites.get(suite_name) or {"name": suite_name, "module": suite_name,
                                       "data": {}, "cases": [], "scenarios": [],
                                       "_stage": stage}
    suite.setdefault("scenarios", [])
    suite.setdefault("cases", [])
    suite.setdefault("module", suite_name)
    # replacing by id, so saving twice edits rather than duplicates
    suite["scenarios"] = [sc for sc in suite["scenarios"] if sc.get("id") != flow.get("id")]
    suite["scenarios"].append(flow)

    path = t.save_suite(suite, stage=stage)
    return jsonify({"ok": True, "file": str(path), "suite": suite_name, "stage": stage,
                    "steps": len(flow.get("steps") or []),
                    "scenarios": len(suite["scenarios"])})


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


@app.get("/api/tests/export")
def export_tests():
    """Everything, or one suite, as JSON you can hand to anyone.

    The same shape `/api/tests/import` accepts, so a suite exported here goes
    back in — to another checkout, to a colleague, or to an assistant asked to
    write more like these. A format that only travels one way is a format that
    strands people."""
    import tests as t
    wanted = request.args.get("suite")
    stage = request.args.get("stage")
    include_drafts = request.args.get("drafts") != "0"
    suites = t.load_suites(include_drafts=include_drafts)
    out = []
    for suite in suites:
        if wanted and suite.get("name") != wanted:
            continue
        if stage and suite.get("_stage") != stage:
            continue
        out.append({k: v for k, v in suite.items() if not k.startswith("_")})

    doc = {
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "spec": project.active_spec(),
        "suites": out,
        "_how": "Import with the Tests view's Import button, or "
                "`python tests.py import --file <this file> --suite <name>`. "
                "Tests land as drafts and are checked on the way in.",
    }
    if request.args.get("download"):
        name = f"tests-{wanted or 'all'}-{time.strftime('%Y%m%d')}.json"
        return Response(json.dumps(doc, indent=2),
                        mimetype="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})
    return jsonify(doc)


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
    for level in (payload.get("levels") or []):
        cmd += ["--level", level]
    for priority in (payload.get("priorities") or []):
        if str(priority).upper() in ("P0", "P1", "P2", "P3"):
            cmd += ["--priority", str(priority).upper()]
    for module in (payload.get("modules") or []):
        cmd += ["--module", module]
    if payload.get("verbose"):
        cmd += ["--verbose"]
    # One file per run, named after the job. A single shared "last run" meant
    # two runs — two tabs, two people, a test suite in the background — silently
    # overwrote each other, and you downloaded somebody else's results believing
    # they were yours.
    job = uuid.uuid4().hex[:12]
    for flag, ext in (("--json", "json"), ("--html", "html"), ("--junit", "xml")):
        cmd += [flag, str(LOG_DIR / f"run-{job}.{ext}")]
    started = start_job(cmd, timeout=900)
    RUN_REPORTS[started] = job
    return jsonify({"job": started, "report": job})


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
            # for the plain list: what a person needs to know, in their terms
            "readonly": bool(env.get("readonly")),
            "builtin": name == "mock",
            "technical": name.startswith("mock-"),
            "signs_in_with": ("a username and password"
                              if (env.get("auth") or {}).get("mode") == "login"
                              else "a token" if (env.get("auth") or {}).get("mode") == "token"
                              else "a cookie" if "Cookie" in (env.get("headers") or {})
                              else "nothing"),
            "needs": [_need_words(var) for var in sorted(set(missing))],
        })
    return jsonify({"environments": out})


def _need_words(var):
    """DEV_PASSWORD, said the way a person would say it."""
    low = var.lower()
    for ending, words in (("base_url", "its address"), ("username", "a username"),
                          ("password", "a password"), ("cookie", "a cookie"),
                          ("token", "a token")):
        if low.endswith(ending):
            return words
    stem = re.sub(r"^[a-z0-9]+_", "", low)            # drop the server's own prefix
    return "a " + stem.replace("_", " ")


@app.get("/api/environments/<name>/data")
def environment_data(name):
    """The test data that belongs to one environment.

    A suite's `data` says what a value MEANS; an environment's says what it IS
    on that server — the id of a row that exists on staging is not the id of
    one on dev, and 250 countries on production is 2 on the mock. Without an
    editor this was a file-editing job, which meant per-environment
    expectations existed in theory and nowhere else."""
    import environments as envmod
    envs = envmod.load()
    if name not in envs:
        return jsonify({"error": f"no environment named {name}"}), 404
    raw = (envs[name].get("data") or {})
    rows = []
    for key, value in raw.items():
        literal = not (isinstance(value, str) and re.fullmatch(r"\$\{\w+\}", value.strip()))
        rows.append({"name": key, "value": value, "literal": literal,
                     "resolved": envmod.resolve(value, []) if not literal else value})
    return jsonify({"environment": name, "data": sorted(rows, key=lambda r: r["name"]),
                    "writes_to": "environments.json (committed)",
                    "note": "Values here are committed. Anything sensitive — a real row "
                            "id, a customer reference — should be written as ${VAR} and "
                            "set in .env instead."})


def _env_file_for(name):
    """The file a server is defined in — the shared one, or this machine's own.

    A server kept in the private file is as real as one in the shared file;
    editing its test data or removing it used to answer "no environment named
    …" because only the shared file was looked at."""
    import environments as envmod
    for path in (envmod.DEFAULT_FILE, envmod.LOCAL_FILE):
        try:
            doc = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(doc.get("environments"), dict) and name in doc["environments"]:
            return path, doc
    return None, None


@app.post("/api/environments/<name>/data")
def set_environment_data(name):
    """Replace one environment's data block."""
    import environments as envmod
    payload = request.get_json(silent=True) or {}
    rows = payload.get("data")
    if not isinstance(rows, list):
        return jsonify({"ok": False, "error": "data must be a list of {name, value}"}), 400

    target, doc = _env_file_for(name)
    if target is None:
        return jsonify({"ok": False, "error": f"no environment named {name}"}), 404

    secrets = []
    data = {}
    for row in rows:
        key = str(row.get("name") or "").strip()
        if not key or not re.fullmatch(r"\w+", key):
            continue
        value = row.get("value")
        if row.get("secret"):
            # keep it out of the committed file: the name goes here, the value
            # goes to .env, which is gitignored
            var = f"{re.sub(r'[^A-Z0-9]+', '_', name.upper())}_{key.upper()}"
            data[key] = f"${{{var}}}"
            if value not in (None, ""):
                secrets.append((var, str(value)))
        else:
            data[key] = _coerce(value)

    doc["environments"][name]["data"] = data
    target.write_text(json.dumps(doc, indent=2) + "\n")
    if secrets:
        envmod.write_dotenv(dict(secrets))
    return jsonify({"ok": True, "environment": name, "count": len(data),
                    "kept_out_of_git": [v for v, _ in secrets],
                    "message": f"{len(data)} value(s) saved for {name}"
                               + (f"; {len(secrets)} written to .env instead of the "
                                  f"committed file" if secrets else "")})


def _coerce(value):
    """"250" typed into a form is the number 250 — an assertion comparing it
    with a real count should not fail on the quotes."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d*\.\d+", text):
        return float(text)
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    return value


@app.post("/api/environments/create")
def create_environment():
    """Add a named target without hand-editing a file.

    What gets written is the SHAPE of the environment — its base URL and how it
    authenticates, expressed as ${VAR} placeholders. The values go to .env
    through the existing vars flow, so a URL or a password typed here still
    cannot reach a commit."""
    import environments as envmod
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,40}", name or ""):
        return jsonify({"ok": False, "error": "a name may use lower-case letters, "
                                              "digits, dot, dash and underscore"}), 400

    existing = envmod.load()
    if name in existing and not payload.get("replace"):
        return jsonify({"ok": False, "error": f"{name} already exists"}), 409

    prefix = re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_") or "ENV"
    mode = payload.get("mode") or "none"
    if mode not in ("none", "token", "login", "cookie"):
        return jsonify({"ok": False, "error": f"unknown auth mode {mode}"}), 400

    env = {"description": (payload.get("description") or "").strip()
                          or f"{name}, added from the console",
           "base_url": f"${{{prefix}_BASE_URL}}"}
    if payload.get("readonly"):
        env["readonly"] = True
    if mode == "token":
        env["auth"] = {"mode": "token", "token": f"${{{prefix}_TOKEN}}"}
    elif mode == "cookie":
        env["auth"] = {"mode": "none"}
        env["headers"] = {"Cookie": f"${{{prefix}_COOKIE}}"}
    elif mode == "login":
        env["auth"] = {"mode": "login", "login": {
            "method": "POST", "path": payload.get("login_path") or "/api/v1/auth/login",
            "send": payload.get("login_send") or "json",
            "username_field": "username", "password_field": "password",
            "username": f"${{{prefix}_USERNAME}}", "password": f"${{{prefix}_PASSWORD}}",
            "use_cookies": True}}
    else:
        env["auth"] = {"mode": "none"}
    env["data"] = {}

    doc = json.loads(envmod.DEFAULT_FILE.read_text()) if envmod.DEFAULT_FILE.exists() \
        else {"environments": {}}
    doc.setdefault("environments", {})[name] = env
    envmod.DEFAULT_FILE.write_text(json.dumps(doc, indent=2) + "\n")

    missing = []
    envmod.resolve(env, missing)
    return jsonify({"ok": True, "name": name, "environment": env,
                    "needs": sorted(set(missing)),
                    "message": f"{name} added to environments.json — it holds only "
                               f"${{VAR}} placeholders, so it is safe to commit. "
                               f"Set its values with Configure."})


@app.post("/api/environments/delete")
def delete_environment():
    """Remove an environment this console added."""
    import environments as envmod
    name = (request.get_json(silent=True) or {}).get("name")
    removed = False
    while True:                       # it may be defined in both files
        target, doc = _env_file_for(name)
        if target is None:
            break
        doc["environments"].pop(name)
        target.write_text(json.dumps(doc, indent=2) + "\n")
        removed = True
    if not removed:
        return jsonify({"ok": False, "error": f"no environment named {name}"}), 404
    return jsonify({"ok": True, "message": f"{name} removed"})


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


def probe_credential(headers, base_url):
    """One real read, so "the credential works" is a fact and not an assumption.

    Returns None when there is nothing to probe against."""
    if not base_url:
        return None
    try:
        import verify as verifymod
        from mockd import Source, Spec
        spec_path = project.active_spec()
        src = Source(spec_path, poll=0, cache_dir=str(LOG_DIR))
        text, _ = src.read(force=True)
        if text is None:
            return None
        spec = Spec(text=text, origin=spec_path)
        blocked = verifymod.auth_probe(spec, base_url, headers, 20)
    except Exception:
        return None
    if blocked is None:
        route = "a read"
        return {"ok": True, "operation": route, "status": 200}
    detail = verifymod.credential_not_seen(headers, blocked["body"]) or ""
    age = verifymod.credential_age(headers) or ""
    tail = (" " + detail if detail else "") + (" " + age if age else "")
    return {"ok": False, "operation": blocked["operation"], "status": blocked["status"],
            "error": (f"The credential was rejected: {blocked['status']} on "
                      f"{blocked['operation']} — {blocked['body'][:120]}.{tail}")}


@app.post("/api/env-login")
def env_login():
    """Prove an environment's credentials before running anything against it."""
    payload = request.get_json(silent=True) or {}
    name = payload.get("name")
    try:
        import environments as envmod
        env = envmod.get(name)
        # Every missing value at once, not the first one the resolver trips on:
        # being told about DEV_USERNAME, fixing it, and then being told about
        # DEV_PASSWORD is three round trips for one piece of information.
        # Only what logging in actually needs. An environment's `data` block is
        # row ids for tests; demanding those before proving a credential turns
        # one question into five.
        missing = []
        envmod.resolve({"base_url": env.get("base_url"), "auth": env.get("auth"),
                        "headers": env.get("headers")}, missing)
        if missing:
            names = sorted(set(missing))
            return jsonify({
                "ok": False, "unresolved": names, "environment": name,
                "error": f"{name} needs {', '.join(names)}. "
                         f"Set {'them' if len(names) > 1 else 'it'} with Configure, "
                         f"which writes the gitignored .env.",
            })
        headers, note = envmod.authenticate(env)
        # An environment whose credential is a static header never "logs in",
        # so reporting success here proved nothing: it said ok while the very
        # next run failed on the same credential. Prove it with a real call.
        proof = probe_credential(headers, envmod.resolve(env.get("base_url", ""), []))
        if proof and not proof["ok"]:
            return jsonify({"ok": False, "error": proof["error"],
                            "probed": proof["operation"], "status": proof["status"]})
        if proof:
            note = f"{note} — and {proof['operation']} answered {proof['status']}"
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
            secret_env["MOCKD_HEADER_1"] = f"Authorization: {value}"
    elif env_name:
        # no token given: let the environment authenticate however it is defined
        cmd += ["--env", env_name]

    if base_url:
        cmd += ["--base-url", base_url]
    for i, raw in enumerate(raw_headers, start=2):
        secret_env[f"MOCKD_HEADER_{i}"] = raw
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
    recorder.stop()
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
        # Loopback by default: this is a developer tool and binding every
        # interface would put it on the office network. A container has to set
        # CONSOLE_HOST=0.0.0.0 deliberately.
        app.run(host=os.environ.get("CONSOLE_HOST", "127.0.0.1"), port=port,
                threaded=True)
    finally:
        recorder.stop()
        if mock.running:
            mock.stop()
            print("mockd console: stopped the mock server")


if __name__ == "__main__":
    main()
