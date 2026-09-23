#!/usr/bin/env python3
"""
verify.py — drive a REAL API and check it against its own swagger document.

Same spec, same generator, opposite direction: mockd answers as if it were the
API, verify.py asks the API whether it behaves as documented.

For every operation it can reach it checks:

  * the status code came back DOCUMENTED (an undocumented 500 is the classic
    integration bug a mock will never show you)
  * the body VALIDATES against the schema declared for that status — missing
    required fields, wrong types, nulls in non-nullable fields, bad enums
  * the content type matches
  * how long it took

and, where the spec declares no shape at all (`"schema": {}` — 65 of the 96
operations in apis.json), it records the shape it actually observed, so the
gap between doc and reality becomes a list someone can work through.

It fills path params intelligently: list endpoints run first and real ids are
harvested from their responses, so `/user/read/{user_id}` is exercised with an
id that exists rather than a random uuid that 404s.

    # read-only sweep against a real environment
    python verify.py --spec apis.json --base-url https://api.dev.example.com \\
        --header 'Authorization: Bearer eyJ...'

    # include POST/PUT/PATCH/DELETE — writes real data, so it is opt-in
    python verify.py --spec apis.json --base-url ... --header '...' --allow-writes

    # CI gate + machine-readable output
    python verify.py --spec apis.json --base-url ... --header '...' \\
        --report verify_report.json --junit verify_report.xml --fail-on error

    # turn what the real API actually returns into mock payloads
    python verify.py --spec apis.json --base-url ... --header '...' \\
        --learn-overlay learned_overlay.json

Nothing here writes to the spec. --allow-writes is the only flag that can
change state in the target environment.
"""
import argparse
import json
import os
import random
import re
import sys

import project
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from pathlib import Path

from generator import generate_from_schema
from mockd import Source, Spec, _success_status

try:
    from jsonschema import Draft202012Validator as _Validator, FormatChecker
except ImportError:                                            # pragma: no cover
    from jsonschema import Draft7Validator as _Validator, FormatChecker

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
OK, WARN, ERROR, SKIP = "ok", "warning", "error", "skipped"


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------


def detect_target(base_url, headers, timeout=5):
    """Is this a mockd instance or a real backend?

    It matters for exactly one decision: whether running POST/PUT/DELETE is a
    free self-check or a write against somebody's database. mockd answers
    /_mock/routes; nothing else does."""
    try:
        req = urllib.request.Request(base_url.rstrip("/") + "/_mock/routes",
                                     headers=dict(headers))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            json.loads(resp.read())
        return "mock"
    except Exception:
        return "live"



MAX_BODY = 2 * 1024 * 1024          # 2 MB is far more than any JSON response
READ_DEADLINE = 15                  # seconds spent reading an ordinary body
STREAM_DEADLINE = 2                 # a stream never ends; sample it and move on

# Media types that mean "this connection stays open on purpose".
STREAM_TYPES = ("text/event-stream", "application/x-ndjson", "application/stream+json",
                "multipart/x-mixed-replace")


def call_with_budget(method, url, headers, body, timeout, budget):
    """Never let one operation stall the sweep.

    read1() and the read deadline bound the cases we understand. This bounds the
    ones we do not: the call runs on its own thread and, if it overruns, the run
    abandons it and carries on. The thread may linger until its socket gives up,
    but the sweep does not wait for it — a report that arrives with one hole in
    it beats one that never arrives."""
    box = ThreadPoolExecutor(max_workers=1)
    future = box.submit(http_call, method, url, headers, body, timeout)
    try:
        result = future.result(timeout=budget)
        box.shutdown(wait=False)
        return result
    except FuturesTimeout:
        box.shutdown(wait=False)
        return {"status": None, "headers": {}, "raw": b"", "ms": int(budget * 1000),
                "abandoned": True,
                "error": f"no response within {budget:.0f}s — abandoned so the run could "
                         f"continue"}


def is_stream(headers):
    ctype = (headers.get("Content-Type") or headers.get("content-type") or "").lower()
    return any(t in ctype for t in STREAM_TYPES)


def _read_capped(resp, limit=MAX_BODY, seconds=READ_DEADLINE):
    """Read a response body that might never end.

    A streaming endpoint (this spec has /api/v1/user/stream/{user_id}) keeps the
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


def http_call(method, url, headers, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=dict(headers))
    if data is not None:
        req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    started = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            headers = dict(resp.headers)
            streaming = is_stream(headers)
            raw, truncated = _read_capped(
                resp, seconds=STREAM_DEADLINE if streaming else READ_DEADLINE)
            return {"status": resp.status, "headers": headers, "raw": raw,
                    "truncated": truncated, "streaming": streaming,
                    "ms": round((time.time() - started) * 1000)}
    except urllib.error.HTTPError as exc:
        raw, truncated = _read_capped(exc)
        return {"status": exc.code, "headers": dict(exc.headers or {}), "raw": raw,
                "truncated": truncated,
                "ms": round((time.time() - started) * 1000)}
    except Exception as exc:
        return {"status": None, "headers": {}, "raw": b"", "error": f"{type(exc).__name__}: {exc}",
                "ms": round((time.time() - started) * 1000)}


# ----------------------------------------------------------------------------
# Id harvesting — so path params point at rows that exist
# ----------------------------------------------------------------------------


class IdPool:
    """Collects ids seen in responses, keyed by both field name and resource."""

    def __init__(self, fixtures=None):
        self.by_field = {}
        self.by_resource = {}
        self.fixtures = dict(fixtures or {})

    _VERB = re.compile(r"/(create|list|read|update|delete|add|remove|get|all|new|edit)$", re.I)

    @classmethod
    def _resource(cls, path):
        """The collection a path belongs to, precise enough to tell
        /metadata/states from /metadata/cities, loose enough to tie
        /user/list to /user/read/{id}."""
        literal = [p for p in path.strip("/").split("/") if not p.startswith("{")]
        key = "/" + "/".join(literal)
        return cls._VERB.sub("", key) or "/"

    def harvest(self, route, payload, depth=0):
        resource = self._resource(route["path"])
        self._walk(payload, resource)

    def _walk(self, node, resource, depth=0):
        if depth > 6:
            return
        if isinstance(node, list):
            for item in node[:10]:
                self._walk(item, resource, depth + 1)
            return
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            if isinstance(value, (dict, list)):
                self._walk(value, resource, depth + 1)
            elif key == "id" and isinstance(value, (str, int)):
                self.by_resource.setdefault(resource, [])
                if value not in self.by_resource[resource]:
                    self.by_resource[resource].insert(0, value)
                    del self.by_resource[resource][20:]
            elif key.endswith("_id") and isinstance(value, (str, int)):
                self.by_field.setdefault(key, [])
                if value not in self.by_field[key]:
                    self.by_field[key].insert(0, value)
                    del self.by_field[key][20:]

    @staticmethod
    def _is_identifier(param_name, schema):
        """Only substitute harvested values for params that actually name a row.
        `/approvals/mode/{mode}` takes an enum, not an id — feeding it a uuid
        earns a 400 that says nothing about the API."""
        schema = schema or {}
        if schema.get("enum"):
            return False
        if schema.get("type") in ("integer", "number") and not param_name.endswith("_id"):
            return param_name in ("id",)
        return param_name == "id" or param_name.endswith("_id") \
            or schema.get("format") == "uuid"

    def value_for(self, param_name, path, schema, rng):
        """Pinned fixture, then an id this operation's OWN collection returned,
        then one seen elsewhere, then a generated one — and say which, so a 404
        can be read correctly.

        Own-resource first matters: a `role_id` scraped out of a user payload
        points at a role the roles endpoint may never have listed, and the 404
        that follows says nothing about the API."""
        if param_name in self.fixtures:
            return self.fixtures[param_name], "fixture"
        if not self._is_identifier(param_name, schema):
            return generate_from_schema(schema or {"type": "string"}, rng), "generated"

        resource = self._resource(path)
        if self.by_resource.get(resource):
            return self.by_resource[resource][0], "harvested"

        stem = re.sub(r"_id$", "", param_name)
        for other, ids in self.by_resource.items():
            if ids and (other.rstrip("s") == stem.rstrip("s") or stem in other):
                return ids[0], "harvested"
        if self.by_field.get(param_name):
            return self.by_field[param_name][0], "harvested:foreign"
        return generate_from_schema(schema or {"type": "string"}, rng), "generated"


# ----------------------------------------------------------------------------
# Checks
# ----------------------------------------------------------------------------


def observed_shape(node, depth=0):
    """A compact description of what came back, for operations the spec does
    not describe. This is the raw material for filling in response_model."""
    if depth > 4:
        return "..."
    if isinstance(node, dict):
        return {k: observed_shape(v, depth + 1) for k, v in list(node.items())[:40]}
    if isinstance(node, list):
        return [observed_shape(node[0], depth + 1)] if node else []
    if node is None:
        return "null"
    return type(node).__name__


def check_response(route, result, rng):
    """Returns (level, [check dicts], parsed body or None)."""
    checks = []
    status = result.get("status")

    if status is None:
        return ERROR, [{"check": "reachable", "level": ERROR,
                        "detail": result.get("error", "no response")}], None

    if result.get("streaming"):
        checks.append({"check": "streaming", "level": WARN,
                       "detail": f"a streaming response ({result['headers'].get('Content-Type')})"
                                 f" — sampled for {STREAM_DEADLINE}s and closed; a stream has "
                                 f"no end-of-body to validate"})
    elif result.get("truncated"):
        checks.append({"check": "body_complete", "level": WARN,
                       "detail": f"stopped reading after {READ_DEADLINE}s — the body below "
                                 f"is a prefix"})

    documented = {str(k) for k in route["responses"]}
    status_documented = str(status) in documented or "default" in documented
    checks.append({
        "check": "status_documented", "level": OK if status_documented else ERROR,
        "detail": f"{status} is documented" if status_documented else
                  f"{status} is NOT documented; spec declares {sorted(documented)}",
    })

    ctype = (result["headers"].get("Content-Type") or "").split(";")[0].strip()
    body = None
    if result["raw"]:
        try:
            body = json.loads(result["raw"])
        except ValueError:
            checks.append({"check": "json_body", "level": ERROR,
                           "detail": f"content-type {ctype!r}, body is not JSON: "
                                     f"{result['raw'][:120]!r}"})
            return ERROR, checks, None

    resp_spec = route["responses"].get(str(status)) or route["responses"].get("default") or {}
    declared = (resp_spec.get("content") or {})
    if declared and ctype and ctype not in declared:
        checks.append({"check": "content_type", "level": WARN,
                       "detail": f"got {ctype!r}, spec declares {sorted(declared)}"})

    schema = (declared.get("application/json") or {}).get("schema")
    if not status_documented:
        return ERROR, checks, body
    if not schema:
        checks.append({"check": "schema_declared", "level": WARN,
                       "detail": f"spec declares no response schema for {status} — "
                                 f"cannot verify the body"})
        return WARN, checks, body
    if body is None:
        checks.append({"check": "body_present", "level": ERROR,
                       "detail": "spec declares a schema but the response body was empty"})
        return ERROR, checks, body

    violations = []
    validator = _Validator(schema, format_checker=FormatChecker())
    for err in sorted(validator.iter_errors(body), key=lambda e: list(e.absolute_path)):
        violations.append({
            "field": "/".join(str(x) for x in err.absolute_path) or "(root)",
            "error": err.message[:300],
        })
        if len(violations) >= 25:
            break
    if violations:
        checks.append({"check": "schema_valid", "level": ERROR,
                       "detail": f"{len(violations)} field(s) violate the documented schema",
                       "violations": violations})
        return ERROR, checks, body

    checks.append({"check": "schema_valid", "level": OK,
                   "detail": "body matches the documented schema"})
    return OK, checks, body


# ----------------------------------------------------------------------------
# Request building
# ----------------------------------------------------------------------------


def build_request(route, base_url, pool, rng, send_optional_query=False):
    path, notes = route["path"], []
    query = {}
    for p in route["parameters"]:
        name, loc = p.get("name"), p.get("in")
        schema = p.get("schema") or {}
        if loc == "path":
            value, origin = pool.value_for(name, route["path"], schema, rng)
            notes.append(f"{name}={value} ({origin})")
            path = path.replace("{%s}" % name, urllib.parse.quote(str(value), safe=""))
        elif loc == "query" and (p.get("required") or send_optional_query):
            value = generate_from_schema(schema, rng)
            if value is not None and not isinstance(value, (dict, list)):
                query[name] = value

    body = None
    media = (route["request_body"].get("content") or {}).get("application/json")
    if media and media.get("schema"):
        body = generate_from_schema(media["schema"], rng, array_items=1)

    url = base_url.rstrip("/") + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    return url, body, notes


# ----------------------------------------------------------------------------
# Negative probes
# ----------------------------------------------------------------------------


def negative_probes(route, base_url, headers, pool, rng, timeout):
    """Only probes that are safe and that the spec itself says should fail."""
    out = []
    documented = {str(k) for k in route["responses"]}
    url, body, _ = build_request(route, base_url, pool, rng)

    if "401" in documented or "403" in documented:
        stripped = {k: v for k, v in headers.items()
                    if k.lower() not in ("authorization", "cookie", "x-api-key")}
        res = http_call(route["method"], url, stripped, body, timeout)
        got = res.get("status")
        level = OK if got in (401, 403) else ERROR
        out.append({"probe": "no_auth", "expected": "401/403", "status": got, "level": level,
                    "detail": "auth is enforced" if level == OK else
                              f"spec documents 401/403 but an unauthenticated call returned {got}"})

    if route["method"] in WRITE_METHODS and body is not None and ("422" in documented or "400" in documented):
        broken = dict(body) if isinstance(body, dict) else body
        media = (route["request_body"].get("content") or {}).get("application/json") or {}
        required = (media.get("schema") or {}).get("required") or []
        if isinstance(broken, dict) and required:
            dropped = required[0]
            broken = {k: v for k, v in broken.items() if k != dropped}
            res = http_call(route["method"], url, headers, broken, timeout)
            got = res.get("status")
            level = OK if got in (400, 422) else ERROR
            out.append({"probe": "missing_required_field", "field": dropped,
                        "expected": "400/422", "status": got, "level": level,
                        "detail": f"omitting required '{dropped}' was rejected" if level == OK
                                  else f"omitting required '{dropped}' returned {got} — "
                                       f"validation is not enforced"})
    return out


# ----------------------------------------------------------------------------
# Run
# ----------------------------------------------------------------------------


def worst(levels):
    for level in (ERROR, WARN, SKIP, OK):
        if level in levels:
            return level
    return OK


AUTH_HINT = """\
Nothing was authenticated, so every finding below is the same finding.

  * Check the credential first, not the contract. A token that is expired, or
    sent the way this API does not read, produces exactly this shape of report.
  * This spec's own login endpoint (POST /api/v1/auth/dev-login) replies with
    Set-Cookie, and the API reads a COOKIE — an `Authorization: Bearer ...`
    header is ignored by a server like that, and it will still say the token is
    missing. Send `Cookie: <name>=<value>`, or use an environment with
    `auth.mode: "login"` so the run logs in and keeps the cookie jar itself.
  * `python environments.py login <env>` proves the credential on its own,
    before you spend a sweep on it."""


def auth_probe(spec, base_url, headers, timeout):
    """One read before the sweep, to tell a broken credential from a broken API.

    Without this, an unauthenticated run reports every operation as returning an
    undocumented status — dozens of findings that are all one problem, and the
    real one is invisible."""
    route = next((r for r in spec.routes
                  if r["method"] == "GET" and not r["path_params"]
                  and not any(p.get("required") for p in r["parameters"])), None)
    if route is None:
        return None
    result = http_call("GET", base_url.rstrip("/") + route["path"], headers, None, timeout)
    if result.get("status") in (401, 403):
        body = (result.get("raw") or b"")[:160].decode(errors="replace")
        return {"operation": route["key"], "status": result["status"], "body": body}
    return None


_SAY_LOCK = threading.Lock()


def say(text):
    """One whole line, written once, under a lock.

    print() writes the text and the newline as two separate calls. Phase 2 runs
    four operations at a time, so another thread's line lands between them and
    the two come out spliced together — which also breaks the console's parser,
    leaving rows stuck as "in flight" for calls that finished long ago."""
    with _SAY_LOCK:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()


def run(spec, base_url, headers, args):
    pool = IdPool(json.loads(Path(args.fixtures).read_text()) if args.fixtures else None)
    skip = {s.strip() for s in (args.skip or [])}
    stream_words = re.compile(r"stream|sse|watch|subscribe|events", re.I)

    def deselected(route):
        if route["key"] in skip:
            return "deselected"
        if args.skip_streaming and stream_words.search(
                route["path"] + " " + (route["summary"] or "")):
            return "looks like a stream"
        return None

    matched = [r for r in spec.routes
               if (not args.only or re.search(args.only, r["key"]))
               and (not args.exclude or not re.search(args.exclude, r["key"]))]
    dropped = [(r, deselected(r)) for r in matched if deselected(r)]
    routes = [r for r in matched if not deselected(r)]
    for route, why in dropped:
        say(f"  SKIP  {route['key']}   {why}")

    readonly = [r for r in routes if r["method"] not in WRITE_METHODS]
    writes = [r for r in routes if r["method"] in WRITE_METHODS]

    # list-ish GETs first: their responses are where real ids come from
    def is_list(r):
        tail = r["path"].rstrip("/").split("/")[-1]
        return not tail.startswith("{")
    phase1 = [r for r in readonly if is_list(r)]
    phase2 = [r for r in readonly if not is_list(r)]

    results = []

    def execute(route):
        rng = random.Random(route["key"])
        url, body, notes = build_request(route, base_url, pool, rng, args.optional_query)
        # printed before the call, so a slow or hung operation is identified by
        # name while it is still running rather than after it finishes
        say(f"  ....  {route['key']}")
        budget = args.timeout + READ_DEADLINE + 5
        result = call_with_budget(route["method"], url, headers, body,
                                  args.timeout, budget)
        level, checks, parsed = check_response(route, result, rng)
        if parsed is not None and result.get("status", 500) < 300:
            pool.harvest(route, parsed)

        record = {
            "operation": route["key"], "summary": route["summary"], "tags": route["tags"],
            "request": {"method": route["method"], "url": url,
                        "path_params": notes, "body": body},
            "status": result.get("status"), "duration_ms": result.get("ms"),
            "bytes": len(result.get("raw") or b""),
            "truncated": bool(result.get("truncated")),
            "level": level, "checks": checks,
            "documented_statuses": sorted(str(k) for k in route["responses"]),
        }
        if parsed is not None:
            record["observed_shape"] = observed_shape(parsed)
            if args.learn_overlay or args.keep_bodies:
                record["body"] = parsed
        if args.negative:
            probes = negative_probes(route, base_url, headers, pool, rng, args.timeout)
            if probes:
                record["negative_probes"] = probes
                record["level"] = worst([record["level"]] + [p["level"] for p in probes])
        return record

    # the shortest honest reason for a non-OK line, so a row is readable on its
    # own — "WARN" with nothing after it tells a reader nothing
    REASON = {
        "schema_declared": "spec declares no response schema — nothing to check",
        "content_type": "content type is not what the spec declares",
        "body_complete": "streaming endpoint — body truncated",
        "status_documented": "status is not documented",
        "schema_valid": "body breaks its own schema",
        "json_body": "body is not JSON",
        "body_present": "empty body where a schema is declared",
        "reachable": "no response",
    }

    def report_line(rec):
        mark = {OK: "PASS", WARN: "WARN", ERROR: "FAIL", SKIP: "SKIP"}[rec["level"]]
        size = rec.get("bytes")
        big = f"   {size // 1024}KB" if size and size > 64 * 1024 else ""
        why = big
        if rec["level"] != OK:
            bad = next((c for c in rec.get("checks", []) if c["level"] == rec["level"]), None)
            if bad:
                why = "   " + REASON.get(bad["check"], bad["check"])
        lines = [f"  {mark}  {rec['status'] or '---':>4}  {rec['duration_ms']:>5}ms  "
                 f"{rec['operation']}{why}"]
        if rec["level"] != OK and not args.quiet:
            for c in rec["checks"]:
                if c["level"] != OK:
                    lines.append(f"          {c['check']}: {c['detail']}")
                    for v in (c.get("violations") or [])[:6]:
                        lines.append(f"            - {v['field']}: {v['error']}")
            for probe in rec.get("negative_probes", []):
                if probe["level"] != OK:
                    lines.append(f"          probe {probe['probe']}: {probe['detail']}")
        say("\n".join(lines))

    blocked = auth_probe(spec, base_url, headers, args.timeout) if args.target == "live" \
        else None
    if blocked:
        print(f"\nAUTHENTICATION FAILED — {blocked['status']} on {blocked['operation']}")
        print(f"  the server said: {blocked['body']}")
        print(AUTH_HINT)
        if not args.ignore_auth_probe:
            print("\nStopping before the sweep. Re-run with --ignore-auth-probe to check "
                  "anyway.")
            return []
        print("\n--ignore-auth-probe given; sweeping anyway.\n")

    # the denominator has to be what will actually be reported, or the counter
    # can never reach it: a read-only sweep never runs the writes
    will_run = len(phase1) + len(phase2) + (len(writes) if args.allow_writes else 0)
    print(f"verifying {len(routes)} operations against {base_url}")
    say(f"  running now: {will_run}"
        + ("" if args.allow_writes
           else f" ({len(writes)} write operation(s) need --allow-writes)"))
    print(f"  phase 1: {len(phase1)} collection reads (harvesting ids)")
    for route in phase1:
        rec = execute(route)
        results.append(rec)
        report_line(rec)

    harvested = sum(len(v) for v in pool.by_resource.values()) + \
        sum(len(v) for v in pool.by_field.values())
    print(f"  phase 2: {len(phase2)} item reads ({harvested} ids harvested)")
    if args.concurrency > 1:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool_exec:
            for rec in pool_exec.map(execute, phase2):
                results.append(rec)
                report_line(rec)
    else:
        for route in phase2:
            rec = execute(route)
            results.append(rec)
            report_line(rec)

    if writes:
        if args.allow_writes:
            note = "in-memory, nothing real is touched" if args.target == "mock" \
                else "--allow-writes: these change real data"
            print(f"  phase 3: {len(writes)} write operations ({note})")
            for route in writes:
                rec = execute(route)
                results.append(rec)
                report_line(rec)
        else:
            print(f"  phase 3: {len(writes)} write operations SKIPPED "
                  f"(pass --allow-writes to run them against a real environment)")
            for route in writes:
                results.append({"operation": route["key"], "summary": route["summary"],
                                "tags": route["tags"], "level": SKIP, "status": None,
                                "duration_ms": 0, "checks": [{"check": "skipped", "level": SKIP,
                                "detail": "write method; --allow-writes not given"}],
                                "documented_statuses":
                                    sorted(str(k) for k in route["responses"])})
    return results


# ----------------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------------


def summarise(spec, results, args):
    if not results:
        return {}
    """Say what the numbers mean. "37 warning" tells a reader nothing about
    whether their API works — and the honest answer is that most of those
    endpoints answered fine, there is just nothing in the spec to check them
    against."""
    counts = {}
    for r in results:
        counts[r["level"]] = counts.get(r["level"], 0) + 1

    ran = [r for r in results if r["level"] != SKIP]
    skipped = [r for r in results if r["level"] == SKIP]
    verified = [r for r in ran if r["level"] == OK]
    failed = [r for r in ran if r["level"] == ERROR]
    unverifiable = [r for r in ran
                    if any(c["check"] == "schema_declared" and c["level"] == WARN
                           for c in r["checks"])]
    other_warnings = [r for r in ran if r["level"] == WARN and r not in unverifiable]
    answered = [r for r in unverifiable if (r.get("status") or 500) < 400]

    print("\n" + "=" * 72)
    if args.target == "mock":
        print("SELF-CHECK — the mock answering its own spec, not a real backend.")
    else:
        print(f"LIVE CHECK — {args.base_url}")
    print(f"{len(results)} operations in the spec")
    print(f"  {len(ran)} exercised")
    print(f"     {len(verified):3d} verified      status is documented AND the body validates "
          f"against the declared schema")
    print(f"     {len(unverifiable):3d} unverifiable  the spec declares no response schema — "
          f"nothing to check the body against")
    if answered:
        print(f"         of those, {len(answered)} answered with a success status anyway; "
              f"they are not broken, just unprovable")
    if other_warnings:
        print(f"     {len(other_warnings):3d} warning       content type or other mismatch "
              f"(see the report)")
    print(f"     {len(failed):3d} failed        undocumented status, or the body breaks "
          f"its own schema")
    if skipped:
        why = ("POST/PUT/PATCH/DELETE — pass --allow-writes to include them "
               "(safe here: the mock's store is in memory)" if args.target == "mock"
               else "POST/PUT/PATCH/DELETE change real data in this environment; "
                    "pass --allow-writes to include them")
        print(f"  {len(skipped)} not run       {why}")

    undocumented = [r for r in results
                    if any(c["check"] == "status_documented" and c["level"] == ERROR
                           for c in r["checks"])]
    invalid = [r for r in results
               if any(c["check"] == "schema_valid" and c["level"] == ERROR for c in r["checks"])]

    if undocumented:
        print(f"\nUNDOCUMENTED STATUS ({len(undocumented)}) — the API returned something the "
              f"swagger doc does not mention:")
        for r in undocumented:
            print(f"  {r['status']}  {r['operation']}  (documented: "
                  f"{', '.join(r['documented_statuses'])})")
    if invalid:
        print(f"\nCONTRACT VIOLATIONS ({len(invalid)}) — the body does not match its own schema:")
        for r in invalid:
            for c in r["checks"]:
                if c["check"] == "schema_valid" and c["level"] == ERROR:
                    print(f"  {r['operation']}")
                    for v in c["violations"][:5]:
                        print(f"      {v['field']}: {v['error']}")
    if unverifiable:
        print(f"\nUNVERIFIABLE ({len(unverifiable)}) — no response schema in the spec, so this "
              f"run could prove nothing about these bodies.")
        print("Adding a response_model to these routes is what turns them into real coverage:")
        for r in unverifiable[:20]:
            print(f"  {r['status'] or '---'}  {r['operation']}")
        if len(unverifiable) > 20:
            print(f"  ... and {len(unverifiable) - 20} more (see the JSON report)")

    slow = sorted((r for r in results if r.get("duration_ms")),
                  key=lambda r: -r["duration_ms"])[:5]
    if slow and slow[0]["duration_ms"] > 500:
        print("\nSLOWEST:")
        for r in slow:
            print(f"  {r['duration_ms']:>6}ms  {r['operation']}")
    return counts


def write_junit(results, path):
    def esc(s):
        return (str(s).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))
    failures = sum(1 for r in results if r["level"] == ERROR)
    skipped = sum(1 for r in results if r["level"] == SKIP)
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             f'<testsuite name="openapi-contract" tests="{len(results)}" '
             f'failures="{failures}" skipped="{skipped}">']
    for r in results:
        classname = esc((r.get("tags") or ["api"])[0])
        lines.append(f'  <testcase classname="{classname}" name="{esc(r["operation"])}" '
                     f'time="{(r.get("duration_ms") or 0) / 1000:.3f}">')
        if r["level"] == ERROR:
            detail = "; ".join(f"{c['check']}: {c['detail']}"
                               for c in r["checks"] if c["level"] == ERROR)
            lines.append(f'    <failure message="{esc(detail)[:500]}">{esc(json.dumps(r["checks"], indent=1))}</failure>')
        elif r["level"] == SKIP:
            lines.append('    <skipped/>')
        lines.append("  </testcase>")
    lines.append("</testsuite>")
    Path(path).write_text("\n".join(lines) + "\n")


def write_learned_overlay(spec, results, path):
    """Real observed responses -> a mockd overlay. The mock then replays what
    the API genuinely returns, including the 65 operations the spec never
    described."""
    by_key = {r["operation"]: r for r in results}
    operations = {}
    for route in spec.routes:
        rec = by_key.get(route["key"])
        if not rec or "body" not in rec or rec.get("status") is None:
            continue
        # these bodies came off a real environment and passed their schema, so
        # they are the strongest evidence available short of the spec itself
        trusted = rec.get("level") in ("ok", "warning")
        operations[route["key"]] = {
            "summary": route["summary"],
            "status": "verified" if trusted else "proposed",
            "responses": {str(rec["status"]): {"body": rec["body"],
                                               "_origin": "observed",
                                               "_observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ")}},
        }
    doc = {"_meta": {"generated_from": "live API responses via verify.py",
                     "operations": len(operations),
                     "note": "Captured from a real environment. Review before sharing — "
                             "responses may contain real data."},
           "operations": operations}
    Path(path).write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    return len(operations)


def mock_spec_mismatch(base_url, spec_text, operations):
    """None if the mock is serving this document, else what to tell the reader."""
    import hashlib
    import json as _json
    try:
        with urllib.request.urlopen(base_url.rstrip("/") + "/_mock/drift", timeout=3) as resp:
            drift = _json.loads(resp.read().decode())
    except Exception:
        return None                      # not our mock, or not reachable: not our business
    theirs = drift.get("spec_digest")
    if not theirs:
        return None                      # an older mock that cannot say
    if theirs == hashlib.sha256(spec_text.encode()).hexdigest():
        return None
    served = (drift.get("spec_source") or {}).get("location") or "something else"
    return (
        f"The mock is serving a different document.\n"
        f"  the mock loaded : {served} ({drift.get('spec_operations')} operations)\n"
        f"  this run is using: {operations} operations\n"
        f"Checking one against the other would report failures that are neither "
        f"side's fault.\nEither restart the mock on this document, or pass the one "
        f"it is serving with --spec."
    )


def main():
    ap = argparse.ArgumentParser(
        description="Verify a live API against its OpenAPI document",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spec", default=None,
                    help="Path or http(s) URL of the spec "
                         "(default: the project spec — see project.py)")
    ap.add_argument("--base-url", help="Root of the API under test "
                                       "(or use --env to take it from environments.json)")
    ap.add_argument("--env", help="Named environment from environments.json — supplies the "
                                  "base URL and performs the login, so no token is typed "
                                  "on a command line")
    ap.add_argument("--header", action="append", default=[], metavar="'K: V'",
                    help="Header sent with every request, repeatable "
                         "(e.g. --header 'Authorization: Bearer ...')")
    ap.add_argument("--target", choices=["auto", "mock", "live"], default="auto",
                    help="auto (default): probe /_mock/routes to tell a mockd instance "
                         "from a real backend. A mock self-check runs writes by default "
                         "— its store is in memory; a live check never does.")
    ap.add_argument("--allow-writes", action="store_true",
                    help="Also run POST/PUT/PATCH/DELETE. Against a live environment "
                         "these create and delete real rows. Default ON for a mock.")
    ap.add_argument("--no-writes", action="store_true",
                    help="Read-only, even against a mock")
    ap.add_argument("--ignore-auth-probe", action="store_true",
                    help="sweep even when the pre-flight read comes back 401/403")
    ap.add_argument("--negative", action="store_true",
                    help="Also probe documented failure paths (no-auth, missing required field)")
    ap.add_argument("--only", help="Regex; run only operations whose 'METHOD /path' matches")
    ap.add_argument("--exclude", help="Regex; skip operations whose 'METHOD /path' matches")
    ap.add_argument("--skip", action="append", default=[], metavar="'GET /path'",
                    help="Skip this exact operation, repeatable. What the console sends "
                         "when you deselect one.")
    ap.add_argument("--skip-streaming", action="store_true",
                    help="Do not call operations whose path or summary says stream/sse/"
                         "watch/subscribe")
    ap.add_argument("--fixtures", help="JSON file of {param_name: value} to pin path params")
    ap.add_argument("--optional-query", action="store_true",
                    help="Also send optional query params (exercises filters and pagination)")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--report", help="Write the full JSON report here")
    ap.add_argument("--junit", help="Write a JUnit XML report here (for CI)")
    ap.add_argument("--learn-overlay", metavar="FILE",
                    help="Write observed responses as a mockd overlay")
    ap.add_argument("--keep-bodies", action="store_true",
                    help="Include full response bodies in the JSON report")
    ap.add_argument("--fail-on", choices=["error", "warning", "never"], default="error",
                    help="Exit non-zero when a result at this level or worse appears")
    ap.add_argument("--quiet", action="store_true", help="One line per operation, no detail")
    args = ap.parse_args()

    headers = {}
    if args.env:
        import environments
        base, headers, note = environments.headers_for(args.env)
        args.base_url = args.base_url or base
        print(f"env: {args.env} -> {args.base_url} ({note})")
    # CLAVIS_HEADER_n keeps credentials out of argv, where `ps` would show them
    extra = [os.environ[k] for k in sorted(os.environ) if k.startswith("CLAVIS_HEADER_")]
    for raw in list(args.header) + extra:
        if ":" not in raw:
            raise SystemExit(f"--header must look like 'Name: value', got {raw!r}")
        name, value = raw.split(":", 1)
        headers[name.strip()] = value.strip()
    if not args.base_url:
        raise SystemExit("give --base-url, or --env NAME to take it from environments.json")

    explicit = args.target != "auto"
    if not explicit:
        args.target = detect_target(args.base_url, headers)
    if args.target == "mock" and not args.no_writes:
        # a mock's writes are in-memory; excluding them would leave half the
        # spec unchecked for no benefit
        args.allow_writes = True
    if args.no_writes:
        args.allow_writes = False

    # no --spec given: use the document this project is about
    args.spec, spec_from = project.resolve(args.spec, "verify")
    src = Source(args.spec, headers=headers, poll=0, cache_dir="logs")
    text, _ = src.read(force=True)
    if text is None:
        raise SystemExit(f"could not read spec from {args.spec}: {src.error}")
    spec = Spec(text=text, origin=args.spec)
    import speclock
    provenance = speclock.compare(text)
    provenance["source"] = args.spec
    if provenance["state"] == "drift":
        print(f"WARNING: {provenance['message']}")
    print(f"spec: {len(spec.routes)} operations, OpenAPI {spec.version}"
          f"{', ' + spec.title if spec.title else ''}  [{spec_from}]")

    # Judging the mock against a document it is not serving produces failures
    # that are nobody's fault and waste the reader's time. Ask it which document
    # it loaded, and stop if it is not this one.
    mismatch = mock_spec_mismatch(args.base_url, text, len(spec.routes))
    if mismatch:
        raise SystemExit(mismatch)
    how = "told" if explicit else "detected"
    print(f"target: {args.base_url} — {how} to be a "
          + ("mockd instance (self-check)" if args.target == "mock" else "real backend"))
    if args.target == "live" and not headers:
        print("warning: no --header given; endpoints behind auth will report 401")

    results = run(spec, args.base_url, headers, args)
    counts = summarise(spec, results, args)

    if args.report:
        Path(args.report).write_text(json.dumps(
            {"base_url": args.base_url, "spec": args.spec, "provenance": provenance,
             "ran_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "summary": counts, "results": results}, indent=2, default=str) + "\n")
        print(f"\nJSON report:  {args.report}")
    if args.junit:
        write_junit(results, args.junit)
        print(f"JUnit report: {args.junit}")
    if args.learn_overlay:
        n = write_learned_overlay(spec, results, args.learn_overlay)
        print(f"learned overlay: {args.learn_overlay} ({n} operations captured from live "
              f"responses — review for real data before committing)")

    if args.fail_on == "never":
        return 0
    bad = counts.get(ERROR, 0) + (counts.get(WARN, 0) if args.fail_on == "warning" else 0)
    return 1 if bad else 0


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
