#!/usr/bin/env python3
"""
build_overlay.py — generate / refresh the response-payload overlay for a spec.

The mock server does not need this file: it synthesises a shaped payload for any
operation the spec leaves undefined. The overlay is where a team turns those
guesses into *agreed* payloads — the ones UI devs build against and QA asserts
on — so they are reviewable, diffable, and stable across restarts.

Re-run it whenever the swagger doc changes. By default it MERGES:

  * operations already in the overlay keep their edited bodies
  * newly added operations get a fresh entry
  * operations that disappeared from the spec are reported, not deleted
  * --prune removes them, --force rebuilds everything from scratch

    python build_overlay.py --spec apis.json --out mock_overlay.json
    python build_overlay.py --spec https://api.dev.example.com/openapi.json --report
    python build_overlay.py --spec apis.json --check      # CI: fail if drifted
"""
import argparse
import copy
import json
import random
import sys

import project
from pathlib import Path

import synth
from generator import generate_from_schema
from mockd import Source, Spec, _success_status


def load_spec(location, headers=None):
    src = Source(location, headers=headers, poll=0, cache_dir="logs")
    text, _ = src.read(force=True)
    if text is None:
        raise SystemExit(f"could not read spec from {location}: {src.error}")
    return Spec(text=text, origin=location)


def response_body(route, status, resp, rng, spec):
    """Best available body for one documented status, plus where it came from."""
    content = (resp.get("content") or {}).get("application/json", {})

    if "example" in content:
        return copy.deepcopy(content["example"]), "spec:example"
    if content.get("examples"):
        first = next(iter(content["examples"].values()))
        return copy.deepcopy(first.get("value")), "spec:examples"
    if content.get("schema"):
        body = generate_from_schema(content["schema"], rng, array_items=2)
        if body is not None:
            code = 200 if status == "default" else int(status)
            return synth.normalise_envelope(body, code, resp.get("description")), "generated"

    if status == _success_status(route["responses"]):
        return synth.synthesise_success(route, spec, rng), "synthesized"
    if str(status).isdigit() and int(status) >= 400:
        return synth.synthesise_error(int(status), resp.get("description")), "synthesized"
    return None, None


def scenarios_for(route, success_body, rng):
    """The variants QA reaches for on every list screen, on top of the
    documented statuses."""
    out = {}
    if not (synth.looks_like_list(route) and isinstance(success_body, dict)):
        return out

    empty = copy.deepcopy(success_body)
    data = empty.get("data")
    if isinstance(data, dict) and "items" in data:
        data["items"], data["total"] = [], 0
    elif isinstance(data, list):
        empty["data"] = []
    else:
        return out
    empty["message"] = "No records found"
    out["empty"] = {"status": 200, "body": empty}

    large = copy.deepcopy(success_body)
    data = large.get("data")
    if isinstance(data, dict) and isinstance(data.get("items"), list) and data["items"]:
        proto = data["items"][0]
        items = []
        for _ in range(25):
            row = copy.deepcopy(proto)
            if isinstance(row, dict) and "id" in row:
                row["id"] = generate_from_schema(synth.UUID_SCHEMA, rng)
            items.append(row)
        data["items"], data["total"], data["page_size"] = items, 137, 25
        out["page_full"] = {"status": 200, "body": large}
    return out


def build(spec, previous=None, force=False):
    """Returns (operations, report). `previous` entries win unless --force."""
    previous = previous or {}
    operations, report = {}, []

    for route in sorted(spec.routes, key=lambda r: (r["path"], r["method"])):
        key = route["key"]
        rng = random.Random(key)                 # stable payloads across rebuilds
        old = {} if force else (previous.get(key) or {})
        old_responses = old.get("responses") or {}

        responses, synthesized_any = {}, False
        for status, resp in sorted(route["responses"].items()):
            status = str(status)
            if status in old_responses and not old_responses[status].get("_regenerate"):
                kept = old_responses[status]
                responses[status] = kept
                # a kept body is still a guess if that is how it was produced
                synthesized_any = synthesized_any or kept.get("_origin") == "synthesized"
                report.append((key, status, "kept"))
                continue
            body, origin = response_body(route, status, resp, rng, spec)
            if body is None:
                continue
            responses[status] = {"body": body, "_origin": origin}
            synthesized_any = synthesized_any or origin == "synthesized"
            report.append((key, status, origin))

        entry = {"summary": route["summary"], "responses": responses}
        # status is the one field a human owns: once someone marks an operation
        # "agreed", a rebuild must not quietly demote it back to a guess.
        entry["status"] = old.get("status") or ("guess" if synthesized_any else "spec")
        if old.get("status_set_at"):
            entry["status_set_at"] = old["status_set_at"]
        if synthesized_any or old.get("synthesized"):
            entry["synthesized"] = True

        success = str(_success_status(route["responses"]))
        success_body = (responses.get(success) or {}).get("body")
        scenarios = old.get("scenarios") or scenarios_for(route, success_body, rng)
        if scenarios:
            entry["scenarios"] = scenarios
        operations[key] = entry

    return operations, report


def main():
    ap = argparse.ArgumentParser(description="Generate or refresh a mockd response overlay")
    ap.add_argument("--spec", default=None,
                    help="Path or http(s) URL of the spec "
                         "(default: the project spec — see project.py)")
    ap.add_argument("--out", default="mock_overlay.json")
    ap.add_argument("--header", action="append", default=[], metavar="'K: V'",
                    help="Header used when fetching a spec URL, repeatable")
    ap.add_argument("--force", action="store_true",
                    help="Rebuild every payload, discarding hand-edited bodies")
    ap.add_argument("--prune", action="store_true",
                    help="Drop overlay entries for operations no longer in the spec")
    ap.add_argument("--check", action="store_true",
                    help="Write nothing; exit 1 if the overlay is out of date (for CI)")
    ap.add_argument("--report", action="store_true",
                    help="List the operations whose payloads had to be synthesised")
    args = ap.parse_args()

    headers = {}
    for raw in args.header:
        name, _, value = raw.partition(":")
        headers[name.strip()] = value.strip()

    # no --spec given: use the document this project is about
    args.spec, spec_from = project.resolve(args.spec, "overlay")
    spec = load_spec(args.spec, headers)
    out_path = Path(args.out)
    previous_doc = json.loads(out_path.read_text()) if out_path.exists() else {}
    previous = previous_doc.get("operations", {})

    operations, report = build(spec, previous, args.force)

    spec_keys = set(operations)
    added = sorted(spec_keys - set(previous))
    removed = sorted(set(previous) - spec_keys)
    kept_removed = []
    if removed and not args.prune:
        for key in removed:
            operations[key] = previous[key]
            operations[key]["_not_in_spec"] = True
        kept_removed = removed

    if args.check:
        drifted = bool(added) or bool(removed)
        print(f"{len(spec_keys)} operations in spec, {len(previous)} in {out_path}")
        if added:
            print(f"  {len(added)} NEW operation(s) missing from the overlay:")
            for k in added:
                print(f"    + {k}")
        if removed:
            print(f"  {len(removed)} overlay entr(ies) no longer in the spec:")
            for k in removed:
                print(f"    - {k}")
        if not drifted:
            print("  overlay is up to date")
        return sys.exit(1 if drifted else 0)

    doc = {
        "_meta": {
            "generated_from": str(args.spec),
            "openapi": spec.version,
            "title": spec.title,
            "operations": len(operations),
            "note": "Bodies with _origin=synthesized are INFERRED — the spec declares no "
                    "shape for them. Correct them and re-run this script; edits are kept "
                    "on merge. Set \"_regenerate\": true on a response to have it rebuilt. "
                    "mockd reads this file live: it always wins over generated data.",
        },
        "operations": operations,
    }
    out_path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")

    counts = {}
    for _, _, origin in report:
        counts[origin] = counts.get(origin, 0) + 1
    print(f"wrote {out_path}: {len(operations)} operations, {len(report)} response bodies")
    for k, v in sorted(counts.items()):
        print(f"  {k:16s} {v}")
    if added and previous:
        print(f"  {len(added)} operation(s) NEW since the last build:")
        for k in added:
            print(f"    + {k}")
    if kept_removed:
        print(f"  {len(kept_removed)} entr(ies) no longer in the spec, flagged "
              f"_not_in_spec (use --prune to delete):")
        for k in kept_removed:
            print(f"    - {k}")

    if args.report:
        synthesised = sorted({key for key, status, origin in report
                              if origin == "synthesized" and str(status).startswith("2")})
        print(f"\n{len(synthesised)} success payloads were SYNTHESISED — the spec declares "
              f"no response shape for these. Have the endpoint owners confirm them:")
        for key in synthesised:
            print(f"  {key}")


if __name__ == "__main__":
    main()
