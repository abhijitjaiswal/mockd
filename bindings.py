#!/usr/bin/env python3
"""
bindings.py — where an id comes from, decided once.

OpenAPI has no concept of a foreign key. A create says `position_id` is a
required uuid and stops there; nothing in the document says which endpoint
returns one. So every time a test is written, somebody works it out again —
and when they guess wrong the server answers "The selected position is invalid
or no longer available", which is a long way from the question that was asked.

Three things are needed, and only the third is a decision:

  1. an INDEX of what every endpoint actually returns. A contract sweep already
     records the shape of every response, so this costs nothing to build.
  2. CANDIDATES, ranked, each carrying the reason it is a candidate. An exact
     field-name match is near-certain; a shared token is a question, not an
     answer, and is never applied on its own.
  3. a DECISION, recorded here, so the question is asked once.

The ranking deliberately refuses to be clever. A wrong binding is worse than
none: it produces a test that fails against a real server for a reason nobody
can see, which is exactly the failure this file exists to end.
"""
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
BINDINGS = HERE / "bindings.json"

# How sure we are, and what that permits.
CERTAIN = "certain"      # the field is returned under its own name
STRONG = "strong"        # the resource's own id, from the resource's own read
WEAK = "weak"            # shares words — offer it, never apply it
RECORDED = "recorded"    # a human said so


def load():
    """Decisions made so far. Absent file is the normal state."""
    try:
        doc = json.loads(BINDINGS.read_text())
    except (OSError, ValueError):
        return {}
    return doc.get("ids") or {}


def save(field, entry):
    """Record one decision, preserving the rest."""
    try:
        doc = json.loads(BINDINGS.read_text())
    except (OSError, ValueError):
        doc = {}
    doc.setdefault("_why", "Where each id comes from, for this API. Written by "
                           "the console when somebody answers the question; read "
                           "whenever a brief or a generated test needs an id the "
                           "document does not explain.")
    ids = doc.setdefault("ids", {})
    if entry is None:
        ids.pop(field, None)
    else:
        ids[field] = entry
    BINDINGS.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    return ids


def forget(field):
    return save(field, None)


# ----------------------------------------------------------------------------
# The index
# ----------------------------------------------------------------------------


def _walk(shape, prefix="", depth=0, out=None):
    """Every field in an observed response, as a dotted path."""
    out = {} if out is None else out
    if depth > 6:
        return out
    if isinstance(shape, dict):
        for key, value in shape.items():
            path = f"{prefix}{key}"
            out.setdefault(key, []).append(path)
            _walk(value, f"{path}.", depth + 1, out)
    elif isinstance(shape, list) and shape:
        trimmed = prefix[:-1] if prefix.endswith(".") else prefix
        _walk(shape[0], f"{trimmed}[0].", depth + 1, out)
    return out


def index_from_report(report):
    """{field name -> [(operation, json path), ...]} from a verify report.

    Only successful reads are indexed: a 422 body describes a complaint, not a
    resource, and binding an id to a field in an error payload would be a
    confident way to be wrong."""
    index = {}
    for row in (report or {}).get("results") or []:
        shape = row.get("observed_shape")
        status = row.get("status")
        method = str(row.get("operation", "")).split(" ", 1)[0].upper()
        if not shape or method != "GET" or not (200 <= int(status or 0) < 300):
            continue
        for field, paths in _walk(shape).items():
            for path in paths:
                index.setdefault(field, []).append((row["operation"], path))
    return index


# ----------------------------------------------------------------------------
# Candidates
# ----------------------------------------------------------------------------

NOISE = {"id", "ids", "the", "a", "of"}


def _tokens(name):
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", str(name or "").lower()) if p]
    return [p for p in parts if p not in NOISE]


def _resource_of(operation):
    """The resource a read is about: the last meaningful path segment."""
    path = str(operation).split(" ", 1)[-1]
    parts = [p for p in path.strip("/").split("/")
             if p and not p.startswith("{") and p not in ("api",)
             and not re.fullmatch(r"v\d+", p)]
    verbs = {"list", "read", "all", "get", "dropdown", "search"}
    parts = [p for p in parts if p not in verbs]
    return parts[-1] if parts else ""


def _needs_an_id(operation):
    """Does calling this read itself require an id you do not have?

    GET /order/read/{order_id} returns a depot_id, but you cannot
    call it without already holding a order. A source with a prerequisite
    of its own is a worse answer than a collection you can simply call."""
    return "{" in str(operation)


def _singular(word):
    word = str(word or "").lower()
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def candidates(field, index, limit=4):
    """Where `field` might come from, best first, each saying why.

    Nothing here is applied automatically below `strong`. The weak tier exists
    so a person is shown the near miss instead of being told nothing — which is
    how `shipping_settings_id` stayed invisible while a placeholder went to
    the server in its place."""
    wanted = _tokens(field)
    if not wanted:
        return []
    head = _singular(wanted[-1]) if wanted else ""
    found = []

    for operation, path in index.get(field, []):
        about = _singular(_resource_of(operation)) == head and head
        found.append({"field": field, "operation": operation, "path": path,
                      "strength": CERTAIN, "needs_id": _needs_an_id(operation),
                      "about_it": bool(about),
                      "why": f"the response returns {field} under that name"
                             + ("" if about else
                                f", though this is a read of "
                                f"{_resource_of(operation) or 'something else'}")})

    # the resource's own id, from a read of that resource
    for operation, path in index.get("id", []):
        if _singular(_resource_of(operation)) == head and head:
            found.append({"field": "id", "operation": operation, "path": path,
                          "strength": STRONG, "needs_id": _needs_an_id(operation),
                          "about_it": True,
                          "why": f"the id of a {head}, from the {head} read"})

    seen_names = {c["field"] for c in found}
    for name, places in index.items():
        if name in seen_names or not str(name).lower().endswith(("_id", "_ids")):
            continue
        shared = set(_tokens(name)) & set(wanted)
        if not shared:
            continue
        operation, path = places[0]
        found.append({"field": name, "operation": operation, "path": path,
                      "strength": WEAK, "shared": sorted(shared),
                      "needs_id": _needs_an_id(operation),
                      "about_it": _singular(_resource_of(operation)) == head,
                      "why": f"shares {', '.join(sorted(shared))} — unconfirmed, "
                             f"nothing in the document says these are the same"})

    # Callable first, and only then strength. An exact field-name match that can
    # only be reached by first obtaining some other id is a worse answer than the
    # resource's own id from a collection you can simply call: both are correct,
    # one is usable. Strength still decides between equals.
    order = {CERTAIN: 0, STRONG: 1, WEAK: 2}
    found.sort(key=lambda c: (1 if c.get("needs_id") else 0,
                              order.get(c["strength"], 9),
                              0 if c.get("about_it") else 1,
                              -len(c.get("shared") or []), len(c["path"])))
    out, taken = [], set()
    for c in found:
        key = (c["operation"], c["path"])
        if key in taken:
            continue
        taken.add(key)
        out.append(c)
        if len(out) >= limit:
            break
    return out


def _param_of(operation):
    """The id a read needs before it can be called."""
    found = re.findall(r"\{([^}]+)\}", str(operation))
    return found[-1] if found else None


def camel(name):
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", str(name or "")) if p]
    if not parts:
        return "value"
    return parts[0].lower() + "".join(p.capitalize() for p in parts[1:])


def chain_for(field, index, depth=0, seen=()):
    """Ordered reads that end with `field` in hand.

    A field is often only reachable through another read: courier_ids live on a
    order, and a order needs a order_id, which a list supplies.
    Reporting "nothing supplies this" because the one source needed an id of its
    own was true and useless — the chain is two calls long and entirely derivable.

    Returns [] rather than a half-chain: a setup step that cannot run is worse
    than saying plainly that nothing reaches this field."""
    if depth > 3 or field in seen:
        return []
    options = candidates(field, index, limit=6)

    for option in options:
        if not option.get("needs_id") and option["strength"] != WEAK:
            return [{"operation": option["operation"], "path": option["path"],
                     "captures": field, "as": camel(field),
                     "strength": option["strength"], "why": option["why"]}]

    for option in options:
        if option["strength"] == WEAK:
            continue
        param = _param_of(option["operation"])
        if not param:
            continue
        prefix = chain_for(param, index, depth + 1, seen + (field,))
        if not prefix:
            continue
        # One pass only. Substituting the prefix captures and then running the
        # generic rule as well rewrapped what had just been written, giving
        # {{{orderid}}}. Each step names its capture with camel(param),
        # so the generic rule alone already produces the right variable.
        operation = re.sub(r"\{([A-Za-z0-9_]+)\}",
                           lambda m: "{{%s}}" % camel(m.group(1)),
                           option["operation"])
        return prefix + [{"operation": operation, "path": option["path"],
                          "captures": field, "as": camel(field),
                          "strength": option["strength"], "why": option["why"],
                          "needed": param}]
    return []


def resolve(field, index=None):
    """A recorded decision if there is one, else the best automatic candidate
    that is good enough to use without asking."""
    recorded = load().get(field)
    if recorded:
        return {**recorded, "strength": RECORDED, "field": field,
                "why": "you recorded this"}
    for candidate in candidates(field, index or {}, limit=1):
        if candidate["strength"] in (CERTAIN, STRONG):
            return candidate
    return None
