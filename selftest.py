#!/usr/bin/env python3
"""
selftest.py — the pure-logic checks, with no server and no browser.

The browser suites in demo/ prove the console is wired together; the smoke test
proves the mock answers. Neither is a good home for "does this function return
the right thing", which is most of what the test model is made of: filtering,
classification, validation, binding. Those run in milliseconds and should not
need a port.

    python selftest.py            every check
    python selftest.py taxonomy   one group
"""
import sys
import traceback

PASSED, FAILED = [], []


def check(name, got, want):
    if got == want:
        PASSED.append(name)
    else:
        FAILED.append((name, f"got {got!r}, want {want!r}"))


def check_true(name, value, detail=""):
    if value:
        PASSED.append(name)
    else:
        FAILED.append((name, detail or "expected true"))


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------

def group_classification():
    import tests as t

    # a level stated outright
    check("levels: explicit wins", t.levels_of({"levels": ["Smoke"]}), ["smoke"])

    # teams already wrote levels into tags; those must keep working untouched
    check("levels: read from existing tags",
          t.levels_of({"tags": ["smoke", "ui"]}), ["smoke"])
    check("levels: a tag that is not a level is ignored",
          t.levels_of({"tags": ["flaky", "ui"]}), ["regression"])
    check("levels: nothing said means regression", t.levels_of({}), ["regression"])

    # module falls back through test -> suite -> name, so nothing must be typed
    check("module: from the test", t.module_of({"module": "billing"}, {}), "billing")
    check("module: from the suite", t.module_of({}, {"module": "billing"}), "billing")
    check("module: from the suite name", t.module_of({}, {"name": "users"}), "users")
    check("module: last resort", t.module_of({}, {}), "unsorted")

    # a misspelled level is the failure this exists to prevent
    errors = t.validate_test({"id": "x", "levels": ["smoek"],
                              "request": {"method": "GET", "path": "/x"},
                              "assertions": [{"type": "status", "equals": 200}]})
    check_true("validate: a misspelled level is refused",
               any("smoek" in e for e in errors), str(errors))
    ok = t.validate_test({"id": "x", "levels": ["smoke"],
                          "request": {"method": "GET", "path": "/x"},
                          "assertions": [{"type": "status", "equals": 200}]})
    check("validate: a correct level passes", ok, [])


def group_taxonomy():
    import tests as t
    suite = {
        "name": "billing", "module": "billing",
        "cases": [{"id": "a", "levels": ["smoke"]}, {"id": "b", "tags": ["negative"]}],
        "scenarios": [{"id": "c", "kind": "e2e", "levels": ["sanity"], "steps": [{}]}],
    }
    tax = t.taxonomy([suite])
    check("taxonomy: counts every test", tax["total"], 3)
    check("taxonomy: one module", [m["name"] for m in tax["modules"]], ["billing"])
    counts = {level["name"]: level["tests"] for level in tax["levels"]}
    check("taxonomy: smoke counted", counts["smoke"], 1)
    check("taxonomy: level read from a tag", counts["negative"], 1)
    check("taxonomy: sanity counted", counts["sanity"], 1)
    check("taxonomy: every level is offered even at zero",
          len(tax["levels"]), len(t.LEVELS))
    kinds = {k["name"]: k["tests"] for k in tax["kinds"]}
    check("taxonomy: kinds separated", (kinds.get("case"), kinds.get("e2e")), (2, 1))


def group_selection():
    import tests as t
    runner = t.Runner.__new__(t.Runner)          # no server needed to test filtering
    suite = {"name": "billing", "module": "billing"}
    smoke = {"id": "a", "name": "list invoices", "levels": ["smoke"]}
    regress = {"id": "b", "name": "refund twice", "levels": ["regression"],
               "tags": ["slow"]}

    check_true("select: no filter takes everything",
               runner.selects(smoke, suite) and runner.selects(regress, suite))
    check_true("select: by level", runner.selects(smoke, suite, levels=["smoke"]))
    check_true("select: level excludes", not runner.selects(regress, suite, levels=["smoke"]))
    check_true("select: by module", runner.selects(smoke, suite, modules=["billing"]))
    check_true("select: module is case-insensitive",
               runner.selects(smoke, suite, modules=["BILLING"]))
    check_true("select: wrong module excludes",
               not runner.selects(smoke, suite, modules=["users"]))
    check_true("select: by tag", runner.selects(regress, suite, tags=["slow"]))
    check_true("select: by name regex", runner.selects(regress, suite, only="refund"))
    check_true("select: filters combine as AND",
               not runner.selects(smoke, suite, levels=["smoke"], modules=["users"]))


def group_binding():
    import tests as t

    dept = "/api/v1/recruitment-settings/positions/departments"
    check("name: an id takes the resource with it", t.binding_name("data.id", dept),
          "departmentId")
    check("name: a title too", t.binding_name("data.title", dept), "departmentTitle")
    check("name: an already-qualified field is not doubled",
          t.binding_name("data.department_id", dept), "departmentId")
    check("name: a list item still names the resource",
          t.binding_name("data.items[0].id", "/api/v1/user/list"), "userId")
    check("name: something unremarkable keeps its own name",
          t.binding_name("total", "/api/v1/user/list"), "total")
    check("name: a taken name is never silently reused",
          t.binding_name("data.id", dept, taken={"departmentId"}), "departmentId2")

    body = {"message": "ok", "data": {"id": "a-1", "title": "Eng", "count": 2,
                                      "nested": {"owner_id": "u-9"}, "flag": True,
                                      "nothing": None}}
    found = t.bindable(body, dept)
    paths = [b["path"] for b in found]
    check_true("bindable: ids come first", paths[0] == "data.id", str(paths))
    check_true("bindable: reaches nested values", "data.nested.owner_id" in paths, str(paths))
    check_true("bindable: skips nulls", "data.nothing" not in paths, str(paths))
    check_true("bindable: skips booleans, which carry no identity",
               "data.flag" not in paths, str(paths))
    check_true("bindable: marks what looks like an id",
               next(b for b in found if b["path"] == "data.id")["looks_like_id"])


def group_readonly():
    import tests as t

    calls = []
    runner = t.Runner("http://example.invalid", {}, readonly=True, env_name="prod")
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        out = runner.run_step({"name": method,
                               "request": {"method": method, "path": "/x"},
                               "assertions": []}, {})
        calls.append((method, out["outcome"], out.get("refused")))
    check("readonly: every write is refused",
          {c[1] for c in calls}, {t.SKIPPED})
    check_true("readonly: and marked as refused, not merely skipped",
               all(c[2] for c in calls))
    check_true("readonly: the reason names the environment",
               "prod" in runner.run_step({"name": "x", "request":
                   {"method": "POST", "path": "/x"}, "assertions": []},
                   {})["checks"][0]["detail"])
    # a writable runner must not refuse anything
    open_runner = t.Runner("http://example.invalid", {}, readonly=False)
    out = open_runner.run_step({"name": "post", "request": {"method": "POST", "path": "/x"},
                                "assertions": []}, {})
    check_true("writable: a write is attempted", not out.get("refused"))


def group_runid():
    import tests as t
    once = t.interpolate("{{$runId}}", {})
    twice = t.interpolate("{{$runId}}", {})
    check("runId: stable within a run", once, twice)
    check_true("runId: looks like a run marker", once.startswith("run-"), once)
    check_true("runId: $uuid stays fresh each read",
               t.interpolate("{{$uuid}}", {}) != t.interpolate("{{$uuid}}", {}))


def group_rebind():
    import tests as t

    renamed = t.rebind_candidates({"data": {"uuid": "a-1", "title": "Eng"}}, "data.id")
    check("rebind: a renamed id is the first suggestion", renamed[0]["path"], "data.uuid")
    moved = t.rebind_candidates({"data": {"department": {"id": "a-1"}}}, "data.id")
    check("rebind: a moved field is found by its name", moved[0]["path"],
          "data.department.id")
    changed = t.rebind_candidates({"result": {"identifier": "a-1"}}, "data.id")
    check("rebind: a new envelope still offers its id", changed[0]["path"],
          "result.identifier")
    nothing = t.rebind_candidates({"message": "deleted"}, "data.id")
    check_true("rebind: never leaves the reader with nothing", len(nothing) == 1,
               str(nothing))
    check("rebind: nothing to suggest for an empty response",
          t.rebind_candidates(None, "data.id"), [])

    for key, want in (("id", True), ("user_id", True), ("userId", True),
                      ("identifier", True), ("uuid", True),
                      ("valid", False), ("width", False), ("paid", False),
                      ("title", False)):
        check(f"id-shaped: {key}", t._id_shaped(key), want)


def group_story():
    import tests as t

    class FakeSpec:
        # shaped like a real Spec route, because the brief reads the contract
        routes = [
            {"key": "POST /api/v1/departments", "method": "POST",
             "path": "/api/v1/departments", "summary": "Create Department",
             "tags": ["departments"], "parameters": [], "path_params": [],
             "request_body": {"content": {"application/json": {"schema": {
                 "type": "object", "required": ["title"],
                 "properties": {"title": {"type": "string"}}}}}},
             "responses": {"200": {"content": {"application/json": {"schema": {
                 "type": "object",
                 "properties": {"id": {"type": "string"}}}}}}}},
            {"key": "GET /api/v1/invoices", "method": "GET",
             "path": "/api/v1/invoices", "summary": "List invoices",
             "tags": ["billing"], "parameters": [], "path_params": [],
             "request_body": {}, "responses": {"200": {}}},
        ]

    brief = t.story_pack(FakeSpec(), "As an admin I want to create a department")
    check_true("story: picks the operation the story is about",
               "POST /api/v1/departments" in brief, brief[:120])
    check_true("story: leaves out the unrelated one",
               "/api/v1/invoices" not in brief)
    check_true("story: forbids inventing endpoints",
               "instead of inventing an endpoint" in brief)
    check_true("story: insists on non-constant data", "{{$uuid}}" in brief)
    check_true("story: insists on cleanup", "cleanup step" in brief)
    check_true("story: insists on a level", "give every test `levels`" in brief)
    # the point of the brief: an assistant cannot invent field names it is shown
    check_true("story: carries the request body schema",
               "request body:" in brief and "title" in brief, brief[:200])
    check_true("story: carries the response schema",
               "response 200:" in brief, brief[:200])

    partial = t.operation_brief({"key": "GET /x", "method": "GET", "path": "/x"})
    check_true("brief: a route missing its contract does not crash",
               "GET /x" in partial, partial)

    none = t.story_pack(FakeSpec(), "As a pilot I want to file a flight plan")
    check_true("story: says so when nothing matches",
               "NO OPERATION MATCHED" in none, none[:160])

    covered = t.story_pack(FakeSpec(), "create a department", existing=["dept-create"])
    check_true("story: lists what is already covered",
               "ALREADY COVERED" in covered and "dept-create" in covered)


def group_length():
    import tests as t

    body = {"data": {"items": [{"id": 1}, {"id": 2}]}, "meta": {"a": 1}}
    res = {"json": body, "status": 200, "ms": 1, "headers": {}, "text": "", "error": None}

    def run(path, op, value=None):
        a = {"type": "jsonpath", "path": path, "op": op}
        if value is not None:
            a["value"] = value
        out = t.evaluate(a, res)
        return out[0], out[-1]

    # every way a person might reasonably express "how many came back"
    check("length: exact count", run("data.items", "length", 2)[0], True)
    check("length: a floor", run("data.items", "length_gte", 1)[0], True)
    check("length: not empty", run("data.items", "not_empty")[0], True)
    check("length: via .length on the path", run("data.items.length", "gt", 1)[0], True)
    check("length: .length compares exactly",
          run("data.items.length", "equals", 2)[0], True)
    check("length: a wrong count fails", run("data.items", "length", 99)[0], False)

    # the failure people actually hit: comparing the list itself
    ok, detail = run("data.items", "gte", 250)
    check("a list compared with gte does not pass", ok, False)
    check_true("and the reason is not a Python TypeError",
               "TypeError" not in detail and "float()" not in detail, detail)
    check_true("it says what the value actually is",
               "list of 2 item(s)" in detail, detail)
    check_true("and names the operator to use instead",
               "length_gte" in detail, detail)
    check_true("and mentions the .length alternative", ".length" in detail, detail)

    ok, detail = run("meta", "gt", 1)
    check_true("an object says object, not list",
               "object with 1 key(s)" in detail, detail)

    # dig has to understand the path the message recommends
    check("dig reads .length on a list", t.dig(body, "data.items.length"), 2)
    check_true("dig does not invent .length on an object",
               t.dig(body, "meta.length") is t.MISSING)


def group_types():
    import tests as t

    body = {"data": {"id": "abc", "count": 3, "ratio": 1.5, "active": True,
                     "tags": ["a"], "meta": {"x": 1}, "gone": None}}
    res = {"json": body, "status": 200, "ms": 1, "headers": {}, "text": "", "error": None}

    def is_type(path, want):
        out = t.evaluate({"type": "jsonpath", "path": path, "op": "type",
                          "value": want}, res)
        return out[0], out[-1]

    for path, want in (("data.id", "string"), ("data.count", "integer"),
                       ("data.count", "number"), ("data.ratio", "number"),
                       ("data.active", "boolean"), ("data.tags", "array"),
                       ("data.meta", "object"), ("data.gone", "null")):
        check(f"type: {path} is {want}", is_type(path, want)[0], True)

    check("type: a float is not an integer", is_type("data.ratio", "integer")[0], False)
    check("type: a string is not an integer", is_type("data.id", "integer")[0], False)
    # the one that matters: true passing a numeric check would defeat the point
    check("type: a boolean is not a number", is_type("data.active", "number")[0], False)

    # people write the type in whatever language they think in
    for alias, canonical in (("str", "string"), ("int", "integer"), ("list", "array"),
                             ("dict", "object"), ("bool", "boolean"), ("none", "null")):
        path = {"string": "data.id", "integer": "data.count", "array": "data.tags",
                "object": "data.meta", "boolean": "data.active", "null": "data.gone"}[canonical]
        check(f"type: {alias!r} is understood as {canonical}", is_type(path, alias)[0], True)

    ok, detail = is_type("data.id", "widget")
    check("type: an unknown type fails", ok, False)
    check_true("and lists the ones JSON has", "string" in detail and "array" in detail, detail)

    ok, detail = is_type("data.id", "integer")
    check_true("a mismatch answers in JSON's words, not Python's",
               "got string" in detail and "str'" not in detail, detail)

    # A path that finds nothing used to report "got object", because the
    # sentinel for "absent" is a bare object() — which sent people hunting for
    # a type problem that was really a wrong path.
    ok, detail = is_type("data.nope", "integer")
    check("type: a path that finds nothing fails", ok, False)
    check_true("and never calls the absence an object",
               "object" not in detail, detail)
    check_true("it says the path found nothing",
               "nothing at that path" in detail, detail)
    check_true("and names what the response does have",
               "data.id" in detail, detail)
    check("json_type_name never reports the sentinel as a type",
          "object" in t.json_type_name(t.MISSING), False)

    # The sentinel for "absent" must never reach a message. It leaked as
    # "got <object object at 0x104311020>" — the tool showing its own internals
    # at the exact moment somebody needed an answer.
    body = {"data": {"items": [1, 2], "page": 1}}
    res2 = {"json": body, "status": 200, "ms": 1, "headers": {}, "text": "", "error": None}
    for op, value in (("equals", 13), ("gte", 1), ("contains", "x"),
                      ("length", 2), ("matches", "x"), ("in", [1, 2])):
        a = {"type": "jsonpath", "path": "data.length", "op": op, "value": value}
        ok, _, detail = t.evaluate(a, res2)
        check(f"absent value, {op}: fails", ok, False)
        check_true(f"absent value, {op}: says so in words",
                   "nothing at that path" in detail, detail)
        check_true(f"absent value, {op}: no object repr",
                   "object object" not in detail and "0x" not in detail, detail)

    ok, _, detail = t.evaluate({"type": "jsonpath", "path": "data.length",
                                "op": "equals", "value": 13}, res2)
    check_true("and it points at what the response does have",
               "data.items.length" in detail, detail)


def group_formats():
    import tests as t

    body = {"d": {"id": "4f767c7f-0eb4-4914-b89d-c5a64825cdaf",
                  "email": "qa@example.com", "when": "2026-09-23T10:00:00Z",
                  "day": "2026-09-23", "slug": "my-thing", "n": 5,
                  "bad": "not-a-uuid"}}
    res = {"json": body, "status": 200, "ms": 1, "headers": {}, "text": "", "error": None}

    def is_type(path, want):
        out = t.evaluate({"type": "jsonpath", "path": path, "op": "type",
                          "value": want}, res)
        return out[0], out[-1]

    # "is it a string" is rarely the question; "is it a uuid" usually is
    for path, want in (("d.id", "uuid"), ("d.email", "email"),
                       ("d.when", "date-time"), ("d.day", "date"),
                       ("d.slug", "slug")):
        check(f"format: {path} is {want}", is_type(path, want)[0], True)

    check("format: a string that is not a uuid fails", is_type("d.bad", "uuid")[0], False)
    check("format: a number is not a uuid", is_type("d.n", "uuid")[0], False)
    check("format: a uuid is still a string", is_type("d.id", "string")[0], True)

    # a name nobody can evaluate must be refused, not quietly passed
    ok, detail = is_type("d.id", "sku")
    check("format: an unknown name is refused", ok, False)
    check_true("and the refusal lists what can be checked",
               "uuid" in detail and "formats" in detail, detail)
    check("check_format says 'cannot say' for an unknown name",
          t.check_format("sku", "anything"), None)

    # the offered list grows from what the project's own tests use
    fake = [{"name": "x", "cases": [{"id": "a", "assertions": [
        {"type": "jsonpath", "path": "d.sku", "op": "type", "value": "sku-code"},
        {"type": "jsonpath", "path": "d.id", "op": "type", "value": "uuid"}]}]}]
    known = t.known_types(fake)
    check_true("types: JSON's own are always offered", "string" in known["json"])
    check_true("types: checkable formats are offered", "uuid" in known["formats"])
    check("types: a project's own type joins the list", known["in_use"], ["sku-code"])
    check_true("types: a builtin is not duplicated into in_use",
               "uuid" not in known["in_use"], str(known["in_use"]))


def group_per_env():
    import tests as t

    # 1. the same check, a different expected value per environment
    res = {"json": {"data": {"total": 250}}, "status": 200, "ms": 1,
           "headers": {}, "text": "", "error": None}
    shared = {"type": "jsonpath", "path": "data.total", "op": "equals",
              "value": "{{expectedTotal}}"}
    ok, *_ = t.evaluate(t.interpolate(shared, {"expectedTotal": 250}, strict=False), res)
    check("per-env: an expected value can come from the environment", ok, True)
    ok, *_ = t.evaluate(t.interpolate(shared, {"expectedTotal": 9}, strict=False), res)
    check("per-env: a different environment expects differently", ok, False)

    # 2. a check that only makes sense somewhere
    check_true("scope: only_on runs there",
               t.applies_here({"only_on": "prod"}, "prod"))
    check_true("scope: only_on is skipped elsewhere",
               not t.applies_here({"only_on": "prod"}, "mock"))
    check_true("scope: only_on accepts a list",
               t.applies_here({"only_on": ["dev", "prod"]}, "dev"))
    check_true("scope: except_on skips there",
               not t.applies_here({"except_on": ["mock"]}, "mock"))
    check_true("scope: except_on runs elsewhere",
               t.applies_here({"except_on": ["mock"]}, "dev"))
    check_true("scope: an unscoped assertion runs everywhere",
               t.applies_here({}, "anything") and t.applies_here({}, None))
    check_true("scope: matching ignores case",
               t.applies_here({"only_on": "PROD"}, "prod"))

    # 3. the shapes that are wrong should be refused, not quietly ignored
    errors = t.validate_test({"id": "x",
        "request": {"method": "GET", "path": "/x"},
        "assertions": [{"type": "jsonpath", "path": "a", "op": "exists",
                        "only_on": "dev", "except_on": "prod"}]})
    check_true("scope: only_on and except_on together is refused",
               any("not both" in e for e in errors), str(errors))
    errors = t.validate_test({"id": "x",
        "request": {"method": "GET", "path": "/x"},
        "assertions": [{"type": "jsonpath", "path": "a", "op": "exists",
                        "only_on": 7}]})
    check_true("scope: a nonsense environment name is refused",
               any("only_on" in e for e in errors), str(errors))
    ok = t.validate_test({"id": "x",
        "request": {"method": "GET", "path": "/x"},
        "assertions": [{"type": "jsonpath", "path": "a", "op": "exists",
                        "only_on": ["dev", "prod"]}]})
    check("scope: a correct scope passes validation", ok, [])


def group_overlay_conflict():
    import mockd

    # A curated payload is a convenience, not a licence to contradict the
    # document. These outlive the spec they were generated from: one paginated
    # a list the document declares as a bare array, so every test written
    # against the mock learned data.items[0].id when the real server answers
    # data[0].id.
    array_schema = {"type": "object", "properties": {
        "data": {"anyOf": [{"type": "array", "items": {}}, {"type": "null"}]}}}

    agrees = {"data": [{"id": 1}]}
    check("overlay: a body that matches the document is kept",
          mockd._overlay_conflict(array_schema, agrees), None)

    contradicts = {"data": {"items": [{"id": 1}], "total": 1}}
    why = mockd._overlay_conflict(array_schema, contradicts)
    check_true("overlay: a body that contradicts it is refused", bool(why), str(why))
    check_true("and the reason names where", why and "data" in why, str(why))

    # where the document says nothing, the overlay is the only answer there is
    check("overlay: nothing declared means nothing to contradict",
          mockd._overlay_conflict(None, contradicts), None)
    check("overlay: no body, no conflict",
          mockd._overlay_conflict(array_schema, None), None)

    # an unjudgeable schema must not take the mock down or block the answer
    check("overlay: an unusable schema does not interfere",
          mockd._overlay_conflict({"$ref": "#/nope"}, contradicts), None)


def group_blocked():
    import tests as t

    runner = t.Runner.__new__(t.Runner)
    runner.base_url = "http://example.invalid"
    runner.headers, runner.spec, runner.timeout = {}, None, 1
    runner.verbose, runner.readonly, runner.env_name = False, False, "mock"

    # BLOCKED means the endpoint under test never ran. A flow where every step
    # is labelled setup has no endpoint under test, so reporting blocked told
    # the reader to fix a setup that IS the test — and no re-run could ever
    # clear it.
    def roles(steps, kind="api"):
        seen = []
        declared = steps
        has_target = any(st.get("role") == "target" for st in declared)
        for i, st in enumerate(declared):
            if kind == "e2e":
                seen.append(st.get("role") or "step")
            elif not has_target and i == len(declared) - 1:
                seen.append("target")
            else:
                seen.append(st.get("role") or ("target" if i == len(declared) - 1 else "setup"))
        return seen

    check("a lone step labelled setup is the target",
          roles([{"role": "setup"}]), ["target"])
    check("an explicit target is respected",
          roles([{"role": "setup"}, {"role": "target"}]), ["setup", "target"])
    check("unlabelled steps: the last one is the target",
          roles([{}, {}, {}]), ["setup", "setup", "target"])
    check("all setup with no target: the last becomes the target",
          roles([{"role": "setup"}, {"role": "setup"}]), ["setup", "target"])
    check("an e2e flow has no setup at all",
          roles([{}, {}], kind="e2e"), ["step", "step"])


GROUPS = {
    "classification": group_classification,
    "taxonomy": group_taxonomy,
    "selection": group_selection,
    "binding": group_binding,
    "readonly": group_readonly,
    "runid": group_runid,
    "rebind": group_rebind,
    "story": group_story,
    "length": group_length,
    "types": group_types,
    "formats": group_formats,
    "per_env": group_per_env,
    "overlay_conflict": group_overlay_conflict,
    "blocked": group_blocked,
}


def main():
    wanted = sys.argv[1:] or list(GROUPS)
    for name in wanted:
        if name not in GROUPS:
            print(f"unknown group {name!r}; have: {', '.join(GROUPS)}")
            return 2
        print(f"\n{name}")
        try:
            GROUPS[name]()
        except Exception:
            FAILED.append((f"{name} (raised)", traceback.format_exc().strip().splitlines()[-1]))
        for entry in PASSED:
            print(f"  ok    {entry}")
        for entry, detail in FAILED:
            print(f"  FAIL  {entry}  — {detail}")
        PASSED.clear()
        if FAILED:
            break

    print()
    if FAILED:
        print(f"{len(FAILED)} failed")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
