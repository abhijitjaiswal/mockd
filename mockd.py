#!/usr/bin/env python3
"""
mockd — spec-driven mock server.

Point it at any OpenAPI 3.0/3.1 file (yaml or json). It serves every path in the
spec, validates incoming requests AGAINST the spec (so bad clients get a
descriptive 400 instead of a silent 200), and answers with spec examples,
overlay payloads, or data generated from the schema. Optional stateful mode
makes create/read/update/delete behave like a real backend held in memory.

Usage:
    python mockd.py --spec apis.json --port 4010
    python mockd.py --spec apis.json --overlay mock_overlay.json --stateful
    python mockd.py --spec apis.json --require-auth

Control headers (sent by the client):
    X-Mock-Status:   404       force a specific documented response status
    X-Mock-Example:  cancelled pick a named example from the spec
    X-Mock-Scenario: empty     pick a named scenario from the overlay
    X-Mock-Delay:    750       delay the response by N ms (loader/timeout tests)
    X-Mock-Nulls:    on        emit null for every nullable field

Introspection:
    GET /_mock/routes   loaded routes (+ where each response body comes from)
    GET /_mock/log      last 200 requests (also appended to logs/requests.jsonl)
    GET /_mock/state    current in-memory state (stateful mode)
    POST /_mock/reset   clear state + log
"""
import argparse
import copy
import hashlib
import json
import random
import re
import string
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import yaml
from flask import Flask, request, jsonify
from flask_cors import CORS

try:  # OpenAPI 3.1 schemas are JSON Schema 2020-12
    from jsonschema import Draft202012Validator as _Validator
except ImportError:  # pragma: no cover - very old jsonschema
    from jsonschema import Draft7Validator as _Validator

from generator import (generate_from_schema, _merge_all_of)  # noqa: E402
import synth  # noqa: E402

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")

# ----------------------------------------------------------------------------
# Spec loading & $ref resolution
# ----------------------------------------------------------------------------


class Spec:
    """Loads the document and fully inlines $refs (cycle-safe, no depth cap)."""

    def __init__(self, path: str = None, text: str = None, origin: str = None):
        self.path = path
        self.origin = origin or path
        raw = text if text is not None else Path(path).read_text()
        self.doc = yaml.safe_load(raw)
        if not isinstance(self.doc, dict) or "openapi" not in self.doc:
            raise ValueError(f"{self.origin}: not an OpenAPI 3.x document "
                             f"(missing 'openapi' key)")
        self.version = str(self.doc.get("openapi"))
        self.title = (self.doc.get("info") or {}).get("title", "")
        self.routes = self._build_routes()

    # -- refs ---------------------------------------------------------------
    def _deref(self, ref: str):
        if not ref.startswith("#/"):
            return {}
        node = self.doc
        for part in ref[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if isinstance(node, list):
                try:
                    node = node[int(part)]
                except (ValueError, IndexError):
                    return {}
            elif isinstance(node, dict):
                node = node.get(part, {})
            else:
                return {}
        return node

    @staticmethod
    def _denullable(node):
        """OpenAPI 3.0 spells a nullable field `nullable: true`; 3.1 spells it as
        a union with null. The validator only understands the 3.1 form, so a 3.0
        spec would see a legal explicit null rejected with a 400. Rewrite it
        once, at load time, and everything downstream stays simple."""
        if not (isinstance(node, dict) and node.pop("nullable", None) is True):
            return node
        if "enum" in node and None not in node["enum"]:
            node["enum"] = list(node["enum"]) + [None]
        for key in ("anyOf", "oneOf"):
            if key in node:
                if not any(isinstance(b, dict) and b.get("type") == "null"
                           for b in node[key]):
                    node[key] = list(node[key]) + [{"type": "null"}]
                return node
        if "type" in node:
            inner = {k: v for k, v in node.items()
                     if k not in ("description", "title", "example", "default",
                                  "readOnly", "writeOnly", "deprecated")}
            keep = {k: v for k, v in node.items() if k not in inner}
            return {**keep, "anyOf": [inner, {"type": "null"}]}
        return node

    def resolve(self, node, _stack=()):
        """Inline every $ref. Recursion is bounded by the ref stack, not by a
        depth cap — a depth cap silently truncates deeply nested schemas."""
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                if ref in _stack:          # cyclic model -> stop, stay permissive
                    return {}
                target = self.resolve(self._deref(ref), _stack + (ref,))
                siblings = {k: self.resolve(v, _stack) for k, v in node.items() if k != "$ref"}
                if siblings and isinstance(target, dict):
                    merged = dict(target)
                    merged.update(siblings)
                    return self._denullable(merged)
                return target
            return self._denullable({k: self.resolve(v, _stack) for k, v in node.items()})
        if isinstance(node, list):
            return [self.resolve(v, _stack) for v in node]
        return node

    # -- routes -------------------------------------------------------------
    def _build_routes(self):
        routes = []
        for path, item in (self.doc.get("paths") or {}).items():
            shared = item.get("parameters", [])
            for method, op in item.items():
                if method.lower() not in HTTP_METHODS:
                    continue
                params = shared + (op.get("parameters") or [])
                regex = re.sub(r"\{([^}]+)\}", r"(?P<\1>[^/]+)", path)
                routes.append({
                    "path": path,
                    "regex": re.compile(f"^{regex}$"),
                    "method": method.upper(),
                    "key": f"{method.upper()} {path}",
                    "operation": op,
                    "operation_id": op.get("operationId", ""),
                    "summary": op.get("summary", ""),
                    "tags": op.get("tags", []),
                    "parameters": [self.resolve(p) for p in params],
                    # pre-resolved once at load time: request-time resolution of
                    # a 3.1 spec with 200+ refs is both slow and repetitive
                    "responses": self.resolve(op.get("responses") or {}),
                    "request_body": self.resolve(op.get("requestBody") or {}),
                    "path_params": re.findall(r"\{([^}]+)\}", path),
                })
        # literal segments win over templated ones, then longest path first
        routes.sort(key=lambda r: (len(r["path_params"]), -len(r["path"]), r["method"]))
        return routes

    def match(self, method: str, path: str):
        for r in self.routes:
            if r["method"] != method:
                continue
            m = r["regex"].match(path)
            if m:
                return r, m.groupdict()
        return None, None


# ----------------------------------------------------------------------------
# Request validation against the spec
# ----------------------------------------------------------------------------


def _param_schema_types(sch: dict):
    """Flatten anyOf/oneOf so an optional `int | None` query param still gets
    type-checked."""
    out = []
    if not isinstance(sch, dict):
        return out
    branches = sch.get("anyOf") or sch.get("oneOf")
    if branches:
        for b in branches:
            out.extend(_param_schema_types(b))
        return out
    if sch.get("allOf"):
        return _param_schema_types(_merge_all_of(sch["allOf"]))
    out.append(sch)
    return out


def validate_request(route, path_params):
    """Returns list of error dicts. Empty list = valid."""
    errors = []

    for p in route["parameters"]:
        name, loc = p.get("name"), p.get("in")
        required = p.get("required", loc == "path")
        if loc == "query":
            value = request.args.get(name)
        elif loc == "path":
            value = path_params.get(name)
        elif loc == "header":
            value = request.headers.get(name)
        else:
            continue
        if value is None or value == "":
            if required:
                errors.append({"in": loc, "field": name, "error": "required parameter missing"})
            continue

        # A present value must satisfy one of the declared non-null branches.
        # `anyOf: [T, null]` means the param may be OMITTED, not that any string
        # is acceptable once it is supplied.
        branches = _param_schema_types(p.get("schema") or {})
        typed = [b for b in branches if b.get("type") not in (None, "null")]
        if not typed:
            continue
        ok, enums = False, None
        for b in typed:
            bt = b.get("type")
            if bt == "integer" and re.fullmatch(r"-?\d+", str(value)):
                ok = True
            elif bt == "number" and re.fullmatch(r"-?\d+(\.\d+)?", str(value)):
                ok = True
            elif bt == "boolean" and str(value).lower() in ("true", "false", "1", "0"):
                ok = True
            elif bt == "string":
                if b.get("enum"):
                    enums = b["enum"]
                    if value in [str(e) for e in b["enum"]]:
                        ok = True
                else:
                    ok = True
            elif bt in ("array", "object"):
                ok = True
        if not ok:
            if enums:
                errors.append({"in": loc, "field": name,
                               "error": f"'{value}' not in allowed values {enums}"})
            else:
                kinds = sorted({b.get("type") for b in typed})
                errors.append({"in": loc, "field": name,
                               "error": f"expected {'/'.join(kinds)}, got '{value}'"})

    body_spec = route["request_body"]
    if body_spec:
        json_media = (body_spec.get("content") or {}).get("application/json")
        if json_media:
            body = request.get_json(silent=True)
            if body is None:
                if body_spec.get("required", False):
                    errors.append({"in": "body", "field": "(root)",
                                   "error": "required JSON body missing or malformed"})
            else:
                schema = json_media.get("schema") or {}
                if schema:
                    for err in _Validator(schema).iter_errors(body):
                        path = [str(x) for x in err.absolute_path]
                        message = err.message
                        if err.validator == "required":
                            # point at the missing field, not at its parent — a
                            # form maps errors back to inputs by this path
                            missing = re.match(r"'([^']+)' is a required property", message)
                            if missing:
                                path.append(missing.group(1))
                                message = "Field required"
                        errors.append({
                            "in": "body",
                            "field": "/".join(path) or "(root)",
                            "error": message,
                        })
    return errors


def _violation_type(message):
    m = (message or "").lower()
    if "required property" in m or "required parameter missing" in m \
            or m == "field required":
        return "missing"
    if "is not of type" in m or "expected " in m:
        return "type_error"
    if "not one of" in m or "not in allowed values" in m:
        return "enum"
    if "too short" in m or "too long" in m or "non-empty" in m:
        return "string_too_short" if "short" in m or "non-empty" in m else "string_too_long"
    return "value_error"


def _as_loc(err):
    loc = [err.get("in") or "body"]
    field = err.get("field") or ""
    if field and field != "(root)":
        for part in str(field).split("/"):
            loc.append(int(part) if part.isdigit() else part)
    return loc


def _is_violation_list(value):
    return (isinstance(value, list) and value and isinstance(value[0], dict)
            and {"loc", "msg"} <= set(value[0]))


def as_documented_validation_error(route, errors):
    """Return validation failures in the shape THIS operation documents.

    This matters more than the shape being descriptive: a UI writes its error
    handling once, against whatever the mock returns. If the mock answers with
    something of its own invention, that handler is wrong the day it meets the
    real backend.

    And the shape is per-operation, not per-API: this spec carries two different
    422 bodies — FastAPI's `{"detail": [...]}` on 57 operations and a custom
    `{"status_code", "message", "error": [...]}` on 27. Each is mirrored from
    what that operation declares, so neither team has to remember which module
    they are calling.

    Returns (status, body), or None when nothing usable is documented, in which
    case the caller falls back to the descriptive form."""
    documented = {str(c) for c in route["responses"]}
    status = "422" if "422" in documented else ("400" if "400" in documented else None)
    if status is None:
        return None

    content = ((route["responses"][status].get("content") or {})
               .get("application/json") or {})
    violations = [{"loc": _as_loc(e), "msg": e.get("error", "invalid"),
                   "type": _violation_type(e.get("error"))} for e in errors]

    # 1. a declared schema whose detail[] carries loc/msg/type — FastAPI's shape
    props = (content.get("schema") or {}).get("properties") or {}
    detail_items = ((props.get("detail") or {}).get("items") or {}).get("properties") or {}
    if {"loc", "msg", "type"} <= set(detail_items):
        return int(status), {"detail": violations}

    # 2. no schema, but a documented example — use it as the template, so a
    #    custom error envelope keeps its own field names
    example = content.get("example")
    if example is None and content.get("examples"):
        example = next(iter(content["examples"].values())).get("value")
    if isinstance(example, dict):
        body = copy.deepcopy(example)
        replaced = False
        for key, value in body.items():
            if _is_violation_list(value):
                body[key] = violations
                replaced = True
                break
        if replaced:
            if isinstance(body.get("status_code"), int):
                body["status_code"] = int(status)
            if isinstance(body.get("message"), str) and violations:
                first = violations[0]
                field = ".".join(str(x) for x in first["loc"][1:]) or "request"
                body["message"] = f"{field}: {first['msg']}"
            return int(status), body

    return None


# ----------------------------------------------------------------------------
# Response selection: overlay -> spec example -> schema
# ----------------------------------------------------------------------------


class Overlay:
    """Hand-authored payloads keyed by 'METHOD /path'. Needed because a spec
    generated from FastAPI without response_model declares `schema: {}` — there
    is nothing for a generator to work from."""

    def __init__(self, path=None, text=None):
        self.operations = {}
        self.path = path
        # Where a curated body contradicts the document it is supposed to
        # illustrate. Worth surfacing: it means the overlay outlived the spec
        # it was generated from, and anything built against it learned a shape
        # the real server does not return.
        self.conflicts = {}
        raw = text
        if raw is None and path and Path(path).exists():
            raw = Path(path).read_text()
        if raw:
            doc = json.loads(raw)
            self.operations = doc.get("operations", doc)

    def note_conflict(self, key, status, why):
        self.conflicts.setdefault(f"{key} [{status}]", why)

    def get(self, key, status=None, scenario=None):
        entry = self.operations.get(key)
        if not entry:
            return None, None
        if scenario:
            sc = (entry.get("scenarios") or {}).get(scenario)
            if sc is None:
                return "missing_scenario", sorted((entry.get("scenarios") or {}).keys())
            return sc.get("status", int(status or 200)), sc.get("body")
        responses = entry.get("responses") or {}
        if status is not None and str(status) in responses:
            return int(status), responses[str(status)].get("body")
        return None, None

    def scenarios(self, key):
        return sorted(((self.operations.get(key) or {}).get("scenarios") or {}).keys())


def _success_status(responses):
    codes = [c for c in responses if str(c).isdigit()]
    twoxx = sorted((c for c in codes if c.startswith("2")), key=int)
    if twoxx:
        return twoxx[0]
    if "default" in responses:
        return "default"
    return sorted(codes, key=int)[0] if codes else None


def _overlay_conflict(schema, body, spec=None):
    """None when a curated body agrees with the declared schema, else why not.

    Only checked when the document actually declares something: most of this
    spec declares nothing, and an overlay is the only answer there is."""
    if not schema or body is None:
        return None
    try:
        resolved = spec.resolve(schema) if spec is not None else schema
        errors = list(_Validator(resolved).iter_errors(body))
    except Exception:
        return None                        # cannot judge: do not interfere
    if not errors:
        return None
    first = errors[0]
    where = ".".join(str(p) for p in first.absolute_path) or "the body"
    return f"{where}: {first.message[:140]}"


def pick_response(route, overlay: Overlay, forced_status=None, example_name=None,
                  scenario=None, rng=None, nulls=False, array_items=2,
                  spec=None, path_params=None, allow_undocumented=False):
    responses = route["responses"]
    if not responses:
        return 200, {"message": "ok (no responses documented)"}, "none"

    if forced_status:
        if str(forced_status) not in responses:
            # 65 of this spec's operations document only 200 + 422, so a QA
            # engineer who needs a 500 has no documented way to ask for one.
            if allow_undocumented and str(forced_status).isdigit():
                code = int(forced_status)
                body = overlay.get(route["key"], code)[1]
                if body is None and spec is not None:
                    body = (synth.synthesise_error(code, "Forced by X-Mock-Status")
                            if code >= 400 else
                            synth.synthesise_success(route, spec, rng, path_params))
                return code, body, "forced:undocumented"
            return 400, {"mock_error": f"status {forced_status} not documented for {route['key']}",
                         "documented": sorted(responses.keys()),
                         "hint": "start mockd with --allow-undocumented-status to force it "
                                 "anyway, or add the status to the spec"}, "error"
        status = str(forced_status)
    else:
        status = _success_status(responses)

    if scenario:
        code, body = overlay.get(route["key"], status, scenario)
        if code == "missing_scenario":
            return 400, {"mock_error": f"scenario '{scenario}' not defined for {route['key']}",
                         "available": body}, "error"
        if code is not None:
            return int(code), body, "overlay:scenario"

    resp = responses[status]
    content = (resp.get("content") or {}).get("application/json", {})
    code = 200 if status == "default" else int(status)

    if example_name:
        named = content.get("examples") or {}
        if example_name not in named:
            return 400, {"mock_error": f"example '{example_name}' not found for {route['key']}",
                         "available": sorted(named.keys())}, "error"
        return code, named[example_name].get("value"), "spec:examples"

    ov_code, ov_body = overlay.get(route["key"], status)
    if ov_code is not None and not nulls:
        # An overlay entry is a curated answer, not a licence to contradict the
        # document. These are generated once and then outlive the spec they
        # came from: this one paginated a list the document declares as a bare
        # array, so every test written against the mock learned the wrong path.
        # The document wins, and the disagreement is reported rather than
        # silently served.
        conflict = _overlay_conflict(content.get("schema"), ov_body, spec)
        if conflict is None:
            return ov_code, ov_body, "overlay"
        overlay.note_conflict(route["key"], status, conflict)

    # X-Mock-Nulls has to generate: a stored example is a fixed document and
    # cannot show the caller what a null-heavy payload looks like.
    if nulls and content.get("schema"):
        body = generate_from_schema(content["schema"], rng or random.Random(route["key"]),
                                    nulls=True, array_items=array_items)
        if body is not None:
            return code, synth.normalise_envelope(body, code, resp.get("description")), \
                   "generated:nulls"
    if ov_code is not None and _overlay_conflict(content.get("schema"), ov_body,
                                                spec) is None:
        return ov_code, ov_body, "overlay"

    if "example" in content:
        return code, content["example"], "spec:example"
    if content.get("examples"):
        first = next(iter(content["examples"].values()))
        return code, first.get("value"), "spec:examples"
    rng = rng or random.Random(route["key"])
    schema = content.get("schema")
    if schema:
        body = generate_from_schema(schema, rng, nulls=nulls, array_items=array_items)
        if body is not None:
            return code, synth.normalise_envelope(body, code, resp.get("description")), "generated"

    if code == 204:
        return code, None, "empty"

    # Nothing declared. Rather than hand the UI a placeholder, infer a shape from
    # the rest of the spec — this is what lets an endpoint added to the swagger
    # doc today answer usefully today, before anyone curates an overlay entry.
    if spec is not None:
        if code < 400:
            return code, synth.synthesise_success(route, spec, rng, path_params), "synthesized"
        return code, synth.synthesise_error(code, resp.get("description")), "synthesized"

    return code, {"message": resp.get("description", "ok")}, "placeholder"


# How far an operation's payload has come from "somebody guessed" to "this is
# the contract". One status all three teams read, so nobody has to ask which
# file is authoritative.
TRUST_ORDER = ["guess", "proposed", "agreed", "verified", "spec"]
TRUST_MEANING = {
    "spec": "the spec itself declares the response — the mock serves what the contract says",
    "verified": "captured from a real environment and validated against the spec",
    "agreed": "written and signed off by a human in the overlay",
    "proposed": "edited in the overlay but not signed off",
    "guess": "inferred by the mock; nobody has confirmed the shape",
}


def operation_trust(route, overlay, spec_state):
    """Where this operation's payload sits on the ladder."""
    if spec_state == "complete":
        return "spec"
    entry = overlay.operations.get(route["key"]) or {}
    status = entry.get("status")
    if status in TRUST_ORDER:
        return status
    if entry:
        # an overlay entry with no explicit status: synthesised unless every
        # response body came from the spec
        origins = {(r or {}).get("_origin") for r in (entry.get("responses") or {}).values()}
        return "guess" if "synthesized" in origins else "proposed"
    return "guess"


def operation_coverage(route):
    """How completely does the spec describe this one operation?

    Three things a UI dev or a QA engineer needs and a document can omit:
    what comes back on success, what the request body must look like, and what
    the failure paths are. Reported per operation so the gaps are a worklist
    rather than a vague complaint about the spec."""
    responses = route["responses"]
    media = (route["request_body"].get("content") or {}).get("application/json")
    if not route["request_body"]:
        request_shape = "not applicable"
    elif media and media.get("schema"):
        request_shape = "schema"
    else:
        request_shape = "none"
    success = _success_status(responses)
    content = ((responses.get(success) or {}).get("content") or {}).get("application/json", {}) \
        if success else {}
    if content.get("schema"):
        response_shape = "schema"
    elif "example" in content or content.get("examples"):
        response_shape = "example"
    else:
        response_shape = "none"

    documented = sorted(c for c in responses if str(c).isdigit())
    failures = [c for c in documented if int(c) >= 400]
    beyond_validation = [c for c in failures if c != "422"]
    with_examples = [c for c in failures
                     if (((responses[c].get("content") or {}).get("application/json") or {}).get("example")
                         or ((responses[c].get("content") or {}).get("application/json") or {}).get("examples"))]

    # How much there is to actually play with: which fields are mandatory, which
    # are optional, how many failure paths can be forced. A tester picking an
    # endpoint to explore wants this before they open it.
    body_schema = ((media or {}).get("schema") or {}) if media else {}
    if body_schema.get("allOf"):
        body_schema = _merge_all_of(body_schema["allOf"])
    body_props = body_schema.get("properties") or {}
    body_required = set(body_schema.get("required") or [])
    params = route["parameters"]
    detail = {
        "body_required": len([k for k in body_props if k in body_required]),
        "body_optional": len([k for k in body_props if k not in body_required]),
        "query_required": len([p for p in params
                               if p.get("in") == "query" and p.get("required")]),
        "query_optional": len([p for p in params
                               if p.get("in") == "query" and not p.get("required")]),
        "path_params": len([p for p in params if p.get("in") == "path"]),
        "forceable_statuses": sorted(str(c) for c in responses if str(c).isdigit()),
    }
    detail["fields_documented"] = detail["body_required"] + detail["body_optional"]

    missing = []
    if response_shape == "none":
        missing.append("success response shape")
    elif response_shape == "example":
        # readable by a person and usable by the mock, but a machine cannot
        # check a response against an example — verify.py has to skip it
        missing.append("response schema (example only)")
    if request_shape == "none":
        missing.append("request body schema")
    if not beyond_validation:
        missing.append("failure responses")

    if response_shape == "none":
        state = "undocumented"
    elif missing:
        state = "partial"
    else:
        state = "complete"

    return {
        "operation": route["key"], "summary": route["summary"], "tags": route["tags"],
        "state": state, "missing": missing,
        "response_shape": response_shape, "request_shape": request_shape,
        "detail": detail,
        "statuses": documented,
        "failure_statuses": failures,
        "failure_examples": with_examples,
    }


def _error_envelope_split(spec):
    """Which 422 body shape each operation documents.

    More than one shape across a single API means the UI needs more than one
    error parser — worth knowing, and not visible from any single endpoint."""
    shapes = {}
    for route in spec.routes:
        resp = route["responses"].get("422")
        if not resp:
            shapes.setdefault("not documented", []).append(route["key"])
            continue
        content = (resp.get("content") or {}).get("application/json") or {}
        props = (content.get("schema") or {}).get("properties") or {}
        if props:
            name = "{" + ", ".join(sorted(props)) + "}"
        else:
            example = content.get("example")
            if example is None and content.get("examples"):
                example = next(iter(content["examples"].values())).get("value")
            name = "{" + ", ".join(sorted(example)) + "}" if isinstance(example, dict) \
                else "undeclared"
        shapes.setdefault(name, []).append(route["key"])
    return {"shapes": {k: len(v) for k, v in sorted(shapes.items(), key=lambda kv: -len(kv[1]))},
            "distinct": len([k for k in shapes if k not in ("not documented", "undeclared")]),
            "operations": shapes}


SPEC_RULES = [
    ("R1", "Declare a schema for every success response",
     "Without it there is nothing to generate from, validate against, or assert on."),
    ("R2", "Declare the failures you actually return",
     "A status the spec omits cannot be requested, asserted on, or proven."),
    ("R3", "One error envelope for the whole API",
     "Two shapes means the UI needs two error parsers."),
    ("R4", "Put an example on the responses that matter",
     "An example makes the mock exact instead of inferred."),
    ("R5", "Name types in components and $ref them",
     "Inline duplicates drift apart and generate inconsistently."),
    ("R6", "Be exact about required, enums, formats and bounds",
     "All of it drives both generation and validation."),
    ("R8", "Give every operation an operationId and a tag",
     "They become Postman folders and generated test names."),
    ("R9", "Declare servers, and keep secrets out",
     "The spec should be portable and safe to share."),
]


def spec_report(spec):
    """Grade the loaded document against SPEC_GUIDE.md.

    Deliberately reported per rule with the offending operations named: an
    architect needs a worklist, not a score."""
    routes = spec.routes
    total = len(routes) or 1
    schemas = (spec.doc.get("components") or {}).get("schemas") or {}
    findings = []

    def add(rule, ok, bad, detail):
        code, title, why = next(r for r in SPEC_RULES if r[0] == rule)
        findings.append({"rule": code, "title": title, "why": why,
                         "pass": len(ok), "fail": len(bad), "total": len(ok) + len(bad),
                         "detail": detail, "offenders": sorted(bad)[:40]})

    ok, bad = [], []
    for r in routes:
        cov = operation_coverage(r)
        (ok if cov["response_shape"] == "schema" else bad).append(r["key"])
    add("R1", ok, bad, "success response declared by a schema")

    ok, bad = [], []
    for r in routes:
        failures = [c for c in r["responses"] if str(c).isdigit() and int(c) >= 400
                    and c != "422"]
        (ok if failures else bad).append(r["key"])
    add("R2", ok, bad, "declares at least one failure beyond 422")

    envelopes = _error_envelope_split(spec)
    distinct = envelopes["distinct"]
    add("R3", ["ok"] * (1 if distinct <= 1 else 0), [] if distinct <= 1 else ["whole API"],
        f"{distinct} distinct 422 shape(s): "
        + ", ".join(f"{k} ({v})" for k, v in envelopes["shapes"].items()))

    ok, bad = [], []
    for r in routes:
        success = _success_status(r["responses"])
        content = ((r["responses"].get(success) or {}).get("content") or {}) \
            .get("application/json", {})
        (ok if ("example" in content or content.get("examples")) else bad).append(r["key"])
    add("R4", ok, bad, "success response carries an example")

    inline = []
    for path, item in (spec.doc.get("paths") or {}).items():
        for method, op in item.items():
            if method.lower() not in HTTP_METHODS:
                continue
            for resp in (op.get("responses") or {}).values():
                sch = ((resp.get("content") or {}).get("application/json") or {}).get("schema")
                if isinstance(sch, dict) and sch and "$ref" not in sch \
                        and (sch.get("properties") or sch.get("items")):
                    inline.append(f"{method.upper()} {path}")
                    break
    add("R5", [r["key"] for r in routes if f"{r['method']} {r['path']}" not in inline],
        list(dict.fromkeys(inline)), f"{len(schemas)} named component schema(s)")

    ok, bad = [], []
    for r in routes:
        media = (r["request_body"].get("content") or {}).get("application/json")
        schema = (media or {}).get("schema") or {}
        if not media:
            ok.append(r["key"])
            continue
        props = schema.get("properties") or {}
        (ok if (schema.get("required") or not props) else bad).append(r["key"])
    add("R6", ok, bad, "request bodies declare which fields are required")

    ok, bad = [], []
    for r in routes:
        (ok if (r["operation_id"] and r["tags"]) else bad).append(r["key"])
    add("R8", ok, bad, "operationId and at least one tag")

    servers = spec.doc.get("servers") or []
    add("R9", ["ok"] * (1 if servers else 0), [] if servers else ["whole document"],
        f"{len(servers)} server(s) declared" if servers else "no `servers` block")

    scored = [f for f in findings if f["total"]]
    score = round(100 * sum(f["pass"] for f in scored) / max(sum(f["total"] for f in scored), 1))
    return {"operations": len(routes), "score": score, "rules": findings,
            "title": spec.title, "version": spec.version}


def coverage(spec, overlay=None):
    """Spec-wide documentation coverage, plus what the mock is doing about it."""
    overlay = overlay or Overlay()
    ops, sources = [], {}
    trust_counts = {t: 0 for t in TRUST_ORDER}
    for route in sorted(spec.routes, key=lambda r: (r["path"], r["method"])):
        entry = operation_coverage(route)
        entry["body_source"] = body_source(route, overlay)
        entry["trust"] = operation_trust(route, overlay, entry["state"])
        entry["trust_meaning"] = TRUST_MEANING[entry["trust"]]
        trust_counts[entry["trust"]] += 1
        sources[entry["body_source"]] = sources.get(entry["body_source"], 0) + 1
        ops.append(entry)

    def count(pred):
        return sum(1 for o in ops if pred(o))

    body_ops = [o for o in ops if o["request_shape"] != "not applicable"]
    return {
        "total": len(ops),
        "states": {
            "complete": count(lambda o: o["state"] == "complete"),
            "partial": count(lambda o: o["state"] == "partial"),
            "undocumented": count(lambda o: o["state"] == "undocumented"),
        },
        "dimensions": {
            "success_response": {
                "documented": count(lambda o: o["response_shape"] != "none"),
                "missing": count(lambda o: o["response_shape"] == "none"),
                "by_kind": {
                    "schema": count(lambda o: o["response_shape"] == "schema"),
                    "example only": count(lambda o: o["response_shape"] == "example"),
                },
            },
            "request_body": {
                "applicable": len(body_ops),
                "documented": sum(1 for o in body_ops if o["request_shape"] == "schema"),
                "missing": sum(1 for o in body_ops if o["request_shape"] == "none"),
                "no_body": len(ops) - len(body_ops),
            },
            "error_envelopes": _error_envelope_split(spec),
            "failure_responses": {
                "documented": count(lambda o: [c for c in o["failure_statuses"] if c != "422"]),
                "validation_only": count(
                    lambda o: o["failure_statuses"] and
                    not [c for c in o["failure_statuses"] if c != "422"]),
                "none": count(lambda o: not o["failure_statuses"]),
                "with_examples": count(
                    lambda o: o["failure_statuses"] and
                    len(o["failure_examples"]) == len(o["failure_statuses"])),
            },
        },
        "served_by": sources,
        "trust": trust_counts,
        "trust_meaning": TRUST_MEANING,
        "safe_to_build_on": sum(trust_counts[t] for t in ("spec", "verified", "agreed")),
        "operations": ops,
    }


def satisfies_contract(route, status, body):
    """Does this body validate against the schema the operation documents for
    this status? Used to stop stateful mode serving a stored object through an
    operation whose response shape is different — /positions/mappings lists
    PositionMappingOut but /positions/mappings/{id} documents PositionOut."""
    resp = route["responses"].get(str(status)) or {}
    schema = ((resp.get("content") or {}).get("application/json") or {}).get("schema")
    if not schema:
        return True                    # nothing declared, nothing to violate
    return not next(_Validator(schema).iter_errors(body), None)


def body_source(route, overlay: Overlay):
    """What /_mock/routes reports, so gaps are visible without probing."""
    status = _success_status(route["responses"])
    if status is None:
        return "none"
    if overlay.get(route["key"], status)[0] is not None:
        return "overlay"
    content = ((route["responses"].get(status) or {}).get("content") or {}).get("application/json", {})
    if "example" in content or content.get("examples"):
        return "spec:example"
    if content.get("schema"):
        return "generated"
    return "synthesized"


# ----------------------------------------------------------------------------
# Stateful store (optional CRUD emulation)
# ----------------------------------------------------------------------------

_VERB_SUFFIX = re.compile(
    r"/(create|list|read|update|delete|add|remove|get|all|new|edit)(?=/|$)", re.I)


class StateStore:
    """Keeps created objects in memory so create -> read -> update -> delete
    flows work. Handles both REST shapes (/users, /users/{id}) and the
    verb-in-path shape this spec uses (/account/create, /account/read/{id})."""

    def __init__(self):
        self.data = {}
        self.shapes = {}   # collection -> (payload template, path to its list)
        self.lock = threading.Lock()

    @staticmethod
    def collection_of(route, path_params):
        """'/api/v1/account/read/{account_id}' and '/api/v1/account/create' must land in
        the SAME bucket, or a created object is invisible to the reader."""
        path = route["path"]
        item_id = None
        if route["path_params"]:
            last = route["path_params"][-1]
            if path.endswith("{%s}" % last):
                item_id = path_params.get(last)
                path = path[: -len("/{%s}" % last)]
        for name in route["path_params"]:
            if name in path_params:
                path = path.replace("{%s}" % name, str(path_params[name]))
        path = _VERB_SUFFIX.sub("", path)
        return path.rstrip("/") or "/", item_id

    @staticmethod
    def identity_of(obj, coll):
        """Not every resource names its key `id`: /positions/mappings returns
        `mapping_id`. Seeding on the wrong field makes every read-by-id 404."""
        if not isinstance(obj, dict):
            return None
        if obj.get("id") is not None:
            return str(obj["id"])
        stems = {seg.rstrip("s").lower() for seg in coll.strip("/").split("/")}
        for key, value in obj.items():
            if key.endswith("_id") and value is not None \
                    and key[:-3].lower() in stems:
                return str(value)
        return None

    @staticmethod
    def _extract_list(payload):
        """Find the list inside a payload so seeded data matches the real shape."""
        if isinstance(payload, list):
            return payload, ("__root__",)
        if isinstance(payload, dict):
            for key in ("items", "results", "data"):
                if isinstance(payload.get(key), list):
                    return payload[key], (key,)
                if isinstance(payload.get(key), dict):
                    inner, trail = StateStore._extract_list(payload[key])
                    if inner is not None:
                        return inner, (key,) + trail
        return None, None

    @staticmethod
    def _rewrite_list(payload, trail, items):
        if trail == ("__root__",):
            return items
        out = copy.deepcopy(payload)
        node = out
        for key in trail[:-1]:
            node = node[key]
        node[trail[-1]] = items
        return out

    def seed(self, coll, payload):
        items, trail = self._extract_list(payload)
        if not items:
            return False
        with self.lock:
            store = self.data.setdefault(coll, {})
            if store:
                return False
            for obj in items:
                if isinstance(obj, dict):
                    # store the object exactly as the spec/overlay wrote it —
                    # injecting an `id` it does not declare breaks its own schema
                    oid = self.identity_of(obj, coll) or uuid.uuid4().hex[:12]
                    store.setdefault(oid, obj)
        self.shapes[coll] = (payload, trail)
        return True

    def envelope(self, coll, items):
        shape = self.shapes.get(coll)
        if not shape:
            return items
        payload, trail = shape
        out = self._rewrite_list(payload, trail, items)
        if isinstance(out, dict):
            for key in ("total", "count"):
                if key in out:
                    out[key] = len(items)
            if isinstance(out.get("data"), dict) and "total" in out["data"]:
                out["data"]["total"] = len(items)
        return out

    @staticmethod
    def last_message(route, code):
        resp = route["responses"].get(str(code)) or {}
        return resp.get("description")

    def handle(self, route, path_params, body):
        method = route["method"]
        coll, item_id = self.collection_of(route, path_params)
        with self.lock:
            store = self.data.setdefault(coll, {})
            if method == "POST" and item_id is None:
                obj = dict(body or {})
                oid = self.identity_of(obj, coll) or uuid.uuid4().hex[:12]
                obj.setdefault("id", oid)
                store[oid] = obj
                return 201 if "201" in route["responses"] else 200, obj
            if item_id is not None and not store and method in ("GET", "PUT", "PATCH", "DELETE"):
                # Nothing has been created or seeded for this collection — which
                # is the normal state for a resource with no list endpoint, e.g.
                # /api/v1/lookups/{lookup_name}. 404ing here would hide the spec's
                # own payload forever; the store only takes over once it holds
                # something.
                return None, None
            if item_id is not None:
                if method == "GET":
                    return (200, store[item_id]) if item_id in store else \
                           (404, {"error": "not found", "id": item_id})
                if method in ("PUT", "PATCH"):
                    if item_id not in store:
                        return 404, {"error": "not found", "id": item_id}
                    if method == "PUT":
                        store[item_id] = dict(body or {}, id=item_id)
                    else:
                        store[item_id].update(body or {})
                    return 200, store[item_id]
                if method == "DELETE":
                    if item_id in store:
                        del store[item_id]
                        return (204, None) if "204" in route["responses"] else \
                               (200, {"message": "deleted", "id": item_id})
                    return 404, {"error": "not found", "id": item_id}
            if method == "GET" and store:
                return 200, self.envelope(coll, list(store.values()))
        return None, None   # nothing stored yet -> fall through to spec/overlay


# ----------------------------------------------------------------------------
# App
# ----------------------------------------------------------------------------


# ----------------------------------------------------------------------------
# Live reload — the spec is a moving target
# ----------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Documentation pages are not specs
# ---------------------------------------------------------------------------

# People copy the URL from their browser, and that URL is almost always the
# rendered documentation — Swagger UI, ReDoc, RapiDoc — not the document behind
# it. The page always names its own spec, so read it out rather than making
# somebody hunt through the network tab for it.
SPEC_URL_PATTERNS = (
    r'<redoc[^>]*\bspec-url\s*=\s*["\']([^"\']+)["\']',        # ReDoc
    r'<rapi-doc[^>]*\bspec-url\s*=\s*["\']([^"\']+)["\']',     # RapiDoc
    r'\bapiDescriptionUrl\s*=\s*["\']([^"\']+)["\']',          # Stoplight Elements
    r'["\']?\burl["\']?\s*:\s*["\']([^"\']+\.(?:json|ya?ml)[^"\']*)["\']',  # Swagger UI
    r'["\']?\burl["\']?\s*:\s*["\']([^"\']*/(?:openapi|swagger|api-docs)[^"\']*)["\']',
    r'<link[^>]+rel=["\'][^"\']*(?:openapi|service-desc)[^"\']*["\'][^>]+href=["\']([^"\']+)["\']',
)

# Tried in order when the page names nothing — the conventional locations.
WELL_KNOWN_SPEC_PATHS = ("/openapi.json", "/swagger.json", "/v3/api-docs",
                         "/api-docs", "/swagger/v1/swagger.json", "/openapi.yaml")


def looks_like_html(text):
    head = (text or "")[:400].lstrip().lower()
    return head.startswith(("<!doctype html", "<html", "<?xml-stylesheet")) or "<html" in head


def spec_urls_in_page(html, page_url):
    """Every spec URL a documentation page points at, most explicit first."""
    found, seen = [], set()

    def offer(raw):
        if not raw or raw.startswith(("data:", "javascript:", "#")):
            return
        absolute = urllib.parse.urljoin(page_url, raw.strip())
        if absolute not in seen:
            seen.add(absolute)
            found.append(absolute)

    for pattern in SPEC_URL_PATTERNS:
        for match in re.finditer(pattern, html or "", re.I):
            offer(match.group(1))

    # Swagger UI can list several documents: urls: [{url: "...", name: "..."}]
    for block in re.finditer(r'\burls\s*:\s*\[(.*?)\]', html or "", re.I | re.S):
        for match in re.finditer(r'\burl\s*:\s*["\']([^"\']+)["\']', block.group(1)):
            offer(match.group(1))

    for path in WELL_KNOWN_SPEC_PATHS:
        offer(urllib.parse.urljoin(page_url, path))
    return found


def looks_like_spec(text):
    """Parses, and says which dialect it is. Cheap enough to try candidates."""
    try:
        doc = yaml.safe_load(text)
    except Exception:
        return False
    return isinstance(doc, dict) and ("openapi" in doc or "swagger" in doc)


class Source:
    """A spec (or overlay) that lives either on disk or behind a URL.

    A local file is cheap to check, so it is checked on every request via
    mtime. A URL is not, so it is polled at most every `poll` seconds and uses
    ETag/Last-Modified so an unchanged document costs a 304 rather than a
    download. The last good copy is cached on disk, which means the mock still
    boots when the upstream /openapi.json is down."""

    def __init__(self, location, headers=None, poll=60, cache_dir=None):
        self.location = location
        self.is_url = bool(location) and str(location).lower().startswith(("http://", "https://"))
        self.headers = dict(headers or {})
        self.poll = max(int(poll), 5)
        self.etag = None
        self.last_modified = None
        self.digest = None
        self.checked_at = 0.0
        self.fetched_at = None
        self.error = None
        self.cache_path = None
        self.resolved_from = None        # set when we followed a docs page
        if self.is_url and cache_dir:
            safe = re.sub(r"[^A-Za-z0-9]+", "_", location)[-80:]
            self.cache_path = Path(cache_dir) / f"spec_cache_{safe}.txt"
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)

    # -- disk ---------------------------------------------------------------
    def _mtime(self):
        try:
            return Path(self.location).stat().st_mtime
        except OSError:
            return None

    # -- network ------------------------------------------------------------
    def _http_get(self):
        req = urllib.request.Request(self.location, headers=self.headers)
        if self.etag:
            req.add_header("If-None-Match", self.etag)
        if self.last_modified:
            req.add_header("If-Modified-Since", self.last_modified)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                text = resp.read().decode(resp.headers.get_content_charset() or "utf-8")
                self.etag = resp.headers.get("ETag") or self.etag
                self.last_modified = resp.headers.get("Last-Modified") or self.last_modified
                self.error = None
                return text
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                self.error = None
                return None                       # unchanged upstream
            self.error = f"HTTP {exc.code} fetching {self.location}"
        except Exception as exc:
            self.error = f"{type(exc).__name__} fetching {self.location}: {exc}"
        return None

    def _follow_page(self, html):
        """The URL was a documentation page. Find the document it renders."""
        for candidate in spec_urls_in_page(html, self.location):
            try:
                req = urllib.request.Request(candidate, headers=self.headers)
                with urllib.request.urlopen(req, timeout=20) as resp:
                    body = resp.read().decode(resp.headers.get_content_charset() or "utf-8")
            except Exception:
                continue
            if looks_like_html(body) or not looks_like_spec(body):
                continue
            # poll the document from now on, not the page that named it
            self.resolved_from = self.location
            self.location = candidate
            self.etag = self.last_modified = None
            self.error = None
            return body
        self.error = (f"{self.location} is a documentation page, not a spec, and no "
                      f"OpenAPI document could be found from it. Open the page's "
                      f"Network tab and use the .json URL it loads.")
        return None

    def _cached(self):
        if self.cache_path and self.cache_path.exists():
            return self.cache_path.read_text()
        return None

    # -- api ----------------------------------------------------------------
    def read(self, force=False):
        """Return (text, changed). text is None when nothing needs reloading."""
        if not self.location:
            return None, False
        if not self.is_url:
            stamp = self._mtime()
            if stamp is None:
                return None, False
            if not force and stamp == self.digest:
                return None, False
            self.digest = stamp
            self.fetched_at = time.time()
            return Path(self.location).read_text(), True

        now = time.time()
        if not force and now - self.checked_at < self.poll:
            return None, False
        self.checked_at = now
        text = self._http_get()
        if text is not None and looks_like_html(text):
            text = self._follow_page(text)
        if text is None:
            if self.digest is None:               # first load failed — fall back
                cached = self._cached()
                if cached:
                    self.digest = hashlib.sha256(cached.encode()).hexdigest()
                    return cached, True
            return None, False
        digest = hashlib.sha256(text.encode()).hexdigest()
        if digest == self.digest:
            return None, False
        self.digest = digest
        self.fetched_at = now
        if self.cache_path:
            try:
                self.cache_path.write_text(text)
            except OSError:
                pass
        return text, True

    def describe(self):
        return {"location": self.location, "kind": "url" if self.is_url else "file",
                "resolved_from": self.resolved_from,
                "poll_seconds": self.poll if self.is_url else None,
                "last_fetch": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.fetched_at))
                              if self.fetched_at else None,
                "error": self.error}


class SpecWatcher:
    """Holds the loaded spec + overlay and reloads them when the source changes.

    The whole point of a spec-driven mock is that the spec is the source of
    truth; a backend team that adds three endpoints on Tuesday should not have
    to ask anyone to restart the mock — and should not have to re-export a file
    either, if the service publishes /openapi.json."""

    def __init__(self, spec_path, overlay_path=None, on_reload=None,
                 headers=None, poll=60, cache_dir="logs"):
        self.spec_source = Source(spec_path, headers, poll, cache_dir)
        self.overlay_source = Source(overlay_path, headers, poll, cache_dir)
        self.spec_path = spec_path
        self.overlay_path = overlay_path
        self.on_reload = on_reload
        self.lock = threading.Lock()
        self.generation = 0
        self.loaded_at = None
        self.last_change = None
        self.spec_digest = None
        self.error = None
        self.spec = None
        self.overlay = Overlay()
        if not self._load(force=True):
            raise SystemExit(f"mockd: cannot load spec — {self.error}")

    def _load(self, force=False):
        spec_text, spec_changed = self.spec_source.read(force)
        if spec_text is not None:
            self.spec_digest = hashlib.sha256(spec_text.encode()).hexdigest()
        overlay_text, overlay_changed = self.overlay_source.read(force)
        if not spec_changed and not overlay_changed and self.spec is not None:
            return False

        previous = self.spec
        try:
            spec = Spec(text=spec_text, origin=self.spec_path) if spec_changed else previous
            if overlay_changed and overlay_text is not None:
                overlay = Overlay(self.overlay_path, text=overlay_text)
            elif self.generation == 0:
                overlay = Overlay(self.overlay_path)
            else:
                overlay = self.overlay
        except Exception as exc:                 # keep serving the last good copy
            self.error = f"{type(exc).__name__}: {exc}"
            return False
        if spec is None:
            self.error = self.spec_source.error or "spec source unavailable"
            return False

        before = {r["key"] for r in previous.routes} if previous else set()
        after = {r["key"] for r in spec.routes}
        self.spec, self.overlay, self.error = spec, overlay, None
        self.generation += 1
        self.loaded_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.last_change = {"added": sorted(after - before),
                            "removed": sorted(before - after),
                            "overlay_reloaded": bool(overlay_changed)}
        if previous is not None and self.on_reload:
            self.on_reload(self)
        return True

    def check(self):
        """Reload if the spec or overlay changed. Returns True if it reloaded."""
        with self.lock:
            return self._load()

    def reload(self):
        with self.lock:
            return self._load(force=True)

    def drift(self):
        """Operations in the spec with no curated overlay entry — the worklist
        for whoever is keeping the mock honest."""
        uncurated, synthesized = [], []
        for r in self.spec.routes:
            src = body_source(r, self.overlay)
            if src == "synthesized":
                synthesized.append(r["key"])
            if src != "overlay":
                uncurated.append({"operation": r["key"], "body_source": src})
        stale = sorted(set(self.overlay.operations) - {r["key"] for r in self.spec.routes})
        return {
            "generation": self.generation,
            "loaded_at": self.loaded_at,
            "spec_source": self.spec_source.describe(),
            "overlay_source": self.overlay_source.describe(),
            # lets a client prove it is judging the mock against the same
            # document the mock is actually serving
            "spec_digest": self.spec_digest,
            "spec_operations": len(self.spec.routes),
            "overlay_operations": len(self.overlay.operations),
            "last_change": self.last_change,
            "synthesized_operations": synthesized,
            "uncurated": uncurated,
            "overlay_entries_no_longer_in_spec": stale,
            "overlay_entries_contradicting_the_spec": self.overlay.conflicts,
            "spec_error": self.error,
        }


# ----------------------------------------------------------------------------
# App
# ----------------------------------------------------------------------------


def build_app(spec_path, stateful=False, log_path=Path("logs/requests.jsonl"),
              overlay_path=None, require_auth=False, array_items=2, seed_state=True,
              watch=True, headers=None, poll=60, allow_undocumented=False,
              validation_mode="spec"):
    store = StateStore()
    log_ring = []
    app = Flask("mockd")
    CORS(app, expose_headers=["X-Mock-Source", "X-Mock-Operation", "X-Mock-Generation"])
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def seed(watcher):
        if not (stateful and seed_state):
            return
        store.data.clear()
        store.shapes.clear()
        for r in watcher.spec.routes:
            if r["method"] != "GET":
                continue
            code, body, _ = pick_response(r, watcher.overlay, rng=random.Random(r["key"]),
                                          array_items=array_items, spec=watcher.spec)
            if code == 200 and body is not None:
                coll, item_id = StateStore.collection_of(
                    r, {n: "seed" for n in r["path_params"]})
                if item_id is None:
                    store.seed(coll, body)

    watcher = SpecWatcher(spec_path, overlay_path, on_reload=seed,
                          headers=headers, poll=poll, cache_dir=str(log_path.parent))
    seed(watcher)

    def record(entry):
        entry["ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        log_ring.append(entry)
        del log_ring[:-200]
        with open(log_path, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    def fresh():
        """Introspection must see the same generation a request would — these
        endpoints are what the console and CI read, so serving a stale spec or
        overlay here is worse than serving it on a mock response."""
        if watch:
            watcher.check()
        return watcher.spec, watcher.overlay

    @app.get("/_mock/routes")
    def _routes():
        fresh()
        return jsonify([{
            "method": r["method"], "path": r["path"], "summary": r["summary"],
            "tags": r["tags"], "operationId": r["operation_id"],
            "statuses": sorted(r["responses"].keys()),
            "body_source": body_source(r, watcher.overlay),
            "scenarios": watcher.overlay.scenarios(r["key"]),
        } for r in sorted(watcher.spec.routes, key=lambda x: (x["path"], x["method"]))])

    @app.get("/_mock/spec-report")
    def _spec_report():
        return jsonify(spec_report(fresh()[0]))

    @app.get("/_mock/coverage")
    def _coverage():
        return jsonify(coverage(*fresh()))

    @app.get("/_mock/drift")
    def _drift():
        fresh()
        return jsonify(watcher.drift())

    @app.get("/_mock/log")
    def _log():
        return jsonify(log_ring)

    @app.get("/_mock/state")
    def _state():
        return jsonify(store.data)

    @app.post("/_mock/reload")
    def _reload():
        watcher.reload()
        return jsonify({"reloaded": True, **watcher.drift()})

    @app.post("/_mock/reset")
    def _reset():
        store.data.clear()
        store.shapes.clear()
        log_ring.clear()
        seed(watcher)
        return jsonify({"reset": True})

    @app.route("/", defaults={"path": ""}, methods=[m.upper() for m in HTTP_METHODS])
    @app.route("/<path:path>", methods=[m.upper() for m in HTTP_METHODS])
    def catch_all(path):
        if watch:
            watcher.check()
        spec, overlay = watcher.spec, watcher.overlay

        full = "/" + path
        route, path_params = spec.match(request.method, full)
        entry = {"method": request.method, "path": full, "query": request.args.to_dict(),
                 "matched": None, "validation_errors": [], "status": None, "source": None,
                 "spec_generation": watcher.generation}

        delay = request.headers.get("X-Mock-Delay")
        if delay and str(delay).isdigit():
            time.sleep(min(int(delay), 30000) / 1000.0)

        if route is None:
            entry["status"] = 404
            record(entry)
            near = sorted({r["path"] for r in spec.routes if r["regex"].match(full)})
            return jsonify({"mock_error": f"no operation {request.method} {full} in spec",
                            "hint": f"path exists but not for {request.method}; documented: {near}"
                                    if near else "path not in spec — check /_mock/routes"}), 404

        entry["matched"] = route["key"]
        forced = request.headers.get("X-Mock-Status")
        example = request.headers.get("X-Mock-Example")
        scenario = request.headers.get("X-Mock-Scenario")
        nulls = str(request.headers.get("X-Mock-Nulls", "")).lower() in ("1", "on", "true")

        if require_auth and not forced and not request.headers.get("Authorization") \
                and not request.cookies:
            # Enforce it for EVERY operation. Only honouring it where the spec
            # documents a 401 meant --require-auth silently did nothing on 65 of
            # this spec's 96 operations — the ones that need testing most.
            if "401" in route["responses"]:
                code, body, _ = pick_response(route, overlay, forced_status="401", spec=spec)
            else:
                code, body = 401, synth.synthesise_error(
                    401, "Authorization token missing")
            entry.update(status=code, source="auth")
            record(entry)
            return jsonify(body), code

        errors = validate_request(route, path_params)
        if errors:
            documented = None if validation_mode == "debug" else \
                as_documented_validation_error(route, errors)
            code = documented[0] if documented else 400
            entry.update(validation_errors=errors, status=code,
                         body=request.get_json(silent=True), source="validation")
            record(entry)
            if documented:
                resp = jsonify(documented[1])
                resp.headers["X-Mock-Source"] = "validation"
                resp.headers["X-Mock-Operation"] = route["key"]
                resp.headers["X-Mock-Violations"] = str(len(errors))
                return resp, code
            return jsonify({"mock_validation_failed": True,
                            "your_request_violates_the_spec": errors,
                            "operation": route["key"]}), 400

        if stateful and not forced and not example and not scenario and not nulls:
            code, body = store.handle(route, path_params, request.get_json(silent=True))
            if code == 404 and "404" in route["responses"]:
                # the spec documents its own not-found payload — serve that
                # rather than the store's bare {"error": "not found"}
                code, body, src = pick_response(route, overlay, forced_status="404",
                                                rng=random.Random(route["key"]), spec=spec)
                entry.update(status=code, source="stateful+" + src)
                record(entry)
                resp = jsonify(body)
                resp.headers["X-Mock-Source"] = "stateful+" + src
                return resp, code
            if code is not None and code < 400 and body is not None:
                body = synth.wrap_like_spec(
                    route, code, body, store.last_message(route, code),
                    reference=overlay.get(route["key"], code)[1])
                if request.method == "GET" and not satisfies_contract(route, code, body):
                    code = None        # stored shape ≠ documented shape; defer
            if code is not None:
                entry.update(status=code, source="stateful")
                record(entry)
                if code == 204 or body is None:
                    return "", code
                resp = jsonify(body)
                resp.headers["X-Mock-Source"] = "stateful"
                resp.headers["X-Mock-Operation"] = route["key"]
                return resp, code

        code, body, src = pick_response(route, overlay, forced, example, scenario,
                                        rng=random.Random(route["key"]), nulls=nulls,
                                        array_items=array_items, spec=spec,
                                        path_params=path_params,
                                        allow_undocumented=allow_undocumented)
        entry.update(status=code, source=src)
        record(entry)
        if body is None:
            return "", code
        resp = jsonify(body)
        resp.headers["X-Mock-Source"] = src
        resp.headers["X-Mock-Operation"] = route["key"]
        resp.headers["X-Mock-Generation"] = str(watcher.generation)
        return resp, code

    return app, watcher


def main():
    ap = argparse.ArgumentParser(description="mockd — OpenAPI mock server")
    ap.add_argument("--spec", required=True,
                    help="Path OR http(s) URL of an OpenAPI 3.x document "
                         "(e.g. https://api.dev.example.com/openapi.json)")
    ap.add_argument("--poll", type=int, default=60,
                    help="Seconds between upstream checks when --spec is a URL "
                         "(default 60; ETag means an unchanged spec costs a 304)")
    ap.add_argument("--header", action="append", default=[], metavar="'K: V'",
                    help="Header sent when fetching the spec URL, repeatable "
                         "(e.g. --header 'Authorization: Bearer ...')")
    ap.add_argument("--port", type=int, default=4010)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--overlay", help="JSON file of hand-authored response payloads")
    ap.add_argument("--stateful", action="store_true",
                    help="Emulate CRUD state in memory (create -> read -> update -> delete)")
    ap.add_argument("--no-seed", action="store_true",
                    help="Start stateful mode with an empty store instead of seeding "
                         "it from the spec/overlay list payloads")
    ap.add_argument("--no-watch", action="store_true",
                    help="Do not reload when the spec or overlay file changes on disk")
    ap.add_argument("--validation-mode", choices=["spec", "debug"], default="spec",
                    help="spec (default): reject bad requests in the shape the API "
                         "documents (FastAPI's 422 + detail[]), so UI error handling "
                         "written against the mock also works against the real backend. "
                         "debug: a 400 listing every violating field by name.")
    ap.add_argument("--allow-undocumented-status", action="store_true",
                    help="Let X-Mock-Status force a status the spec does not document "
                         "(this spec documents only 200/422 for 65 of 96 operations)")
    ap.add_argument("--require-auth", action="store_true",
                    help="Return the documented 401 when Authorization is absent")
    ap.add_argument("--array-items", type=int, default=2,
                    help="How many items to generate for arrays (default 2)")
    ap.add_argument("--log", default="logs/requests.jsonl")
    args = ap.parse_args()

    headers = {}
    for raw in args.header:
        if ":" not in raw:
            raise SystemExit(f"--header must look like 'Name: value', got {raw!r}")
        name, value = raw.split(":", 1)
        headers[name.strip()] = value.strip()

    overlay_path = args.overlay
    if overlay_path is None:
        # Auto-adopt a sibling overlay only if it is actually about THIS spec.
        # Pointing mockd at a second spec in the same folder used to pick up the
        # first one's overlay and warn about 96 stale entries.
        candidates = []
        if not str(args.spec).lower().startswith(("http://", "https://")):
            candidates.append(Path(args.spec).with_name("mock_overlay.json"))
        candidates.append(Path("mock_overlay.json"))
        probe = Spec(args.spec) if not str(args.spec).lower().startswith(("http", "https")) \
            else None
        keys = {r["key"] for r in probe.routes} if probe else set()
        for guess in candidates:
            if not guess.exists():
                continue
            try:
                ops = set(Overlay(str(guess)).operations)
            except Exception:
                continue
            if not keys or ops & keys:
                overlay_path = str(guess)
                break
            print(f"mockd: ignoring {guess.name} — none of its {len(ops)} operations "
                  f"are in this spec. Pass --overlay to use it anyway.")

    app, watcher = build_app(args.spec, args.stateful, Path(args.log), overlay_path,
                             args.require_auth, args.array_items, not args.no_seed,
                             not args.no_watch, headers, args.poll,
                             args.allow_undocumented_status, args.validation_mode)
    spec, overlay = watcher.spec, watcher.overlay
    drift = watcher.drift()

    kind = "URL" if watcher.spec_source.is_url else "file"
    print(f"mockd: {len(spec.routes)} routes from {kind} {args.spec} "
          f"(OpenAPI {spec.version}{', ' + spec.title if spec.title else ''})")
    if watcher.spec_source.is_url:
        print(f"mockd: re-checking upstream every {args.poll}s; cached copy at "
              f"{watcher.spec_source.cache_path}")
    if overlay_path:
        print(f"mockd: overlay {overlay_path} ({len(overlay.operations)} operations)")
    cov = coverage(spec, overlay)
    st = cov["states"]
    print(f"mockd: spec coverage — {st['complete']} fully documented, "
          f"{st['partial']} partial, {st['undocumented']} with no response shape "
          f"(GET /_mock/coverage)")

    if drift["synthesized_operations"]:
        print(f"mockd: {len(drift['synthesized_operations'])} operations declare no response "
              f"shape in the spec and are answered with SYNTHESISED payloads:")
        for k in drift["synthesized_operations"][:8]:
            print(f"          {k}")
        if len(drift["synthesized_operations"]) > 8:
            print(f"          ... and {len(drift['synthesized_operations']) - 8} more "
                  f"(GET /_mock/drift)")
        print("mockd: run build_overlay.py to turn those into an editable overlay file.")
    if drift["overlay_entries_no_longer_in_spec"]:
        print(f"mockd: {len(drift['overlay_entries_no_longer_in_spec'])} overlay entries no "
              f"longer exist in the spec (GET /_mock/drift)")
    print(f"mockd: {'STATEFUL' if args.stateful else 'stateless'}"
          f"{' + auth' if args.require_auth else ''}"
          f"{'' if args.no_watch else ' + live-reload'} on "
          f"http://{args.host}:{args.port}  (introspection: /_mock/routes)")
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
