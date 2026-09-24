#!/usr/bin/env python3
"""
generator.py — turn a JSON Schema into a plausible example value.

Split out of mockd.py so the mock server, the overlay builder and the synthesis
layer all produce identical data for the same schema.

Written against the quirks of a FastAPI/pydantic OpenAPI 3.1 export:

  * `type: integer` with a FLOAT bound (`ge=0` serialises as `minimum: 0.0`) —
    random.randint rejects floats, which used to crash generation outright
  * `anyOf: [T, {type: null}]` for every optional field — serve T so a UI sees a
    populated field, and flip to null on demand for null-handling tests
  * `allOf: [{$ref: SomeEnum}]` wrapping a scalar — merging generated VALUES
    turns that into {}, so allOf merges SCHEMAS and then generates
  * 3.1 spellings: `type` as a list, `const`, schema-level `examples` arrays
"""
import copy
import math
import random
import re
import string
import uuid

_WORDS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel"]
_FIRST = ["Aarav", "Priya", "Rohan", "Meera", "Karan", "Anita", "Vikram", "Sneha"]
_LAST = ["Sharma", "Iyer", "Kapoor", "Nair", "Reddy", "Bose", "Khan", "Menon"]


def _det_uuid(rng: random.Random) -> str:
    return str(uuid.UUID(int=rng.getrandbits(128), version=4))


def _string_by_format(fmt: str, rng: random.Random):
    if fmt == "uuid":
        return _det_uuid(rng)
    if fmt == "date-time":
        return f"2026-0{rng.randint(1, 9)}-1{rng.randint(0, 9)}T10:30:00Z"
    if fmt == "date":
        return f"2026-0{rng.randint(1, 9)}-1{rng.randint(0, 9)}"
    if fmt == "time":
        return "10:30:00"
    if fmt == "email":
        return f"{rng.choice(_FIRST).lower()}.{rng.choice(_LAST).lower()}@example.com"
    if fmt in ("uri", "url", "uri-reference"):
        return "https://example.com/resource"
    if fmt == "hostname":
        return "api.example.com"
    if fmt == "ipv4":
        return "192.0.2.%d" % rng.randint(1, 254)
    if fmt == "ipv6":
        return "2001:db8::1"
    if fmt == "binary":
        return "<binary>"
    if fmt == "password":
        return "S3cret!pass"
    return None


def _string_by_name(field: str, rng: random.Random):
    """Field-name heuristics — a UI reviewing a mock needs plausible text, not
    'sample-string' in every column. Order matters: the name/title rules run
    before the date rules, or 'candidate_name' matches on the 'date' inside it."""
    f = (field or "").lower()
    words = set(re.split(r"[^a-z0-9]+", f))

    if f == "id" or f.endswith("_id") or f.endswith("_ids"):
        return _det_uuid(rng)
    if "email" in f:
        return f"{rng.choice(_FIRST).lower()}.{rng.choice(_LAST).lower()}@example.com"
    if "phone" in f or "mobile" in f:
        return "+91-98%08d" % rng.randrange(10 ** 8)
    if "first_name" in f:
        return rng.choice(_FIRST)
    if "last_name" in f or "surname" in f:
        return rng.choice(_LAST)
    if "middle_name" in f:
        return rng.choice(_FIRST)
    if "username" in f or f in ("user", "login"):
        return f"{rng.choice(_FIRST).lower()}{rng.randint(10, 99)}"
    if "password" in f or "secret" in f or "token" in f or f.endswith("_key"):
        return "".join(rng.choices(string.ascii_letters + string.digits, k=24))
    if "url" in words or "link" in words or f.endswith("_url") or f.endswith("_uri"):
        return "https://example.com/resource"
    if "path" in words or f.endswith("_path"):
        return "/uploads/mock/file.png"
    if "description" in f or "summary" in f or "notes" in f or "remark" in f:
        return "Auto-generated mock text for integration testing."
    # name/title/label BEFORE the date rules
    if "name" in words or f.endswith("_name") or "title" in words or "label" in words:
        if "city" in f:
            return rng.choice(["Bengaluru", "Pune", "Mumbai", "Hyderabad", "Chennai"])
        if "country" in f:
            return rng.choice(["India", "United States", "Germany", "Singapore"])
        if "state" in f:
            return rng.choice(["Karnataka", "Maharashtra", "Telangana", "Tamil Nadu"])
        # a field naming a PERSON wants a person's name; anything else wants words
        person = ("person", "user", "customer", "contact", "owner", "author",
                  "member", "employee", "first", "last", "full")
        return rng.choice(_FIRST) + " " + rng.choice(_LAST) \
            if any(w in f for w in person) \
            else rng.choice(_WORDS).capitalize() + " " + rng.choice(_WORDS).capitalize()
    if "city" in words:
        return rng.choice(["Bengaluru", "Pune", "Mumbai", "Hyderabad", "Chennai"])
    if "state" in words:
        return rng.choice(["Karnataka", "Maharashtra", "Telangana", "Tamil Nadu"])
    if "country" in words:
        return rng.choice(["India", "United States", "Germany", "Singapore"])
    if "currency" in words:
        return rng.choice(["INR", "USD", "EUR"])
    if "nationality" in words:
        return rng.choice(["Indian", "American", "German"])
    if "gender" in words:
        return rng.choice(["Male", "Female", "Other"])
    if f.endswith("_at") or f.endswith("_on") or "timestamp" in words:
        return f"2026-0{rng.randint(1, 9)}-1{rng.randint(0, 9)}T10:30:00Z"
    if "date" in words or f.endswith("_date") or f == "dob":
        return f"2026-0{rng.randint(1, 9)}-1{rng.randint(0, 9)}"
    if "time" in words:
        return "10:30:00"
    if "code" in words or f.endswith("_code"):
        return "".join(rng.choices(string.ascii_uppercase, k=3))
    return None


def _integer_by_name(field: str, lo, hi, rng: random.Random):
    """Envelope and ordering fields have obvious values; a random 81 in
    `status_code` just makes the mock look broken."""
    f = (field or "").lower()
    def clamp(v):
        return None if v < lo or v > hi else v
    if f in ("status_code", "statuscode", "code", "http_status"):
        return clamp(200)
    if f.endswith("_order") or f in ("order", "sequence", "rank", "position"):
        return clamp(1)
    if f in ("page", "page_number", "current_page"):
        return clamp(1)
    if f in ("page_size", "per_page", "limit"):
        return clamp(10)
    if f.endswith("_count") or f in ("count", "total", "total_count"):
        return clamp(rng.randint(1, 5))
    return None


def _num_bounds(schema: dict, integral: bool):
    lo, hi = schema.get("minimum"), schema.get("maximum")
    ex_lo, ex_hi = schema.get("exclusiveMinimum"), schema.get("exclusiveMaximum")
    # draft-4 style booleans
    if ex_lo is True:
        ex_lo = lo
    if ex_hi is True:
        ex_hi = hi
    step = 1 if integral else 0.01
    if isinstance(ex_lo, (int, float)):
        lo = ex_lo + step if lo is None else max(lo, ex_lo + step)
    if isinstance(ex_hi, (int, float)):
        hi = ex_hi - step if hi is None else min(hi, ex_hi - step)
    if lo is None:
        lo = 1 if integral else 0.0
    if hi is None:
        hi = max(lo + (99 if integral else 99.0), lo)
    if lo > hi:                      # contradictory bounds in the spec
        hi = lo
    if integral:
        # FastAPI/pydantic emit float bounds for int fields (ge=0 -> 0.0);
        # random.randint rejects floats, so coerce and round inwards.
        lo, hi = int(math.ceil(lo)), int(math.floor(hi))
        if lo > hi:
            hi = lo
    return lo, hi


def _merge_all_of(subs):
    """Merge allOf members into one schema. Must merge SCHEMAS, not generated
    values — `allOf: [{$ref: SomeEnum}]` is a scalar, and merging values turns
    it into {}."""
    merged = {}
    for sub in subs:
        if not isinstance(sub, dict):
            continue
        for k, v in sub.items():
            if k == "properties":
                merged.setdefault("properties", {}).update(v or {})
            elif k == "required":
                merged["required"] = sorted(set(merged.get("required", [])) | set(v or []))
            elif k not in merged:
                merged[k] = v
    return merged


def _is_object_schema(schema):
    return isinstance(schema, dict) and (
        schema.get("type") == "object" or "properties" in schema
        or (isinstance(schema.get("type"), list) and "object" in schema["type"]))


def _pick_branch(branches, want_null):
    """anyOf/oneOf: FastAPI models optional fields as [T, null]. Serve T so the
    UI sees a populated field; X-Mock-Nulls flips that for null-handling tests.

    Under X-Mock-Nulls an OBJECT branch is still populated: nulling a whole
    nested object collapses the payload to `{"data": null}`, which tells a UI
    dev nothing. Nulling the scalar and array leaves inside it is the test they
    actually want."""
    nulls = [b for b in branches if isinstance(b, dict) and b.get("type") == "null"]
    real = [b for b in branches if not (isinstance(b, dict) and b.get("type") == "null")]
    if want_null and nulls and not any(_is_object_schema(b) for b in real):
        return nulls[0]
    return (real or nulls or [{}])[0]


def generate_from_schema(schema, rng: random.Random, field="", depth=0,
                         nulls=False, array_items=2):
    if not isinstance(schema, dict) or depth > 30:
        return None

    if "example" in schema:
        return copy.deepcopy(schema["example"])
    examples = schema.get("examples")          # OpenAPI 3.1 / JSON Schema form
    if isinstance(examples, list) and examples:
        return copy.deepcopy(examples[0])
    if "const" in schema:
        return copy.deepcopy(schema["const"])
    if schema.get("default") is not None:
        return copy.deepcopy(schema["default"])
    if schema.get("enum"):
        non_null = [e for e in schema["enum"] if e is not None]
        return copy.deepcopy((non_null or schema["enum"])[0])

    if schema.get("allOf"):
        merged = _merge_all_of(schema["allOf"])
        merged = {**{k: v for k, v in schema.items() if k != "allOf"}, **merged}
        return generate_from_schema(merged, rng, field, depth + 1, nulls, array_items)
    for comb in ("oneOf", "anyOf"):
        if schema.get(comb):
            branch = _pick_branch(schema[comb], nulls)
            if isinstance(branch, dict) and branch.get("type") == "null":
                return None
            branch = {**{k: v for k, v in schema.items() if k not in ("oneOf", "anyOf")},
                      **branch}
            return generate_from_schema(branch, rng, field, depth + 1, nulls, array_items)

    t = schema.get("type")
    if isinstance(t, list):                    # 3.1: type: ["string", "null"]
        non_null = [x for x in t if x != "null"]
        if nulls and "null" in t and "object" not in non_null:
            return None
        t = non_null[0] if non_null else "null"
    if t is None:
        if "properties" in schema:
            t = "object"
        elif "items" in schema or "prefixItems" in schema:
            t = "array"
        else:
            return None                        # schema: {} — nothing declared

    if t == "null":
        return None

    if t == "object":
        out = {}
        props = schema.get("properties") or {}
        for name, sub in props.items():
            out[name] = generate_from_schema(sub, rng, name, depth + 1, nulls, array_items)
        if not props:
            ap = schema.get("additionalProperties")
            if isinstance(ap, dict):
                out["key"] = generate_from_schema(ap, rng, "key", depth + 1, nulls, array_items)
        return out

    if t == "array":
        prefix = schema.get("prefixItems") or []
        out = [generate_from_schema(s, rng, field, depth + 1, nulls, array_items) for s in prefix]
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            n = max(int(schema.get("minItems", 0)), array_items) - len(out)
            n = min(n, int(schema.get("maxItems", 10 ** 6)) - len(out))
            for _ in range(max(n, 0)):
                out.append(generate_from_schema(item_schema, rng, field, depth + 1,
                                                nulls, array_items))
        return out

    if t == "string":
        val = _string_by_format(schema.get("format", ""), rng) or _string_by_name(field, rng)
        if val is None:
            base = schema.get("title") or field or "string"
            val = f"sample-{str(base).lower().replace(' ', '-').replace('_', '-')}"
        min_len, max_len = schema.get("minLength", 0), schema.get("maxLength")
        if len(val) < min_len:
            val += "".join(rng.choices(string.ascii_lowercase, k=min_len - len(val)))
        if max_len is not None and len(val) > max_len:
            val = val[:max_len]
        return val

    if t == "integer":
        lo, hi = _num_bounds(schema, integral=True)
        named = _integer_by_name(field, lo, hi, rng)
        if named is not None:
            return named
        val = rng.randint(lo, hi)
        mult = schema.get("multipleOf")
        if isinstance(mult, int) and mult > 0:
            val = max(lo, (val // mult) * mult)
        return val

    if t == "number":
        lo, hi = _num_bounds(schema, integral=False)
        return round(rng.uniform(lo, hi), 2)

    if t == "boolean":
        return True
    return None


