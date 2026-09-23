#!/usr/bin/env python3
"""
synth.py — payload synthesis for operations the spec leaves undefined.

apis.json is a FastAPI export: 65 of its 96 operations declare `"schema": {}`
for their success response, because the route has no `response_model`. There is
nothing there for a schema-driven generator to work from.

This module infers a plausible body for such an operation from whatever the spec
DOES carry — its request schema, a component schema matching the resource name,
the path verb, the enums — wrapped in this API's `{status_code, message, data}`
envelope.

It is used in two places, deliberately:

  * build_overlay.py — to seed an editable overlay file that a team can curate
  * mockd.py         — live, at request time, as the last fallback

The second one is what makes the mock survive a swagger update: an endpoint
added to apis.json this morning answers with a shaped payload this afternoon,
with no overlay entry and no code change. Curating it later only improves it.
"""
import copy
import random

from generator import generate_from_schema

# ----------------------------------------------------------------------------
# House conventions
# ----------------------------------------------------------------------------

LIST_HINTS = {
    "list", "all", "dropdown", "search", "dashboard", "groups", "questions",
    "secrets", "mappings", "levels", "positions", "departments", "types",
    "cities", "states", "countries", "currencies", "languages", "phonecodes",
    "regions", "skills", "all-cities", "set", "items", "records",
}

RESOURCE_SCHEMA_HINTS = {
    "user": ["UserCreateSchema", "UserUpdateSchema"],
    "role": ["RoleCreateSchema", "RoleUpdateSchema"],
    "address": ["AddressCreateSchema", "AddressUpdateSchema"],
    "device": ["DeviceCreateSchema", "DeviceUpdateSchema"],
    "requisition": ["RequisitionSchema"],
    "integrations": ["IntegrationCreate", "IntegrationUpdate"],
    "secrets": ["IntegrationSecretCreate", "IntegrationSecretUpdate"],
}

# Metadata endpoints carry no schema anywhere in the spec. These are the
# conventional shapes for the data they serve; correct them if the API differs.
METADATA_SHAPES = {
    "country-data": lambda: {"countries": 250, "states": 5038, "cities": 151024},
    "all-cities": lambda: [
        {"id": 57582, "name": "Bengaluru", "state_id": 4023, "country_id": 101},
        {"id": 57589, "name": "Pune", "state_id": 4008, "country_id": 101},
    ],
    "countries": lambda: [
        {"id": 101, "name": "India", "iso2": "IN", "iso3": "IND",
         "phone_code": "91", "currency": "INR", "region": "Asia"},
        {"id": 233, "name": "United States", "iso2": "US", "iso3": "USA",
         "phone_code": "1", "currency": "USD", "region": "Americas"},
    ],
    "states": lambda: [
        {"id": 4023, "name": "Karnataka", "state_code": "KA", "country_id": 101},
        {"id": 4008, "name": "Maharashtra", "state_code": "MH", "country_id": 101},
    ],
    "cities": lambda: [
        {"id": 57582, "name": "Bengaluru", "state_id": 4023, "country_id": 101,
         "latitude": "12.97194", "longitude": "77.59369"},
        {"id": 57589, "name": "Pune", "state_id": 4008, "country_id": 101,
         "latitude": "18.51957", "longitude": "73.85535"},
    ],
    "currencies": lambda: [
        {"id": 1, "name": "Indian Rupee", "code": "INR", "symbol": "₹"},
        {"id": 2, "name": "US Dollar", "code": "USD", "symbol": "$"},
    ],
    "languages": lambda: [
        {"id": 1, "name": "English", "code": "en"},
        {"id": 2, "name": "Hindi", "code": "hi"},
    ],
    "phonecodes": lambda: [
        {"id": 101, "name": "India", "iso2": "IN", "phone_code": "91"},
        {"id": 233, "name": "United States", "iso2": "US", "phone_code": "1"},
    ],
    "regions": lambda: [{"id": 1, "name": "Asia"}, {"id": 2, "name": "Americas"}],
    "skills": lambda: [
        {"id": 1, "name": "Python", "category": "Programming"},
        {"id": 2, "name": "React", "category": "Frontend"},
    ],
}

UUID_SCHEMA = {"type": "string", "format": "uuid"}


def envelope(message, data, status=200):
    return {"status_code": status, "message": message, "data": data}


def paginate(items, page=1, page_size=10):
    return {"items": items, "total": len(items), "page": page, "page_size": page_size}


def resource_of(path):
    """'/api/v1/user/read/{user_id}' -> 'user'."""
    parts = [p for p in path.strip("/").split("/") if not p.startswith("{")]
    parts = [p for p in parts if p not in ("api", "v1")]
    return parts[0] if parts else ""


def looks_like_list(route):
    if route["method"] != "GET":
        return False
    tail = route["path"].rstrip("/").split("/")[-1].lower()
    return not tail.startswith("{") and (tail in LIST_HINTS or tail.endswith("s"))


def normalise_envelope(body, status, description):
    """A schema-generated StandardResponseModel comes out as
    {"status_code": 81, "message": "sample-message"} — random noise where the
    spec's own convention has an obvious answer."""
    if not isinstance(body, dict):
        return body
    if "status_code" in body and isinstance(body.get("status_code"), int):
        body["status_code"] = int(status)
    if "message" in body and isinstance(body.get("message"), str) \
            and body["message"].startswith("sample-"):
        body["message"] = description or "Successful Response"
    return body


# ----------------------------------------------------------------------------
# Entity inference
# ----------------------------------------------------------------------------


def _audit(obj, rng):
    out = {"id": generate_from_schema(UUID_SCHEMA, rng)}
    out.update(obj)
    out.setdefault("created_at", "2026-05-14T09:15:00Z")
    out.setdefault("updated_at", "2026-06-02T11:40:00Z")
    return out


def entity_from_request(route, spec, rng):
    """The operation's own request schema is the best available description of
    the entity — a response almost always echoes it plus server-side fields."""
    schema = ((route["request_body"].get("content") or {})
              .get("application/json", {}).get("schema"))
    if not schema:
        return None
    obj = generate_from_schema(schema, rng, array_items=2)
    return _audit(obj, rng) if isinstance(obj, dict) else None


def entity_from_components(route, spec, rng):
    """GET/DELETE have no request body — match a component schema by resource
    name, so a newly added /api/v1/offer/read/{id} picks up OfferCreateSchema
    or OfferOut without anyone configuring anything."""
    schemas = (spec.doc.get("components") or {}).get("schemas") or {}
    res = resource_of(route["path"])
    names = [n for n in RESOURCE_SCHEMA_HINTS.get(res, []) if n in schemas]
    if not names:
        stem = res.rstrip("s").lower()
        if stem:
            names = sorted(
                (n for n in schemas
                 if n.lower().startswith(stem) and not n.startswith("StandardResponseModel")),
                # prefer Out/Read shapes over Create/Update inputs
                key=lambda n: (0 if ("out" in n.lower() or "read" in n.lower()) else 1, len(n)),
            )
    for name in names:
        obj = generate_from_schema(spec.resolve(schemas[name]), rng, array_items=2)
        if isinstance(obj, dict) and obj:
            return _audit(obj, rng)
    return None


def enum_payload(spec, enum_name=None):
    """GET /api/v1/enums/{enum_name} — the spec already carries every enum, so
    serve the real values instead of a placeholder."""
    schemas = (spec.doc.get("components") or {}).get("schemas") or {}
    candidates = {k: v for k, v in schemas.items() if v.get("enum")}
    chosen = None
    if enum_name:
        want = enum_name.replace("_", "").replace("-", "").lower()
        for name, sch in candidates.items():
            if name.lower().replace("enum", "").startswith(want.replace("enum", "")):
                chosen = sch
                break
    if chosen is None:
        chosen = candidates.get("UserStatusEnum") or (
            next(iter(candidates.values())) if candidates else {"enum": ["Active", "Inactive"]})
    return envelope("Enum fetched successfully",
                    [{"label": v, "value": v} for v in chosen.get("enum", [])])


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------


def synthesise_success(route, spec, rng=None, path_params=None):
    """Plausible success body for an operation whose response shape is undeclared."""
    rng = rng or random.Random(route["key"])
    path, method = route["path"], route["method"]
    tail = path.rstrip("/").split("/")[-1].lower()
    summary = route["summary"] or route["key"]
    res = resource_of(path)

    if "/enums/" in path:
        return enum_payload(spec, (path_params or {}).get("enum_name"))

    if "/metadata/" in path:
        for key, factory in METADATA_SHAPES.items():
            if key in path:
                data = factory()
                if not isinstance(data, list):
                    return envelope("Stats fetched successfully", data)
                if route["path_params"] and path.rstrip("/").endswith("}"):
                    return envelope("Record fetched successfully", data[0])
                return envelope(f"{key.replace('-', ' ').title()} fetched successfully",
                                paginate(data))

    if "/auth/" in path:
        if "login" in path:
            return envelope("Login successful", {
                "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.mock.access",
                "refresh_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.mock.refresh",
                "token_type": "bearer", "expires_in": 3600})
        if "refresh" in path:
            return envelope("Token refreshed", {
                "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.mock.access2",
                "token_type": "bearer", "expires_in": 3600})
        if "logout" in path:
            return envelope("Logged out successfully", None)
        if path.endswith("/me"):
            return envelope("User fetched successfully", {
                "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
                "username": "priya.sharma", "email": "priya.sharma@example.com",
                "first_name": "Priya", "last_name": "Sharma",
                "role": {"id": "9c1e6b8a-2d44-4f31-8c77-1f2b9de0a512", "title": "Recruiter"},
                "status": "Active", "is_active": True})
        if "callback" in path:
            return envelope("Authentication callback processed", {"redirect_url": "/dashboard"})

    if method == "DELETE" or tail.startswith("delete"):
        return envelope(f"{res.capitalize()} deleted successfully", None)

    entity = entity_from_request(route, spec, rng) or entity_from_components(route, spec, rng)
    if entity is None:
        entity = {
            "id": generate_from_schema(UUID_SCHEMA, rng),
            "_mock_note": f"{route['key']} declares no response schema and no component "
                          f"schema matched '{res}' — add a payload to the overlay file",
        }

    if looks_like_list(route):
        second = copy.deepcopy(entity)
        second["id"] = generate_from_schema(UUID_SCHEMA, rng)
        return envelope(f"{summary} successful", paginate([entity, second]))

    if method == "POST" and not route["path_params"]:
        return envelope(f"{res.capitalize()} created successfully", entity)
    if method in ("PUT", "PATCH"):
        return envelope(f"{res.capitalize()} updated successfully", entity)
    return envelope(f"{res.capitalize()} fetched successfully", entity)


def synthesise_error(status, description):
    return {"status_code": int(status),
            "message": description or "Error",
            "detail": description or "Error"}


def _success_schema(route):
    codes = [c for c in route["responses"] if str(c).startswith("2")]
    if not codes:
        return {}
    resp = route["responses"][sorted(codes, key=str)[0]]
    return ((resp.get("content") or {}).get("application/json") or {}).get("schema") or {}


def wrap_like_spec(route, status, body, message=None, reference=None):
    """Stateful mode stores bare objects, but this API documents every success
    as {status_code, message, data} — and a Page as {items, total, page,
    page_size}. Returning the bare object violates the operation's own schema,
    which is exactly what verify.py flags. Wrap it to match what is declared."""
    if isinstance(body, dict) and "status_code" in body and "data" in body:
        return body          # StateStore.envelope already restored the shape

    schema = _success_schema(route)
    props = (schema or {}).get("properties") or {}
    if not {"status_code", "message", "data"} <= set(props):
        # The spec declares nothing for this operation (65 of 96 here). Match
        # what the same endpoint returns when served from the overlay, so a
        # stateful response is not shaped differently from a stateless one.
        if isinstance(reference, dict) and {"status_code", "data"} <= set(reference):
            inner = reference.get("data")
            data = body
            if isinstance(inner, dict) and "items" in inner and isinstance(body, list):
                data = {"items": body, "total": len(body), "page": 1, "page_size": 10}
            return {"status_code": int(status),
                    "message": message or reference.get("message") or "Successful Response",
                    "data": data}
        return body

    data_schema = props.get("data") or {}
    branches = data_schema.get("anyOf") or data_schema.get("oneOf") or [data_schema]
    real = next((b for b in branches
                 if isinstance(b, dict) and b.get("type") != "null"), {})
    inner = real.get("properties") or {}

    data = body
    if isinstance(body, list) and "items" in inner:
        data = {"items": body, "total": len(body), "page": 1, "page_size": 10}
    elif isinstance(body, list) and real.get("type") != "array" and len(body) == 1:
        data = body[0]

    return {"status_code": int(status), "message": message or "Successful Response",
            "data": data}
