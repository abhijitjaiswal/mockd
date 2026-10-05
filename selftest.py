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


def group_references():
    """A mock that knows which rows exist, and says so."""
    import mockd as m
    import blueprint as b

    store = m.StateStore()
    store.data = {
        "/api/v1/customers": {"c-1": {"id": "c-1", "region": "north"},
                              "c-2": {"id": "c-2", "region": "south"}},
        "/api/v1/customers/dropdown": {"x-9": {"id": "x-9", "label": "one"}},
        "/api/v1/orders": {"o-1": {"id": "o-1", "customer_id": "made-up",
                                   "lines": [{"backup_customer_id": "also-made-up"}]}},
        "/api/v1/orders/seed/notes": {"n-1": {"id": "n-1"}},
    }

    check("held: a resource the mock holds has its rows' ids",
          store._held("customer_id"), {"c-1", "c-2"})
    check("held: a compound name is read by its last word",
          store._held("backup_customer_id"), {"c-1", "c-2"})
    grown = m.StateStore()
    grown.data = {"/api/v1/maps": {"m-1": {"id": "m-1", "level_id": "seeded-level"}},
                  "/api/v1/depts/d-1/levels": {"new-level": {"id": "new-level"}}}
    check_true("known: an id the mock's own rows point at still exists after "
               "the first real one is created",
               {"seeded-level", "new-level"} <= grown.known("level_id"),
               grown.known("level_id"))

    check("known: a resource it holds nothing for is no evidence, not 'none exist'",
          store.known("warehouse_id"), None)
    check("known: a list seeded under a placeholder parent is not evidence",
          store.known("note_id"), None)

    store.link()

    check("path: an id that is not held is reported",
          store.missing_reference({"customer_id": "nope"}), ("customer_id", "nope"))
    check("path: one that is held is fine",
          store.missing_reference({"customer_id": "c-1"}), None)
    check("path: an id for a resource it knows nothing about is left alone",
          store.missing_reference({"warehouse_id": "anything"}), None)

    check("body: a made-up reference is caught",
          store.unknown_references({"customer_id": "nope"}), [("customer_id", "nope")])
    check("body: so is one nested in a list",
          store.unknown_references({"lines": [{"customer_id": "nope"}]}),
          [("lines.customer_id", "nope")])
    check("body: a list of ids is checked too",
          store.unknown_references({"customer_ids": ["c-1", "nope"]}),
          [("customer_ids", "nope")])
    check("body: real references pass",
          store.unknown_references({"customer_id": "c-2", "customer_ids": ["c-1"]}), [])
    check("body: a new row's own identifier is not a reference",
          store.unknown_references({"customer_id": "brand-new"}, own="customer"), [])

    route = {"parameters": [{"name": "region", "in": "query"},
                            {"name": "page", "in": "query"}]}
    rows = list(store.data["/api/v1/customers"].values())
    check("filter: a declared parameter that is a field narrows the rows",
          [r["id"] for r in store._filtered(route, rows, {"region": "south"})], ["c-2"])
    check("filter: a value nothing has returns nothing",
          store._filtered(route, rows, {"region": "west"}), [])
    check("filter: paging words never select rows",
          len(store._filtered(route, rows, {"page": "2"})), 2)
    check("filter: an undeclared parameter is not ours to interpret",
          len(store._filtered(route, rows, {"colour": "red"})), 2)

    order = store.data["/api/v1/orders"]["o-1"]
    check_true("link: a seeded foreign key now points at a row that exists",
               order["customer_id"] in {"c-1", "c-2"}, order)
    check_true("link: nested ones too",
               order["lines"][0]["backup_customer_id"] in {"c-1", "c-2"}, order)
    check_true("link: a dropdown shows the ids of the rows it lists",
               set(store.data["/api/v1/customers/dropdown"]) <= {"c-1", "c-2"},
               list(store.data["/api/v1/customers/dropdown"]))
    check("link: a row keeps its own id", order["id"], "o-1")

    # the generator must not invent what the mock would now refuse
    body = {"customer_id": "11111111-2222-3333-4444-555555555555", "order_id": "new",
            "lines": [{"courier_ids": ["x"], "note": "keep"}]}
    data = {}
    b.plant_references(body, "order", data)
    check("plant: a foreign key becomes a named variable",
          body["customer_id"], "{{customerId}}")
    check("plant: the new row's own id is left as it was", body["order_id"], "new")
    own = {"order_id": "11111111-2222-3333-4444-555555555555"}
    b.plant_references(own, "order", {})
    check("plant: unless it is a uuid, which must differ on every run",
          own["order_id"], "{{$uuid}}")
    check("plant: a list of ids becomes a one-item list of a variable",
          body["lines"][0]["courier_ids"], ["{{courierId}}"])
    check("plant: other fields are untouched", body["lines"][0]["note"], "keep")
    check_true("plant: each variable starts as an honest placeholder",
               all(v.startswith("<") for v in data.values()) and len(data) == 2, data)


def group_bug():
    """A failure written up so somebody else can act on it without asking."""
    import tests as t

    item = {"id": "order-create", "name": "An order can be created", "priority": "P1",
            "links": ["ABC-12"], "description": "Creating an order works.",
            "outcome": "fail",
            "verdict": {"headline": "The API contradicted its own spec",
                        "evidence": ["returned 500"], "next": "provider to fix"},
            "steps": [
                {"name": "pick a customer", "outcome": "pass", "status": 200,
                 "request": {"method": "GET", "url": "http://dev/customers"},
                 "checks": [{"ok": True, "label": "status is 200"}]},
                {"name": "create the order", "outcome": "fail", "status": 500,
                 "request": {"method": "POST", "url": "http://dev/orders",
                             "headers": {"Cookie": "access_token=SECRET"},
                             "body": {"customer_id": "c-1", "note": "it's"}},
                 "checks": [{"ok": False, "label": "status in [200, 201]",
                             "why": "500 not one of [200, 201]"}],
                 "response_excerpt": '{"detail":"boom"}'}]}
    title, text = t.bug_report(item, "orders", {"env": "dev", "base_url": "http://dev",
                                                "ran_at": "2026-01-01T00:00:00Z"})
    check_true("bug: the title names the test and the server",
               "An order can be created" in title and "dev" in title, title)
    check_true("bug: it states what was expected and what happened",
               "**Expected:** status in [200, 201]" in text
               and "**Actual:** 500 not one of [200, 201]" in text, text[:300])
    check_true("bug: it says whose problem the evidence points at",
               "The API contradicted its own spec" in text)
    check_true("bug: every step is listed with how far it got",
               "pick a customer" in text and "(ok)" in text and "(FAILED)" in text)
    check_true("bug: the failing request is a curl that can be pasted",
               "curl -X POST 'http://dev/orders'" in text and '"customer_id": "c-1"' in text)
    check_true("bug: a quote in the body does not break the command",
               "it'\\''s" in text, text)
    check_true("bug: the response is included", '{"detail":"boom"}' in text)
    check_true("bug: no credential is ever written into it",
               "SECRET" not in text and "access_token" not in text)
    check_true("bug: it tells whoever runs it to add their own",
               "add your own credentials" in text)
    check_true("bug: the ticket and priority travel with it",
               "ABC-12" in text and "priority P1" in text)

    lone = {"id": "x", "name": "one call", "outcome": "fail",
            "steps": [{"name": "call", "outcome": "fail", "status": 404,
                       "request": {"method": "GET", "url": "http://dev/x"},
                       "checks": [{"ok": False, "label": "status is 200", "why": "got 404"}]}]}
    _, short = t.bug_report(lone, "m", {})
    check_true("bug: a single-call test has no step list to wade through",
               "**Steps**" not in short and "curl -X GET" in short)


def group_baseline():
    """Regenerating must never overwrite somebody's work."""
    import blueprint as b

    def case(ident, path="/a", **extra):
        return {"id": ident, "request": {"method": "GET", "path": path},
                "assertions": [{"type": "status", "equals": 200}], **extra}

    fresh = b.stamp({"name": "baseline", "data": {},
                     "cases": [case("one"), case("two")], "scenarios": []})
    check_true("stamp: every generated test carries what it looked like",
               all((c.get("generated") or {}).get("fingerprint") for c in fresh["cases"]))
    check_true("untouched: a test nobody edited is recognised", b.untouched(fresh["cases"][0]))

    saved, done = b.merge(None, fresh)
    check("merge: the first time, everything is new", done["added"], 2)

    again = b.stamp({"name": "baseline", "data": {},
                     "cases": [case("one"), case("two")], "scenarios": []})
    _, done = b.merge(saved, again)
    check("merge: nothing changed means nothing is rewritten",
          (done["unchanged"], done["updated"], done["added"]), (2, 0, 0))

    # somebody makes one theirs
    saved["cases"][0]["priority"] = "P0"
    check_true("untouched: an edited test is no longer ours to replace",
               not b.untouched(saved["cases"][0]))
    moved = b.stamp({"name": "baseline", "data": {},
                     "cases": [case("one", "/changed"), case("two", "/changed")],
                     "scenarios": []})
    merged, done = b.merge(saved, moved)
    by_id = {c["id"]: c for c in merged["cases"]}
    check("merge: the edited one is kept exactly as it was",
          (by_id["one"]["priority"], by_id["one"]["request"]["path"]), ("P0", "/a"))
    check("merge: the untouched one follows the document",
          by_id["two"]["request"]["path"], "/changed")
    check("merge: and the count says so", (done["kept_edited"], done["updated"]), (1, 1))

    # an operation leaves the document
    gone = b.stamp({"name": "baseline", "data": {}, "cases": [case("one")],
                    "scenarios": []})
    merged, done = b.merge(merged, gone)
    check_true("merge: an untouched test for a removed operation goes with it",
               "two" not in {c["id"] for c in merged["cases"]} and done["removed"] == 1,
               done)
    check_true("merge: an edited one stays even then",
               "one" in {c["id"] for c in merged["cases"]})

    # a hand-written test sharing the suite
    merged["cases"].append(case("mine"))
    merged, done = b.merge(merged, gone)
    check_true("merge: a test somebody wrote by hand in the same suite is kept",
               "mine" in {c["id"] for c in merged["cases"]}, done)


def group_record():
    """A test as something to manage: how much it matters, whether it is in use."""
    import tests as t

    check("record: priority defaults to the middle", t.priority_of({}), "P2")
    check("record: and is read case-insensitively", t.priority_of({"priority": "p0"}), "P0")
    check("record: an unknown priority falls back rather than vanishing",
          t.priority_of({"priority": "urgent"}), "P2")
    check("record: status defaults to in use", t.status_of({}), "ready")
    check("record: links accept a single string", t.links_of({"links": "ABC-1"}), ["ABC-1"])
    check("record: and drop blanks", t.links_of({"links": ["ABC-1", " ", ""]}), ["ABC-1"])

    base = {"id": "x", "request": {"method": "GET", "path": "/a"},
            "assertions": [{"type": "status", "equals": 200}]}
    check("validate: a full record is accepted",
          t.validate_test({**base, "priority": "P1", "status": "blocked", "owner": "sam",
                           "links": ["ABC-1"], "description": "reads a thing"}), [])
    check_true("validate: a made-up priority is refused",
               any("priority" in e for e in t.validate_test({**base, "priority": "P9"})))
    check_true("validate: a made-up status is refused",
               any("status" in e for e in t.validate_test({**base, "status": "paused"})))

    runner = t.Runner.__new__(t.Runner)
    suite = {"name": "m"}
    check_true("select: priority narrows a run",
               runner.selects({**base, "priority": "P0"}, suite, priorities=["P0"])
               and not runner.selects({**base, "priority": "P3"}, suite, priorities=["P0"]))
    check_true("select: no priority chosen means every priority",
               runner.selects({**base, "priority": "P3"}, suite))
    check_true("select: a retired test never runs, even by name",
               not runner.selects({**base, "status": "retired"}, suite)
               and not runner.selects({**base, "status": "retired"}, suite, only="^x"))
    check_true("select: a blocked test is left out of a general run",
               not runner.selects({**base, "status": "blocked"}, suite))
    check_true("select: but runs when asked for by name",
               runner.selects({**base, "status": "blocked"}, suite, only="^x"))

    counted = t.taxonomy([{"name": "m", "cases": [{**base, "priority": "P0"},
                                                  {**base, "id": "y"}]}])
    by_name = {p["name"]: p["tests"] for p in counted["priorities"]}
    check("taxonomy: priorities are counted", (by_name["P0"], by_name["P2"]), (1, 1))
    check_true("taxonomy: each says what it means",
               all(p.get("means") for p in counted["priorities"]))


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


def group_plain_rest():
    """An API that answers with the bare object — no envelope — which is how
    most are written. Everything here was found by pointing the tool at a
    product it had never seen."""
    import json as _json
    import random
    import re
    import tempfile
    from pathlib import Path
    import bindings as bd
    import blueprint as b
    import generator as g
    import verdict as v
    from mockd import Spec, build_app

    uid = {"type": "string", "format": "uuid"}
    widget = {"type": "object", "required": ["id", "name", "created_at"], "properties": {
        "id": uid, "name": {"type": "string"},
        "created_at": {"type": "string", "format": "date-time"}}}
    listing = {"type": "object", "required": ["items", "total"], "properties": {
        "items": {"type": "array", "items": widget}, "total": {"type": "integer"}}}
    err = {"description": "no", "content": {"application/json": {"schema": {
        "type": "object", "properties": {"detail": {"type": "string"}}}}}}
    ok = lambda sch, d="ok": {"description": d, "content": {"application/json": {"schema": sch}}}
    doc = {"openapi": "3.1.0", "info": {"title": "W", "version": "1"}, "paths": {
        "/widgets": {
            "get": {"responses": {"200": ok(listing)}},
            "post": {"requestBody": {"content": {"application/json": {"schema": {
                        "type": "object", "required": ["name"],
                        "properties": {"name": {"type": "string"}}}}}},
                     "responses": {"201": ok(widget), "422": err}}},
        "/widgets/{widget_id}": {
            "get": {"parameters": [{"name": "widget_id", "in": "path", "required": True,
                                    "schema": uid}],
                    "responses": {"200": ok(widget), "404": err}}}}}

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "w.json"
        path.write_text(_json.dumps(doc))
        app, _ = build_app(str(path), stateful=True, log_path=Path(tmp) / "r.jsonl")[:2]
        c = app.test_client()
        made = c.post("/widgets", json={"name": "mine"})
        body = made.get_json() or {}
        check("rest: create answers the documented 201", made.status_code, 201)
        check("rest: with what was sent, not an invented row", body.get("name"), "mine")
        check_true("rest: and the fields a server adds itself", bool(body.get("created_at")),
                   str(body))
        back = c.get(f"/widgets/{body.get('id')}")
        check("rest: it reads back", back.status_code, 200)
        check("rest: as the same row", (back.get_json() or {}).get("name"), "mine")
        check("rest: with the same server-made value each time",
              (back.get_json() or {}).get("created_at"), body.get("created_at"))
        rows = (c.get("/widgets").get_json() or {}).get("items") or []
        check_true("rest: and it is in the list", any(r.get("id") == body.get("id") for r in rows),
                   str(rows)[:200])
        check("rest: an id nothing has is the documented 404",
              c.get("/widgets/00000000-0000-4000-8000-00000000dead").status_code, 404)
        ask = c.open("/widgets", method="OPTIONS", headers={
            "Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type"})
        check_true("rest: a browser's permission request is granted, not 404",
                   200 <= ask.status_code < 300, str(ask.status_code))
        check("rest: for the origin that asked", ask.headers.get("Access-Control-Allow-Origin"),
              "http://localhost:3000")

        spec = Spec(text=_json.dumps(doc), origin="w.json")
        check("rest: the rows of a list are found where the document puts them",
              b.list_path_in(b.success_schema(spec.match("GET", "/widgets")[0], spec), spec),
              "items")
        suite, _ = b.build(spec, name="baseline")
        flow = next(iter(suite.get("scenarios") or []), {})
        check("rest: a flow is named the way a person would say it",
              flow.get("name"), "a widget can be created, used and removed")
        read = next((st for st in flow.get("steps") or [] if st["name"] == "read it back"), {})
        check_true("rest: each step is held to the documented shape, not just a status",
                   {"type": "schema"} in (read.get("assertions") or []), str(read)[:200])
        missing = [x for x in suite.get("cases") or [] if x["id"].endswith("-missing")]
        check("rest: an id nothing has gets its own check", len(missing), 1)
        check("rest: which expects 404",
              (missing or [{}])[0].get("assertions"), [{"type": "status", "equals": 404}])

    rng = random.Random(3)
    for pattern in (r"^[0-9]{13}$", r"^[A-Z]{2}-\d{4}$", r"^[a-z0-9_-]+$"):
        value = g.string_matching(pattern, rng)
        check_true(f"pattern: a value is made that {pattern} accepts",
                   value is not None and re.search(pattern, value) is not None, str(value))
    check("pattern: one it cannot read is declined, not guessed",
          g.string_matching(r"^(a|b)+$", rng), None)
    check_true("pattern: a generated field honours it",
               re.fullmatch(r"[0-9]{13}", g.generate_from_schema(
                   {"type": "string", "pattern": "^[0-9]{13}$"}, rng, "isbn") or "") is not None)

    index = {"widget_id": [("GET /orders", "items[0].lines[0].widget_id")],
             "id": [("GET /widgets", "items[0].id"), ("GET /orders", "items[0].id")]}
    first = (bd.candidates("widget_id", index) or [{}])[0]
    check("ids: a thing's own list is preferred to a mention of it elsewhere",
          first.get("operation"), "GET /widgets")

    item = {"id": "get-widgets-missing", "tags": ["missing", "generated"], "outcome": "fail",
            "steps": [{"outcome": "fail", "status": 200,
                       "request": {"method": "GET", "url": "http://x/widgets/abc"},
                       "checks": [{"ok": False, "label": "status is 404"}]}]}
    check("verdict: a 200 for an id nothing has is the API's to fix",
          (v.attribute(item) or {}).get("kind"), v.BACKEND_BROKE)


def group_mcp():
    """The MCP server, spoken to over stdin and stdout as a client would.

    Read-only calls against the built-in sample document, so nothing here
    touches anybody's tests."""
    import json as _json
    import os
    import subprocess
    from pathlib import Path

    here = Path(__file__).resolve().parent
    proc = subprocess.Popen(
        [sys.executable, str(here / "mcp_server.py")], text=True, cwd=str(here),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "MOCKD_SPEC": "sample_spec.yaml"})
    lines, count = [], [0]

    def ask(method, params=None, notify=False):
        message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            count[0] += 1
            message["id"] = count[0]
        proc.stdin.write(_json.dumps(message) + "\n")
        proc.stdin.flush()
        if notify:
            return None
        line = proc.stdout.readline()
        lines.append(line)
        return _json.loads(line)

    def call(name, **arguments):
        result = ask("tools/call", {"name": name, "arguments": arguments})["result"]
        return _json.loads(result["content"][0]["text"]), result["isError"]

    try:
        hello = ask("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                   "clientInfo": {"name": "selftest", "version": "0"}})["result"]
        check("mcp: it answers in the protocol version the client asked for",
              hello.get("protocolVersion"), "2024-11-05")
        check_true("mcp: and says how it is meant to be used",
                   "find_operations" in (hello.get("instructions") or ""))
        ask("notifications/initialized", notify=True)
        tools = ask("tools/list")["result"]["tools"]
        check("mcp: ten tools", len(tools), 10)
        check_true("mcp: each with a description and an input schema",
                   all(tool.get("description") and tool.get("inputSchema", {}).get("type") == "object"
                       for tool in tools))
        check_true("mcp: none that deletes or overwrites",
                   not any(w in tool["name"] for tool in tools for w in ("delete", "remove")))
        check("mcp: an unknown method is a protocol error, not a crash",
              (ask("no/such/method").get("error") or {}).get("code"), -32601)

        fmt, _ = call("get_test_format")
        check_true("mcp: the test format includes the rules", "RULES" in fmt.get("format", ""))
        found, _ = call("find_operations", request="create an order and read it back")
        check_true("mcp: a request finds operations of this API",
                   any("/orders" in o["operation"] for o in found.get("operations") or []),
                   str(found)[:200])
        away, _ = call("find_operations", request="book a flight to Paris")
        check("mcp: a request about something else finds none", away.get("operations"), [])
        op = (found.get("operations") or [{}])[0].get("operation")
        got, failed = call("get_operation", operation=op)
        check_true("mcp: an operation's contract is returned", not failed and bool(got.get("contract")))
        nope, failed = call("get_operation", operation="GET /definitely/not/here")
        check_true("mcp: one that is not in the document is refused",
                   failed and "not in the API document" in nope.get("error", ""))
        bad, _ = call("validate_tests", tests=[{
            "id": "t", "request": {"method": "GET", "path": "http://elsewhere.example/x"},
            "assertions": [{"type": "status", "equals": 200}]}])
        check("mcp: a test that names a host is not valid", bad.get("ok"), False)
        ghost, _ = call("validate_tests", tests=[{
            "id": "t", "request": {"method": "GET", "path": "/definitely/not/here"},
            "assertions": [{"type": "status", "equals": 200}]}])
        check_true("mcp: nor one for an endpoint the document does not have",
                   ghost.get("ok") is False and "not an operation" in " ".join(ghost.get("problems") or []))
        shown, _ = call("list_servers")
        check_true("mcp: servers are listed by name and address, nothing more",
                   all(set(sv) <= {"name", "address", "ready", "read_only", "is_the_mock",
                                   "not_ready_because"} for sv in shown.get("servers") or [{}]))
        where, failed = call("run_tests", module="no-such-module")
        check_true("mcp: running a module that does not exist is refused", failed)
        check_true("mcp: every line it wrote was one JSON message",
                   all(_json.loads(line).get("jsonrpc") == "2.0" for line in lines))
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)
    check("mcp: it ends cleanly when the client goes away", proc.returncode, 0)


def group_needs():
    """A value only a person knows: named while missing, filled once, never guessed."""
    import tests as t

    test = {"id": "x", "data": {"depotId": "<a real depot id>", "note": "plain"},
            "request": {"method": "GET", "path": "/depots/{{depotId}}"}, "assertions": []}
    check("needs: a placeholder the test holds is named", t.waiting_for(test), ["depotId"])
    shared = {"id": "y", "request": {"method": "GET", "path": "/depots/{{depotId}}"},
              "assertions": []}
    check("needs: so is one it uses from the module's data",
          t.waiting_for(shared, {"depotId": "TODO fill in", "unused": "<nobody uses this>"}),
          ["depotId"])
    check("needs: a real value is not waited for",
          t.waiting_for({**test, "data": {"depotId": "D-7"}}), [])

    suite = {"name": "m", "data": {"region": "<a real region>"},
             "cases": [test, {"id": "z", "request": {"method": "GET", "path": "/r/{{region}}"},
                              "assertions": []}], "scenarios": []}
    check("needs: filling goes to the test that declared it",
          t.fill_value(suite, "x", "depotId", "D-7"), "test")
    check("needs: and is there afterwards", suite["cases"][0]["data"]["depotId"], "D-7")
    check("needs: a value that is already real is not replaced",
          t.fill_value(suite, "x", "depotId", "D-8"), None)
    check("needs: nor one the test never asked for", t.fill_value(suite, "x", "note", "n"), None)
    check("needs: a module-level placeholder is filled at module level",
          t.fill_value(suite, "z", "region", "north"), "suite")
    check("needs: and the module holds it", suite["data"]["region"], "north")


GROUPS = {
    "needs": group_needs,
    "mcp": group_mcp,
    "plain_rest": group_plain_rest,
    "classification": group_classification,
    "taxonomy": group_taxonomy,
    "selection": group_selection,
    "binding": group_binding,
    "readonly": group_readonly,
    "runid": group_runid,
    "rebind": group_rebind,
    "record": group_record,
    "baseline": group_baseline,
    "bug": group_bug,
    "references": group_references,
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
