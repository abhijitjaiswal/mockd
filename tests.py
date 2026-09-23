#!/usr/bin/env python3
"""
tests.py — saved, replayable API tests: the model and the runner.

The console lets QA fire a request and see what comes back. That is exploration.
This turns an exploration into something a colleague can re-run six months later
against a different server without asking anyone how it worked.

Three ideas, and they are the whole design:

  1. A CASE is one request plus its assertions. It is saved, not re-typed.
  2. Every value that could differ between servers or runs is a VARIABLE.
     Nothing in a test file names a host or holds a token.
  3. A SCENARIO is an ordered list of steps where later steps use values
     CAPTURED from earlier ones — which is how you test an endpoint that needs
     data to exist first.

Scenarios come in two kinds, because they fail differently and should be read
differently:

  kind: "api"   pre-data steps exist only to make the endpoint under test
                reachable. The last step is the one being tested. If a setup
                step fails the target never ran, so it is reported BLOCKED, not
                failed — nobody should debug an endpoint that was never called.
  kind: "e2e"   every step is the point. A flow a human would perform, run as
                sanity. A failure anywhere is a failure of the flow.

Usage:

    python tests.py list
    python tests.py run --env mock                    # everything
    python tests.py run --env dev --kind e2e          # sanity flows only
    python tests.py run --env staging --suite users --junit results.xml

Files live in tests/*.json, one suite per file, and are meant to be committed:
they are the team's shared definition of "working".
"""
import argparse
import copy
import json
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import verdict
import project

HERE = Path(__file__).resolve().parent
TESTS_DIR = HERE / "tests"
DRAFTS_DIR = TESTS_DIR / "drafts"
HISTORY = HERE / "logs" / "test_history.json"

PASS, FAIL, BLOCKED, SKIPPED, ERROR = "pass", "fail", "blocked", "skipped", "error"
MARK = {PASS: "PASS", FAIL: "FAIL", BLOCKED: "BLOCK", SKIPPED: "SKIP", ERROR: "ERR "}


# ----------------------------------------------------------------------------
# Variables
# ----------------------------------------------------------------------------

_BUILTINS = {
    "$uuid": lambda: str(uuid.uuid4()),
    "$timestamp": lambda: str(int(time.time())),
    "$isoDate": lambda: time.strftime("%Y-%m-%d"),
    "$isoDateTime": lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "$randomInt": lambda: str(random.randint(1, 100000)),
    "$randomEmail": lambda: f"qa+{uuid.uuid4().hex[:8]}@example.com",
}

_VAR = re.compile(r"\{\{\s*([^}\s]+)\s*\}\}")


class MissingVariable(Exception):
    pass


def interpolate(node, scope, strict=True):
    """Replace {{name}} everywhere in a structure.

    A lone "{{x}}" keeps x's real type — an id that is an int stays an int
    rather than becoming the string "17", which a strict backend would reject.
    """
    if isinstance(node, dict):
        return {k: interpolate(v, scope, strict) for k, v in node.items()}
    if isinstance(node, list):
        return [interpolate(v, scope, strict) for v in node]
    if not isinstance(node, str):
        return node

    whole = _VAR.fullmatch(node.strip())
    if whole:
        return _lookup(whole.group(1), scope, strict, node)

    def swap(match):
        value = _lookup(match.group(1), scope, strict, node)
        return "" if value is None else str(value)
    return _VAR.sub(swap, node)


def _lookup(name, scope, strict, original):
    if name in _BUILTINS:
        return _BUILTINS[name]()
    if name in scope:
        return scope[name]
    if "." in name:                       # {{user.id}} into a captured object
        value = scope.get(name.split(".")[0])
        for part in name.split(".")[1:]:
            if isinstance(value, dict):
                value = value.get(part)
            else:
                value = None
                break
        if value is not None:
            return value
    if strict:
        raise MissingVariable(
            f"{{{{{name}}}}} is not defined (in {original!r}). Set it in the suite's "
            f"`data`, capture it from an earlier step, or pass --var {name}=...")
    return None


# ----------------------------------------------------------------------------
# JSON paths — dot notation with [i] indexing
# ----------------------------------------------------------------------------

MISSING = object()


def dig(payload, path):
    """'data.items[0].id' out of a response. Returns MISSING when absent, which
    is different from a field that is present and null."""
    node = payload
    if path in ("", "$", "."):
        return node
    for part in re.split(r"\.(?![^\[]*\])", str(path)):
        if not part:
            continue
        match = re.match(r"^([^\[]*)((?:\[\d+\])*)$", part)
        if not match:
            return MISSING
        key, indexes = match.group(1), re.findall(r"\[(\d+)\]", match.group(2))
        if key:
            if not isinstance(node, dict) or key not in node:
                return MISSING
            node = node[key]
        for index in indexes:
            if not isinstance(node, list) or int(index) >= len(node):
                return MISSING
            node = node[int(index)]
    return node


# ----------------------------------------------------------------------------
# Assertions
# ----------------------------------------------------------------------------


def _compare(op, actual, expected):
    """Returns (ok, explanation-if-not)."""
    try:
        if op in ("equals", "eq"):
            return actual == expected, f"expected {expected!r}, got {actual!r}"
        if op in ("not_equals", "ne"):
            return actual != expected, f"expected anything but {expected!r}"
        if op == "exists":
            return actual is not MISSING, "not present in the response"
        if op == "not_exists":
            return actual is MISSING, f"present, with value {actual!r}"
        if op == "is_null":
            return actual is None, f"expected null, got {actual!r}"
        if op == "not_null":
            return actual is not None and actual is not MISSING, "is null or missing"
        if op == "type":
            kinds = {"string": str, "number": (int, float), "integer": int,
                     "boolean": bool, "array": list, "object": dict, "null": type(None)}
            want = kinds.get(expected)
            if want is None:
                return False, f"unknown type {expected!r}"
            if expected == "number" and isinstance(actual, bool):
                return False, "expected number, got boolean"
            return isinstance(actual, want), \
                f"expected type {expected}, got {type(actual).__name__}"
        if op == "contains":
            if isinstance(actual, (list, dict)):
                return expected in actual, f"{expected!r} not in {type(actual).__name__}"
            return str(expected) in str(actual), f"{expected!r} not found in {actual!r}"
        if op == "not_contains":
            body = actual if isinstance(actual, (list, dict)) else str(actual)
            target = expected if isinstance(actual, (list, dict)) else str(expected)
            return target not in body, f"{expected!r} was present"
        if op == "matches":
            return bool(re.search(str(expected), str(actual))), \
                f"{actual!r} does not match /{expected}/"
        if op == "in":
            return actual in expected, f"{actual!r} not one of {expected!r}"
        if op in ("gt", "gte", "lt", "lte"):
            if actual is MISSING or actual is None:
                return False, f"cannot compare: value is {actual!r}"
            a, b = float(actual), float(expected)
            ok = {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]
            return ok, f"expected {op} {expected}, got {actual}"
        if op == "length":
            return len(actual) == expected, f"expected length {expected}, got {len(actual)}"
        if op == "length_gte":
            return len(actual) >= expected, f"expected at least {expected}, got {len(actual)}"
        if op == "empty":
            return len(actual) == 0, f"expected empty, got {len(actual)} item(s)"
        if op == "not_empty":
            return len(actual) > 0, "is empty"
    except (TypeError, ValueError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return False, f"unknown operator {op!r}"


def evaluate(assertion, result, spec_check=None):
    """One assertion against one response. Returns (ok, label, detail)."""
    kind = assertion.get("type", "jsonpath")
    label = assertion.get("name")

    if kind == "status":
        actual = result["status"]
        if "in" in assertion:
            ok, why = _compare("in", actual, assertion["in"])
            return ok, label or f"status in {assertion['in']}", why
        expected = assertion.get("equals", assertion.get("value"))
        ok, why = _compare("equals", actual, expected)
        return ok, label or f"status is {expected}", why

    if kind == "responseTime":
        op = assertion.get("op", "lt")
        value = assertion.get("value", 2000)
        ok, why = _compare(op, result["ms"], value)
        return ok, label or f"responds {op} {value}ms", why

    if kind == "header":
        name = assertion["name"] if "name" in assertion and "op" in assertion \
            else assertion.get("header", assertion.get("name"))
        actual = next((v for k, v in result["headers"].items()
                       if k.lower() == str(name).lower()), MISSING)
        op = assertion.get("op", "exists")
        ok, why = _compare(op, actual, assertion.get("value"))
        return ok, label or f"header {name} {op} {assertion.get('value', '')}".strip(), why

    if kind == "body_contains":
        ok, why = _compare("contains", result["text"], assertion["value"])
        return ok, label or f"body contains {assertion['value']!r}", why

    if kind == "schema":
        if spec_check is None:
            return True, label or "matches the spec schema", "no spec loaded — skipped"
        ok, why = spec_check(result)
        return ok, label or "matches the spec schema", why

    if kind == "jsonpath":
        path = assertion.get("path", "")
        actual = dig(result["json"], path) if result["json"] is not None else MISSING
        op = assertion.get("op", "exists")
        ok, why = _compare(op, actual, assertion.get("value"))
        shown = assertion.get("value")
        return ok, label or f"{path} {op}{'' if shown is None else ' ' + repr(shown)}", why

    return False, label or kind, f"unknown assertion type {kind!r}"


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------



MAX_BODY = 2 * 1024 * 1024          # 2 MB is far more than any JSON response
READ_DEADLINE = 15                  # seconds spent reading one body


def _read_capped(resp, limit=MAX_BODY, seconds=READ_DEADLINE):
    """Read a response body that might never end.

    A streaming endpoint (this spec has /api/v1/account/stream/{account_id}) keeps the
    connection open and trickles data, so a plain .read() blocks forever and the
    socket timeout never fires — the socket is not idle, it is just never done.
    Read in chunks against a wall clock instead, and say so when truncated."""
    deadline = time.time() + seconds
    chunks, total, truncated = [], 0, False
    while total < limit:
        if time.time() > deadline:
            truncated = True
            break
        try:
            # read1() returns whatever one underlying read yields. read(n) waits
            # for n bytes, so against a trickling stream it blocks for minutes
            # and the deadline below never gets a turn — which is exactly how a
            # "timed out" read still hung the run.
            chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(1)
        except Exception:
            break
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    else:
        truncated = True
    return b"".join(chunks), truncated


def send(method, url, headers, body, timeout=30):
    data = None
    headers = dict(headers or {})
    if body is not None:
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        headers.setdefault("Content-Type", "application/json")
    headers.setdefault("Accept", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw, _ = _read_capped(resp)
            status, hdrs = resp.status, dict(resp.headers)
    except urllib.error.HTTPError as exc:
        raw, _ = _read_capped(exc)
        status, hdrs = exc.code, dict(exc.headers or {})
    except Exception as exc:
        return {"status": None, "headers": {}, "text": "", "json": None,
                "ms": round((time.time() - started) * 1000),
                "error": f"{type(exc).__name__}: {exc}"}
    text = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text) if text.strip() else None
    except ValueError:
        parsed = None
    return {"status": status, "headers": hdrs, "text": text, "json": parsed,
            "ms": round((time.time() - started) * 1000), "error": None}


# ----------------------------------------------------------------------------
# Suites
# ----------------------------------------------------------------------------


def load_suites(directory=None, names=None, include_drafts=False, drafts_only=False):
    """Shared suites live in tests/ and are committed. Drafts live in
    tests/drafts/, are gitignored, and are what QA writes while they are still
    deciding whether the assertions are right. CI runs the shared ones only —
    that is the whole point of the split."""
    out = []
    sources = []
    if not drafts_only:
        sources.append((Path(directory or TESTS_DIR), "shared"))
    if include_drafts or drafts_only:
        sources.append((Path(directory or TESTS_DIR) / "drafts", "draft"))

    for folder, stage in sources:
        if not folder.exists():
            continue
        for path in sorted(folder.glob("*.json")):
            if path.name.startswith("_"):
                continue
            doc = json.loads(path.read_text())
            doc["_path"] = str(path)
            doc["_stage"] = stage
            doc.setdefault("name", path.stem)
            if names and doc["name"] not in names:
                continue
            out.append(doc)
    return out


def save_suite(suite, directory=None, stage=None):
    stage = stage or suite.get("_stage", "draft")
    folder = Path(directory) if directory else (DRAFTS_DIR if stage == "draft" else TESTS_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    existing = suite.get("_path")
    path = Path(existing) if existing and Path(existing).parent == folder \
        else folder / f"{suite['name']}.json"
    body = {k: v for k, v in suite.items() if not k.startswith("_")}
    path.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n")
    return path


# -- history: a test may only be promoted once it has actually passed ---------


def load_history():
    try:
        return json.loads(HISTORY.read_text())
    except Exception:
        return {}


def record_history(results, base_url, env_name=None, spec_digest=None):
    history = load_history()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for suite_name, items in results:
        for item in items:
            key = f"{suite_name}/{item.get('id') or item.get('name')}"
            entry = history.get(key, {"passes": 0, "runs": 0})
            entry["runs"] += 1
            entry["last_outcome"] = item["outcome"]
            entry["last_run"] = stamp
            entry["last_base_url"] = base_url
            entry["last_env"] = env_name or base_url
            entry["last_spec"] = spec_digest
            if item["outcome"] == PASS:
                entry["passes"] += 1
                entry["last_pass"] = stamp
                entry["last_pass_spec"] = spec_digest
            history[key] = entry
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text(json.dumps(history, indent=2) + "\n")
    return history


def find_test(suite, ident):
    for case in suite.get("cases") or []:
        if case.get("id") == ident:
            return "cases", case
    for scenario in suite.get("scenarios") or []:
        if scenario.get("id") == ident:
            return "scenarios", scenario
    return None, None


def promote(suite_name, ident, force=False):
    """Move one test from the draft workspace into the shared, committed suite.

    Gated on having passed at least once, because a red assertion checked into
    the common repo costs every other team member time. --force exists for the
    case where the endpoint is legitimately broken and the test is right."""
    drafts = {s["name"]: s for s in load_suites(drafts_only=True)}
    draft = drafts.get(suite_name)
    if not draft:
        raise SystemExit(f"no draft suite named {suite_name!r}")
    kind, test = find_test(draft, ident)
    if test is None:
        raise SystemExit(f"{ident!r} is not in draft suite {suite_name!r}")

    history = load_history().get(f"{suite_name}/{ident}", {})
    if not force and history.get("last_outcome") != PASS:
        raise SystemExit(
            f"{ident!r} has not passed yet (last outcome: "
            f"{history.get('last_outcome', 'never run')}). Run it green first, "
            f"or pass --force if the endpoint is the thing that is wrong.")

    shared = {s["name"]: s for s in load_suites()}
    target = shared.get(suite_name) or {"name": suite_name,
                                        "data": draft.get("data") or {},
                                        "cases": [], "scenarios": []}
    target.setdefault("cases", [])
    target.setdefault("scenarios", [])
    # carry any variables the test depends on
    for key, value in (draft.get("data") or {}).items():
        target.setdefault("data", {}).setdefault(key, value)

    target[kind] = [t for t in target[kind] if t.get("id") != ident]
    promoted = dict(test)
    promoted["promoted_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    target[kind].append(promoted)
    shared_path = save_suite(target, stage="shared")

    draft[kind] = [t for t in draft[kind] if t.get("id") != ident]
    if not (draft.get("cases") or draft.get("scenarios")):
        Path(draft["_path"]).unlink(missing_ok=True)
    else:
        save_suite(draft, stage="draft")
    return shared_path


# ----------------------------------------------------------------------------
# Running
# ----------------------------------------------------------------------------


class Runner:
    def __init__(self, base_url, headers, spec=None, timeout=30, verbose=True):
        self.base_url = base_url.rstrip("/")
        self.headers = dict(headers or {})
        self.spec = spec
        self.timeout = timeout
        self.verbose = verbose

    # -- one request -------------------------------------------------------
    def _spec_check(self, request):
        """Validate the body against the schema the spec declares for the
        status that actually came back. Free coverage — QA does not have to
        restate what the contract already says."""
        if self.spec is None:
            return None
        import mockd
        route, _ = self.spec.match(request["method"], request["_path_only"])
        if route is None:
            return None

        def check(result):
            resp = route["responses"].get(str(result["status"]))
            if not resp:
                return False, (f"status {result['status']} is not documented for "
                               f"{route['key']} (documented: "
                               f"{', '.join(sorted(route['responses']))})")
            schema = ((resp.get("content") or {}).get("application/json") or {}).get("schema")
            if not schema:
                return True, "the spec declares no schema for this status — nothing checked"
            errors = list(mockd._Validator(schema).iter_errors(result["json"]))
            if not errors:
                return True, ""
            first = errors[0]
            where = "/".join(str(x) for x in first.absolute_path) or "(root)"
            return False, f"{len(errors)} violation(s); first: {where}: {first.message[:160]}"
        return check

    def run_step(self, step, scope):
        spec = step.get("request", step)
        method = str(spec.get("method", "GET")).upper()
        path = interpolate(spec.get("path", "/"), scope)
        query = interpolate(spec.get("query") or {}, scope)
        headers = {**self.headers, **interpolate(spec.get("headers") or {}, scope)}
        body = interpolate(spec.get("body"), scope) if spec.get("body") is not None else None

        url = self.base_url + (path if str(path).startswith("/") else "/" + str(path))
        if query:
            pairs = {k: ("" if v is None else v) for k, v in query.items()}
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(pairs)

        result = send(method, url, headers, body, self.timeout)
        result["_url"] = url
        result["_method"] = method

        request_info = {"method": method, "_path_only": str(path).split("?")[0]}
        spec_check = self._spec_check(request_info)

        checks = []
        if result["error"]:
            checks.append({"ok": False, "label": "request completed", "detail": result["error"]})
        else:
            for assertion in (step.get("assertions") or []):
                ok, label, detail = evaluate(assertion, result, spec_check)
                checks.append({"ok": ok, "label": label, "detail": "" if ok else detail})

        captured = {}
        for name, path_expr in (step.get("capture") or {}).items():
            value = dig(result["json"], path_expr) if result["json"] is not None else MISSING
            if value is MISSING:
                checks.append({"ok": False, "label": f"capture {name}",
                               "detail": f"{path_expr} not found in the response — later "
                                         f"steps that use {{{{{name}}}}} cannot run"})
            else:
                captured[name] = value
                scope[name] = value

        ok = all(c["ok"] for c in checks) and not result["error"]
        return {
            "name": step.get("name") or f"{method} {path}",
            "role": step.get("role", "target"),
            "request": {"method": method, "url": url,
                        "body": body if isinstance(body, (dict, list)) else body},
            "status": result["status"], "ms": result["ms"],
            "checks": checks, "captured": captured,
            "outcome": PASS if ok else FAIL,
            "response_excerpt": (result["text"] or "")[:600],
        }

    # -- cases and scenarios ----------------------------------------------
    def run_case(self, case, data):
        scope = dict(data)
        try:
            outcome = self.run_step(case, scope)
        except MissingVariable as exc:
            return {"id": case.get("id"), "name": case.get("name") or case.get("id"),
                    "kind": "case", "outcome": ERROR, "steps": [],
                    "error": str(exc)}
        return {"id": case.get("id"), "name": case.get("name") or case.get("id"),
                "kind": "case", "tags": case.get("tags", []),
                "outcome": outcome["outcome"], "steps": [outcome]}

    def run_scenario(self, scenario, data):
        scope = dict(data)
        scope.update(interpolate(scenario.get("data") or {}, scope, strict=False))
        kind = scenario.get("kind", "api")
        steps, outcome = [], PASS
        blocked_by = None

        for index, step in enumerate(scenario.get("steps") or []):
            if kind == "e2e":
                # every step is the point in an end-to-end flow; calling them
                # "setup" would imply they are scaffolding
                role = step.get("role") or "step"
            else:
                role = step.get("role") or (
                    "target" if index == len(scenario["steps"]) - 1 else "setup")
            step = {**step, "role": role}
            if blocked_by:
                steps.append({"name": step.get("name") or f"step {index + 1}", "role": role,
                              "outcome": BLOCKED, "checks": [], "status": None, "ms": 0,
                              "captured": {},
                              "blocked_by": blocked_by, "request": {}, "response_excerpt": ""})
                continue
            try:
                done = self.run_step(step, scope)
            except MissingVariable as exc:
                done = {"name": step.get("name") or f"step {index + 1}", "role": role,
                        "outcome": ERROR, "checks": [{"ok": False, "label": "variables",
                                                      "detail": str(exc)}],
                        "status": None, "ms": 0, "captured": {}, "request": {},
                        "response_excerpt": ""}
            steps.append(done)
            if done["outcome"] != PASS:
                # In an "api" scenario a failed SETUP means the endpoint under
                # test never ran — that is blocked, not failed, and nobody
                # should go debugging the target.
                if kind == "api" and role in ("setup", "cleanup"):
                    blocked_by = done["name"]
                    outcome = BLOCKED
                else:
                    outcome = FAIL
                    if kind == "e2e":
                        blocked_by = done["name"]

        cleanup = []
        for step in (scenario.get("cleanup") or []):
            try:
                done = self.run_step({**step, "role": "cleanup"}, scope)
            except MissingVariable:
                continue                  # cleanup is best-effort by design
            cleanup.append(done)

        return {"id": scenario.get("id"), "name": scenario.get("name") or scenario.get("id"),
                "kind": "scenario", "scenario_kind": kind, "tags": scenario.get("tags", []),
                "outcome": outcome, "steps": steps, "cleanup": cleanup,
                "blocked_by": blocked_by if outcome == BLOCKED else None}

    # -- a whole suite -----------------------------------------------------
    def run_suite(self, suite, only=None, kinds=None, tags=None):
        data = interpolate(suite.get("data") or {}, {}, strict=False)
        results = []

        for case in suite.get("cases") or []:
            if only and not re.search(only, f"{case.get('id', '')} {case.get('name', '')}"):
                continue
            if kinds and "case" not in kinds:
                continue
            if tags and not (set(tags) & set(case.get("tags", []))):
                continue
            results.append(self.run_case(case, data))

        for scenario in suite.get("scenarios") or []:
            if only and not re.search(only, f"{scenario.get('id', '')} {scenario.get('name', '')}"):
                continue
            if kinds and scenario.get("kind", "api") not in kinds:
                continue
            if tags and not (set(tags) & set(scenario.get("tags", []))):
                continue
            results.append(self.run_scenario(scenario, data))
        return results


# ----------------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------------


def _print_verdict(item):
    v = item.get("verdict")
    if not v:
        return
    print(f"        VERDICT  {v['headline']}")
    for line in v["evidence"]:
        print(f"                 {line}")
    if v.get("next"):
        print(f"                 next: {v['next']}")


def print_results(suite_name, results, verbose=False):
    print(f"\nSUITE {suite_name}")
    for item in results:
        mark = MARK[item["outcome"]]
        if item["kind"] == "case":
            step = item["steps"][0] if item["steps"] else {}
            extra = f"{step.get('status') or '---'}  {step.get('ms', 0):>4}ms"
            print(f"  {mark}  {item['name']:<44s} {extra}")
            if item.get("error"):
                print(f"          {item['error']}")
            _print_verdict(item)
            for check in step.get("checks", []):
                if not check["ok"] or verbose:
                    flag = " " if check["ok"] else "x"
                    print(f"        {flag} {check['label']}"
                          + (f" — {check['detail']}" if check["detail"] else ""))
        else:
            print(f"  {mark}  {item['name']:<44s} [{item['scenario_kind']}]")
            if item.get("blocked_by"):
                print(f"        target never ran — blocked by: {item['blocked_by']}")
            _print_verdict(item)
            for step in item["steps"]:
                sm = MARK[step["outcome"]]
                print(f"        {sm}  {step['role']:<7s} {step['name']:<32s} "
                      f"{step.get('status') or '---'}  {step.get('ms', 0):>4}ms"
                      + (f"  -> {', '.join(step['captured'])}" if step.get("captured") else ""))
                for check in step.get("checks", []):
                    if not check["ok"] or verbose:
                        flag = " " if check["ok"] else "x"
                        print(f"             {flag} {check['label']}"
                              + (f" — {check['detail']}" if check["detail"] else ""))


def summarise(all_results, args=None):
    counts = {}
    for _, results in all_results:
        for item in results:
            counts[item["outcome"]] = counts.get(item["outcome"], 0) + 1
    total = sum(counts.values())
    print("\n" + "=" * 72)
    print(f"target: {getattr(args, 'env', None) or ''}"
          f"{'  ' if getattr(args, 'env', None) else ''}{args.base_url or ''}".rstrip())
    print(f"{total} test(s): " + "  ".join(
        f"{counts.get(k, 0)} {k}" for k in (PASS, FAIL, BLOCKED, ERROR, SKIPPED)
        if counts.get(k)))
    if counts.get(BLOCKED):
        print("  blocked = the endpoint under test never ran because its setup failed. "
              "Fix the setup first.")

    tally = verdict.summarise([i.get("verdict") for _, rs in all_results for i in rs])
    if tally:
        print("\nWHOSE PROBLEM:")
        for kind, n in tally:
            print(f"  {n:3d}  {verdict.MEANING[kind][0]}")
            print(f"       {verdict.MEANING[kind][1]}")
    return counts


def write_junit(all_results, path, env=None, base_url=None, digest=None):
    def esc(text):
        return (str(text).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))
    total = failures = skipped = 0
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<testsuites>"]
    for suite_name, results in all_results:
        s_fail = sum(1 for r in results if r["outcome"] in (FAIL, ERROR))
        s_skip = sum(1 for r in results if r["outcome"] in (BLOCKED, SKIPPED))
        total += len(results)
        failures += s_fail
        skipped += s_skip
        lines.append(f'  <testsuite name="{esc(suite_name)}" tests="{len(results)}" '
                     f'failures="{s_fail}" skipped="{s_skip}">')
        lines.append("    <properties>"
                     + f'<property name="environment" value="{esc(env or "—")}"/>'
                     + f'<property name="base_url" value="{esc(base_url or "—")}"/>'
                     + f'<property name="spec_digest" value="{esc((digest or "")[:16])}"/>'
                     + "</properties>")
        for item in results:
            duration = sum(s.get("ms", 0) for s in item.get("steps", [])) / 1000
            lines.append(f'    <testcase classname="{esc(suite_name)}" '
                         f'name="{esc(item["name"])}" time="{duration:.3f}">')
            if item["outcome"] in (FAIL, ERROR):
                detail = []
                for step in item.get("steps", []):
                    for check in step.get("checks", []):
                        if not check["ok"]:
                            detail.append(f"{step['name']}: {check['label']} — {check['detail']}")
                lines.append(f'      <failure message="{esc((detail or [item.get("error", "failed")])[0])[:400]}">'
                             f'{esc(chr(10).join(detail))}</failure>')
            elif item["outcome"] in (BLOCKED, SKIPPED):
                lines.append(f'      <skipped message="{esc(item.get("blocked_by") or "skipped")}"/>')
            lines.append("    </testcase>")
        lines.append("  </testsuite>")
    lines.append("</testsuites>")
    Path(path).write_text("\n".join(lines) + "\n")
    return total, failures, skipped


# ----------------------------------------------------------------------------
# Validation — the gate anything pasted in has to pass
# ----------------------------------------------------------------------------

ASSERTION_TYPES = {"status", "schema", "jsonpath", "header", "responseTime", "body_contains"}
OPERATORS = {"equals", "eq", "not_equals", "ne", "exists", "not_exists", "is_null",
             "not_null", "type", "contains", "not_contains", "matches", "in",
             "gt", "gte", "lt", "lte", "length", "length_gte", "empty", "not_empty"}
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


def validate_assertion(assertion, where):
    errors = []
    if not isinstance(assertion, dict):
        return [f"{where}: each assertion must be an object"]
    kind = assertion.get("type", "jsonpath")
    if kind not in ASSERTION_TYPES:
        errors.append(f"{where}: unknown assertion type {kind!r} "
                      f"(one of {', '.join(sorted(ASSERTION_TYPES))})")
        return errors
    if kind == "status" and "equals" not in assertion and "in" not in assertion \
            and "value" not in assertion:
        errors.append(f"{where}: a status assertion needs `equals` or `in`")
    if kind == "jsonpath":
        if "path" not in assertion:
            errors.append(f"{where}: a jsonpath assertion needs `path`")
        op = assertion.get("op", "exists")
        if op not in OPERATORS:
            errors.append(f"{where}: unknown operator {op!r}")
        elif op not in ("exists", "not_exists", "is_null", "not_null", "empty", "not_empty") \
                and "value" not in assertion:
            errors.append(f"{where}: operator {op!r} needs a `value`")
    if kind == "header" and not (assertion.get("name") or assertion.get("header")):
        errors.append(f"{where}: a header assertion needs `name`")
    return errors


def validate_request(spec_block, where):
    errors = []
    if not isinstance(spec_block, dict):
        return [f"{where}: `request` must be an object"]
    method = str(spec_block.get("method", "GET")).upper()
    if method not in METHODS:
        errors.append(f"{where}: {method!r} is not an HTTP method")
    path = spec_block.get("path")
    if not isinstance(path, str) or not path.startswith("/"):
        errors.append(f"{where}: `path` must be a string starting with /")
    for key in ("query", "headers"):
        if key in spec_block and not isinstance(spec_block[key], dict):
            errors.append(f"{where}: `{key}` must be an object")
    return errors


def validate_test(test, kind=None):
    """Check a case or scenario before it is written to disk.

    This exists because tests will increasingly arrive as pasted JSON — from a
    teammate, from a generator, from an assistant. Plausible-looking output with
    a misspelled operator should fail here with a clear message, not silently
    pass at runtime because an unknown assertion quietly evaluated to false."""
    errors = []
    if not isinstance(test, dict):
        return ["a test must be a JSON object"]
    ident = test.get("id") or "(no id)"
    if not test.get("id"):
        errors.append("missing `id`")
    elif not re.fullmatch(r"[A-Za-z0-9._-]+", str(test["id"])):
        errors.append(f"{ident}: `id` may only contain letters, digits, . _ -")

    is_scenario = "steps" in test or kind == "scenario"
    if is_scenario:
        if test.get("kind") not in ("api", "e2e", None):
            errors.append(f"{ident}: scenario `kind` must be \"api\" or \"e2e\"")
        steps = test.get("steps")
        if not isinstance(steps, list) or not steps:
            errors.append(f"{ident}: a scenario needs a non-empty `steps` array")
            return errors
        produced = set()
        for index, step in enumerate(steps):
            where = f"{ident}.steps[{index}]"
            if not isinstance(step, dict):
                errors.append(f"{where}: each step must be an object")
                continue
            errors += validate_request(step.get("request", step), where)
            for a_index, assertion in enumerate(step.get("assertions") or []):
                errors += validate_assertion(assertion, f"{where}.assertions[{a_index}]")
            if step.get("role") and step["role"] not in ("setup", "target", "step", "cleanup"):
                errors.append(f"{where}: `role` must be setup, target, step or cleanup")
            capture = step.get("capture") or {}
            if not isinstance(capture, dict):
                errors.append(f"{where}: `capture` must be an object of name -> json path")
            else:
                produced |= set(capture)
            # a variable used before anything produces it is the classic
            # generated-scenario bug; catch it at import time
            used = set(_VAR.findall(json.dumps(step)))
            unknown = {u for u in used
                       if u not in produced and u not in _BUILTINS
                       and not u.startswith("$")}
            for name in sorted(unknown):
                errors.append(f"{where}: uses {{{{{name}}}}} but nothing before it captures "
                              f"that, and it is not in the suite's `data`"
                              if name not in (test.get("data") or {}) else "")
        errors = [e for e in errors if e]
    else:
        errors += validate_request(test.get("request", test), ident)
        assertions = test.get("assertions")
        if not isinstance(assertions, list) or not assertions:
            errors.append(f"{ident}: a case needs at least one assertion")
        else:
            for index, assertion in enumerate(assertions):
                errors += validate_assertion(assertion, f"{ident}.assertions[{index}]")
    return errors


def import_tests(payload, suite_name, stage="draft", data=None):
    """Accept one test, a list of tests, or a whole suite document.

    Deliberately forgiving about the wrapper and strict about the content: the
    shapes a generator or a teammate will hand you vary, the grammar does not.
    """
    if isinstance(payload, str):
        payload = json.loads(payload)

    cases, scenarios, suite_data = [], [], dict(data or {})
    if isinstance(payload, dict) and ("cases" in payload or "scenarios" in payload):
        cases = payload.get("cases") or []
        scenarios = payload.get("scenarios") or []
        suite_data.update(payload.get("data") or {})
        suite_name = suite_name or payload.get("name")
    else:
        items = payload if isinstance(payload, list) else [payload]
        for item in items:
            (scenarios if isinstance(item, dict) and "steps" in item else cases).append(item)

    problems = []
    for case in cases:
        problems += validate_test({**case, "data": suite_data}, "case")
    for scenario in scenarios:
        problems += validate_test({**scenario, "data": suite_data}, "scenario")
    if problems:
        return {"ok": False, "errors": problems,
                "counted": {"cases": len(cases), "scenarios": len(scenarios)}}

    existing = {s["name"]: s for s in load_suites(include_drafts=True)
                if s.get("_stage") == stage}
    suite = existing.get(suite_name) or {"name": suite_name, "data": {}, "cases": [],
                                         "scenarios": [], "_stage": stage}
    suite.setdefault("cases", [])
    suite.setdefault("scenarios", [])
    suite.setdefault("data", {}).update(suite_data)

    added, replaced = 0, 0
    for group, incoming in (("cases", cases), ("scenarios", scenarios)):
        for item in incoming:
            before = len(suite[group])
            suite[group] = [x for x in suite[group] if x.get("id") != item.get("id")]
            replaced += before - len(suite[group])
            suite[group].append(item)
            added += 1
    path = save_suite(suite, stage=stage)
    return {"ok": True, "file": str(path), "suite": suite_name, "stage": stage,
            "imported": added, "replaced": replaced,
            "cases": len(suite["cases"]), "scenarios": len(suite["scenarios"])}


# ----------------------------------------------------------------------------
# Assertion suggestions — the thing that makes authoring bearable
# ----------------------------------------------------------------------------


def suggest(result, spec=None, method=None, path=None):
    """Propose assertions from a response QA just saw.

    Writing assertions from a blank page is the reason test suites do not get
    written. Reading a real response and ticking the parts that matter is not.
    """
    out = [{"type": "status", "equals": result.get("status"),
            "name": f"status is {result.get('status')}", "_suggested": True}]
    if spec is not None and method and path:
        route, _ = spec.match(method, path)
        if route:
            resp = route["responses"].get(str(result.get("status"))) or {}
            if ((resp.get("content") or {}).get("application/json") or {}).get("schema"):
                out.append({"type": "schema",
                            "name": "body matches the schema the spec declares",
                            "_suggested": True})
    out.append({"type": "responseTime", "op": "lt", "value": 2000,
                "name": "responds under 2s", "_suggested": True})

    payload = result.get("json")
    if isinstance(payload, dict):
        for key in ("status_code", "message"):
            if key in payload and not isinstance(payload[key], (dict, list)):
                out.append({"type": "jsonpath", "path": key, "op": "equals",
                            "value": payload[key], "_suggested": True})
        container = payload.get("data", payload)
        if isinstance(container, dict):
            prefix = "data." if "data" in payload else ""
            if isinstance(container.get("items"), list):
                out.append({"type": "jsonpath", "path": f"{prefix}items",
                            "op": "type", "value": "array", "_suggested": True})
                out.append({"type": "jsonpath", "path": f"{prefix}items",
                            "op": "not_empty", "_suggested": True})
                first = container["items"][0] if container["items"] else {}
                for key in list(first)[:6] if isinstance(first, dict) else []:
                    out.append({"type": "jsonpath", "path": f"{prefix}items[0].{key}",
                                "op": "exists", "_suggested": True})
            else:
                for key in list(container)[:8]:
                    value = container[key]
                    if isinstance(value, (dict, list)):
                        out.append({"type": "jsonpath", "path": f"{prefix}{key}",
                                    "op": "exists", "_suggested": True})
                    else:
                        kind = {str: "string", bool: "boolean", int: "integer",
                                float: "number", type(None): "null"}.get(type(value), "string")
                        out.append({"type": "jsonpath", "path": f"{prefix}{key}",
                                    "op": "type", "value": kind, "_suggested": True})
    elif isinstance(payload, list):
        out.append({"type": "jsonpath", "path": "", "op": "type", "value": "array",
                    "_suggested": True})
    return out


def suggest_captures(result):
    """Ids worth exporting for a later step."""
    out = {}
    payload = result.get("json")
    if not isinstance(payload, dict):
        return out
    container = payload.get("data", payload)
    prefix = "data." if "data" in payload else ""
    if isinstance(container, dict):
        for key, value in container.items():
            if (key == "id" or key.endswith("_id")) and isinstance(value, (str, int)):
                out[_camel(key)] = f"{prefix}{key}"
        if isinstance(container.get("items"), list) and container["items"]:
            first = container["items"][0]
            if isinstance(first, dict) and "id" in first:
                out.setdefault("firstId", f"{prefix}items[0].id")
    return out


def _camel(name):
    head, *rest = str(name).split("_")
    return head + "".join(p.capitalize() for p in rest)


# ----------------------------------------------------------------------------
# Context pack — everything a generator needs to write tests for an operation
# ----------------------------------------------------------------------------

GRAMMAR = """\
A test is JSON. Two shapes:

CASE — one request plus assertions:
{
  "id": "kebab-case-id", "name": "human sentence", "tags": ["smoke"],
  "request": { "method": "GET", "path": "/api/v1/...",
               "query": {"page": 1}, "headers": {}, "body": {} },
  "assertions": [ ... ],
  "capture": { "someId": "data.id" }
}

SCENARIO — ordered steps, later steps use values captured earlier:
{
  "id": "kebab-case-id", "name": "...", "kind": "api" | "e2e",
  "steps": [ { "role": "setup"|"target"|"step", "name": "...",
               "request": {...}, "assertions": [...], "capture": {...} } ],
  "cleanup": [ { "name": "...", "request": {...}, "assertions": [...] } ]
}

kind "api"  = the last step is the endpoint under test; earlier steps only exist
              to create the data it needs. A failing setup reports BLOCKED.
kind "e2e"  = every step matters; a flow run as sanity.

ASSERTIONS
  {"type":"status","equals":200}                  {"type":"status","in":[200,201]}
  {"type":"schema"}                               validate the body against the spec
  {"type":"responseTime","op":"lt","value":2000}
  {"type":"header","name":"Content-Type","op":"contains","value":"json"}
  {"type":"body_contains","value":"text"}
  {"type":"jsonpath","path":"data.items[0].id","op":"exists"}

JSONPATH OPERATORS
  equals not_equals exists not_exists is_null not_null type contains not_contains
  matches in gt gte lt lte length length_gte empty not_empty
  `type` values: string number integer boolean array object null

VARIABLES — nothing may hardcode a host, a token or an id.
  {{name}}        from the suite's `data`, or captured by an earlier step
  {{$uuid}} {{$randomInt}} {{$randomEmail}} {{$timestamp}} {{$isoDate}} {{$isoDateTime}}

RULES
  - Never write a base URL. `path` starts at / and the runner adds the host.
  - Never write a token. Auth comes from the environment.
  - A variable must be in `data` or captured by an EARLIER step, or import fails.
  - Assert only what the spec actually declares; do not invent field names.
"""


def context_pack(spec, operation=None, sample=None, existing=None):
    """A self-contained brief: the grammar, the operation's contract, a real
    response, and what is already covered.

    Vendor-neutral on purpose. Paste it into whatever assistant the company
    already uses, get JSON back, and import it through the same validator a
    human's paste goes through."""
    lines = [GRAMMAR, ""]
    if operation and spec is not None:
        method, _, path = operation.partition(" ")
        route = next((r for r in spec.routes
                      if r["method"] == method.upper() and r["path"] == path), None)
        if route:
            lines.append("=" * 70)
            lines.append(f"OPERATION  {route['key']}")
            if route["summary"]:
                lines.append(f"summary: {route['summary']}")
            params = route["parameters"]
            if params:
                lines.append("\nPARAMETERS")
                for prm in params:
                    schema = prm.get("schema") or {}
                    kind = schema.get("type") or (
                        "/".join(str(b.get("type")) for b in (schema.get("anyOf") or [])
                                 if isinstance(b, dict)) or "?")
                    flag = "required" if prm.get("required") else "optional"
                    enum = schema.get("enum") or next(
                        (b.get("enum") for b in (schema.get("anyOf") or [])
                         if isinstance(b, dict) and b.get("enum")), None)
                    lines.append(f"  {prm.get('in'):6s} {prm.get('name'):28s} {kind:16s} {flag}"
                                 + (f"  one of {enum}" if enum else ""))
            media = (route["request_body"].get("content") or {}).get("application/json")
            if media and media.get("schema"):
                lines.append("\nREQUEST BODY SCHEMA")
                lines.append(json.dumps(media["schema"], indent=2)[:3000])
            lines.append("\nDOCUMENTED RESPONSES")
            for status in sorted(route["responses"]):
                resp = route["responses"][status]
                content = (resp.get("content") or {}).get("application/json") or {}
                has = "schema" if content.get("schema") else (
                    "example only" if "example" in content or content.get("examples")
                    else "nothing declared")
                lines.append(f"  {status}  {resp.get('description', '')}  [{has}]")
            from mockd import _success_status
            success = _success_status(route["responses"])
            schema = (((route["responses"].get(success) or {}).get("content") or {})
                      .get("application/json") or {}).get("schema")
            if schema:
                lines.append(f"\nRESPONSE SCHEMA FOR {success}")
                lines.append(json.dumps(schema, indent=2)[:3000])
            else:
                lines.append("\nNOTE: the spec declares NO response schema for this "
                             "operation. Do not use {\"type\":\"schema\"}; assert only "
                             "on fields visible in the sample response below, and keep "
                             "the assertions loose.")
    if sample is not None:
        lines.append("\nACTUAL RESPONSE OBSERVED")
        lines.append(json.dumps(sample, indent=2)[:3000])
    if existing:
        lines.append("\nALREADY COVERED — do not duplicate these ids:")
        for ident in existing:
            lines.append(f"  {ident}")
    lines.append("\n" + "=" * 70)
    lines.append("Return ONLY a JSON array of test objects. No prose, no markdown fence.")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------


def build_runner(args):
    base_url, headers, env_data = args.base_url, {}, {}
    if args.env:
        import environments
        base, headers, note = environments.headers_for(args.env)
        env_data = environments.data_for(args.env)
        base_url = base_url or base
        print(f"env: {args.env} -> {base_url} ({note})"
              + (f", {len(env_data)} env value(s)" if env_data else ""))
    for raw in (args.header or []):
        if ":" in raw:
            name, value = raw.split(":", 1)
            headers[name.strip()] = value.strip()
    if not base_url:
        raise SystemExit("give --base-url, or --env NAME")

    # None means "not given" -> the project's document; '' means "deliberately
    # no schema checking" and must stay off.
    if args.spec is None:
        args.spec = project.active_spec(None, "tests")

    spec, provenance = None, None
    if args.spec:
        from mockd import Source, Spec
        src = Source(args.spec, poll=0, cache_dir="logs")
        text, _ = src.read(force=True)
        if text:
            spec = Spec(text=text, origin=args.spec)
            # A result is only meaningful with the document it was judged against.
            import speclock
            provenance = speclock.compare(text)
            provenance["source"] = args.spec
            if provenance["state"] == "drift":
                print(f"WARNING: {provenance['message']}")
                print("         Results below are against THIS document, not the pinned one.")
            elif provenance["state"] == "unlocked":
                print(f"note: {provenance['message']}")
    return Runner(base_url, headers, spec, args.timeout), base_url, env_data, provenance


def main():
    ap = argparse.ArgumentParser(description="Run saved API tests against any environment")
    sub = ap.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("list", help="show every saved suite, case and scenario")
    listing.add_argument("--dir", default=None)
    listing.add_argument("--drafts", action="store_true", help="include the draft workspace")

    imp = sub.add_parser("import", help="bulk-import tests from a JSON file or stdin")
    imp.add_argument("--suite", required=True)
    imp.add_argument("--file", help="JSON file; omit to read stdin")
    imp.add_argument("--stage", choices=["draft", "shared"], default="draft")

    val = sub.add_parser("validate", help="check a JSON file of tests without saving")
    val.add_argument("--file")

    brief = sub.add_parser("prompt",
                           help="print the context an assistant needs to write tests")
    brief.add_argument("--operation", help="e.g. 'GET /api/v1/account/list'")
    brief.add_argument("--spec", default=None)
    brief.add_argument("--base-url", help="fetch a real sample response from here")

    prom = sub.add_parser("promote",
                          help="move a proven draft into the shared, committed suite")
    prom.add_argument("--suite", required=True)
    prom.add_argument("--id", required=True)
    prom.add_argument("--force", action="store_true",
                      help="promote even though it has not passed")

    run = sub.add_parser("run", help="run them")
    run.add_argument("--dir", default=None)
    run.add_argument("--drafts", action="store_true",
                     help="also run the draft workspace (tests/drafts) — CI should not")
    run.add_argument("--drafts-only", action="store_true")
    run.add_argument("--env", help="named environment from environments.json")
    run.add_argument("--base-url")
    run.add_argument("--header", action="append", default=[], metavar="'K: V'")
    run.add_argument("--spec", default=None,
                     help="used by 'schema' assertions; --spec '' to disable")
    run.add_argument("--suite", action="append", help="only these suites, repeatable")
    run.add_argument("--only", help="regex over test id and name")
    run.add_argument("--kind", action="append", choices=["case", "api", "e2e"],
                     help="case | api | e2e; repeatable. --kind e2e is the sanity run")
    run.add_argument("--tag", action="append")
    run.add_argument("--var", action="append", default=[], metavar="name=value",
                     help="override suite data, repeatable")
    run.add_argument("--timeout", type=int, default=30)
    run.add_argument("--require-locked-spec", action="store_true",
                     help="refuse to run unless the spec matches spec.lock.json — "
                          "what CI should use, so results cannot be produced against "
                          "a substituted document")
    run.add_argument("--json", help="write the full result as JSON")
    run.add_argument("--junit", help="write a JUnit XML report")
    run.add_argument("--verbose", action="store_true", help="show passing assertions too")
    args = ap.parse_args()

    if args.command in ("import", "validate"):
        raw = Path(args.file).read_text() if args.file else sys.stdin.read()
        payload = json.loads(raw)
        if args.command == "validate":
            items = payload if isinstance(payload, list) else [payload]
            problems = []
            for item in items:
                problems += validate_test(item)
            if problems:
                print(f"{len(problems)} problem(s):")
                for problem in problems:
                    print(f"  - {problem}")
                return 1
            print(f"{len(items)} test(s) are valid")
            return 0
        outcome = import_tests(payload, args.suite, args.stage)
        if not outcome["ok"]:
            print(f"refused — {len(outcome['errors'])} problem(s):")
            for problem in outcome["errors"]:
                print(f"  - {problem}")
            return 1
        print(f"imported {outcome['imported']} test(s) into {outcome['file']} "
              f"({outcome['replaced']} replaced)")
        return 0

    if args.command == "prompt":
        if args.spec is None:
            args.spec = project.active_spec(None, "tests")
        spec = None
        if args.spec:
            from mockd import Source, Spec
            src = Source(args.spec, poll=0, cache_dir="logs")
            text, _ = src.read(force=True)
            if text:
                spec = Spec(text=text, origin=args.spec)
        sample = None
        if args.base_url and args.operation:
            method, _, path = args.operation.partition(" ")
            method = method.upper()
            body = None
            route = next((r for r in (spec.routes if spec else [])
                          if r["method"] == method and r["path"] == path), None)
            if route:
                import random as _r

                from generator import generate_from_schema
                media = (route["request_body"].get("content") or {}).get("application/json")
                if media and media.get("schema"):
                    # a bare POST returns a 422; the useful sample is what a
                    # VALID request gets back
                    body = generate_from_schema(media["schema"], _r.Random(route["key"]),
                                                array_items=1)
                for prm in route["parameters"]:
                    if prm.get("in") == "path":
                        value = generate_from_schema(prm.get("schema") or {"type": "string"},
                                                     _r.Random(prm["name"]))
                        path = path.replace("{%s}" % prm["name"], str(value))
            result = send(method, args.base_url.rstrip("/") + path, {}, body)
            sample = result.get("json")
        covered = []
        for suite in load_suites(include_drafts=True):
            covered += [c.get("id") for c in (suite.get("cases") or [])]
            covered += [s.get("id") for s in (suite.get("scenarios") or [])]
        print(context_pack(spec, args.operation, sample, covered))
        return 0

    if args.command == "promote":
        path = promote(args.suite, args.id, args.force)
        print(f"promoted {args.id} into {path} — commit it and the team has it")
        return 0

    if args.command == "list":
        suites = load_suites(args.dir, include_drafts=getattr(args, "drafts", False))
        history = load_history()
        if not suites:
            print(f"no suites in {Path(args.dir or TESTS_DIR)}")
            return 0
        for suite in suites:
            cases = suite.get("cases") or []
            scenarios = suite.get("scenarios") or []
            print(f"{suite['name']}  [{suite.get('_stage', 'shared')}]  "
                  f"({len(cases)} case(s), {len(scenarios)} scenario(s))"
                  f"  {suite.get('description', '')}")
            for case in cases:
                n = len(case.get("assertions") or [])
                h = history.get(f"{suite['name']}/{case.get('id')}", {})
                print(f"   case      {case.get('id', ''):<28s} {case.get('name', '')} "
                      f"({n} assertion(s)) {h.get('last_outcome', '')}")
            for scenario in scenarios:
                h = history.get(f"{suite['name']}/{scenario.get('id')}", {})
                print(f"   {scenario.get('kind', 'api'):<9s} {scenario.get('id', ''):<28s} "
                      f"{scenario.get('name', '')} ({len(scenario.get('steps') or [])} steps)"
                      f" {h.get('last_outcome', '')}")
        return 0

    runner, base_url, env_data, provenance = build_runner(args)
    # widest to narrowest: what the suite means, what this server holds,
    # what this run overrides
    overrides = dict(env_data)
    for raw in args.var:
        name, _, value = raw.partition("=")
        overrides[name.strip()] = value
    kinds = set(args.kind) if args.kind else None

    suites = load_suites(args.dir, args.suite, args.drafts, args.drafts_only)
    if not suites:
        print(f"no suites to run in {Path(args.dir or TESTS_DIR)}")
        return 0

    if args.require_locked_spec:
        if not provenance or provenance["state"] != "match":
            print("refusing to run: " + ((provenance or {}).get("message")
                                         or "no spec provenance available"))
            print("  --require-locked-spec means results must be traceable to the "
                  "pinned document.")
            return 1
        print(f"spec: {provenance['digest'][:12]}… matches the lock")
    target = f"{args.env} ({base_url})" if args.env else base_url
    print(f"running against {target}")
    history_before = load_history()
    all_results = []
    for suite in suites:
        if overrides:
            suite = {**suite, "data": {**(suite.get("data") or {}), **overrides}}
        results = runner.run_suite(suite, args.only, kinds, args.tag)
        for item in results:
            if item["outcome"] != PASS:
                key = f"{suite['name']}/{item.get('id') or item.get('name')}"
                item["verdict"] = verdict.attribute(
                    item, runner.spec, history_before.get(key), provenance)
        if results:
            print_results(suite["name"], results, args.verbose)
            all_results.append((suite["name"], results))

    record_history(all_results, base_url, args.env, (provenance or {}).get("digest"))
    args.base_url = base_url
    counts = summarise(all_results, args)
    if args.json:
        Path(args.json).write_text(json.dumps(
            {"env": args.env, "base_url": base_url,
             "ran_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "spec": provenance,          # which document this result is about
             "summary": counts,
             "suites": [{"name": n, "results": r} for n, r in all_results]},
            indent=2, default=str) + "\n")
        print(f"JSON report:  {args.json}")
    if args.junit:
        write_junit(all_results, args.junit, args.env, base_url,
                    (provenance or {}).get("digest"))
        print(f"JUnit report: {args.junit}")
    return 1 if (counts.get(FAIL) or counts.get(ERROR) or counts.get(BLOCKED)) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:                       # a message, not a stack trace
        print(f"{type(exc).__name__}: {exc}")
        sys.exit(2)
