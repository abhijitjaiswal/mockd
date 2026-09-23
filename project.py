#!/usr/bin/env python3
"""
project.py — the one document this project is about, and who may disagree.

Every tool here used to take its own guess at which spec it meant: six CLIs
defaulted to a hardcoded "apis.json", and the console fell back to whatever the
mock happened to be running. So "which document am I being judged against?"
depended on which door you came through — the exact confusion this product
exists to remove.

One project-wide answer lives in clavis.json, and any single module may be
pinned to a different document when there is a reason:

    python project.py show
    python project.py use specs/fetched.json            # the whole system
    python project.py use apis.json --for tests         # just this module
    python project.py clear --for tests                 # back to the global one

clavis.json is committed on purpose. Which document the team builds against is
a shared decision, the same category as spec.lock.json, and it belongs in
review. A module override is committed too — a deliberate, visible exception,
not a surprise.

Resolution, narrowest wins:

    1. an explicit --spec on the command
    2. $CLAVIS_SPEC_<MODULE>      e.g. CLAVIS_SPEC_VERIFY=specs/old.json
    3. $CLAVIS_SPEC              one command, any module
    4. clavis.json -> modules.<module>
    5. clavis.json -> spec
    6. apis.json

Modules are named after the tool: mock, verify, tests, postman, overlay,
coverage, authoring.
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "clavis.json"
FALLBACK = "apis.json"
MODULES = ("mock", "verify", "tests", "postman", "overlay", "coverage", "authoring")


def _first_present():
    """Nothing chosen yet: name a document that is actually here, so a fresh
    clone with no clavis.json still points at something real."""
    for candidate in (FALLBACK, "sample_spec.yaml", "openapi.json", "swagger.json"):
        if (HERE / candidate).exists():
            return candidate
    for folder in (HERE, HERE / "specs"):
        if folder.is_dir():
            for item in sorted(folder.iterdir()):
                if item.suffix.lower() in (".json", ".yaml", ".yml") and \
                        "overlay" not in item.name and item.name != "environments.json":
                    return str(item.relative_to(HERE))
    return FALLBACK


def load():
    try:
        doc = json.loads(CONFIG.read_text())
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _module_env(module):
    return f"CLAVIS_SPEC_{module.upper()}" if module else None


def resolve(explicit=None, module=None):
    """Return (spec, where_it_came_from). The 'where' is why, in words."""
    if explicit:
        return explicit, "the --spec you passed"
    name = _module_env(module)
    if name and os.environ.get(name):
        return os.environ[name], f"${name}"
    if os.environ.get("CLAVIS_SPEC"):
        return os.environ["CLAVIS_SPEC"], "$CLAVIS_SPEC"
    doc = load()
    per_module = (doc.get("modules") or {}).get(module) if module else None
    if per_module:
        return per_module, f"{CONFIG.name} -> modules.{module}"
    if doc.get("spec"):
        return doc["spec"], f"{CONFIG.name}"
    return _first_present(), "the built-in default"


def active_spec(explicit=None, module=None):
    return resolve(explicit, module)[0]


def source(module=None):
    return resolve(None, module)[1]


def active_overlay(explicit=None, module=None):
    if explicit is not None:
        return explicit
    doc = load()
    per_module = (doc.get("overlays") or {}).get(module) if module else None
    return (os.environ.get("CLAVIS_OVERLAY") or per_module
            or doc.get("overlay") or "")


def set_active(spec=None, overlay=None, module=None):
    doc = load()
    if module:
        if spec is not None:
            doc.setdefault("modules", {})[module] = spec
        if overlay is not None:
            doc.setdefault("overlays", {})[module] = overlay
    else:
        if spec is not None:
            doc["spec"] = spec
        if overlay is not None:
            doc["overlay"] = overlay
    doc.setdefault("_why", "The document this project is about. Committed: which spec "
                           "the team builds against is a shared decision. Per-module "
                           "entries are deliberate exceptions. Override one command "
                           "with CLAVIS_SPEC=...")
    CONFIG.write_text(json.dumps(doc, indent=2) + "\n")
    return doc


def clear_module(module):
    doc = load()
    (doc.get("modules") or {}).pop(module, None)
    (doc.get("overlays") or {}).pop(module, None)
    CONFIG.write_text(json.dumps(doc, indent=2) + "\n")
    return doc


def report():
    """What every module would use right now, and why."""
    rows = []
    for module in MODULES:
        spec, why = resolve(None, module)
        exists = (HERE / spec).exists() or spec.lower().startswith(("http://", "https://"))
        rows.append({"module": module, "spec": spec, "from": why, "exists": exists,
                     "overridden": "modules." in why or why.startswith("$CLAVIS_SPEC_")})
    return rows


def main():
    args = sys.argv[1:]
    module = None
    if "--for" in args:
        i = args.index("--for")
        module = args[i + 1] if len(args) > i + 1 else None
        if module not in MODULES:
            print(f"unknown module {module!r}; expected one of {', '.join(MODULES)}")
            return 2
        args = args[:i] + args[i + 2:]

    if not args or args[0] == "show":
        spec, why = resolve(None, None)
        print(f"project spec: {spec}   ({why})")
        print()
        print(f"  {'module':10s} {'spec':38s} from")
        for row in report():
            mark = "*" if row["overridden"] else " "
            missing = "" if row["exists"] else "   << does not exist"
            print(f"  {mark}{row['module']:9s} {row['spec']:38s} {row['from']}{missing}")
        print("\n  * = pinned to something other than the project spec")
        return 0 if all(r["exists"] for r in report()) else 1

    if args[0] == "use" and len(args) >= 2:
        doc = set_active(spec=args[1], module=module)
        where = f"modules.{module}" if module else "the whole project"
        print(f"{where}: {args[1]}  (written to {CONFIG.name} — commit it)")
        return 0

    if args[0] == "clear" and module:
        clear_module(module)
        print(f"modules.{module} cleared — it follows the project spec again")
        return 0

    print(__doc__.strip())
    return 2


if __name__ == "__main__":
    sys.exit(main())
