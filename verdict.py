#!/usr/bin/env python3
"""
verdict.py — when a test fails, say whose problem it is.

A red X starts an argument. Three people spend two hours deciding whether it was
the backend, the consumer's assumption, a stale spec, or somebody else's leftover
data. That triage is the most expensive recurring ritual in integration work, and
a diff tool cannot do it: answering needs the spec, the response, the history of
what passed before, and the digest of the document each run was judged against.
All four are already recorded here, so the verdict costs nothing extra to compute.

Deliberately six blunt categories, not a model. Each carries the evidence it used
so a human can overrule it in one glance — a confident wrong verdict is worse
than an honest "not sure".

    SPEC_CHANGED          the contract moved under the test
    BACKEND_BROKE         the response contradicts the contract
    TEST_ASSUMES          the test asserts something the contract never promised
    SPEC_SILENT           the contract says nothing, so neither side is wrong
    ENVIRONMENT           the data or the environment, not the code
    UNKNOWN               say so rather than guess
"""
import re

SPEC_CHANGED = "spec-changed"
BACKEND_BROKE = "backend-broke-contract"
TEST_ASSUMES = "test-assumes-undocumented"
SPEC_SILENT = "spec-silent"
ENVIRONMENT = "environment-or-data"
UNKNOWN = "unknown"

MEANING = {
    SPEC_CHANGED: ("The spec changed since this test last passed",
                   "Look at the spec diff before anything else — the other findings in "
                   "this run may all follow from it."),
    BACKEND_BROKE: ("The API contradicted its own spec",
                    "The response does not match what the document promises. This is the "
                    "provider's to fix, or the document is out of date."),
    TEST_ASSUMES: ("The test asserts something the spec never promised",
                   "The consumer relies on undocumented behaviour. Either the provider "
                   "should document and guarantee it, or the assertion should go."),
    SPEC_SILENT: ("The spec declares nothing here, so neither side is provably wrong",
                  "Add a response_model to this route; until then this is opinion, not "
                  "contract."),
    ENVIRONMENT: ("The environment or its data, not the code",
                  "Setup did not produce what the test needed, or the row it wanted is "
                  "not in this environment."),
    UNKNOWN: ("Not enough evidence to attribute this",
              "Everything checked was inconclusive — read the failure below."),
}


def _declared_paths(schema, prefix="", depth=0, out=None):
    """Every field path the spec declares for a response, flattened."""
    out = set() if out is None else out
    if not isinstance(schema, dict) or depth > 8:
        return out
    for branch in (schema.get("anyOf") or schema.get("oneOf") or schema.get("allOf") or []):
        _declared_paths(branch, prefix, depth + 1, out)
    for name, sub in (schema.get("properties") or {}).items():
        path = f"{prefix}.{name}" if prefix else name
        out.add(path)
        _declared_paths(sub, path, depth + 1, out)
    items = schema.get("items")
    if isinstance(items, dict):
        _declared_paths(items, prefix, depth + 1, out)
    return out


def _normalise(path):
    """data.items[0].id -> data.items.id, so a declared array matches an indexed use."""
    return re.sub(r"\[\d+\]", "", str(path or ""))


def _route_for(spec, step):
    if spec is None or not step:
        return None
    request = step.get("request") or {}
    path = str(request.get("url") or "")
    path = re.sub(r"^https?://[^/]+", "", path).split("?")[0]
    route, _ = spec.match(request.get("method", "GET"), path)
    return route


def attribute(item, spec=None, history=None, provenance=None):
    """Return a verdict dict for one failed test, or None if it passed."""
    if item.get("outcome") == "pass":
        return None

    steps = item.get("steps") or []
    failed = next((s for s in steps if s.get("outcome") in ("fail", "error")), None)
    evidence = []

    # 1. Did the contract move under it? Say this first — everything else in the
    #    run may be a consequence.
    hist = history or {}
    was_digest = hist.get("last_pass_spec")
    now_digest = (provenance or {}).get("digest")
    if was_digest and now_digest and was_digest != now_digest:
        evidence.append(f"passed at spec {was_digest[:12]}…, now running {now_digest[:12]}…")
        if hist.get("last_pass"):
            evidence.append(f"last passed {hist['last_pass']}")
        return _verdict(SPEC_CHANGED, evidence,
                        "python speclock.py diff --spec <the spec you are running>")

    # 2. Blocked means the endpoint under test never ran.
    if item.get("outcome") == "blocked":
        evidence.append(f"setup step failed: {item.get('blocked_by')}")
        return _verdict(ENVIRONMENT, evidence,
                        "Fix the setup first — the endpoint under test was never called.")

    if failed is None:
        return _verdict(UNKNOWN, ["no failing step recorded"], None)

    status = failed.get("status")
    checks = [c for c in (failed.get("checks") or []) if not c.get("ok")]
    labels = " ".join(c.get("label", "") for c in checks)
    details = " ".join(c.get("detail", "") for c in checks)
    route = _route_for(spec, failed)

    # 3. A status the document never mentions, or a body that breaks its own
    #    schema, is the provider contradicting itself.
    if route is not None and status is not None:
        documented = {str(c) for c in route["responses"]}
        if str(status) not in documented and "default" not in documented:
            evidence.append(f"returned {status}; the spec documents "
                            f"{', '.join(sorted(documented))}")
            if status in (401, 403):
                return _verdict(ENVIRONMENT, evidence,
                                "Authentication, most likely — check the credential before "
                                "the contract.")
            if status in (404, 409):
                return _verdict(ENVIRONMENT, evidence,
                                "The row this test wanted is not in this environment.")
            return _verdict(BACKEND_BROKE, evidence, None)

    # A status assertion that failed while the status IS documented is not the
    # document's fault and not the test's — the API legitimately answered with
    # something else, and which side owns that depends on the class.
    if route is not None and status is not None and "status" in labels.lower():
        documented = {str(c) for c in route["responses"]}
        if str(status) in documented:
            evidence.append(f"expected a different status; {status} is documented for "
                            f"{route['key']}")
            if 500 <= int(status) < 600:
                return _verdict(BACKEND_BROKE, evidence,
                                "A server error is the provider's, even when the spec "
                                "lists it as possible.")
            if int(status) in (401, 403):
                return _verdict(ENVIRONMENT, evidence,
                                "Authentication — check the credential for this "
                                "environment.")
            if 400 <= int(status) < 500:
                return _verdict(ENVIRONMENT, evidence,
                                "The request was rejected: look at the data it sent and "
                                "the rows this environment holds.")

    if "matches the spec schema" in labels or "schema" in labels.lower():
        evidence.append(details[:200] or "the body does not validate against the "
                                         "declared schema")
        return _verdict(BACKEND_BROKE, evidence, None)

    # 4. Is the test asserting on something the contract never declared?
    if route is not None:
        resp = route["responses"].get(str(status)) or {}
        schema = ((resp.get("content") or {}).get("application/json") or {}).get("schema")
        asserted = [_normalise(c.get("label", "").split(" ")[0]) for c in checks]
        asserted = [a for a in asserted if a and "." in a or (a and a.isidentifier())]
        if not schema:
            evidence.append(f"the spec declares no response schema for "
                            f"{route['key']} {status}")
            return _verdict(SPEC_SILENT, evidence,
                            "Add a response_model to this route to make this checkable.")
        declared = _declared_paths(schema)
        undocumented = [a for a in asserted
                        if a and _normalise(a) not in declared
                        and not any(d.endswith("." + _normalise(a)) for d in declared)]
        if undocumented:
            evidence.append(f"asserts on {', '.join(undocumented[:4])}, which the schema "
                            f"for {status} does not declare")
            return _verdict(TEST_ASSUMES, evidence, None)

    # 5. A test that used to pass on this environment and now does not, with no
    #    spec movement, points at state rather than code.
    if hist.get("passes") and hist.get("last_outcome") != "pass":
        evidence.append(f"passed {hist['passes']} time(s) before, most recently "
                        f"{hist.get('last_pass', 'unknown')}")
        if status in (404, 409, 500):
            return _verdict(ENVIRONMENT, evidence,
                            "Same test, same spec, different result — look at the data in "
                            "this environment.")

    if checks:
        evidence.append(checks[0].get("label", "") + " — " + checks[0].get("detail", ""))
    return _verdict(UNKNOWN, evidence, None)


def _verdict(kind, evidence, next_step):
    headline, guidance = MEANING[kind]
    return {"kind": kind, "headline": headline, "guidance": guidance,
            "evidence": [e for e in evidence if e], "next": next_step}


def summarise(verdicts):
    """Counts by category, most-actionable first."""
    order = [SPEC_CHANGED, BACKEND_BROKE, TEST_ASSUMES, ENVIRONMENT, SPEC_SILENT, UNKNOWN]
    counts = {}
    for v in verdicts:
        if v:
            counts[v["kind"]] = counts.get(v["kind"], 0) + 1
    return [(k, counts[k]) for k in order if k in counts]
