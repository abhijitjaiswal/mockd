#!/usr/bin/env python3
"""mockd as a set of tools an AI assistant can call — an MCP server.

The "copy this prompt into your AI tool" path works, but it is one guess at
what the assistant will need, made before it has read anything, and every
mistake comes back to a person to paste in again. Here the assistant asks for
what it needs as it goes — which endpoints a sentence is about, what one of
them takes and returns, where an id comes from — writes the tests, and hands
them back to be checked, saved and run. It reads the failures itself.

There is no AI in this file and none is needed to run it. It answers function
calls with the same code the console uses. The assistant is whatever tool the
tester connects: Claude Desktop, Claude Code, Cursor.

    python mcp_server.py            # speaks MCP over stdin/stdout

What it will not do, by construction:
  * return a token, password or cookie — a server is named, and signed in to
    here; credentials never travel to the assistant
  * write to a server marked read only — the runner refuses, as it always has
  * delete or overwrite anybody's tests — it only adds, to the draft workspace
  * work on anything but this project's API document
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# The protocol owns stdout: one JSON message per line. Anything a library
# prints would corrupt it, so everything else is sent to stderr.
_WIRE = sys.stdout
sys.stdout = sys.stderr

import bindings                                           # noqa: E402
import blueprint                                          # noqa: E402
import environments as envmod                             # noqa: E402
import project                                            # noqa: E402
import tests as t                                         # noqa: E402
from mockd import Source, Spec                            # noqa: E402

NAME, VERSION = "mockd", "1.0.0"
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = """\
mockd holds this project's API document, a mock of it, and the team's API tests.
Use these tools to write tests for what the user describes.

A good order:
  1. get_test_format          once, to learn the JSON a test is written in
  2. find_operations          which endpoints the request is about
  3. get_operation            exactly what each takes and returns — never guess a field
  4. where_does_this_id_come_from   for every id a request needs; never invent one
  5. list_tests               so you do not repeat what exists
  6. save_tests               they are validated first; fix what it reports
  7. run_tests                on the mock, then on a real server if one is set up
Read the failures run_tests returns and correct the tests before telling the
user they are done. The mock answers in the documented shape but knows none of
the API's own rules (totals, stock, what is refused the second time): a test of
those can only be settled on a real server — see list_servers.
Only write tests for operations these tools return. If the request is about
something else, say so."""

RULES = "\n".join([
    "RULES",
    "  - `path` starts at / — never write a host; the runner adds it.",
    "  - Never write a token, cookie or password. Sign-in comes from the server chosen at run time.",
    "  - Never invent an id. Capture it in an earlier step (see where_does_this_id_come_from).",
    "  - Every {{name}} must be captured by an EARLIER step or declared in that test's own",
    "    \"data\": {...}. Anything else is refused when saving.",
    "  - A value that must differ between runs uses {{$uuid}} or {{$runId}}, never a constant.",
    "  - Anything a flow creates gets a cleanup step that deletes it, when a delete exists.",
    "  - Give every test `levels` (one or more of: " + ", ".join(t.LEVELS) + "),",
    "    a `priority` (" + ", ".join(t.PRIORITIES) + " — P0 is what must never break) and a",
    "    one-sentence `description` a non-engineer could read.",
    "  - kind \"api\": earlier steps only make the last one reachable. kind \"e2e\": every step matters.",
    "  - Assert what was asked for, not merely that a 200 came back; and only fields the",
    "    operation documents.",
])


# ----------------------------------------------------------------- the project
def load_spec():
    path = project.active_spec(None)
    text, _ = Source(path, poll=0, cache_dir=str(HERE / "logs")).read(force=True)
    if text is None:
        raise Problem(f"The project's API document ({path}) could not be read. "
                      f"Load one from the console's API document screen.")
    return Spec(text=text, origin=path), path


def mock_url():
    return envmod.resolve("${MOCK_BASE_URL}", [])


def mock_is_up():
    try:
        with urllib.request.urlopen(mock_url() + "/_mock/routes", timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def servers():
    """Where tests can run. Names and addresses only — never a credential."""
    out = []
    for name, env in sorted(envmod.load().items()):
        if name.startswith("mock-"):
            continue                              # variants that exercise sign-in handling
        missing = []
        envmod.resolve({"base_url": env.get("base_url"), "auth": env.get("auth"),
                        "headers": env.get("headers")}, missing)
        ready = not missing and (name != "mock" or mock_is_up())
        out.append({"name": name,
                    "address": envmod.resolve(env.get("base_url", ""), []),
                    "ready": ready,
                    "read_only": bool(env.get("readonly")),
                    "is_the_mock": name == "mock",
                    **({"not_ready_because": "the mock is not running — start it from the console"}
                       if name == "mock" and not ready else
                       {"not_ready_because": "it has not been fully set up in the console"}
                       if not ready else {})})
    return out


class Problem(Exception):
    """Something the assistant can act on, said plainly."""


def find_route(spec, operation):
    wanted = re.sub(r"\s+", " ", str(operation or "").strip())
    match = re.fullmatch(r"([A-Za-z]+) (/\S*)", wanted)
    if not match:
        raise Problem('Give the operation as "METHOD /path", for example "GET /api/v1/orders".')
    method, path = match.group(1).upper(), match.group(2)
    for route in spec.routes:
        if route["method"] == method and route["path"] == path:
            return route
    route, _ = spec.match(method, path)
    if route is not None:
        return route
    near = sorted(r["key"] for r in spec.routes if path.rstrip("/") in r["path"])[:6]
    raise Problem(f"{method} {path} is not in the API document."
                  + (f" Close to it: {', '.join(near)}." if near else
                     " Use find_operations to see what is."))


# ------------------------------------------------------------------- the tools
def tool_get_test_format(_args):
    return {"format": t.GRAMMAR.rstrip() + "\n\n" + RULES}


def tool_find_operations(args):
    story = str(args.get("request") or args.get("story") or "").strip()
    if len(story) < 3:
        raise Problem("Say what is to be tested, in a sentence.")
    spec, path = load_spec()
    limit = max(1, min(int(args.get("limit") or 8), 20))
    if not t.story_is_about_this_api(spec, story):
        return {"operations": [], "api_document": path,
                "note": "Nothing in this project's API document matches that. These tools only "
                        "write tests for this API — tell the user, rather than inventing an endpoint."}
    found = t.relevant_routes(spec, story, limit=limit)
    return {"api_document": path,
            "operations": [{"operation": r["key"], "summary": r.get("summary") or ""}
                           for r in found],
            "next": "Call get_operation for each one you will use."}


def tool_get_operation(args):
    spec, _ = load_spec()
    route = find_route(spec, args.get("operation"))
    sample = None
    if route["method"] == "GET" and not route.get("path_params") and mock_is_up():
        try:
            with urllib.request.urlopen(mock_url() + route["path"], timeout=5) as resp:
                sample = json.loads(resp.read().decode() or "null")
        except Exception:
            sample = None
    needs = sorted(set(re.findall(r"\{([^}]+)\}", route["path"])))
    return {"operation": route["key"],
            "contract": t.operation_brief(route, sample=sample, limit=1800),
            **({"ids_in_the_path": needs,
                "next": "Call where_does_this_id_come_from for each of these."} if needs else {})}


def tool_where_does_this_id_come_from(args):
    field = str(args.get("field") or "").strip().strip("{}")
    if not field:
        raise Problem("Name the id, for example book_id.")
    index = blueprint.load_index()
    if not index:
        return {"field": field, "found": False,
                "note": "mockd has not yet learned what each endpoint returns. Ask the user to "
                        "start the mock from the console once; it checks itself and learns this."}
    recorded = bindings.load().get(field)
    chain = bindings.chain_for(field, index)
    others = [{"call": c["operation"], "capture_path": c["path"], "confidence": c["strength"],
               "why": c["why"]} for c in bindings.candidates(field, index, limit=4)]
    if recorded:
        return {"field": field, "found": True, "decided_by_the_team": True,
                "steps": [{"call": recorded.get("operation"),
                           "capture_path": recorded.get("path"),
                           "save_as": bindings.camel(field)}]}
    if not chain:
        return {"field": field, "found": False, "near_misses": others,
                "note": "No endpoint is known to return this. Put it in the test's own "
                        "\"data\" with a placeholder like \"<a real " + field + ">\" and tell the "
                        "user it needs a real value — do not make one up."}
    return {"field": field, "found": True,
            "steps": [{"call": step["operation"], "capture_path": step["path"],
                       "save_as": step["as"], "why": step["why"]} for step in chain],
            "how": "Add each step, in this order, as a setup step with "
                   "\"capture\": {\"<save_as>\": \"<capture_path>\"}, then use {{<save_as>}}.",
            "other_sources": others[1:]}


def tool_list_servers(_args):
    return {"servers": servers(),
            "note": "Name one of these in run_tests. Sign-in is handled by mockd; you never "
                    "need, and are never given, a credential."}


def tool_list_tests(args):
    module = str(args.get("module") or "").strip()
    out = []
    for suite in t.load_suites(include_drafts=True):
        if module and suite.get("name") != module:
            continue
        rows = [{"id": x.get("id"), "name": x.get("name") or x.get("id"),
                 "operations": sorted({f"{(s.get('request') or {}).get('method', 'GET')} "
                                       f"{(s.get('request') or {}).get('path', '')}"
                                       for s in (x.get("steps") or [x])})}
                for x in (suite.get("scenarios") or []) + (suite.get("cases") or [])]
        if len(rows) > 60 and not module:
            rows = rows[:60] + [{"note": f"…and {len(rows) - 60} more; ask for this module by name"}]
        out.append({"module": suite.get("name"),
                    "made_automatically": suite.get("name") == "baseline",
                    "tests": rows})
    return {"modules": out,
            "note": "The baseline module is generated from the API document and already checks "
                    "that each endpoint answers as documented. Do not repeat it."}


def _items(payload):
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError as exc:
            raise Problem(f"`tests` is not valid JSON: {exc}")
    if isinstance(payload, dict) and ("cases" in payload or "scenarios" in payload):
        return (payload.get("scenarios") or []) + (payload.get("cases") or [])
    return payload if isinstance(payload, list) else [payload]


def _problems(items):
    spec, _ = load_spec()
    problems = []
    for item in items:
        if not isinstance(item, dict):
            problems.append("every test must be a JSON object")
            continue
        problems += t.validate_test(item, "scenario" if "steps" in item else "case")
        # an operation the document does not have is a test of nothing
        for step in (item.get("steps") or [item]) + (item.get("cleanup") or []):
            request = step.get("request") or {}
            path = re.sub(r"\{\{[^}]+\}\}", "x", str(request.get("path") or ""))
            method = str(request.get("method") or "GET").upper()
            if path.startswith("/") and spec.match(method, path.split("?")[0])[0] is None:
                problems.append(f"{item.get('id') or 'a test'}: {method} {request.get('path')} "
                                f"is not an operation in the API document")
    return problems


def tool_validate_tests(args):
    items = _items(args.get("tests"))
    if not items:
        raise Problem("There are no tests in that.")
    problems = _problems(items)
    return {"ok": not problems, "tests": len(items), "problems": problems[:30],
            **({"next": "Fix these and validate again."} if problems else
               {"next": "Call save_tests."})}


def _unique_module(base):
    slug = re.sub(r"[^a-z0-9]+", "-", (base or "from-assistant").lower()).strip("-")[:32]
    slug = slug or "from-assistant"
    taken = {s.get("name") for s in t.load_suites(include_drafts=True)}
    if slug not in taken:
        return slug
    while True:
        candidate = f"{slug}-{uuid.uuid4().hex[:4]}"
        if candidate not in taken:
            return candidate


def tool_save_tests(args):
    items = _items(args.get("tests"))
    if not items:
        raise Problem("There are no tests in that.")
    problems = _problems(items)
    if problems:
        return {"ok": False, "saved": 0, "problems": problems[:30],
                "next": "Nothing was saved. Fix these and call save_tests again."}

    module = str(args.get("module") or "").strip()
    if module and not re.fullmatch(r"[A-Za-z0-9._-]+", module):
        raise Problem("A module name can only use letters, numbers, dots, dashes and underscores.")
    if module == "baseline":
        raise Problem("The baseline module is generated from the API document; choose another name.")
    ids = [x.get("id") for x in items]
    existing = {s.get("name"): s for s in t.load_suites(include_drafts=True)}
    if module in existing:
        held = {x.get("id") for x in (existing[module].get("scenarios") or [])
                + (existing[module].get("cases") or [])}
        clash = sorted(set(ids) & held)
        if clash:
            raise Problem(f"{module} already has a test with id {', '.join(clash)}. Tests are "
                          f"added, never overwritten — choose different ids.")
        if existing[module].get("_stage") != "draft":
            raise Problem(f"{module} is a module the team shares. Save into a new module; a "
                          f"person promotes tests into shared ones.")
    module = module or _unique_module(args.get("about") or (items[0].get("name") or ""))

    written = t.import_tests(items, module, stage="draft")
    if not written.get("ok"):
        return {"ok": False, "saved": 0, "problems": (written.get("errors") or [])[:30]}

    # ids the assistant left as placeholders, fixed up the way a person would have to
    notes, index = [], blueprint.load_index()
    suite = next((su for su in t.load_suites(include_drafts=True)
                  if su.get("name") == module and su.get("_stage") == "draft"), None)
    if suite is not None and index:
        touched = False
        for test in (suite.get("scenarios") or []) + (suite.get("cases") or []):
            if test.get("id") in ids:
                changed, said = t.rebind_placeholders(test, index)
                touched, notes = touched or changed, notes + said
        if touched:
            t.save_suite(suite, stage="draft")
    return {"ok": True, "saved": len(items), "module": module, "ids": ids, "notes": notes,
            "where": "They are in the draft workspace and appear on the console's Tests screen.",
            "next": f'Call run_tests with module "{module}".'}


def _explain(item, on_mock):
    out = {"id": item.get("id"), "name": item.get("name"), "outcome": item.get("outcome")}
    if item.get("outcome") == "pass":
        return out
    if item.get("error"):
        out["error"] = str(item["error"])[:400]
    for number, step in enumerate(item.get("steps") or [], 1):
        failed = [c for c in (step.get("checks") or []) if not c.get("ok")]
        if step.get("outcome") in ("pass", None) and not failed:
            continue
        request = step.get("request") or {}
        out["failed_at"] = {
            "step": number, "name": step.get("name"),
            "request": f"{request.get('method', '')} {request.get('url', '')}".strip(),
            "status": step.get("status"),
            "checks_that_failed": [
                (c.get("label") or "") + (f" — {c.get('why') or c.get('detail')}"
                                          if (c.get("why") or c.get("detail")) else "")
                for c in failed][:6],
            "response": str(step.get("response_excerpt") or "")[:700]}
        break
    verdict = (item.get("verdict") or {}).get("headline")
    if verdict and not on_mock:
        out["whose_problem_it_looks_like"] = verdict
    return out


def tool_run_tests(args):
    module = str(args.get("module") or "").strip()
    if not module:
        raise Problem("Say which module to run — the one save_tests returned.")
    if not any(s.get("name") == module for s in t.load_suites(include_drafts=True)):
        raise Problem(f"There is no module named {module}. See list_tests.")
    server = str(args.get("server") or "mock").strip()
    known = {s["name"]: s for s in servers()}
    if server not in known:
        raise Problem(f"There is no server named {server}. Known: {', '.join(sorted(known))}.")
    if not known[server]["ready"]:
        raise Problem(f"{server} cannot be used yet: {known[server].get('not_ready_because')}.")
    ids = [str(i) for i in (args.get("ids") or []) if i]

    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "run.json"
        cmd = [sys.executable, str(HERE / "tests.py"), "run", "--env", server, "--drafts",
               "--suite", module, "--json", str(report)]
        if ids:
            cmd += ["--only", "^(" + "|".join(re.escape(i) for i in ids) + ")( |$)"]
        try:
            done = subprocess.run(cmd, cwd=str(HERE), capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            raise Problem("The run took more than ten minutes and was stopped.")
        try:
            result = json.loads(report.read_text())
        except Exception:
            raise Problem("The run did not finish: " + (done.stdout or done.stderr or "")[-400:])

    on_mock = server == "mock"
    rows = [_explain(item, on_mock)
            for suite in result.get("suites") or [] for item in suite.get("results") or []]
    passed = sum(1 for r in rows if r["outcome"] == "pass")
    out = {"server": server, "ran": len(rows), "passed": passed,
           "did_not_pass": len(rows) - passed, "tests": rows}
    if not rows:
        out["note"] = "Nothing ran — check the ids against list_tests."
    elif on_mock and passed < len(rows):
        real = [s["name"] for s in known.values()
                if s["ready"] and not s["is_the_mock"] and not s["read_only"]]
        out["note"] = ("The mock answers in the documented shape but does not do the API's own "
                       "rules — a computed total, remaining stock, a refusal the second time. "
                       "If a failure is about one of those the test may be right. "
                       + (f"Run it on a real server: {', '.join(real)}." if real else
                          "No real server is set up; tell the user one can be added under Servers."))
    return out


def _schema(properties=None, required=()):
    return {"type": "object", "properties": properties or {}, "required": list(required),
            "additionalProperties": False}


TOOLS = [
    ("get_test_format", tool_get_test_format,
     "The JSON a test is written in, and the rules it must follow. Read this once before writing any test.",
     _schema()),
    ("find_operations", tool_find_operations,
     "Which endpoints of this project's API a request is about. Start here: it returns operations "
     "by name, and nothing else may be used in a test.",
     _schema({"request": {"type": "string",
                          "description": "What is to be tested, in the user's words."},
              "limit": {"type": "integer", "minimum": 1, "maximum": 20}}, ["request"])),
    ("get_operation", tool_get_operation,
     "Exactly what one operation takes and returns: every field, its type and limits, the "
     "statuses it documents, and a real response from the mock when one can be had. Never "
     "guess a field name — look here.",
     _schema({"operation": {"type": "string",
                            "description": 'As "METHOD /path", e.g. "POST /api/v1/orders".'}},
             ["operation"])),
    ("where_does_this_id_come_from", tool_where_does_this_id_come_from,
     "For an id a request needs (book_id, order_id…): which call supplies it and the path to "
     "capture it from. Ids must be captured, never invented.",
     _schema({"field": {"type": "string", "description": "The id's name, e.g. book_id."}},
             ["field"])),
    ("list_tests", tool_list_tests,
     "The tests that already exist, by module, so nothing is written twice.",
     _schema({"module": {"type": "string", "description": "Only this module (optional)."}})),
    ("list_servers", tool_list_servers,
     "Where tests can run: the mock and any real servers set up in the console. Names and "
     "addresses only.",
     _schema()),
    ("validate_tests", tool_validate_tests,
     "Check tests without saving them. Returns every problem, or ok.",
     _schema({"tests": {"description": "A JSON array of tests (or one test)."}}, ["tests"])),
    ("save_tests", tool_save_tests,
     "Validate tests and add them to the project's draft workspace. Refuses, saving nothing, "
     "if any is invalid. Never overwrites an existing test.",
     _schema({"tests": {"description": "A JSON array of tests (or one test)."},
              "module": {"type": "string",
                         "description": "Module to add them to. Leave out and one is named for you."},
              "about": {"type": "string",
                        "description": "A few words on what these test, used to name the module."}},
             ["tests"])),
    ("run_tests", tool_run_tests,
     "Run a module's tests on the mock or a named server and get each result, with the failing "
     "step, the checks that failed and the response. Read these and fix the tests.",
     _schema({"module": {"type": "string"},
              "ids": {"type": "array", "items": {"type": "string"},
                      "description": "Only these test ids (optional)."},
              "server": {"type": "string",
                         "description": 'A name from list_servers. Defaults to "mock".'}},
             ["module"])),
]
BY_NAME = {name: fn for name, fn, _, _ in TOOLS}


# ----------------------------------------------------------------- the protocol
def send(message):
    _WIRE.write(json.dumps(message, ensure_ascii=False) + "\n")
    _WIRE.flush()


def handle(message):
    method, ident = message.get("method"), message.get("id")
    params = message.get("params") or {}
    if ident is None:
        return None                                # a notification: nothing to answer
    if method == "initialize":
        asked = params.get("protocolVersion")
        return {"protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": NAME, "version": VERSION},
                "instructions": INSTRUCTIONS}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [{"name": name, "description": about, "inputSchema": schema}
                          for name, _, about, schema in TOOLS]}
    if method == "tools/call":
        name = params.get("name")
        if name not in BY_NAME:
            raise LookupError(f"unknown tool {name}")
        try:
            result = BY_NAME[name](params.get("arguments") or {})
            failed = False
        except Problem as exc:
            result, failed = {"error": str(exc)}, True
        except Exception as exc:                   # a bug here must not end the session
            result, failed = {"error": f"mockd could not do that: {type(exc).__name__}: {exc}"}, True
        return {"content": [{"type": "text",
                             "text": json.dumps(result, indent=2, ensure_ascii=False)}],
                "isError": failed}
    if method in ("resources/list", "prompts/list"):
        return {method.split("/")[0]: []}
    raise LookupError(f"method not found: {method}")


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(__doc__, file=sys.stderr)
        return
    os.chdir(HERE)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            send({"jsonrpc": "2.0", "id": None,
                  "error": {"code": -32700, "message": "that line is not JSON"}})
            continue
        try:
            result = handle(message)
        except LookupError as exc:
            send({"jsonrpc": "2.0", "id": message.get("id"),
                  "error": {"code": -32601, "message": str(exc)}})
            continue
        except Exception as exc:
            send({"jsonrpc": "2.0", "id": message.get("id"),
                  "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"}})
            continue
        if result is not None:
            send({"jsonrpc": "2.0", "id": message.get("id"), "result": result})


if __name__ == "__main__":
    main()
