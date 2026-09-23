#!/usr/bin/env python3
"""
from_postman.py — import a Postman collection as tests.

The other direction from postman.py, and the one that matters for a team that
already has a collection: their requests and their `pm.test(...)` scripts become
cases in `tests/drafts/`, keeping the assertions they already trust.

    python from_postman.py --file FastAPI.postman_collection.json --suite imported
    python from_postman.py --file team.json --split-by-folder     # one section per folder

What survives the trip:

  * the request — method, path, query, headers, JSON body
  * `{{var}}` — Postman variables ARE this tool's variables, so they pass through
    untouched; `{{baseUrl}}` is dropped because the runner supplies the host
  * the assertions a script states plainly: status codes, response time, header
    checks, `to.have.property`, `jsonSchema`, and `pm.expect(json.a.b)` forms
  * a folder becomes a section with --split-by-folder

What does not, and is reported rather than guessed at: loops, conditionals,
`pm.sendRequest`, variable mutation, and anything computed at run time. Those
lines are preserved verbatim as a `_postman_script` note on the case so nobody
has to go back to the original file to see what was lost.
"""
import argparse
import json
import re
import sys
from pathlib import Path

import tests as t

# ----------------------------------------------------------------------------
# Script translation
# ----------------------------------------------------------------------------

# Each pattern lifts one plainly-stated assertion out of a pm.test body.
PATTERNS = [
    # pm.response.to.have.status(200) / pm.response.code === 201
    (r"to\.have\.status\((\d{3})\)",
     lambda m: {"type": "status", "equals": int(m.group(1))}),
    (r"pm\.response\.code\s*(?:===?|to\.equal\(|\.to\.eql\()\s*(\d{3})",
     lambda m: {"type": "status", "equals": int(m.group(1))}),
    (r"expect\(pm\.response\.code\)\.to\.be\.oneOf\(\[([\d,\s]+)\]",
     lambda m: {"type": "status",
                "in": [int(x) for x in re.findall(r"\d+", m.group(1))]}),
    # pm.expect(pm.response.responseTime).to.be.below(500)
    (r"responseTime\)?\.to\.be\.below\((\d+)\)",
     lambda m: {"type": "responseTime", "op": "lt", "value": int(m.group(1))}),
    # pm.response.to.have.header("Content-Type")
    (r"to\.have\.header\(['\"]([^'\"]+)['\"]\)",
     lambda m: {"type": "header", "name": m.group(1), "op": "exists"}),
    # pm.expect(pm.response.text()).to.include("x")
    (r"response\.text\(\)\)\.to\.include\(['\"]([^'\"]+)['\"]\)",
     lambda m: {"type": "body_contains", "value": m.group(1)}),
    # pm.response.to.have.jsonBody("data.id") / to.have.jsonSchema(...)
    (r"to\.have\.jsonSchema\(", lambda m: {"type": "schema"}),
]

# pm.expect(x.a.b).to.<op>(value) — the common jsonpath forms
# `be.an` must precede `be.a`, or the alternation eats the shorter one and the
# argument is never read.
JSON_EXPECT = re.compile(
    r"expect\(\s*(\w+)((?:\.\w+|\[\d+\]|\[['\"][^'\"]+['\"]\])+)\s*\)"
    r"\s*\.to(?:\.not)?\.(be\.an|be\.a|eql|equal|be\.above|be\.below|be\.empty|exist|"
    r"include|have\.lengthOf)\s*(?:\(\s*([^)]*)\))?")

# pm.expect(json.data).to.have.property('total')  ->  data.total
PROPERTY = re.compile(
    r"expect\(\s*(\w+)((?:\.\w+|\[\d+\])*)\s*\)\s*\.to(?:\.not)?"
    r"\.have\.property\(\s*['\"]([^'\"]+)['\"]\s*(?:,\s*([^)]+))?\)")

# roots that are not the response body — asserting on them as a json path is
# nonsense, and pm.response.responseTime already has its own assertion type
NOT_BODY = {"pm", "response"}

OP_MAP = {"be.a": "type", "be.an": "type", "eql": "equals", "equal": "equals",
          "be.above": "gt", "be.below": "lt", "be.empty": "empty",
          "exist": "exists", "include": "contains", "have.lengthOf": "length"}


def _literal(text):
    if text is None:
        return None
    text = text.strip().rstrip(",").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text.strip("'\"") or None


BODY_ROOTS = {"json", "body", "jsonData", "responseJson", "jsonResponse", "res", "data_"}


def _clean_path(raw):
    """'.data.items[0].id' -> 'data.items[0].id'."""
    path = (raw or "").lstrip(".")
    return re.sub(r"\[['\"]([^'\"]+)['\"]\]", r".\1", path)


def translate(script_lines):
    """Turn a pm.test script into assertions, and report what was left behind."""
    text = "\n".join(script_lines or [])
    found, seen = [], set()

    def add(assertion):
        key = json.dumps(assertion, sort_keys=True)
        if key not in seen:
            seen.add(key)
            found.append(assertion)

    for pattern, build in PATTERNS:
        for m in re.finditer(pattern, text):
            add(build(m))

    for m in JSON_EXPECT.finditer(text):
        root, rest, op_raw, value_raw = m.groups()
        if root in NOT_BODY:
            continue
        path = _clean_path(root + rest if root not in BODY_ROOTS else rest)
        op = OP_MAP.get(op_raw)
        if not path or not op:
            continue
        value = _literal(value_raw)
        if op in ("exists", "empty"):
            add({"type": "jsonpath", "path": path, "op": op})
        elif value is not None:
            add({"type": "jsonpath", "path": path, "op": op, "value": value})

    for m in PROPERTY.finditer(text):
        root, rest, name, value_raw = m.groups()
        if root in NOT_BODY:
            continue
        prefix = _clean_path((root + rest) if root not in BODY_ROOTS else rest)
        path = f"{prefix}.{name}" if prefix else name
        value = _literal(value_raw)
        add({"type": "jsonpath", "path": path,
             **({"op": "equals", "value": value} if value is not None else {"op": "exists"})})

    # anything with control flow or side effects is not translated — say so
    untranslated = [ln.strip() for ln in (script_lines or [])
                    if re.search(r"pm\.sendRequest|pm\.environment\.set|pm\.collectionVariables"
                                 r"|for\s*\(|while\s*\(|if\s*\(|setTimeout", ln)]
    return found, untranslated


# ----------------------------------------------------------------------------
# Collection walking
# ----------------------------------------------------------------------------


def _url_parts(url, variables):
    """Postman URLs come as a string or an object; both reduce to path + query."""
    if isinstance(url, str):
        raw = url
        query = {}
    else:
        raw = url.get("raw") or ""
        query = {q["key"]: q.get("value", "") for q in (url.get("query") or [])
                 if not q.get("disabled") and q.get("key")}
        if url.get("path"):
            raw = "/" + "/".join(str(p) for p in url["path"])
    path = raw.split("?")[0]
    for name, value in (variables or {}).items():            # {{baseUrl}} etc.
        path = path.replace("{{%s}}" % name, value)
    path = re.sub(r"^\{\{[^}]+\}\}", "", path)               # any other host variable
    path = re.sub(r"^https?://[^/]+", "", path)
    # Postman path variables (:id) become this tool's ({{id}})
    path = re.sub(r"/:(\w+)", r"/{{\1}}", path)
    if not path.startswith("/"):
        path = "/" + path
    return path, query


def _slug(text, fallback="test"):
    out = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return out[:60] or fallback


def convert(collection, split_by_folder=False):
    """Returns {section: [cases]} plus a list of notes about what did not survive."""
    variables = {v["key"]: v.get("value", "")
                 for v in (collection.get("variable") or []) if v.get("key")}
    sections, notes = {}, []
    seen_ids = set()

    def walk(items, folder):
        for item in items or []:
            if "item" in item:
                walk(item["item"], item.get("name") or folder)
                continue
            request = item.get("request") or {}
            if isinstance(request, str):
                request = {"method": "GET", "url": request}
            method = (request.get("method") or "GET").upper()
            path, query = _url_parts(request.get("url"), variables)

            headers = {}
            for h in (request.get("header") or []):
                if not h.get("disabled") and h.get("key"):
                    if h["key"].lower() in ("content-length", "host"):
                        continue
                    headers[h["key"]] = h.get("value", "")

            body = None
            raw_body = ((request.get("body") or {}).get("raw") or "").strip()
            if raw_body:
                try:
                    body = json.loads(raw_body)
                except ValueError:
                    body = raw_body

            assertions, untranslated = [], []
            for event in (item.get("event") or []):
                if event.get("listen") != "test":
                    continue
                got, left = translate((event.get("script") or {}).get("exec") or [])
                assertions += got
                untranslated += left

            name = item.get("name") or f"{method} {path}"
            ident = _slug(name)
            n = 2
            while ident in seen_ids:
                ident, n = f"{_slug(name)}-{n}", n + 1
            seen_ids.add(ident)

            if not assertions:
                # a request with no script still deserves the one assertion
                # everybody would have written
                assertions = [{"type": "status", "equals": 200}]
                notes.append(f"{name}: no test script — added `status equals 200`, "
                             f"check it is right")
            if untranslated:
                notes.append(f"{name}: {len(untranslated)} line(s) not translated "
                             f"(kept on the case as _postman_script)")

            case = {
                "id": ident,
                "name": name,
                "tags": ["postman"],
                "request": {"method": method, "path": path,
                            **({"query": query} if query else {}),
                            **({"headers": headers} if headers else {}),
                            **({"body": body} if body is not None else {})},
                "assertions": assertions,
            }
            if untranslated:
                case["_postman_script"] = untranslated
            sections.setdefault(_slug(folder) if split_by_folder else "__all__",
                                []).append(case)

    walk(collection.get("item"), collection.get("info", {}).get("name") or "imported")
    return sections, notes


def main():
    ap = argparse.ArgumentParser(description="Import a Postman collection as tests")
    ap.add_argument("--file", required=True)
    ap.add_argument("--suite", default="imported",
                    help="section to write into (ignored with --split-by-folder)")
    ap.add_argument("--split-by-folder", action="store_true",
                    help="one section per Postman folder")
    ap.add_argument("--stage", choices=["draft", "shared"], default="draft")
    ap.add_argument("--dry-run", action="store_true", help="show what would be written")
    args = ap.parse_args()

    collection = json.loads(Path(args.file).read_text())
    name = (collection.get("info") or {}).get("name", args.file)
    sections, notes = convert(collection, args.split_by_folder)
    total = sum(len(v) for v in sections.values())
    print(f"{name}: {total} request(s) in {len(sections)} section(s)")

    for section, cases in sorted(sections.items()):
        target = args.suite if section == "__all__" else section
        if args.dry_run:
            print(f"  would write {len(cases)} case(s) to {args.stage}/{target}.json")
            for c in cases[:5]:
                print(f"    {c['request']['method']:6s} {c['request']['path']:40s} "
                      f"{len(c['assertions'])} assertion(s)")
            continue
        outcome = t.import_tests(cases, target, args.stage)
        if not outcome["ok"]:
            print(f"  {target}: refused — {len(outcome['errors'])} problem(s)")
            for e in outcome["errors"][:10]:
                print(f"    - {e}")
            return 1
        print(f"  {target}: {outcome['imported']} case(s) -> {outcome['file']}")

    if notes:
        print(f"\n{len(notes)} thing(s) to look at:")
        for note in notes[:25]:
            print(f"  - {note}")
        if len(notes) > 25:
            print(f"  ... and {len(notes) - 25} more")
    if not args.dry_run:
        print("\nThey land as drafts. Run them, then promote the ones that pass:")
        print(f"  python tests.py run --env mock --drafts --tag postman")
    return 0


if __name__ == "__main__":
    sys.exit(main())
