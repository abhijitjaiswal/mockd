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
    # Stable for the whole run, different every run. $uuid gives a fresh value
    # every time it is read, which is wrong when two steps must agree on the
    # same name — and it leaves nothing to identify a run's leftovers by.
    "$runId": lambda: RUN_ID,
}

RUN_ID = f"run-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"

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
    is different from a field that is present and null.

    `.length` on a list is understood, because that is what `bindable` offers
    for a list — "how many came back" is usually the thing worth asserting."""
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
            if key == "length" and isinstance(node, list):
                node = len(node)
                continue
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


# Operators whose whole job is to talk about absence. Every other one is being
# handed a value that is not there, and should say so rather than print the
# sentinel — "got <object object at 0x104311020>" is the tool leaking its own
# internals at the exact moment somebody needs an answer.
def _show(value):
    """A value as a person should read it — never the sentinel's repr."""
    return "nothing" if value is MISSING else repr(value)


ABSENCE_OPS = ("exists", "not_exists", "is_null", "not_null")


def _compare(op, actual, expected):
    """Returns (ok, explanation-if-not)."""
    if actual is MISSING and op not in ABSENCE_OPS:
        return False, "there is nothing at that path"
    try:
        if op in ("equals", "eq"):
            return actual == expected, f"expected {expected!r}, got {actual!r}"
        if op in ("not_equals", "ne"):
            return actual != expected, f"expected anything but {expected!r}"
        if op == "exists":
            return actual is not MISSING, "not present in the response"
        if op == "not_exists":
            return actual is MISSING, f"present, with value {_show(actual)}"
        if op == "is_null":
            return actual is None, f"expected null, got {_show(actual)}"
        if op == "not_null":
            return actual is not None and actual is not MISSING, "is null or missing"
        if op == "type":
            kinds = {"string": str, "number": (int, float), "integer": int,
                     "boolean": bool, "array": list, "object": dict, "null": type(None)}
            asked = TYPE_ALIASES.get(str(expected).strip().lower(),
                                     str(expected).strip().lower())
            want = kinds.get(asked)
            if want is None:
                # A project's idea of a type is narrower than JSON's: an id is
                # a uuid, not merely a string. Checking the format is the
                # assertion people actually mean.
                checked = check_format(asked, actual)
                if checked is not None:
                    return checked, (f"expected {asked}, got {json_type_name(actual)}"
                                     if not checked else f"is a valid {asked}")
                return False, (f"unknown type {expected!r} — JSON has "
                               f"{', '.join(kinds)}; formats: "
                               f"{', '.join(sorted(FORMATS))}")
            if actual is MISSING:
                # _compare only sees the value; the path and the response are
                # the caller's to name, so keep this plain and let evaluate()
                # add what is actually there.
                return False, "there is nothing at that path, so it has no type to check"
            if asked == "number" and isinstance(actual, bool):
                # true is not 1 here: a flag passing a numeric check is the kind
                # of thing a type assertion exists to catch
                return False, "expected number, got boolean"
            # report the JSON name, not Python's: being told a string is a
            # `str` answers a question nobody asked
            return isinstance(actual, want), \
                f"expected {asked}, got {json_type_name(actual)}"
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
            if isinstance(actual, (list, dict)):
                # Comparing a collection with > almost always means "how many",
                # and the alternative is a float() TypeError naming a Python
                # builtin — which says nothing about what to write instead.
                what = (f"a list of {len(actual)} item(s)" if isinstance(actual, list)
                        else f"an object with {len(actual)} key(s)")
                friendly = {"gt": "length_gte", "gte": "length_gte",
                            "lt": "length", "lte": "length"}[op]
                tail = (" or put `.length` on the end of the path and keep "
                        f"{op}" if isinstance(actual, list) else "")
                return False, (
                    f"this is {what}, not a number, so it cannot be compared with {op}. "
                    f"To check how many, use `{friendly}` on the same path{tail}.")
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


# People write the type in whatever language they think in. The assertion is
# about JSON, so accept the common spellings and answer in JSON's words.
# JSON's seven types answer "is this a string"; a project usually wants "is
# this a uuid". These are the formats worth naming, checked properly rather
# than by eye. Anything else a team writes is refused with the list, because
# silently passing an assertion nobody can evaluate is worse than saying no.
FORMATS = {
    "uuid": r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
    "email": r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    "date": r"^\d{4}-\d{2}-\d{2}$",
    "date-time": r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}",
    "time": r"^\d{2}:\d{2}:\d{2}",
    "url": r"^https?://[^\s]+$",
    "uri": r"^[a-zA-Z][a-zA-Z0-9+.-]*:[^\s]+$",
    "ipv4": r"^(\d{1,3}\.){3}\d{1,3}$",
    "slug": r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    "numeric-string": r"^-?\d+(\.\d+)?$",
}


def check_format(name, value):
    """True/False if `name` is a format we can check, None if we cannot.

    None matters: it is the difference between "this failed" and "nobody can
    say", and only the second deserves to be refused as unknown."""
    pattern = FORMATS.get(str(name).strip().lower())
    if pattern is None:
        return None
    if not isinstance(value, str):
        return False
    return bool(re.match(pattern, value))


TYPE_ALIASES = {
    "str": "string", "text": "string",
    "int": "integer", "long": "integer",
    "float": "number", "double": "number", "decimal": "number", "num": "number",
    "bool": "boolean",
    "list": "array", "arr": "array",
    "dict": "object", "map": "object", "obj": "object",
    "none": "null", "nil": "null", "nonetype": "null",
}


def json_type_name(value):
    if value is MISSING:
        # Reporting the sentinel's Python type said "object", which sent people
        # looking for a type problem when the path simply found nothing.
        return "nothing — that path does not exist in the response"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def applies_here(assertion, env_name):
    """Whether this assertion runs against this environment.

    Most assertions hold everywhere and say nothing. The exceptions are the
    ones worth naming: a count that is only meaningful on a seeded server, a
    field only production returns. Scoping them beats the alternatives —
    duplicating the whole test per environment, or asserting only what is true
    everywhere, which is usually not much."""
    only = assertion.get("only_on")
    skip = assertion.get("except_on")
    name = (env_name or "").lower()
    if only:
        return name in {str(x).lower() for x in (only if isinstance(only, list) else [only])}
    if skip:
        return name not in {str(x).lower() for x in (skip if isinstance(skip, list) else [skip])}
    return True


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
        # A path that found nothing is a different problem from a value that
        # was wrong, and the response is right here to say what IS there.
        if not ok and actual is MISSING and op not in ("not_exists", "is_null"):
            nearby = rebind_candidates(result["json"], path)
            if nearby:
                why = (why + ". The response does have "
                       + ", ".join(c["path"] for c in nearby[:3]))
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


def suite_module(suite, path=None):
    """A suite covers one part of the API. Its own `module`, else its file name —
    so an existing suite is already classified without anybody editing it."""
    return suite.get("module") or (Path(path).stem if path else None) or suite.get("name")


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
            doc.setdefault("module", suite_module(doc, path))
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

            # Per environment as well as overall. One slot meant running
            # against dev overwrote the fact that it passes on the mock, and
            # "green here, red there" is the most useful thing a test can tell
            # you — it is the difference between a broken test and a broken
            # environment.
            where = env_name or base_url
            per_env = entry.setdefault("by_env", {})
            seen = per_env.setdefault(where, {"passes": 0, "runs": 0})
            seen["runs"] += 1
            seen["last_outcome"] = item["outcome"]
            seen["last_run"] = stamp
            seen["last_spec"] = spec_digest
            if item["outcome"] == PASS:
                seen["passes"] += 1
                seen["last_pass"] = stamp
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


WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")


class Runner:
    def __init__(self, base_url, headers, spec=None, timeout=30, verbose=True,
                 readonly=False, env_name=None):
        self.base_url = base_url.rstrip("/")
        self.headers = dict(headers or {})
        self.spec = spec
        self.timeout = timeout
        self.verbose = verbose
        # Some servers must never be written to from a test run. Making that a
        # property of the environment means nobody has to remember which suite
        # is safe to point where — the target refuses, rather than the author
        # being careful.
        self.readonly = bool(readonly)
        self.env_name = env_name

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

        if self.readonly and method in WRITE_METHODS:
            where = f" ({self.env_name})" if self.env_name else ""
            return {
                "name": step.get("name") or f"{method} {path}",
                "role": step.get("role", "target"),
                "request": {"method": method, "url": self.base_url + str(path), "body": body},
                "status": None, "ms": 0,
                "checks": [{"ok": False, "label": f"{method} refused",
                            "detail": f"this environment{where} is read-only, so nothing "
                                      f"here may create, change or delete anything. Run "
                                      f"write tests against a server where that is safe."}],
                "captured": {}, "outcome": SKIPPED, "response_excerpt": "",
                "response_json": None, "bindable": [], "refused": True,
            }

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
                # Two kinds of environment difference, and they want different
                # answers. "The same check, a different number" is a variable:
                # the assertion is shared and only its value moves, so it is
                # interpolated from the environment's own data. "This check
                # only makes sense there" is a different assertion, and says so
                # with only_on / except_on.
                if not applies_here(assertion, self.env_name):
                    continue
                ok, label, detail = evaluate(interpolate(assertion, scope, strict=False),
                                             result, spec_check)
                checks.append({"ok": ok, "label": label, "detail": "" if ok else detail})

        captured = {}
        for name, path_expr in (step.get("capture") or {}).items():
            value = dig(result["json"], path_expr) if result["json"] is not None else MISSING
            if value is MISSING:
                # "not found" leaves the reader diffing two JSON blobs by eye.
                # The response is right here, so say what it DOES have at that
                # place — a renamed field is then a one-word fix rather than an
                # investigation.
                instead = rebind_candidates(result["json"], path_expr)
                hint = ""
                if instead:
                    shown = ", ".join(c["path"] for c in instead[:3])
                    hint = (f". The response does have {shown} — if the field was renamed, "
                            f"point this capture at the new one")
                checks.append({"ok": False, "label": f"capture {name}",
                               "detail": f"{path_expr} not found in the response — later "
                                         f"steps that use {{{{{name}}}}} cannot run{hint}",
                               "rebind": {"name": name, "was": path_expr,
                                          "candidates": instead[:6]}})
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
            # the parsed body and what could be carried out of it, so the
            # workbench can offer a binding instead of asking for a JSON path
            "response_json": result["json"] if _small_enough(result["json"]) else None,
            "bindable": bindable(result["json"], path) if result["json"] is not None else [],
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
                "levels": levels_of(case), "module": case.get("module"),
                "outcome": outcome["outcome"], "steps": [outcome]}

    def run_scenario(self, scenario, data):
        scope = dict(data)
        scope.update(interpolate(scenario.get("data") or {}, scope, strict=False))
        kind = scenario.get("kind", "api")
        steps, outcome = [], PASS
        blocked_by = None

        declared = scenario.get("steps") or []
        # BLOCKED means "the endpoint under test never ran". A flow where every
        # step is labelled setup has no endpoint under test, so a failure there
        # reported as blocked forever — telling the reader to go fix a setup
        # that IS the test. The last step is the target when nothing else
        # claims to be.
        has_target = any(st.get("role") == "target" for st in declared)
        last_index = len(declared) - 1

        for index, step in enumerate(declared):
            if kind == "e2e":
                # every step is the point in an end-to-end flow; calling them
                # "setup" would imply they are scaffolding
                role = step.get("role") or "step"
            elif not has_target and index == last_index:
                role = "target"
            else:
                role = step.get("role") or (
                    "target" if index == last_index else "setup")
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
                "levels": levels_of(scenario), "module": scenario.get("module"),
                "outcome": outcome, "steps": steps, "cleanup": cleanup,
                "blocked_by": blocked_by if outcome == BLOCKED else None}

    # -- a whole suite -----------------------------------------------------
    def selects(self, test, suite, only=None, tags=None, levels=None, modules=None):
        """Every filter in one place, so a case and a scenario cannot drift
        apart in which runs they appear in."""
        if only and not re.search(only, f"{test.get('id', '')} {test.get('name', '')}"):
            return False
        if tags and not (set(tags) & set(test.get("tags", []))):
            return False
        if levels and not (set(levels) & set(levels_of(test))):
            return False
        if modules and module_of(test, suite).lower() not in {m.lower() for m in modules}:
            return False
        return True

    def run_suite(self, suite, only=None, kinds=None, tags=None, levels=None,
                  modules=None):
        data = interpolate(suite.get("data") or {}, {}, strict=False)
        results = []

        for case in suite.get("cases") or []:
            if kinds and "case" not in kinds:
                continue
            if not self.selects(case, suite, only, tags, levels, modules):
                continue
            results.append(self.run_case(case, data))

        for scenario in suite.get("scenarios") or []:
            if kinds and scenario.get("kind", "api") not in kinds:
                continue
            if not self.selects(scenario, suite, only, tags, levels, modules):
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


def write_html(all_results, path, env=None, base_url=None, digest=None):
    """A report a person can open.

    JUnit XML is for a CI server and JSON is for a program; neither is
    something you can send to whoever asked "did it pass on staging?". This
    says what ran, where, against which document, and — for anything that
    failed — which assertion and whose problem it is."""
    from html import escape

    rows, totals = [], {}
    for suite_name, items in all_results:
        for item in items:
            outcome = item.get("outcome", UNKNOWN_OUTCOME)
            totals[outcome] = totals.get(outcome, 0) + 1
            rows.append((suite_name, item, outcome))

    def steps_of(item):
        return item.get("steps") or []

    def detail_html(item):
        out = []
        for step in steps_of(item):
            checks = step.get("checks") or []
            bad = [c for c in checks if not c.get("ok")]
            out.append(
                f"<div class='step {'bad' if bad else 'good'}'>"
                f"<span class='role'>{escape(str(step.get('role', '')))}</span> "
                f"<b>{escape(str(step.get('name', '')))}</b> "
                f"<span class='muted'>{escape(str(step.get('status') or '—'))}"
                f" · {escape(str(step.get('ms', '?')))}ms</span>"
                + "".join(
                    f"<div class='check {'ok' if c.get('ok') else 'no'}'>"
                    f"{'PASS' if c.get('ok') else 'FAIL'} {escape(str(c.get('label', '')))}"
                    + (f" — {escape(str(c.get('detail', '')))}" if not c.get("ok") else "")
                    + "</div>" for c in checks)
                + "</div>")
        v = item.get("verdict")
        if v:
            out.append(
                f"<div class='verdict'><b>{escape(v.get('headline', ''))}</b>"
                + "".join(f"<div class='muted'>{escape(str(e))}</div>"
                          for e in (v.get("evidence") or []))
                + (f"<div><b>Next:</b> {escape(v.get('next') or v.get('guidance') or '')}"
                   f"</div>" if (v.get("next") or v.get("guidance")) else "")
                + "</div>")
        return "".join(out)

    failed = totals.get(FAIL, 0) + totals.get(ERROR, 0)
    summary = "  ".join(f"{n} {k}" for k, n in sorted(totals.items()))
    body = "".join(
        f"<tr class='{outcome}'>"
        f"<td><span class='pill {outcome}'>{outcome}</span></td>"
        f"<td>{escape(suite_name)}</td>"
        f"<td><b>{escape(str(item.get('name') or item.get('id')))}</b>"
        f"<div class='muted'>{escape(str(item.get('id', '')))}"
        + (f" · {escape(', '.join(item.get('levels') or []))}"
           if item.get("levels") else "") + "</div></td>"
        f"<td>{len(steps_of(item))}</td>"
        f"<td><details><summary>show</summary>{detail_html(item)}</details></td></tr>"
        for suite_name, item, outcome in rows)

    html = f"""<!doctype html>
<meta charset="utf-8"><title>Test report — {escape(str(env or base_url or ''))}</title>
<style>
 body{{font:14px/1.5 system-ui,sans-serif;margin:30px auto;max-width:1100px;padding:0 16px;
      color:#1a1a1a}}
 h1{{margin:0 0 4px}} .muted{{color:#666;font-size:12px}}
 table{{border-collapse:collapse;width:100%;margin-top:16px}}
 th,td{{border-bottom:1px solid #e4e4e4;padding:7px 9px;text-align:left;vertical-align:top;
       font-size:13px}}
 th{{background:#f6f6f6}}
 .pill{{font-size:11px;font-weight:700;padding:2px 7px;border-radius:10px;
       border:1px solid currentColor}}
 .pass{{color:#136c34}} .fail,.error{{color:#b3261e}} .blocked{{color:#8a6100}}
 .skipped{{color:#666}}
 .step{{margin:6px 0;padding:6px 8px;border-left:3px solid #ddd;background:#fafafa}}
 .step.bad{{border-color:#b3261e}} .step.good{{border-color:#136c34}}
 .role{{font-size:10px;text-transform:uppercase;color:#666}}
 .check{{font-family:ui-monospace,Menlo,monospace;font-size:11.5px;margin-left:8px}}
 .check.ok{{color:#136c34}} .check.no{{color:#b3261e}}
 .verdict{{margin:6px 0;padding:7px 9px;background:#fff8e6;border:1px solid #f0d9a0;
          border-radius:5px}}
 .tiles{{display:flex;gap:22px;margin:14px 0}} .n{{font-size:24px;font-weight:700}}
 @media(prefers-color-scheme:dark){{
   body{{background:#141414;color:#e8e8e8}} th{{background:#1f1f1f}}
   th,td{{border-color:#333}} .step{{background:#1b1b1b;border-color:#444}}
   .verdict{{background:#241f10;border-color:#4a3c18}}}}
</style>
<h1>Test report</h1>
<p class="muted">
  against <b>{escape(str(env or base_url or 'unknown'))}</b>
  {f"({escape(str(base_url))})" if base_url and env else ""}<br>
  document {escape(str(digest or 'not recorded'))[:16]}<br>
  {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}
</p>
<div class="tiles">
  <div><div class="n pass">{totals.get(PASS, 0)}</div>passed</div>
  <div><div class="n fail">{failed}</div>failed</div>
  <div><div class="n blocked">{totals.get(BLOCKED, 0)}</div>blocked</div>
  <div><div class="n skipped">{totals.get(SKIPPED, 0)}</div>skipped</div>
</div>
<p class="muted">{escape(summary)}
{" · blocked means the endpoint under test never ran, because its setup failed"
 if totals.get(BLOCKED) else ""}</p>
<table>
  <tr><th>Outcome</th><th>Section</th><th>Test</th><th>Steps</th><th>Detail</th></tr>
  {body}
</table>
"""
    Path(path).write_text(html)
    return path


UNKNOWN_OUTCOME = "unknown"


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
        for scope_key in ("only_on", "except_on"):
            value = assertion.get(scope_key)
            if value is not None and not isinstance(value, (str, list)):
                errors.append(f"{where}: `{scope_key}` must be an environment name "
                              f"or a list of them")
        if assertion.get("only_on") and assertion.get("except_on"):
            errors.append(f"{where}: give `only_on` or `except_on`, not both")
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


# ----------------------------------------------------------------------------
# Classification
# ----------------------------------------------------------------------------
#
# "Run the smoke tests for billing against staging" has to be answerable by the
# runner, not by a person remembering which files those are. Free-text tags
# could not answer it: nothing said a tag was a level rather than a note, and
# nothing said which part of the API a test belonged to.
#
# So two axes, deliberately few:
#
#   module   which part of the API — one per suite, because a suite is already
#            a file per area. Defaulted from the operation's own spec tag, so
#            nobody types it.
#   level    how far it runs — a controlled list, because a typo in a free
#            string silently removes a test from the run that was meant to
#            include it.
#
# Anything else a team wants to say stays in `tags`, which is unrestricted.

LEVELS = ("smoke", "sanity", "regression", "negative", "performance")
LEVEL_MEANING = {
    "smoke": "does the surface answer at all — the first thing to run, seconds not minutes",
    "sanity": "an end-to-end flow a person would perform, proving the pieces fit together",
    "regression": "everything that has broken before, and everything that must not",
    "negative": "the API refusing what it should refuse, in the shape it documents",
    "performance": "it answered, but did it answer in time",
}


def levels_of(test):
    """A test's levels, accepting the tags teams already wrote."""
    declared = test.get("levels")
    if declared:
        return [str(x).lower() for x in declared]
    # tags were the only home for this before; honour the ones that are levels
    return [t for t in (test.get("tags") or []) if str(t).lower() in LEVELS] or ["regression"]


def module_of(test, suite):
    """Which part of the API this belongs to."""
    return (test.get("module") or suite.get("module")
            or suite.get("name") or "unsorted")


# ----------------------------------------------------------------------------
# Binding one step's output to the next step's input
# ----------------------------------------------------------------------------
#
# The model has always supported this — capture a value, use {{it}} later. What
# it lacked was any way to discover the path: you had to already know the
# response said `data.id`, and type it. So the workbench runs a step, shows the
# real body, and turns a clicked field into a binding. The name is proposed
# from what was clicked and where it came from, because naming things is the
# part people skip, and `{{departmentId}}` reads in a way `{{id2}}` never will.

ID_KEYS = ("id", "uuid", "guid", "key", "code", "token", "slug", "number", "ref")
NOISE = ("api", "v1", "v2", "v3", "list", "read", "create", "update", "delete",
         "search", "all", "get")


def _id_shaped(key):
    """Does this field name identify something?

    Deliberately a little generous: `identifier`, `userId` and `pk` all name a
    thing, and the cost of offering one candidate too many is a glance, while
    the cost of missing the right one is a hunt through the response."""
    raw = str(key)
    lowered = raw.lower()
    if lowered in ID_KEYS or lowered in ("pk", "_id"):
        return True
    if "identifier" in lowered:
        return True
    # a real word boundary, or "valid" and "width" come along for the ride
    return bool(re.search(r"(_id|[a-z0-9]Id)$", raw))


def _resource_from_path(request_path):
    """'/api/v1/recruitment-settings/positions/departments' -> 'department'."""
    parts = [p for p in str(request_path or "").split("/")
             if p and not p.startswith("{") and p.lower() not in NOISE]
    if not parts:
        return ""
    word = re.split(r"[-_]", parts[-1])[-1]
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def _camel_join(*words):
    parts = [w for chunk in words for w in re.split(r"[^A-Za-z0-9]+", str(chunk)) if w]
    if not parts:
        return "value"
    head, *rest = parts
    return head[:1].lower() + head[1:] + "".join(w[:1].upper() + w[1:] for w in rest)


def rebind_candidates(payload, missing_path):
    """Where a value that used to be at `missing_path` might have moved to.

    Ranked by how little would have to change: the same leaf name somewhere
    else, then the same container holding something id-shaped, then anything
    id-shaped at all. This is the whole maintenance story — a field rename
    should cost one edit, not an afternoon."""
    if payload is None:
        return []
    leaf = str(missing_path).split(".")[-1]
    leaf = re.sub(r"\[\d+\]$", "", leaf).lower()
    container = ".".join(str(missing_path).split(".")[:-1])

    ranked = []
    for entry in bindable(payload):
        path = entry["path"]
        tail = re.sub(r"\[\d+\]$", "", path.split(".")[-1]).lower()
        same_container = ".".join(path.split(".")[:-1]) == container
        if tail == leaf:
            score = 0                              # moved, same name
        elif same_container and entry["looks_like_id"]:
            score = 1                              # renamed, same place
        elif same_container:
            score = 2
        elif entry["looks_like_id"]:
            score = 3
        else:
            score = 4                              # nothing alike; still worth seeing
        ranked.append((score, len(path), {**entry, "why": _REBIND_WHY[score]}))
    ranked.sort(key=lambda r: (r[0], r[1]))
    return [r[2] for r in ranked]


_REBIND_WHY = {
    0: "the same field name, in a different place",
    1: "an id in the same place the old one was",
    2: "in the same place the old one was",
    3: "an id elsewhere in the response",
    4: "also in the response",
}


def _small_enough(payload, limit=200_000):
    """A response the browser can render as a clickable tree without stalling."""
    if payload is None:
        return False
    try:
        return len(json.dumps(payload)) <= limit
    except (TypeError, ValueError):
        return False


def binding_name(json_path, request_path="", taken=()):
    """A name a person would have chosen for this value."""
    leaf = str(json_path).split(".")[-1]
    leaf = re.sub(r"\[\d+\]$", "", leaf)
    resource = _resource_from_path(request_path)
    if leaf.lower() in ID_KEYS and resource:
        name = _camel_join(resource, leaf)             # data.id on /departments -> departmentId
    elif resource and leaf.lower().startswith(resource.lower()):
        name = _camel_join(leaf)                       # department_id -> departmentId
    elif resource and leaf.lower() in ("name", "title", "email", "status"):
        name = _camel_join(resource, leaf)             # title on /departments -> departmentTitle
    else:
        name = _camel_join(leaf)

    if name not in taken:
        return name
    n = 2                                          # never silently overwrite a binding
    while f"{name}{n}" in taken:
        n += 1
    return f"{name}{n}"


def bindable(payload, request_path="", prefix="", depth=0, out=None):
    """Every scalar in a response that could be carried into a later step.

    Ids first: they are what a chained step almost always needs, and putting
    them at the top is the difference between clicking once and scrolling."""
    out = [] if out is None else out
    if depth > 4:
        return out
    if isinstance(payload, dict):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, (dict, list)):
                bindable(value, request_path, path, depth + 1, out)
            elif value is not None and not isinstance(value, bool):
                out.append({"path": path, "value": value,
                            "suggested": binding_name(path, request_path),
                            "looks_like_id": _id_shaped(key)})
    elif isinstance(payload, list):
        # How many came back is usually the thing worth asserting on — "there
        # is at least one country" — and without this the only evidence a list
        # was a list at all was an [0] buried in a path.
        out.append({"path": f"{prefix}.length" if prefix else "length",
                    "value": len(payload), "of_list": True,
                    "suggested": binding_name(f"{prefix}Count" if prefix else "count",
                                              request_path),
                    "looks_like_id": False})
        if payload:
            bindable(payload[0], request_path, f"{prefix}[0]", depth + 1, out)
    if not prefix:
        out.sort(key=lambda b: (not b["looks_like_id"], b["path"]))
    return out


def known_types(suites=None):
    """Every type an assertion may name: JSON's own, the formats we can check,
    and any the project has already used successfully.

    The last part is the point. A fixed list goes stale the moment a team
    starts caring about something it does not contain, and a list that only
    grows by editing this file is a list nobody grows."""
    builtin = ["string", "number", "integer", "boolean", "array", "object", "null"]
    formats = sorted(FORMATS)
    used = []
    for suite in (suites or []):
        for item in (suite.get("cases") or []) + (suite.get("scenarios") or []):
            for step in (item.get("steps") or [item]):
                for assertion in (step.get("assertions") or []):
                    if assertion.get("op") != "type":
                        continue
                    value = str(assertion.get("value", "")).strip().lower()
                    if value and value not in builtin and value not in formats:
                        used.append(value)
    return {"json": builtin, "formats": formats,
            "in_use": sorted(set(used)),
            "all": builtin + formats + sorted(set(used))}


def taxonomy(suites):
    """What can be selected, and how much each selection would run.

    The console builds its pickers from this and the CI generator writes the
    same names into a pipeline, so the two can never offer different choices."""
    modules, levels, kinds = {}, {}, {}
    total = 0
    for suite in suites:
        for item in (suite.get("cases") or []):
            _count(item, suite, "case", modules, levels, kinds)
            total += 1
        for item in (suite.get("scenarios") or []):
            _count(item, suite, item.get("kind", "api"), modules, levels, kinds)
            total += 1
    return {
        "total": total,
        "modules": [{"name": k, "tests": v} for k, v in sorted(modules.items())],
        "levels": [{"name": k, "tests": levels.get(k, 0), "means": LEVEL_MEANING[k]}
                   for k in LEVELS],
        "kinds": [{"name": k, "tests": v} for k, v in sorted(kinds.items())],
    }


def _count(item, suite, kind, modules, levels, kinds):
    module = module_of(item, suite)
    modules[module] = modules.get(module, 0) + 1
    kinds[kind] = kinds.get(kind, 0) + 1
    for level in levels_of(item):
        levels[level] = levels.get(level, 0) + 1


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

    for level in (test.get("levels") or []):
        if str(level).lower() not in LEVELS:
            errors.append(f"{ident}: level {level!r} is not one of {', '.join(LEVELS)}. "
                          f"Anything else belongs in `tags`.")

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
    # A whole export — {"suites": [...]} — is the one wrapper a person is most
    # likely to hand back, because it is what the export button produced.
    if isinstance(payload, dict) and isinstance(payload.get("suites"), list):
        merged = {"cases": [], "scenarios": [], "data": {}}
        for suite in payload["suites"]:
            if not isinstance(suite, dict):
                continue
            merged["cases"] += suite.get("cases") or []
            merged["scenarios"] += suite.get("scenarios") or []
            merged["data"].update(suite.get("data") or {})
            suite_name = suite_name or suite.get("name")
        payload = merged
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


def shape_of(payload):
    """{path: json type} for a response — its shape, without its values.

    Two servers answering the same operation rarely disagree about values in a
    way that matters; they disagree about SHAPE, and that is what breaks a
    binding. A test authored against a mock captures data.id because the mock
    invented data.id, and nothing says so until the real server returns
    something else."""
    return {b["path"]: json_type_name(b["value"])
            for b in bindable(payload)} if payload is not None else {}


def compare_shapes(here, there, here_name="here", there_name="there"):
    """What a flow written against one server would find missing on another."""
    a, b = shape_of(here), shape_of(there)
    missing = sorted(set(a) - set(b))
    added = sorted(set(b) - set(a))
    retyped = sorted(path for path in (set(a) & set(b)) if a[path] != b[path])
    return {
        "same": not (missing or added or retyped),
        "only_on_" + here_name: missing,
        "only_on_" + there_name: added,
        "different_type": [{"path": p, here_name: a[p], there_name: b[p]}
                           for p in retyped],
        "counts": {"shared": len(set(a) & set(b)), "missing": len(missing),
                   "added": len(added), "retyped": len(retyped)},
    }


def bindings_at_risk(steps, shape_there):
    """Which captures and assertions would not survive the move.

    This is the question worth answering: not "do the shapes differ" — they
    always do a little — but "does anything this flow depends on disappear"."""
    at_risk = []
    for index, step in enumerate(steps or []):
        for name, path in (step.get("capture") or {}).items():
            if path not in shape_there:
                at_risk.append({"step": index + 1, "kind": "capture",
                                "name": name, "path": path})
        for assertion in (step.get("assertions") or []):
            path = assertion.get("path")
            if assertion.get("type", "jsonpath") == "jsonpath" and path \
                    and path not in shape_there \
                    and assertion.get("op") not in ("not_exists", "is_null"):
                at_risk.append({"step": index + 1, "kind": "assertion",
                                "name": f"{path} {assertion.get('op', 'exists')}",
                                "path": path})
    return at_risk


def operation_brief(route, sample=None, limit=900):
    """One operation's contract, compactly: what it takes and what it returns.

    The briefs used to list operations by name and summary alone, which asks an
    assistant to invent field names — and it will, plausibly, and the tests
    then fail for a reason nobody can see. The schemas and a real response are
    the whole difference between generated tests that run and generated tests
    that look right."""
    lines = [f"{route['key']}"
             + (f"   — {route['summary']}" if route.get("summary") else "")]

    # tolerate a partial route: a brief is a convenience, and crashing while
    # building one is a poor trade for a key that might be absent
    params = route.get("parameters") or []
    required = [p for p in params if p.get("required")]
    optional = [p for p in params if not p.get("required")]
    for group, label in ((required, "required"), (optional, "optional")):
        for prm in group[:8]:
            schema = prm.get("schema") or {}
            kind = schema.get("type") or "?"
            enum = schema.get("enum")
            lines.append(f"    {label} {prm.get('in')} param  {prm.get('name')}: {kind}"
                         + (f"  one of {enum}" if enum else ""))

    media = ((route.get("request_body") or {}).get("content") or {}).get("application/json")
    if media and media.get("schema"):
        lines.append("    request body:")
        lines.append(_indent(json.dumps(media["schema"], indent=2)[:limit], 6))
    elif route["method"] in ("POST", "PUT", "PATCH"):
        lines.append("    request body: the document declares none")

    responses = route.get("responses") or {}
    from mockd import _success_status
    success = _success_status(responses) if responses else "200"
    schema = (((responses.get(success) or {}).get("content") or {})
              .get("application/json") or {}).get("schema")
    if schema:
        lines.append(f"    response {success}:")
        lines.append(_indent(json.dumps(schema, indent=2)[:limit], 6))
    else:
        lines.append(f"    response {success}: NO schema declared — assert only on "
                     f"fields visible in the example, and do not use type \"schema\"")
    others = [str(c) for c in sorted(responses) if str(c) != str(success)]
    if others:
        lines.append(f"    also documents: {', '.join(others)}")
    if sample is not None:
        lines.append("    a real response:")
        lines.append(_indent(json.dumps(sample, indent=2)[:limit], 6))
    return "\n".join(lines)


def _indent(text, spaces):
    pad = " " * spaces
    return "\n".join(pad + line for line in str(text).splitlines())


def more_like_pack(spec, suites, module=None, limit=4, samples=None):
    """A brief for "write more tests like the ones we already have".

    story_pack starts from a requirement; this starts from the team's own work,
    which is the better starting point once there IS any: the house style is
    already decided, and what is missing is coverage rather than direction. So
    show real examples, then name the operations nothing touches yet — that gap
    is the whole request, and spelling it out beats asking for "more tests"."""
    examples, covered_paths, ids = [], set(), []
    for suite in suites:
        if module and (suite.get("module") or suite.get("name")) != module:
            continue
        for item in (suite.get("cases") or []) + (suite.get("scenarios") or []):
            ids.append(item.get("id"))
            for step in (item.get("steps") or [item]):
                request = step.get("request") or {}
                if request.get("path"):
                    covered_paths.add((str(request.get("method", "GET")).upper(),
                                       request["path"]))
            if len(examples) < limit:
                examples.append(item)

    untouched = []
    for route in (spec.routes if spec is not None else []):
        if module and module.lower() not in (
                [t.lower() for t in (route.get("tags") or [])] + [route["path"].lower()]):
            continue
        hit = any(m == route["method"] and _same_shape(p, route["path"])
                  for m, p in covered_paths)
        if not hit:
            untouched.append(route)

    lines = [
        "Write more API tests for a project that already has some.",
        "Match the style of the examples: same shapes, same naming, same care "
        "about what is asserted.",
        "",
        "WHAT THIS PROJECT'S TESTS LOOK LIKE",
        "-" * 70,
        json.dumps(examples, indent=2) if examples else "(none yet)",
        "",
    ]
    if untouched:
        lines += ["OPERATIONS NOTHING COVERS YET — this is the gap to fill",
                  "-" * 70]
        for route in untouched[:12]:
            lines.append(operation_brief(route, (samples or {}).get(route["key"])))
            lines.append("")
        if len(untouched) > 12:
            lines.append(f"... and {len(untouched) - 12} more operations with no coverage")
            lines.append("")
    else:
        lines += ["Every operation is touched by something already. Aim at the cases "
                  "around them instead: the documented failures, the empty list, the "
                  "value at its limit.", ""]

    lines += [GRAMMAR, ""]
    if ids:
        lines += ["IDS ALREADY TAKEN — yours must not collide", "-" * 70,
                  ", ".join(sorted(i for i in ids if i)), ""]
    lines += [
        "WHAT TO RETURN",
        "-" * 70,
        "A JSON array of cases and scenarios, nothing else.",
        "  * every test needs `levels`, one of: " + ", ".join(LEVELS),
        "  * a value that must differ between runs uses {{$uuid}} or {{$runId}}",
        "  * anything created gets a cleanup step",
        "  * assert on what the response means, not only that it arrived",
    ]
    return "\n".join(lines)


def _same_shape(a, b):
    """/x/{id} and /x/{{thing}} and /x/abc all describe the same route."""
    norm = lambda p: re.sub(r"\{\{?[^}]+\}?\}", "{}", str(p))
    return norm(a) == norm(b)


def relevant_routes(spec, story, limit=8):
    """The operations a story is probably about.

    Separate from the brief so a caller can fetch real responses for exactly
    these — sampling some other six operations puts the wrong data in front of
    the assistant, which is worse than none."""
    words = {w for w in re.split(r"[^A-Za-z0-9]+", (story or "").lower())
             if len(w) > 3 and w not in STORY_STOPWORDS}
    scored = []
    for route in (spec.routes if spec is not None else []):
        haystack = " ".join([route["path"], route.get("summary") or "",
                             " ".join(route.get("tags") or [])]).lower()
        hits = sum(1 for w in words if w in haystack)
        if hits:
            scored.append((hits, route))
    scored.sort(key=lambda pair: (-pair[0], len(pair[1]["path"])))
    return [route for _, route in scored[:limit]]


def story_pack(spec, story, existing=None, limit=6, samples=None):
    """A brief for turning a user story into tests.

    context_pack starts from an operation; this starts from what somebody wants
    the system to do, which is how requirements actually arrive. The work is
    choosing WHICH operations to put in front of the assistant: a whole spec is
    too much to reason about and produces vague tests, so match the story's own
    words against the paths, summaries and tags and send only those.

    What comes back is imported through the same validator as a human's paste,
    into drafts, and earns promotion by passing. Nothing generated is trusted
    because of where it came from."""
    chosen = relevant_routes(spec, story, limit)

    lines = [
        "You are writing API tests for a team that already has a house format.",
        "",
        "THE STORY",
        "-" * 70,
        (story or "").strip(),
        "",
    ]
    if chosen:
        lines += ["OPERATIONS THAT LOOK RELEVANT",
                  "Use these and no others. If the story needs something absent from",
                  "this list, say so instead of inventing an endpoint.",
                  "-" * 70]
        for route in chosen:
            lines.append(operation_brief(route, (samples or {}).get(route["key"])))
            lines.append("")
    else:
        lines += ["NO OPERATION MATCHED THE STORY.",
                  "Say which endpoints you would need rather than guessing at names.",
                  ""]

    lines += [GRAMMAR, ""]
    if existing:
        lines += ["ALREADY COVERED — do not repeat these", "-" * 70]
        lines += [f"  {item}" for item in existing[:40]]
        lines.append("")
    lines += [
        "WHAT TO RETURN",
        "-" * 70,
        "A JSON array of cases and scenarios, nothing else — no prose around it.",
        "Rules that matter here:",
        "  * a value that must differ between runs uses {{$uuid}} or {{$runId}},",
        "    never a constant, or the second run collides with the first",
        "  * anything a flow creates gets a cleanup step that deletes it",
        "  * give every test `levels`, one of: " + ", ".join(LEVELS),
        "  * a flow whose earlier steps only make the target reachable is",
        "    kind \"api\"; a flow where every step matters is kind \"e2e\"",
        "  * assert what the story promises, not merely that a 200 came back",
    ]
    return "\n".join(lines)


STORY_STOPWORDS = {
    "that", "this", "with", "from", "have", "should", "would", "when", "then",
    "given", "want", "need", "able", "user", "users", "system", "they", "their",
    "must", "into", "what", "which", "your", "than", "them", "some", "only",
    "also", "about", "after", "before", "being", "does", "each", "make",
}


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
    readonly = False
    if args.env:
        import environments as envmod
        readonly = envmod.is_readonly(args.env)
    return (Runner(base_url, headers, spec, args.timeout, readonly=readonly,
                   env_name=args.env),
            base_url, env_data, provenance)


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
    brief.add_argument("--story", help="a user story to turn into tests, instead of "
                                       "starting from one operation")
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
    run.add_argument("--level", action="append", choices=list(LEVELS),
                     help="how far to run: smoke, sanity, regression, negative, "
                          "performance. Repeatable.")
    run.add_argument("--module", action="append",
                     help="which part of the API, as named by the suite. Repeatable.")
    run.add_argument("--var", action="append", default=[], metavar="name=value",
                     help="override suite data, repeatable")
    run.add_argument("--timeout", type=int, default=30)
    run.add_argument("--require-locked-spec", action="store_true",
                     help="refuse to run unless the spec matches spec.lock.json — "
                          "what CI should use, so results cannot be produced against "
                          "a substituted document")
    run.add_argument("--json", help="write the full result as JSON")
    run.add_argument("--junit", help="write a JUnit XML report")
    run.add_argument("--html", help="write a report a person can open and send on")
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
        if args.story:
            print(story_pack(spec, args.story, covered))
        else:
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
        results = runner.run_suite(suite, args.only, kinds, args.tag,
                                   args.level, args.module)
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
    if args.html:
        write_html(all_results, args.html, args.env, base_url,
                   (provenance or {}).get("digest"))
        print(f"HTML report: {args.html}")
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
