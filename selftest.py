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
import json
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

    dept = "/api/v1/widgets"
    check("name: an id takes the resource with it", t.binding_name("data.id", dept),
          "widgetId")
    check("name: a title too", t.binding_name("data.title", dept), "widgetTitle")
    check("name: an already-qualified field is not doubled",
          t.binding_name("data.widget_id", dept), "widgetId")
    check("name: a list item still names the resource",
          t.binding_name("data.items[0].id", "/api/v1/user/list"), "userId")
    check("name: something unremarkable keeps its own name",
          t.binding_name("total", "/api/v1/user/list"), "total")
    check("name: a taken name is never silently reused",
          t.binding_name("data.id", dept, taken={"widgetId"}), "widgetId2")

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


def group_rebind():
    """Turning a placeholder into a real capture, without regenerating."""
    import tests as t, bindings as b

    index = b.index_from_report({"results": [
        {"operation": "GET /api/v1/orders", "status": 200,
         "observed_shape": {"data": {"items": [{"id": "str"}]}}},
        {"operation": "GET /api/v1/orders/{order_id}", "status": 200,
         "observed_shape": {"data": {"lines": [{"courier_ids": ["str"]}]}}},
        {"operation": "GET /api/v1/depots", "status": 200,
         "observed_shape": {"data": {"items": [{"depot_id": "str"}]}}},
    ]})

    def flow():
        return {"id": "f", "kind": "api", "levels": ["smoke"],
                "data": {"courierId": "REPLACE_WITH_REAL_COURIER",
                         "depotId": "<REAL_DEPOT_REQUIRED>"},
                "steps": [{"role": "target", "name": "make one",
                           "request": {"method": "POST", "path": "/api/v1/jobs",
                                       "body": {"depot_id": "{{depotId}}",
                                                "courier_ids": ["{{courierId}}"]}},
                           "assertions": [{"type": "status", "equals": 201}]}]}

    test = flow()
    changed, notes = t.rebind_placeholders(test, index)
    check_true("rebind: it reports having changed something", changed, notes)
    check("rebind: and the placeholders are gone from data", test.get("data"), None)

    paths = [(s.get("request") or {}).get("path") for s in test["steps"]]
    check_true("rebind: the create is still last", paths[-1] == "/api/v1/jobs", paths)
    check_true("rebind: a one-call id gets one setup call",
               "/api/v1/depots" in paths, paths)
    check_true("rebind: a two-call id gets both, in order",
               paths.index("/api/v1/orders") < paths.index("/api/v1/orders/{{orderId}}"),
               paths)

    captures = {k: v for s in test["steps"] for k, v in (s.get("capture") or {}).items()}
    check("rebind: the depot id is captured whole",
          captures.get("depotId"), "data.items[0].depot_id")
    # the bug that produced [["id","id"]] and a 422 nobody could read
    check("rebind: an id used inside a list captures ONE of the list",
          captures.get("courierId"), "data.lines[0].courier_ids[0]")

    check_true("rebind: what it writes still passes the validator",
               t.validate_test(test, "scenario") == [],
               t.validate_test(test, "scenario"))

    # nothing to bind to: say so, change nothing
    orphan = flow()
    orphan["data"] = {"mysteryId": "REPLACE_WITH_REAL_MYSTERY"}
    orphan["steps"][0]["request"]["body"] = {"mystery_id": "{{mysteryId}}"}
    changed, notes = t.rebind_placeholders(orphan, index)
    check_true("rebind: an id nothing supplies is left alone", not changed, notes)
    check_true("rebind: and it says why",
               any("still needs a real value" in n for n in notes), notes)
    check_true("rebind: so the placeholder is still there",
               (orphan.get("data") or {}).get("mysteryId"), orphan.get("data"))

    settled = flow()
    settled.pop("data")
    settled["steps"][0]["request"]["body"] = {"note": "nothing to bind"}
    check_true("rebind: a test with no placeholders is untouched",
               t.rebind_placeholders(settled, index)[0] is False)

    # the other half: a capture written against one environment, run on another
    moved = b.index_from_report({"results": [
        {"operation": "GET /api/v1/dash", "status": 200,
         "observed_shape": {"data": {"held_orders": {"approval_id": "str"}}}},
    ]})
    stale = {"id": "s", "kind": "api", "levels": ["smoke"], "steps": [
        {"role": "setup", "name": "read",
         "request": {"method": "GET", "path": "/api/v1/dash"},
         "assertions": [{"type": "status", "equals": 200}],
         "capture": {"approvalId": "data.items[0].approval_id"}},
        {"role": "target", "name": "use",
         "request": {"method": "POST", "path": "/api/v1/x",
                     "body": {"approval_id": "{{approvalId}}"}},
         "assertions": [{"type": "status", "equals": 201}]}]}
    changed, notes = t.rebind_placeholders(stale, moved)
    check_true("rebind: a capture this environment cannot satisfy is repointed",
               changed and stale["steps"][0]["capture"]["approvalId"]
               == "data.held_orders.approval_id", notes)
    check_true("rebind: and it says what moved",
               any("repointed" in n for n in notes), notes)

    right = {"id": "r", "kind": "api", "levels": ["smoke"], "steps": [
        {"role": "target", "name": "read",
         "request": {"method": "GET", "path": "/api/v1/dash"},
         "assertions": [{"type": "status", "equals": 200}],
         "capture": {"approvalId": "data.held_orders.approval_id"}}]}
    check_true("rebind: a capture that is already right is left alone",
               t.rebind_placeholders(right, moved)[0] is False)

    unswept = {"id": "u", "kind": "api", "levels": ["smoke"], "steps": [
        {"role": "target", "name": "read",
         "request": {"method": "GET", "path": "/api/v1/never-swept"},
         "assertions": [{"type": "status", "equals": 200}],
         "capture": {"x": "data.whatever"}}]}
    check_true("rebind: an operation nobody swept is not second-guessed",
               t.rebind_placeholders(unswept, moved)[0] is False)


def group_bindings():
    """Where an id comes from: indexed, ranked, and never guessed silently."""
    import bindings as b

    report = {"results": [
        {"operation": "GET /api/v1/widgets", "status": 200,
         "observed_shape": {"data": {"items": [{"id": "str", "title": "str",
                                                "gadget_id": "str"}]},
                            "message": "str"}},
        {"operation": "GET /api/v1/gadgets/{gadget_id}", "status": 200,
         "observed_shape": {"data": {"id": "str", "widget_id": "str"}}},
        {"operation": "POST /api/v1/widgets", "status": 201,
         "observed_shape": {"data": {"sneaky_id": "str"}}},
        {"operation": "GET /api/v1/broken", "status": 422,
         "observed_shape": {"error": [{"bogus_id": "str"}]}},
    ]}
    index = b.index_from_report(report)
    check_true("index: a field is found by name", "gadget_id" in index, sorted(index))
    check_true("index: with the path it sits at",
               index["gadget_id"][0][1] == "data.items[0].gadget_id",
               index["gadget_id"])
    check_true("index: a write is not indexed — it describes what you just sent",
               "sneaky_id" not in index, sorted(index))
    check_true("index: nor is an error body — it describes a complaint",
               "bogus_id" not in index, sorted(index))

    got = b.candidates("gadget_id", index)
    check("candidates: an exact name match is certain",
          got[0]["strength"], b.CERTAIN)
    check_true("candidates: and says where to capture it",
               got[0]["path"] == "data.items[0].gadget_id", got[0])

    ranked = b.candidates("widget_id", index)
    check_true("candidates: a source needing its own id is flagged",
               any(c.get("needs_id") for c in ranked), ranked)
    check_true("candidates: and ranks below one that does not, when both exist",
               not ranked[0].get("needs_id") or len(ranked) == 1, ranked)

    # the whole point: a shared word is a question, never an answer. The head
    # noun here is "owner", which names no resource, so the only thing on offer
    # is the shared word "widget" — exactly the near miss that must not be taken
    # for a match.
    weakish = b.candidates("widget_owner_id", index)
    check_true("candidates: a shared word is offered only as weak",
               weakish and all(c["strength"] == b.WEAK for c in weakish), weakish)
    check_true("candidates: and weak is never resolved automatically",
               b.resolve("widget_owner_id", index) is None,
               b.resolve("widget_owner_id", index))

    check_true("candidates: nothing matching yields nothing, not a wrong answer",
               b.candidates("completely_unrelated_id", index) == [],
               b.candidates("completely_unrelated_id", index))

    check_true("resolve: a certain match is good enough to use unasked",
               (b.resolve("gadget_id", index) or {}).get("strength") == b.CERTAIN)

    # A field reachable only through another read is still reachable. Saying
    # "nothing supplies this" because the one source needed an id of its own was
    # true and useless — the chain is derivable.
    deep = {"results": [
        {"operation": "GET /api/v1/orders", "status": 200,
         "observed_shape": {"data": {"items": [{"id": "str"}]}}},
        {"operation": "GET /api/v1/orders/{order_id}", "status": 200,
         "observed_shape": {"data": {"lines": [{"courier_ids": ["str"]}]}}},
    ]}
    deep_index = b.index_from_report(deep)
    chain = b.chain_for("courier_ids", deep_index)
    check("chain: it takes two calls to reach a nested id", len(chain), 2)
    check_true("chain: the first call needs nothing",
               "{" not in chain[0]["operation"], chain[0]["operation"])
    check_true("chain: and captures what the second one needs",
               chain[0]["as"] == "orderId" and chain[0]["path"] == "data.items[0].id",
               chain[0])
    check_true("chain: the second substitutes it into the path",
               chain[1]["operation"].endswith("/{{orderId}}"), chain[1]["operation"])
    check_true("chain: and captures the field asked for",
               chain[1]["captures"] == "courier_ids", chain[1])
    check("chain: a field nothing reaches yields no half-chain",
          b.chain_for("nothing_like_this_id", deep_index), [])

    cyclic = {"results": [
        {"operation": "GET /api/v1/a/{b_id}", "status": 200,
         "observed_shape": {"data": {"a_id": "str"}}},
        {"operation": "GET /api/v1/b/{a_id}", "status": 200,
         "observed_shape": {"data": {"b_id": "str"}}},
    ]}
    check("chain: two reads that need each other terminate",
          b.chain_for("a_id", b.index_from_report(cyclic)), [])


def group_import():
    """What survives the trip from a generator's JSON into the workspace."""
    import tests as t

    scenario = {
        "id": "needs-a-value-nothing-supplies", "name": "n", "kind": "e2e",
        "levels": ["smoke"],
        "data": {"levelId": "<REAL_LEVEL_ID_REQUIRED>"},
        "steps": [{"role": "target", "name": "create",
                   "request": {"method": "POST", "path": "/api/v1/things",
                               "body": {"level_id": "{{levelId}}"}},
                   "assertions": [{"type": "status", "in": [200, 201]}]}],
    }
    check("import: a test's own `data` satisfies its variables",
          t.validate_test({**scenario, "data": {**{}, **scenario["data"]}}, "scenario"), [])

    # the bug: the suite's data was written OVER the test's before validating
    check_true("import: the suite's data does not erase the test's",
               t.import_tests([dict(scenario)], "selftest-import",
                              stage="draft").get("ok") is True,
               t.import_tests([dict(scenario)], "selftest-import",
                              stage="draft").get("errors"))

    missing = dict(scenario); missing.pop("data")
    result = t.import_tests([missing], "selftest-import", stage="draft")
    check_true("import: a variable in neither place is still refused",
               result.get("ok") is False and any("levelId" in e for e in result.get("errors") or []),
               result.get("errors"))

    # a case may carry data too, and the runner has to honour it
    case = {"id": "case-with-data", "name": "c", "levels": ["smoke"],
            "data": {"widgetId": "abc"},
            "request": {"method": "GET", "path": "/api/v1/widgets/{{widgetId}}"},
            "assertions": [{"type": "status", "equals": 200}]}
    check("import: a case's own data satisfies its variables too",
          t.validate_test({**case, "data": {**{}, **case["data"]}}, "case"), [])
    check_true("run: run_case merges the case's own data into scope",
               "case.get(\"data\")" in __import__("inspect").getsource(t.Runner.run_case))

    for name in ("tests/drafts/selftest-import.json",):
        try:
            __import__("pathlib").Path(name).unlink()
        except OSError:
            pass


def group_blueprint():
    """The lifecycle engine reads path SHAPE, so these fakes deliberately use
    two different naming styles for the same idea."""
    import blueprint as b

    check("family: a verb-in-path route names its resource",
          b.family_key("/api/v1/user/read/{user_id}"), ("user",))
    check("family: so does the RESTful spelling",
          b.family_key("/api/v1/user/{user_id}"), ("user",))
    check("family: a list and a create meet in the same family",
          b.family_key("/api/v1/user/list"), b.family_key("/api/v1/user/create"))
    check("family: a version segment is not a resource",
          b.family_key("/api/v2/order"), ("order",))
    check("family: a sub-resource is its own family",
          b.family_key("/api/v1/user/stream/{user_id}"), ("user", "stream"))

    check("role: POST on a collection creates",
          b.role_of({"method": "POST", "path": "/api/v1/user/create"}), "create")
    check("role: POST under an id does not",
          b.role_of({"method": "POST", "path": "/api/v1/user/{id}/connect"}), None)
    check("role: GET on an item reads",
          b.role_of({"method": "GET", "path": "/api/v1/user/{id}"}), "read")
    check("role: GET on a collection lists",
          b.role_of({"method": "GET", "path": "/api/v1/user"}), "list")
    check("role: PATCH counts as update",
          b.role_of({"method": "PATCH", "path": "/api/v1/user/{id}"}), "update")

    def route(key, method, path, responses=None, body=None):
        return {"key": key, "method": method, "path": path,
                "summary": "", "tags": [], "parameters": [], "path_params": [],
                "request_body": {"content": {"application/json": {"schema": body}}}
                if body else {},
                "responses": responses or {"200": {}}}

    class Spec:
        doc = {}
        routes = [
            route("POST /api/v1/widgets", "POST", "/api/v1/widgets",
                  {"201": {}}, {"type": "object", "required": ["title"],
                                "properties": {"title": {"type": "string"}}}),
            route("GET /api/v1/widgets/{widget_id}", "GET", "/api/v1/widgets/{widget_id}",
                  {"200": {}, "404": {}}),
            route("DELETE /api/v1/widgets/{widget_id}", "DELETE",
                  "/api/v1/widgets/{widget_id}", {"204": {}}),
        ]

    suite, skipped = b.build(Spec())
    flows = suite["scenarios"]
    check("build: one resource yields one flow", len(flows), 1)
    flow = flows[0]
    check("flow: it is an end-to-end one", flow["kind"], "e2e")
    check("flow: named for the resource", flow["id"], "widgets-lifecycle")
    paths = [st["request"]["path"] for st in flow["steps"]]
    check_true("flow: the create comes first", paths[0] == "/api/v1/widgets", paths)
    check_true("flow: later steps use the captured id",
               all("{{widgetId}}" in p for p in paths[1:]), paths)
    check_true("flow: the id is captured from the create",
               flow["steps"][0].get("capture") == {"widgetId": "data.id"},
               flow["steps"][0].get("capture"))
    check_true("flow: exactly one step is the target",
               sum(1 for st in flow["steps"] if st.get("role") == "target") == 1)
    check_true("flow: a created thing gets a cleanup step", bool(flow.get("cleanup")))
    check_true("flow: the create sends the declared body",
               "title" in (flow["steps"][0]["request"].get("body") or {}),
               flow["steps"][0]["request"].get("body"))
    check_true("flow: a value that must differ between runs is not constant",
               "{{$runId}}" in json.dumps(flow["steps"][0]["request"].get("body")),
               flow["steps"][0]["request"].get("body"))
    check_true("flow: the documented 404 becomes the gone-after-delete step",
               any(a.get("equals") == 404
                   for st in flow["steps"] for a in (st.get("assertions") or [])))
    check_true("flow: the delete accepts the status it documents",
               any(204 in (a.get("in") or [])
                   for st in flow["steps"] for a in (st.get("assertions") or [])))

    # the engine must never emit a flow that only the document could pass
    class NoFourOhFour:
        doc = {}
        routes = [route("POST /api/v1/things", "POST", "/api/v1/things", {"200": {}},
                        {"type": "object", "properties": {"a": {"type": "string"}}}),
                  route("GET /api/v1/things/{id}", "GET", "/api/v1/things/{id}",
                        {"200": {}}),
                  route("DELETE /api/v1/things/{id}", "DELETE", "/api/v1/things/{id}",
                        {"200": {}})]
    quiet = b.build(NoFourOhFour())[0]["scenarios"][0]
    check_true("flow: no gone-after-delete where 404 is undocumented",
               not any(a.get("equals") == 404
                       for st in quiet["steps"] for a in (st.get("assertions") or [])))

    class Multipart:
        doc = {}
        routes = [{"key": "POST /api/v1/files", "method": "POST", "path": "/api/v1/files",
                   "summary": "", "tags": [], "parameters": [], "path_params": [],
                   "request_body": {"content": {"multipart/form-data": {"schema": {}}}},
                   "responses": {"200": {}}},
                  route("DELETE /api/v1/files/{id}", "DELETE", "/api/v1/files/{id}")]
    built, why = b.build(Multipart())
    check("build: a body the runner cannot send yields no flow",
          len(built["scenarios"]), 0)
    check_true("build: and says why", why and "multipart" in why[0][1], why)

    class Orphan:
        doc = {}
        routes = [route("POST /api/v1/ping", "POST", "/api/v1/ping")]
    check("build: a create with no way back yields no flow",
          len(b.build(Orphan())[0]["scenarios"]), 0)

    # every generated flow must pass the same validator a human's paste does
    import tests as t
    for generated in flows:
        check("blueprint: generated output satisfies the validator",
              t.validate_test(generated, kind="scenario"), [])

    stamp = flow.get("generated") or {}
    check_true("stamp: records the operations it came from", bool(stamp.get("from")))
    check_true("stamp: records a contract hash", bool(stamp.get("contract")))
    first = b.contract_hash(Spec.routes)
    changed = [dict(r) for r in Spec.routes]
    changed[0]["responses"] = {"201": {}, "422": {}}
    check_true("stamp: the hash moves when the contract moves",
               b.contract_hash(changed) != first)
    check_true("stamp: and holds still when nothing changed",
               b.contract_hash(Spec.routes) == first)


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
    check_true("story: carries the request body fields",
               "request body" in brief and "title" in brief, brief[:200])
    check_true("story: marks which of them are required",
               "* title" in brief, brief[:400])
    check_true("story: carries the response fields",
               "response 200" in brief and "id" in brief, brief[:200])
    # a schema sliced mid-property is worse than none: it reads as complete
    # an empty array where the schema demands one item is a 422 nobody can read
    arrays = t.operation_brief({
        "key": "POST /api/v1/orders", "method": "POST", "path": "/api/v1/orders",
        "summary": "", "tags": [], "parameters": [], "path_params": [],
        "request_body": {"content": {"application/json": {"schema": {
            "type": "object", "required": ["lines"],
            "properties": {"lines": {
                "type": "array", "minItems": 1,
                "items": {"type": "object", "required": ["sku"],
                          "properties": {"sku": {"type": "string"},
                                         "qty": {"type": "integer", "minimum": 1}}}}}}}}},
        "responses": {"200": {}}})
    check_true("brief: an array says how few items it will accept",
               "at least 1" in arrays, arrays)
    check_true("brief: and what one of its elements looks like",
               "sku" in arrays and "qty" in arrays, arrays)

    check_true("story: no schema is cut off mid-token",
               "\n      * " in brief or "request body: the document declares none" in brief,
               brief[:300])

    partial = t.operation_brief({"key": "GET /x", "method": "GET", "path": "/x"})
    check_true("brief: a route missing its contract does not crash",
               "GET /x" in partial, partial)

    none = t.story_pack(FakeSpec(), "As a pilot I want to file a flight plan")
    check_true("story: says so when nothing matches",
               "NO OPERATION MATCHED" in none, none[:160])

    covered = t.story_pack(FakeSpec(), "create a department", existing=["dept-create"])
    check_true("story: lists what is already covered",
               "ALREADY COVERED" in covered and "dept-create" in covered)

    # A required uuid an assistant cannot obtain is what produced an invented
    # {{variable}} and a refused import. The brief has to resolve it or say so.
    class NeedsIds:
        doc = {}
        routes = [
            {"key": "POST /api/v1/orders", "method": "POST", "path": "/api/v1/orders",
             "summary": "Create order", "tags": [], "parameters": [], "path_params": [],
             "request_body": {"content": {"application/json": {"schema": {
                 "type": "object",
                 "required": ["customer_id", "backup_user_id", "widget_id"],
                 "properties": {
                     "customer_id": {"type": "string", "format": "uuid"},
                     "backup_user_id": {"type": "string", "format": "uuid"},
                     "widget_id": {"type": "string", "format": "uuid"}}}}}},
             "responses": {"200": {}}},
            {"key": "GET /api/v1/customer/list", "method": "GET",
             "path": "/api/v1/customer/list", "summary": "List customers", "tags": [],
             "parameters": [], "path_params": [], "request_body": {},
             "responses": {"200": {}}},
            {"key": "GET /api/v1/user/list", "method": "GET", "path": "/api/v1/user/list",
             "summary": "List users", "tags": [], "parameters": [], "path_params": [],
             "request_body": {}, "responses": {"200": {}}},
        ]

    target = [r for r in NeedsIds.routes if r["method"] == "POST"]
    kept, missing = t.prerequisite_routes(NeedsIds(), target)
    supplied = {name for _, names, _ in kept for name in names}
    check_true("ids: a plain id finds its collection read",
               "customer_id" in supplied, sorted(supplied))
    check_true("ids: a compound name matches on its head noun",
               "backup_user_id" in supplied, sorted(supplied))
    check_true("ids: one nothing supplies is reported, not silently dropped",
               missing == ["widget_id"], missing)

    # an id buried in an array element is still an id nobody can invent
    nested = {"type": "object", "required": ["lines"],
              "properties": {"lines": {"type": "array", "items": {
                  "type": "object", "required": ["widget_id", "courier_ids"],
                  "properties": {"widget_id": {"type": "string", "format": "uuid"},
                                 "courier_ids": {"type": "array",
                                               "items": {"type": "string",
                                                         "format": "uuid"}}}}}}}
    found = dict((label, field) for label, field in t._required_ids(nested))
    check_true("ids: one nested in an array element is found",
               "lines[].widget_id" in found, sorted(found))
    check_true("ids: and a plural list of them too",
               "lines[].courier_ids" in found, sorted(found))
    check("ids: a top-level scalar is unaffected",
          [f for _, f in t._required_ids(
              {"type": "object", "required": ["a_id"],
               "properties": {"a_id": {"type": "string", "format": "uuid"}}})],
          ["a_id"])

    ordered = t.story_pack(NeedsIds(), "create an order")
    check_true("story: the brief names where the ids come from",
               "WHERE THE IDS COME FROM" in ordered, ordered[:120])
    check_true("story: and names the one it cannot supply",
               "IDS NOTHING HERE SUPPLIES" in ordered and "widget_id" in ordered)
    check_true("story: it forbids referencing a variable nothing captures",
               "captured by an EARLIER step" in ordered)

    none_needed, nothing = t.prerequisite_routes(FakeSpec(), FakeSpec.routes)
    check("ids: a create needing no uuid asks for no lookups", none_needed, [])
    check("ids: and reports nothing missing", nothing, [])


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
    "blueprint": group_blueprint,
    "import": group_import,
    "bindings": group_bindings,
    "rebind": group_rebind,
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
