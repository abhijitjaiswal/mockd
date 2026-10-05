#!/usr/bin/env python3
"""
impact.py — when the API document changes, what does that do to us?

specdiff.py says what changed between two documents and who it hurts in
principle. That still leaves a person to work out the part they care about:
which of OUR tests touch what changed, and so which need running, rereading or
rewriting. Nobody does that by hand for a forty-line diff, which is how an
unannounced contract change reaches staging.

So this joins the two: the differences, and the tests that call the operations
they are in. It runs by itself wherever a new document appears — when one is
loaded in the console, before it is put to use, and in CI on a pull request
that touches the document.

    python impact.py --from old.json --to new.json
    python impact.py --from old.json --to new.json --format markdown   # for a PR
    python impact.py --from old.json --to new.json --fail-on-breaking  # the gate
    python impact.py --against origin/main --fail-on-breaking          # in a pull request
"""
import argparse
import json
import pathlib
import re
import sys

import specdiff

BREAKING, ADDITIVE, NOTE = specdiff.BREAKING, specdiff.ADDITIVE, specdiff.NOTE
ORDER = {BREAKING: 0, NOTE: 1, ADDITIVE: 2}


def _spec(text, name):
    from mockd import Spec
    return Spec(text=text, origin=name)


def operations_of(test, *specs):
    """The documented operations a test calls, as "METHOD /path/{param}".

    A test writes /books/{{bookId}}; the document says /books/{book_id}. They
    are matched through the documents themselves — the old one and the new —
    so a test that calls an operation the new document removed is still found."""
    found = set()
    steps = (test.get("steps") or [test]) + (test.get("cleanup") or [])
    for step in steps:
        request = step.get("request") or {}
        path = re.sub(r"\{\{[^}]+\}\}", "x", str(request.get("path") or "")).split("?")[0]
        method = str(request.get("method") or "GET").upper()
        if not path.startswith("/"):
            continue
        for spec in specs:
            route, _ = spec.match(method, path)
            if route is not None:
                found.add(route["key"])
    return found


def assess(from_text, to_text, suites=None, from_name="before", to_name="after"):
    """The differences between two documents, and the tests they reach."""
    result = specdiff.compare(from_text, to_text, from_name, to_name)
    before, after = _spec(from_text, from_name), _spec(to_text, to_name)

    by_operation = {}
    for change in result["changes"]:
        by_operation.setdefault(change["operation"], []).append(change)

    affected, total = [], 0
    for suite in suites or []:
        for test in (suite.get("scenarios") or []) + (suite.get("cases") or []):
            total += 1
            touched = operations_of(test, before, after) & set(by_operation)
            if not touched:
                continue
            reasons = [c for op in sorted(touched) for c in by_operation[op]]
            worst = min(reasons, key=lambda c: ORDER[c["severity"]])["severity"]
            affected.append({
                "suite": suite.get("name"), "stage": suite.get("_stage", "shared"),
                "id": test.get("id"), "name": test.get("name") or test.get("id"),
                "severity": worst, "operations": sorted(touched),
                "because": [f"{c['operation']}: {c['what']}"
                            + (f" ({c['detail']})" if c.get("detail") else "")
                            for c in sorted(reasons, key=lambda c: ORDER[c["severity"]])][:4]})
    # tests somebody wrote before the generated ones: they are what a person
    # will recognise, and the generated baseline is remade for the new document
    affected.sort(key=lambda t: (ORDER[t["severity"]], t["suite"] == "baseline",
                                 t["suite"] or "", t["id"] or ""))

    counts = result["counts"]
    return {**result,
            "tests": {"total": total, "affected": affected,
                      "breaking": sum(1 for t in affected if t["severity"] == BREAKING)},
            "sentence": sentence(counts, affected, total, result["identical"])}


def sentence(counts, affected, total, identical=False):
    """The whole finding in one line a person can act on."""
    if identical:
        return "Nothing in it changes how the API behaves for a caller."
    parts = []
    if counts[BREAKING]:
        n = counts[BREAKING]
        parts.append(f"{n} change{'s' if n != 1 else ''} would break something that works today")
    if counts[NOTE]:
        parts.append(f"{counts[NOTE]} worth a look")
    if counts[ADDITIVE]:
        n = counts[ADDITIVE]
        parts.append(f"{n} addition{'s that break' if n != 1 else ' that breaks'} nothing")
    head = ", ".join(parts) + "."
    if not total:
        return head
    if not affected:
        return head + " None of your tests use what changed."
    return (head + f" {len(affected)} of your {total} tests use what changed"
            + (f" — {sum(1 for t in affected if t['severity'] == BREAKING)} of them "
               f"a breaking change" if counts[BREAKING] else "") + ".")


def as_markdown(found):
    out = [f"## API document: {found['from']['name']} → {found['to']['name']}", "",
           found["sentence"], ""]
    out.append(specdiff.as_markdown(found, breaking_only=False))
    tests = found["tests"]["affected"]
    if tests:
        out += ["", f"### Tests that use what changed ({len(tests)} of {found['tests']['total']})", ""]
        for t in tests[:40]:
            out.append(f"- **{specdiff.LABEL[t['severity']]}** — `{t['suite']}/{t['id']}` "
                       f"{t['name']}: {'; '.join(t['because'][:2])}")
        if len(tests) > 40:
            out.append(f"- …and {len(tests) - 40} more")
    return "\n".join(out) + "\n"


def document_at(ref):
    """The project's API document as it is on another git ref, written to a
    temporary file. Returns its path, or None when that ref has no document —
    a first commit of one is not a change to anything."""
    import subprocess
    import tempfile
    import project

    def show(path):
        done = subprocess.run(["git", "show", f"{ref}:{path}"], capture_output=True)
        return done.stdout if done.returncode == 0 else None

    spec = project.active_spec()
    settings = show("mockd.json")
    if settings:
        try:
            spec = json.loads(settings).get("spec") or spec
        except ValueError:
            pass
    if spec.lower().startswith(("http://", "https://")):
        return None
    content = show(spec)
    if content is None:
        return None
    folder = pathlib.Path(tempfile.mkdtemp(prefix="mockd-base-"))
    target = folder / pathlib.Path(spec).name          # same suffix, so it is read the same way
    target.write_bytes(content)
    return str(target)


def main():
    ap = argparse.ArgumentParser(description="What a change to the API document does to the tests")
    ap.add_argument("--from", dest="left", help="the document in use")
    ap.add_argument("--to", dest="right", help="the document proposed (default: the project's)")
    ap.add_argument("--against", metavar="REF",
                    help="compare the project's document with the one on this git ref, "
                         "e.g. origin/main — what a pull request changes")
    ap.add_argument("--format", choices=["text", "markdown", "json"], default="text")
    ap.add_argument("--out", help="write to this file instead of stdout")
    ap.add_argument("--no-tests", action="store_true", help="compare the documents only")
    ap.add_argument("--fail-on-breaking", action="store_true",
                    help="exit 1 if anything breaking is found — for CI")
    args = ap.parse_args()

    if args.against:
        import project
        args.right = args.right or project.active_spec()
        args.left = document_at(args.against)
        args.left_name = f"{args.against}:{pathlib.Path(args.left).name}" if args.left else None
        if args.left is None:
            message = f"There is no API document on {args.against} to compare with.\n"
            if args.out:
                pathlib.Path(args.out).write_text(message)
            print(message, end="")
            return 0
    if not args.left or not args.right:
        ap.error("give --from and --to, or --against REF")

    suites = []
    if not args.no_tests:
        import tests as t
        suites = t.load_suites(include_drafts=True)
    left_name = getattr(args, "left_name", None) or args.left
    found = assess(specdiff._read(args.left), specdiff._read(args.right), suites,
                   left_name, args.right)

    if args.format == "json":
        rendered = json.dumps(found, indent=2)
    elif args.format == "markdown":
        rendered = as_markdown(found)
    else:
        lines = [f"{left_name}  ->  {args.right}", "", found["sentence"], ""]
        for change in found["changes"]:
            lines.append(f"  {specdiff.LABEL[change['severity']]:<13} {change['operation']}: "
                         f"{change['what']}" + (f" ({change['detail']})" if change["detail"] else ""))
        if found["tests"]["affected"]:
            lines += ["", "Tests that use what changed:"]
            lines += [f"  {specdiff.LABEL[t['severity']]:<13} {t['suite']}/{t['id']}"
                      for t in found["tests"]["affected"]]
        rendered = "\n".join(lines) + "\n"

    if args.out:
        pathlib.Path(args.out).write_text(rendered)
        print(f"{args.out}: {found['sentence']}")
    else:
        print(rendered, end="")
    return 1 if (args.fail_on_breaking and found["counts"][BREAKING]) else 0


if __name__ == "__main__":
    sys.exit(main())
