#!/usr/bin/env python3
"""
specdiff.py — what actually changed between two OpenAPI documents.

speclock's diff answers "which operations appeared or disappeared", which is
the right question for provenance and the wrong one for impact. Almost nothing
breaks because an endpoint vanished; things break because a response stopped
carrying a field somebody read, or a request started demanding one nobody
sends. Those changes leave the operation list identical.

So this compares the resolved shapes, field by field, and sorts every
difference by who it hurts:

    BREAKING   existing callers or consumers stop working
    ADDITIVE   new surface; nothing that worked before stops
    NOTE       worth seeing, harmful only in particular readings

The asymmetry matters and is the whole reason this is not a text diff: a field
added to a RESPONSE is additive, the same field added to a REQUEST as required
is breaking. A field removed from a response is breaking; removed from a
request, it is merely ignored.

    python specdiff.py --from apis.json --to specs/fetched.json
    python specdiff.py --from A --to B --json
"""
import argparse
import json
import pathlib
import sys
import time

BREAKING, ADDITIVE, NOTE = "breaking", "additive", "note"
METHODS = ("get", "post", "put", "patch", "delete", "head", "options")
MAX_DEPTH = 6


def _read(source):
    from mockd import Source
    src = Source(source, poll=0, cache_dir="logs")
    text, _ = src.read(force=True)
    if text is None:
        raise SystemExit(f"could not read {source}: {src.error}")
    return text


def _type_of(schema):
    if not isinstance(schema, dict):
        return "?"
    kind = schema.get("type")
    if isinstance(kind, list):                       # 3.1 list form
        kind = "|".join(sorted(str(k) for k in kind))
    if not kind:
        for key in ("anyOf", "oneOf", "allOf"):
            if schema.get(key):
                parts = sorted({_type_of(b) for b in schema[key]})
                return "|".join(p for p in parts if p != "?") or "?"
        if "properties" in schema:
            return "object"
        if "enum" in schema:
            return "enum"
    return str(kind or "?")


def flatten(schema, spec=None, prefix="", depth=0, out=None, seen=None):
    """{field path: {type, required, enum}} for one schema, refs resolved."""
    out = {} if out is None else out
    seen = set() if seen is None else seen
    if not isinstance(schema, dict) or depth > MAX_DEPTH:
        return out

    if "$ref" in schema and spec is not None:
        ref = schema["$ref"]
        if ref in seen:                              # self-referential model
            return out
        seen = seen | {ref}
        schema = spec.resolve(schema)
        if not isinstance(schema, dict):
            return out

    for key in ("allOf", "anyOf", "oneOf"):
        for branch in (schema.get(key) or []):
            flatten(branch, spec, prefix, depth + 1, out, seen)

    required = set(schema.get("required") or [])
    for name, sub in (schema.get("properties") or {}).items():
        path = f"{prefix}.{name}" if prefix else name
        resolved = spec.resolve(sub) if (spec is not None and isinstance(sub, dict)
                                         and "$ref" in sub) else sub
        entry = out.setdefault(path, {})
        entry["type"] = _type_of(resolved)
        entry["required"] = entry.get("required", False) or (name in required)
        if isinstance(resolved, dict) and resolved.get("enum") is not None:
            entry["enum"] = sorted(str(v) for v in resolved["enum"])
        flatten(resolved, spec, path, depth + 1, out, seen)

    items = schema.get("items")
    if isinstance(items, dict):
        flatten(items, spec, f"{prefix}[]" if prefix else "[]", depth + 1, out, seen)
    return out


def _json_schema(container, spec):
    """The application/json schema out of a requestBody or a response."""
    content = (container or {}).get("content") or {}
    for media, body in content.items():
        if "json" in str(media).lower():
            return (body or {}).get("schema") or {}
    return {}


def _routes(spec):
    return {r["key"]: r for r in spec.routes}


def _enum_change(before, after):
    old, new = set(before.get("enum") or []), set(after.get("enum") or [])
    if not old and not new:
        return None
    gone, fresh = sorted(old - new), sorted(new - old)
    return (gone, fresh) if (gone or fresh) else None


def compare(from_text, to_text, from_name="from", to_name="to"):
    """Structured differences between two documents, worst first."""
    from mockd import Spec
    before, after = Spec(text=from_text, origin=from_name), Spec(text=to_text, origin=to_name)
    old, new = _routes(before), _routes(after)
    changes = []

    def note(kind, operation, what, detail, why):
        changes.append({"severity": kind, "operation": operation, "what": what,
                        "detail": detail, "why": why})

    for key in sorted(set(old) - set(new)):
        note(BREAKING, key, "operation removed", "",
             "anything calling this endpoint now gets a 404")
    for key in sorted(set(new) - set(old)):
        note(ADDITIVE, key, "operation added", "", "new surface; nothing else changes")

    for key in sorted(set(old) & set(new)):
        a, b = old[key], new[key]

        # --- parameters -----------------------------------------------------
        pa = {p.get("name"): p for p in a["parameters"] if p.get("name")}
        pb = {p.get("name"): p for p in b["parameters"] if p.get("name")}
        for name in sorted(set(pb) - set(pa)):
            if pb[name].get("required"):
                note(BREAKING, key, "required parameter added", name,
                     "existing callers do not send it")
            else:
                note(ADDITIVE, key, "optional parameter added", name, "safe to ignore")
        for name in sorted(set(pa) - set(pb)):
            note(NOTE, key, "parameter removed", name,
                 "callers still sending it will have it ignored")
        for name in sorted(set(pa) & set(pb)):
            was, now = bool(pa[name].get("required")), bool(pb[name].get("required"))
            if now and not was:
                note(BREAKING, key, "parameter became required", name,
                     "existing callers may omit it")
            elif was and not now:
                note(ADDITIVE, key, "parameter became optional", name, "a relaxation")

        # --- request body ---------------------------------------------------
        ra = flatten(_json_schema(a["request_body"], before), before)
        rb = flatten(_json_schema(b["request_body"], after), after)
        for field in sorted(set(rb) - set(ra)):
            if rb[field].get("required"):
                note(BREAKING, key, "required request field added", field,
                     "existing callers do not send it, so the call is rejected")
            else:
                note(ADDITIVE, key, "optional request field added", field,
                     "callers may ignore it")
        for field in sorted(set(ra) - set(rb)):
            note(NOTE, key, "request field removed", field,
                 "callers sending it will have it ignored")
        for field in sorted(set(ra) & set(rb)):
            if ra[field]["type"] != rb[field]["type"]:
                note(BREAKING, key, "request field type changed",
                     f"{field}: {ra[field]['type']} -> {rb[field]['type']}",
                     "what callers already send may no longer validate")
            if rb[field].get("required") and not ra[field].get("required"):
                note(BREAKING, key, "request field became required", field,
                     "existing callers may omit it")
            elif ra[field].get("required") and not rb[field].get("required"):
                note(ADDITIVE, key, "request field became optional", field, "a relaxation")
            enum = _enum_change(ra[field], rb[field])
            if enum and enum[0]:
                note(BREAKING, key, "request enum values removed",
                     f"{field}: {', '.join(enum[0])}",
                     "callers sending a removed value are now rejected")
            elif enum and enum[1]:
                note(ADDITIVE, key, "request enum values added",
                     f"{field}: {', '.join(enum[1])}", "a widening")

        # --- responses ------------------------------------------------------
        sa, sb = set(map(str, a["responses"])), set(map(str, b["responses"]))
        for status in sorted(sb - sa):
            note(ADDITIVE, key, "response status added", status, "newly documented")
        for status in sorted(sa - sb):
            note(NOTE, key, "response status removed", status,
                 "no longer documented; handlers for it may be dead code")
        for status in sorted(sa & sb):
            fa = flatten(_json_schema(a["responses"].get(status)
                                      or a["responses"].get(int(status), {}), before), before)
            fb = flatten(_json_schema(b["responses"].get(status)
                                      or b["responses"].get(int(status), {}), after), after)
            for field in sorted(fa.keys() - fb.keys()):
                note(BREAKING, key, f"response field removed ({status})", field,
                     "consumers reading it get nothing")
            for field in sorted(fb.keys() - fa.keys()):
                note(ADDITIVE, key, f"response field added ({status})", field,
                     "consumers can ignore it")
            for field in sorted(fa.keys() & fb.keys()):
                if fa[field]["type"] != fb[field]["type"]:
                    note(BREAKING, key, f"response field type changed ({status})",
                         f"{field}: {fa[field]['type']} -> {fb[field]['type']}",
                         "consumers parsing the old type break")
                enum = _enum_change(fa[field], fb[field])
                if enum and enum[1]:
                    note(NOTE, key, f"response enum values added ({status})",
                         f"{field}: {', '.join(enum[1])}",
                         "consumers switching exhaustively will not recognise them")
                elif enum and enum[0]:
                    note(ADDITIVE, key, f"response enum values removed ({status})",
                         f"{field}: {', '.join(enum[0])}", "a narrowing of what is sent")

    counts = {BREAKING: 0, ADDITIVE: 0, NOTE: 0}
    for change in changes:
        counts[change["severity"]] += 1
    order = {BREAKING: 0, NOTE: 1, ADDITIVE: 2}
    changes.sort(key=lambda c: (order[c["severity"]], c["operation"], c["what"]))
    return {
        "from": {"name": from_name, "title": before.title, "operations": len(old)},
        "to": {"name": to_name, "title": after.title, "operations": len(new)},
        "counts": counts,
        "identical": not changes,
        "changes": changes,
    }


# ---------------------------------------------------------------------------
# Shareable output
# ---------------------------------------------------------------------------
#
# A diff that only exists in someone's terminal cannot be argued about. These
# renderings are meant to be pasted into the place the decision actually gets
# made — a pull request, a ticket, a message to the team owning the API — so
# breaking changes lead, and every row says who it hurts rather than only what
# moved.

LABEL = {BREAKING: "Breaking", NOTE: "Worth seeing", ADDITIVE: "Additive"}


def _grouped(changes, severity):
    grouped = {}
    for change in changes:
        if change["severity"] == severity:
            grouped.setdefault(change["operation"], []).append(change)
    return grouped


def as_markdown(result, breaking_only=False):
    """For a pull request description, a ticket, or a chat message."""
    counts = result["counts"]
    out = [
        f"# API contract changes",
        "",
        f"`{result['from']['name']}` → `{result['to']['name']}`  ",
        f"{result['from']['operations']} operations → {result['to']['operations']}  ",
        f"_generated {time.strftime('%Y-%m-%d %H:%M')}_",
        "",
    ]
    if result["identical"]:
        out += ["**No differences in shape.** The two documents describe the same API.", ""]
        return "\n".join(out)

    out += [
        "| | Count | Meaning |",
        "|---|---:|---|",
        f"| **Breaking** | {counts[BREAKING]} | existing callers or consumers stop working |",
        f"| Worth seeing | {counts[NOTE]} | harmful only in particular readings |",
        f"| Additive | {counts[ADDITIVE]} | new surface; nothing that worked before stops |",
        "",
    ]

    sections = [(BREAKING, "## Breaking changes", True)]
    if not breaking_only:
        sections += [(NOTE, "## Worth seeing", False), (ADDITIVE, "## Additive", False)]

    for severity, heading, expanded in sections:
        grouped = _grouped(result["changes"], severity)
        if not grouped:
            continue
        out += [heading, ""]
        if not expanded:
            out += ["<details><summary>"
                    f"{counts[severity]} change(s) across {len(grouped)} operation(s)"
                    "</summary>", ""]
        for operation in sorted(grouped):
            out += [f"### `{operation}`", "", "| Change | Detail | Why it matters |",
                    "|---|---|---|"]
            for change in grouped[operation]:
                detail = f"`{change['detail']}`" if change["detail"] else ""
                out.append(f"| {change['what']} | {detail} | {change['why']} |")
            out.append("")
        if not expanded:
            out += ["</details>", ""]

    if counts[BREAKING]:
        out += ["---", "",
                f"**{counts[BREAKING]} breaking change(s).** Anything built against "
                f"`{result['from']['name']}` should be checked before this is adopted.", ""]
    return "\n".join(out)


def as_csv(result, breaking_only=False):
    """For a spreadsheet, when someone wants to assign the rows out."""
    import csv
    import io
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["severity", "operation", "change", "detail", "why_it_matters"])
    for change in result["changes"]:
        if breaking_only and change["severity"] != BREAKING:
            continue
        writer.writerow([LABEL[change["severity"]], change["operation"], change["what"],
                         change["detail"], change["why"]])
    return buffer.getvalue()


def as_html(result, breaking_only=False):
    """A single file that opens in a browser — no toolchain needed to read it."""
    from html import escape
    counts = result["counts"]
    colour = {BREAKING: "#b3261e", NOTE: "#8a6100", ADDITIVE: "#136c34"}
    rows = []
    for severity in (BREAKING, NOTE, ADDITIVE):
        if breaking_only and severity != BREAKING:
            continue
        grouped = _grouped(result["changes"], severity)
        for operation in sorted(grouped):
            for change in grouped[operation]:
                rows.append(
                    f"<tr><td style='color:{colour[severity]};white-space:nowrap'>"
                    f"{LABEL[severity]}</td>"
                    f"<td><code>{escape(operation)}</code></td>"
                    f"<td>{escape(change['what'])}</td>"
                    f"<td><code>{escape(change['detail'] or '')}</code></td>"
                    f"<td>{escape(change['why'])}</td></tr>")
    return f"""<!doctype html>
<meta charset="utf-8">
<title>API contract changes</title>
<style>
 body{{font:14px/1.5 system-ui,sans-serif;margin:32px auto;max-width:1100px;padding:0 16px;color:#1a1a1a}}
 table{{border-collapse:collapse;width:100%;margin-top:18px}}
 th,td{{border-bottom:1px solid #e3e3e3;padding:7px 9px;text-align:left;vertical-align:top;font-size:13px}}
 th{{background:#f6f6f6}} code{{font-family:ui-monospace,Menlo,monospace;font-size:12px}}
 .n{{font-size:26px;font-weight:700}} .tiles{{display:flex;gap:26px;margin:18px 0}}
 @media(prefers-color-scheme:dark){{
   body{{background:#141414;color:#e8e8e8}} th{{background:#1f1f1f}}
   th,td{{border-color:#333}}}}
</style>
<h1>API contract changes</h1>
<p><code>{escape(result['from']['name'])}</code> &rarr;
   <code>{escape(result['to']['name'])}</code><br>
   {result['from']['operations']} operations &rarr; {result['to']['operations']}<br>
   <small>generated {time.strftime('%Y-%m-%d %H:%M')}</small></p>
<div class="tiles">
  <div><div class="n" style="color:{colour[BREAKING]}">{counts[BREAKING]}</div>Breaking</div>
  <div><div class="n" style="color:{colour[NOTE]}">{counts[NOTE]}</div>Worth seeing</div>
  <div><div class="n" style="color:{colour[ADDITIVE]}">{counts[ADDITIVE]}</div>Additive</div>
</div>
<table><tr><th>Severity</th><th>Operation</th><th>Change</th><th>Detail</th>
<th>Why it matters</th></tr>
{"".join(rows) or "<tr><td colspan=5>No differences in shape.</td></tr>"}
</table>
"""


RENDERERS = {"markdown": as_markdown, "csv": as_csv, "html": as_html}


def main():
    ap = argparse.ArgumentParser(description="Compare two OpenAPI documents by shape")
    ap.add_argument("--from", dest="left", required=True, help="the older document")
    ap.add_argument("--to", dest="right", required=True, help="the newer document")
    ap.add_argument("--json", action="store_true", help="same as --format json")
    ap.add_argument("--format", choices=["text", "markdown", "html", "csv", "json"],
                    default="text",
                    help="markdown for a pull request or ticket, html to open in a "
                         "browser, csv for a spreadsheet")
    ap.add_argument("--out", help="write to this file instead of stdout")
    ap.add_argument("--breaking-only", action="store_true")
    ap.add_argument("--fail-on-breaking", action="store_true",
                    help="exit 1 if anything breaking is found — for CI")
    args = ap.parse_args()

    result = compare(_read(args.left), _read(args.right), args.left, args.right)
    fmt = "json" if args.json else args.format
    failed = 1 if (args.fail_on_breaking and result["counts"][BREAKING]) else 0

    if fmt != "text":
        rendered = (json.dumps(result, indent=2) if fmt == "json"
                    else RENDERERS[fmt](result, args.breaking_only))
        if args.out:
            pathlib.Path(args.out).write_text(rendered)
            print(f"{args.out}: {result['counts'][BREAKING]} breaking, "
                  f"{result['counts'][NOTE]} worth seeing, "
                  f"{result['counts'][ADDITIVE]} additive")
        else:
            print(rendered)
        return failed

    counts = result["counts"]
    print(f"{args.left}  ->  {args.right}")
    print(f"  {result['from']['operations']} operations -> {result['to']['operations']}")
    if result["identical"]:
        print("\nNo differences in shape.")
        return 0
    print(f"\n  {counts[BREAKING]} breaking   {counts[NOTE]} worth seeing   "
          f"{counts[ADDITIVE]} additive")

    shown = 0
    current = None
    for change in result["changes"]:
        if args.breaking_only and change["severity"] != BREAKING:
            continue
        if change["operation"] != current:
            current = change["operation"]
            print(f"\n  {current}")
        label = {BREAKING: "BREAKING", NOTE: "note    ", ADDITIVE: "additive"}[change["severity"]]
        detail = f"  {change['detail']}" if change["detail"] else ""
        print(f"    {label}  {change['what']}{detail}")
        print(f"              {change['why']}")
        shown += 1
    if not shown:
        print("\nNothing breaking.")
    return failed


if __name__ == "__main__":
    sys.exit(main())
