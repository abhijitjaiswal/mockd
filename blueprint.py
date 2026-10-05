#!/usr/bin/env python3
"""
blueprint.py — lifecycle flows derived from the document, with no model involved.

An assistant is the right tool for a business rule the document cannot state
("an approved order may not be edited"). It is an expensive and unreliable
tool for the mechanical majority: create a thing, read it back, change it, list
it, delete it, prove it is gone. That shape is not domain knowledge — it is
visible in the paths themselves, and any team's spec carries it.

So this derives those flows structurally. It reads:

    POST   /api/v1/user/create          -> create
    GET    /api/v1/user/read/{user_id}  -> read
    PUT    /api/v1/user/update/{user_id}-> update
    GET    /api/v1/user/list            -> list
    DELETE /api/v1/user/delete/{user_id}-> delete

and emits one scenario in the house format, chained on the id the create
returns. The same pass reads

    POST   /api/v1/widgets
    GET    /api/v1/widgets/{widget_id}

identically, because the rule is about path SHAPE — a collection and an item
under it — not about anybody's vocabulary. Nothing here knows what a user or a
order is, which is the whole point: the output generalises to whatever
document it is pointed at.

What it deliberately does NOT do:

  * invent assertions the document cannot support. A "gone after delete" step
    is emitted only where the read operation actually documents 404; asserting
    undocumented behaviour manufactures failures that are nobody's bug.
  * duplicate verify.py. Per-operation contract checking (is this status
    documented, does this body match its schema) already exists and runs across
    every operation. This produces the FLOWS that a contract sweep cannot.
  * overwrite your edits. Every generated scenario carries the operations it
    came from and a hash of their contract, so a regeneration can touch only
    what actually changed.

Output goes through the same validator as a human's paste, into drafts, and
earns promotion by passing — exactly like anything an assistant produced.
"""
import argparse
import hashlib
import json
import random
import re
import sys
from pathlib import Path

import generator
import project

HERE = Path(__file__).resolve().parent

# Segments that name an ACTION rather than a resource. Two spellings of the same
# API — /user/create and POST /users — must land in the same family, so these are
# dropped when deciding what a path is about. Generic HTTP-ish vocabulary only;
# anything domain-specific belongs in conventions.local.json.
DEFAULT_VERBS = {"create", "new", "add", "list", "all", "read", "get", "fetch",
                 "update", "edit", "modify", "patch", "delete", "remove", "destroy"}

# Leading segments that are routing, not resource.
DEFAULT_PREFIXES = {"api", "rest", "svc"}

ID_KEYS = ("id", "uuid", "_id")


class Unsendable(Exception):
    """The create takes a body in a media type the runner cannot send."""


def _conventions():
    merged = {"path_verbs": sorted(DEFAULT_VERBS),
              "path_prefixes": sorted(DEFAULT_PREFIXES),
              "id_paths": ["data.id", "data.data.id", "id", "data.uuid"]}
    for name in ("conventions.json", "conventions.local.json"):
        try:
            doc = json.loads((HERE / name).read_text())
        except (OSError, ValueError):
            continue
        for key in merged:
            if doc.get(key):
                merged[key] = doc[key]
    return merged


CONV = _conventions()
VERBS = set(CONV["path_verbs"])
PREFIXES = set(CONV["path_prefixes"])


def _version_segment(part):
    return bool(re.fullmatch(r"v\d+(\.\d+)?", part or "", re.I))


def family_key(path):
    """What a path is ABOUT, with action and routing noise removed.

    '/api/v1/user/read/{user_id}' and '/api/v1/user/list' both reduce to
    ('user',), so they are recognised as the same resource. '/api/v1/user/
    stream/{user_id}' reduces to ('user', 'stream') and is therefore a
    different thing — which is how a streaming endpoint avoids being mistaken
    for the resource's read."""
    parts = []
    for raw in str(path or "").strip("/").split("/"):
        if not raw or raw.startswith("{"):
            continue
        low = raw.lower()
        if low in PREFIXES or _version_segment(low):
            continue
        if low in VERBS:
            continue
        parts.append(low)
    return tuple(parts)


def path_params(path):
    return re.findall(r"\{([^}]+)\}", str(path or ""))


def role_of(route):
    """create / read / list / update / delete, or None.

    Decided by method plus whether the path addresses one item, which is the
    part every REST-ish spec agrees on regardless of naming style."""
    method = (route.get("method") or "").upper()
    has_id = bool(path_params(route.get("path")))
    if method == "POST" and not has_id:
        return "create"
    if method == "GET":
        return "read" if has_id else "list"
    if method in ("PUT", "PATCH") and has_id:
        return "update"
    if method == "DELETE" and has_id:
        return "delete"
    return None


def families(spec):
    """Group the document's operations into resource families.

    Where two routes claim the same role, the shorter path wins: given both
    '/user/read/{id}' and '/user/read/{id}/history', the former is the read."""
    found = {}
    for route in (spec.routes if spec is not None else []):
        role = role_of(route)
        if not role:
            continue
        key = family_key(route.get("path"))
        if not key:
            continue
        slot = found.setdefault(key, {})
        current = slot.get(role)
        if current is None or len(route["path"]) < len(current["path"]):
            slot[role] = route
    return found


# ----------------------------------------------------------------------------
# Reading a schema
# ----------------------------------------------------------------------------


def resolve(schema, spec, seen=()):
    """Follow $ref and drop the null branch FastAPI wraps optionals in."""
    if not isinstance(schema, dict):
        return {}
    if "$ref" in schema:
        name = str(schema["$ref"]).split("/")[-1]
        if name in seen:
            return {}
        components = ((spec.doc.get("components") or {}).get("schemas") or {}) \
            if spec is not None else {}
        return resolve(components.get(name, {}), spec, seen + (name,))
    branches = schema.get("anyOf") or schema.get("oneOf")
    if branches:
        for branch in branches:
            real = resolve(branch, spec, seen)
            if real.get("type") != "null" and real:
                return real
    return schema


def success_schema(route, spec):
    responses = route.get("responses") or {}
    codes = sorted((c for c in responses if str(c).startswith("2")), key=str)
    if not codes:
        return {}
    content = (responses[codes[0]].get("content") or {}).get("application/json") or {}
    return resolve(content.get("schema") or {}, spec)


def success_codes(route, fallback=(200,)):
    codes = [int(c) for c in (route.get("responses") or {}) if str(c).isdigit()
             and str(c).startswith("2")]
    return sorted(codes) or list(fallback)


def documents(route, status):
    return str(status) in {str(c) for c in (route.get("responses") or {})}


def id_path_in(schema, spec, prefix="", depth=0):
    """Where an identifier sits in a declared response, as a dotted path.

    Returns None when the document declares nothing useful — which is the
    common case for a FastAPI envelope whose `data` is a bare object. The
    caller then falls back to the house convention rather than guessing."""
    schema = resolve(schema, spec)
    if depth > 4 or not isinstance(schema, dict):
        return None
    props = schema.get("properties") or {}
    for name, sub in props.items():
        low = str(name).lower()
        if low in ID_KEYS or low.endswith("_id"):
            real = resolve(sub, spec)
            if real.get("type") in ("string", "integer", None):
                return f"{prefix}{name}"
    for name, sub in props.items():
        deeper = id_path_in(sub, spec, f"{prefix}{name}.", depth + 1)
        if deeper:
            return deeper
    return None


def capture_path(create_route, spec):
    found = id_path_in(success_schema(create_route, spec), spec)
    if found:
        return found, True
    return (CONV["id_paths"][0] if CONV["id_paths"] else "data.id"), False


# ----------------------------------------------------------------------------
# Building a request
# ----------------------------------------------------------------------------

UNIQUE_HINTS = ("name", "title", "code", "slug", "label", "username", "reference")


def body_media(route):
    """The media type this operation's body is declared in, and its schema.

    Not every API takes JSON. Reading only application/json made an operation
    declared as multipart look like one that takes no body at all, which would
    have emitted a create step that any real server rejects. Returns
    (media_type, schema, sendable) so the caller can decline rather than emit
    something that cannot pass."""
    content = (route.get("request_body") or {}).get("content") or {}
    if not content:
        return None, None, True                # genuinely no body: fine
    for media in content:
        if media == "application/json" or str(media).endswith("+json"):
            return media, (content[media] or {}).get("schema"), True
    media = sorted(content)[0]
    # the runner speaks JSON; anything else would be sent wrongly and fail for
    # a reason that has nothing to do with the endpoint
    return media, (content[media] or {}).get("schema"), False


def request_body(route, spec, rng):
    """A body from the declared schema, with the fields that must differ
    between runs marked so they do.

    A generated constant collides with itself the second time the suite runs —
    the single most common reason a generated test passes once and then fails
    forever."""
    _, raw, sendable = body_media(route)
    if not sendable:
        return None
    schema = resolve(raw or {}, spec)
    if not schema:
        return None
    body = generator.generate_from_schema(schema, rng, array_items=1)
    if not isinstance(body, dict):
        return body
    props = schema.get("properties") or {}
    for key, value in list(body.items()):
        prop = resolve(props.get(key) or {}, spec)
        if prop.get("enum") or not isinstance(value, str):
            continue
        fmt = str(prop.get("format") or "").lower()
        if fmt == "email":
            body[key] = "{{$randomEmail}}"
            continue
        if fmt or prop.get("pattern"):
            continue                      # a constrained string is not ours to invent
        low = key.lower()
        if any(hint in low for hint in UNIQUE_HINTS):
            limit = prop.get("maxLength")
            marker = f"{value[:12]}-{{{{$runId}}}}"
            if limit is None or len(marker) <= int(limit):
                body[key] = marker
    return body


def camel(name):
    parts = re.split(r"[^A-Za-z0-9]+", str(name or ""))
    parts = [p for p in parts if p]
    if not parts:
        return "id"
    return parts[0].lower() + "".join(p.capitalize() for p in parts[1:])


def bound_path(path, variable):
    """Replace the single path parameter with the captured variable."""
    return re.sub(r"\{[^}]+\}", "{{%s}}" % variable, str(path or ""))


# ----------------------------------------------------------------------------
# The flow
# ----------------------------------------------------------------------------


def contract_hash(routes):
    """Identifies the contract these operations present, so a regeneration can
    tell 'this endpoint changed' from 'this endpoint was merely re-read'."""
    blob = json.dumps(
        [[r.get("key"), r.get("parameters"), r.get("request_body"),
          sorted(str(c) for c in (r.get("responses") or {}))] for r in routes],
        sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _one(word):
    word = str(word or "").lower()
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def plant_references(body, own, data):
    """Swap the invented foreign keys in a body for named variables.

    A generated uuid in customer_id is well-formed and points at nothing, so
    the flow passed on a mock that never looked and failed on every server that
    did. Each one becomes a variable with a placeholder; whoever knows where the
    id comes from — the bindings, when there is an index — then captures it."""
    if isinstance(body, dict):
        for key, value in list(body.items()):
            low = str(key).lower()
            noun = _one(re.sub(r"_ids?$", "", low).split("_")[-1])
            if low.endswith("_id") and isinstance(value, str):
                if noun == own:
                    # the new row's own identifier: ours to choose, but a
                    # constant would collide with itself on the second run
                    if re.fullmatch(r"[0-9a-fA-F-]{32,36}", value):
                        body[key] = "{{$uuid}}"
                    continue
                name = camel(key)
                data.setdefault(name, f"<a real {key}>")
                body[key] = "{{%s}}" % name
            elif low.endswith("_ids") and isinstance(value, list):
                name = camel(re.sub(r"s$", "", str(key)))
                data.setdefault(name, f"<a real {key} entry>")
                body[key] = ["{{%s}}" % name]
            elif isinstance(value, (dict, list)):
                plant_references(value, own, data)
    elif isinstance(body, list):
        for item in body:
            plant_references(item, own, data)


def lifecycle(key, slot, spec, index=None):
    """One resource's flow, or None when the document cannot support one."""
    create = slot.get("create")
    if not create:
        return None
    media, _, sendable = body_media(create)
    if not sendable:
        raise Unsendable(media)
    reachable = slot.get("read") or slot.get("update") or slot.get("delete")
    if not reachable:
        return None                        # nothing to do with the thing we made

    item_routes = [r for r in (slot.get("read"), slot.get("update"), slot.get("delete"))
                   if r is not None]
    params = {tuple(path_params(r["path"])) for r in item_routes}
    if any(len(p) != 1 for p in params):
        return None                        # nested resources need a parent; not yet

    param_name = list(params)[0][0] if params else "id"
    variable = camel(param_name)
    path, from_schema = capture_path(create, spec)
    rng = random.Random(create["key"])
    name = "-".join(key)
    used = [create] + item_routes + ([slot["list"]] if slot.get("list") else [])

    steps = []
    body = request_body(create, spec, rng)
    steps.append({
        "role": "step", "name": f"create a {name}",
        "request": {"method": create["method"], "path": create["path"],
                    **({"body": body} if body is not None else {})},
        "assertions": [
            {"type": "status", "in": success_codes(create, (200, 201))},
            {"type": "jsonpath", "path": path, "op": "not_null"},
        ],
        "capture": {variable: path},
    })

    if slot.get("read"):
        read = slot["read"]
        steps.append({
            "role": "step", "name": "read it back",
            "request": {"method": "GET", "path": bound_path(read["path"], variable)},
            "assertions": [
                {"type": "status", "in": success_codes(read)},
                # the read returns the thing we just made, not merely something
                {"type": "jsonpath", "path": path, "op": "equals",
                 "value": "{{%s}}" % variable},
            ],
        })

    if slot.get("update"):
        upd = slot["update"]
        upd_body = request_body(upd, spec, random.Random(upd["key"]))
        steps.append({
            "role": "step", "name": "change it",
            "request": {"method": upd["method"], "path": bound_path(upd["path"], variable),
                        **({"body": upd_body} if upd_body is not None else {})},
            "assertions": [{"type": "status", "in": success_codes(upd)}],
        })

    if slot.get("list"):
        lst = slot["list"]
        steps.append({
            "role": "step", "name": f"it appears in the {name} list",
            "request": {"method": "GET", "path": lst["path"]},
            "assertions": [
                {"type": "status", "in": success_codes(lst)},
                {"type": "jsonpath", "path": "data", "op": "not_empty"},
            ],
        })

    cleanup = []
    if slot.get("delete"):
        dele = slot["delete"]
        steps.append({
            "role": "step", "name": "delete it",
            "request": {"method": "DELETE", "path": bound_path(dele["path"], variable)},
            "assertions": [{"type": "status", "in": success_codes(dele, (200, 204))}],
        })
        # Only where the document says a missing one is a 404. Asserting
        # undocumented behaviour invents failures nobody owns.
        if slot.get("read") and documents(slot["read"], 404):
            steps.append({
                "role": "target", "name": "and it is gone",
                "request": {"method": "GET",
                            "path": bound_path(slot["read"]["path"], variable)},
                "assertions": [{"type": "status", "equals": 404}],
            })
        # belt and braces: if the flow fails before its own delete, this still runs
        cleanup.append({
            "name": f"remove the {name} if the flow did not",
            "request": {"method": "DELETE",
                        "path": bound_path(dele["path"], variable)},
        })

    if steps and not any(s.get("role") == "target" for s in steps):
        steps[-1]["role"] = "target"

    flow = {
        "id": f"{name.replace('/', '-')}-lifecycle",
        "name": f"a {name} can be created, used and removed",
        "kind": "e2e",
        "levels": ["sanity"],
        "tags": ["lifecycle", "generated"],
        "priority": "P1",
        "description": f"A {name} can be created, read back, changed, found in the "
                       f"list and removed — the whole life of one record.",
        "steps": steps,
        "generated": {
            "by": "blueprint",
            "from": [r["key"] for r in used],
            "contract": contract_hash(used),
            "capture_from_schema": from_schema,
        },
    }
    if cleanup:
        flow["cleanup"] = cleanup

    # foreign keys: named, then captured where the API says where they live
    data = {}
    for step in steps:
        plant_references((step.get("request") or {}).get("body"), _one(key[-1]), data)
    if data:
        flow["data"] = data
        if index:
            import tests as _tests
            _tests.rebind_placeholders(flow, index)
        left = sorted((flow.get("data") or {}))
        if left:
            flow["generated"]["needs_values"] = left
    return flow


# ----------------------------------------------------------------------------
# Cases: one operation at a time
# ----------------------------------------------------------------------------


def slug(route):
    parts = [p for p in str(route["path"]).strip("/").split("/")
             if p and not p.startswith("{")]
    parts = [p for p in parts if p.lower() not in PREFIXES and not _version_segment(p)]
    return re.sub(r"[^A-Za-z0-9]+", "-",
                  f"{route['method'].lower()}-{'-'.join(parts)}").strip("-").lower()


def declares_schema(route, spec):
    return bool(success_schema(route, spec))


def contract_cases(spec):
    """Every operation that can be called without an id: does it answer what it
    says it answers? This is the floor — a suite that has it cannot regress a
    documented status or shape without somebody being told."""
    out = []
    for route in (spec.routes if spec is not None else []):
        if route["method"] != "GET" or route.get("path_params"):
            continue
        # A required query parameter left out is a 422, and the test would be
        # reporting the operation broken when it was the test that was.
        query, rng = {}, random.Random(route["key"])
        for prm in (route.get("parameters") or []):
            if prm.get("in") != "query" or not prm.get("required"):
                continue
            value = generator.generate_from_schema(
                resolve(prm.get("schema") or {}, spec), rng)
            if isinstance(value, (str, int, float, bool)):
                query[prm["name"]] = value
        assertions = [{"type": "status", "in": success_codes(route)}]
        if declares_schema(route, spec):
            assertions.append({"type": "schema"})
        out.append({
            "id": f"{slug(route)}-contract",
            "name": f"{route['key']} answers what it documents",
            "levels": ["smoke"], "tags": ["contract", "generated"],
            "priority": "P0",
            "description": f"Calling {route['key']} returns the status and shape the "
                           f"API document promises.",
            "request": {"method": route["method"], "path": route["path"],
                        **({"query": query} if query else {})},
            "assertions": assertions,
            "generated": {"by": "blueprint", "kind": "contract",
                          "from": [route["key"]], "contract": contract_hash([route])},
        })
    return out


def omission_cases(spec):
    """A required field the document names, left out on purpose.

    The spec says 400 or 422 here; if the server accepts it anyway, validation
    is not being enforced and every consumer is free to send nonsense."""
    out = []
    for route in (spec.routes if spec is not None else []):
        if route["method"] not in ("POST", "PUT", "PATCH") or route.get("path_params"):
            continue
        media, raw, sendable = body_media(route)
        if not sendable:
            continue
        schema = resolve(raw or {}, spec)
        required = [f for f in (schema.get("required") or [])]
        if not required:
            continue
        expect = sorted({int(c) for c in route["responses"]
                         if str(c) in ("400", "422")})
        if not expect:
            continue
        body = request_body(route, spec, random.Random(route["key"]))
        if not isinstance(body, dict):
            continue
        dropped = required[0]
        out.append({
            "id": f"{slug(route)}-without-{re.sub(r'[^a-z0-9]+', '-', dropped.lower())}",
            "name": f"{route['key']} rejects a body with no {dropped}",
            "levels": ["negative"], "tags": ["validation", "generated"],
            "priority": "P2",
            "description": f"Sending {route['key']} without the required field "
                           f"{dropped} is refused, as the API document says it must be.",
            "request": {"method": route["method"], "path": route["path"],
                        "body": {k: v for k, v in body.items() if k != dropped}},
            "assertions": [{"type": "status", "in": expect}],
            "generated": {"by": "blueprint", "kind": "omission", "field": dropped,
                          "from": [route["key"]], "contract": contract_hash([route])},
        })
    return out


def _offending_value(schema):
    """A value the document itself says is not allowed."""
    real = resolve(schema, None) if not isinstance(schema, dict) else schema
    branches = real.get("anyOf") or real.get("oneOf") or [real]
    real = next((b for b in branches
                 if isinstance(b, dict) and b.get("type") != "null"), real)
    if real.get("enum"):
        return "not-a-documented-value", f"is not one of {real['enum']}"
    if real.get("minimum") is not None:
        return real["minimum"] - 1, f"is below the documented minimum {real['minimum']}"
    if real.get("maximum") is not None:
        return real["maximum"] + 1, f"is above the documented maximum {real['maximum']}"
    return None, None


def parameter_cases(spec):
    """A query parameter pushed outside the range the document states."""
    out = []
    for route in (spec.routes if spec is not None else []):
        if route.get("path_params") or "422" not in {str(c) for c in route["responses"]}:
            continue
        for prm in (route.get("parameters") or []):
            if prm.get("in") != "query":
                continue
            value, why = _offending_value(resolve(prm.get("schema") or {}, spec))
            if value is None:
                continue
            name = prm.get("name")
            out.append({
                "id": f"{slug(route)}-{re.sub(r'[^a-z0-9]+', '-', str(name).lower())}"
                      f"-out-of-range",
                "name": f"{route['key']} rejects {name} that {why}",
                "levels": ["negative"], "tags": ["validation", "generated"],
                "priority": "P3",
                "description": f"{route['key']} refuses a value for {name} that {why}.",
                "request": {"method": route["method"], "path": route["path"],
                            "query": {name: value}},
                "assertions": [{"type": "status", "equals": 422}],
                "generated": {"by": "blueprint", "kind": "parameter", "param": name,
                              "from": [route["key"]], "contract": contract_hash([route])},
            })
    return out


KINDS = ("lifecycle", "contract", "omission", "parameter")


def load_index():
    """What each endpoint returns, from the mock's last self-check, if any."""
    try:
        import bindings
        return bindings.index_from_report(
            json.loads((HERE / "logs" / "mock-selfcheck.json").read_text()))
    except Exception:
        return {}


def build(spec, only=None, kinds=KINDS, name="derived", index=None):
    """Every lifecycle the document supports, as a suite document."""
    flows, skipped = [], []
    for key, slot in sorted(families(spec).items() if "lifecycle" in kinds else []):
        resource = "/".join(key)
        if only and not re.search(only, resource):
            continue
        try:
            flow = lifecycle(key, slot, spec, index)
        except Unsendable as exc:
            skipped.append((resource, f"its create takes {exc} — the runner sends JSON, "
                                  f"so a generated flow here could only fail"))
            continue
        if flow:
            flows.append(flow)
        elif slot.get("create"):
            skipped.append((resource, "no way to read, change or remove what it creates"
                            if not (slot.get("read") or slot.get("update")
                                    or slot.get("delete"))
                            else "the item path needs more than one id"))
    cases = []
    if "contract" in kinds:
        cases += contract_cases(spec)
    if "omission" in kinds:
        cases += omission_cases(spec)
    if "parameter" in kinds:
        cases += parameter_cases(spec)
    if only:
        cases = [c for c in cases if re.search(only, c["id"])]

    return {"name": name,
            "_why": "Derived from the spec by blueprint.py, not written by hand and "
                    "not written by a model. Regenerate with: python blueprint.py. "
                    "These flows read back what they create, so against the mock they "
                    "need it started in stateful mode — a stateless mock synthesises a "
                    "fresh id per request and the read cannot match the create.",
            "data": {},
            "cases": cases,
            "scenarios": flows}, skipped


def main():
    ap = argparse.ArgumentParser(
        description="Derive lifecycle flows from the spec — no model involved")
    ap.add_argument("--spec", help="defaults to the project spec")
    ap.add_argument("--only", help="regex; only resources whose name matches")
    ap.add_argument("--kinds", default=",".join(KINDS),
                    help=f"what to derive: {', '.join(KINDS)}")
    ap.add_argument("--out", help="write the suite here "
                                  "(default: print a summary, write nothing)")
    ap.add_argument("--json", action="store_true", help="print the suite to stdout")
    args = ap.parse_args()

    spec_path = project.active_spec(args.spec)
    from mockd import Source, Spec
    text, _ = Source(spec_path, poll=0).read(force=True)
    if not text:
        print(f"could not read {spec_path}", file=sys.stderr)
        return 2
    spec = Spec(text=text, origin=spec_path)

    name = Path(args.out).stem if args.out else "derived"
    suite, skipped = build(spec, only=args.only, name=name, index=load_index(),
                           kinds=tuple(k.strip() for k in args.kinds.split(",") if k.strip()))
    flows = suite["scenarios"]

    if args.json:
        print(json.dumps(suite, indent=2))
    else:
        cases = suite["cases"]
        by_kind = {}
        for c in cases:
            by_kind.setdefault((c.get("generated") or {}).get("kind", "?"), []).append(c)
        print(f"\n{spec_path} — {len(spec.routes)} operations")
        print(f"{len(flows)} lifecycle flow(s) and {len(cases)} case(s) derived")
        for kind in sorted(by_kind):
            print(f"    {len(by_kind[kind]):3d}  {kind}")
        print()
        for flow in flows:
            print(f"  {flow['id']}   ({len(flow['steps'])} steps)")
            for step in flow["steps"]:
                req = step["request"]
                print(f"      {req['method']:6s} {req['path']}")
            if flow["generated"].get("needs_values"):
                print(f"      needs a real value for: "
                      f"{', '.join(flow['generated']['needs_values'])}")
            if not flow["generated"]["capture_from_schema"]:
                print(f"      note: the document declares no id in the create "
                      f"response; using the house default")
        for name, why in skipped:
            print(f"  -- {name}: {why}")
        if flows:
            print("\n  These read back what they create. Against the mock, start it in "
                  "stateful mode\n  (Source -> stateful, or mockd.py --stateful) or the "
                  "read cannot match the create.")

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(suite, indent=2) + "\n")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
