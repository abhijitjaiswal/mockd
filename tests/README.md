# tests/

## Structure

**A section is a file, and a file is a module.** The sections come from the
spec's own `tags`, so `tests/user.json` holds the tests for the operations
tagged `user`. Nobody has to invent a taxonomy, and a test lands next to the
endpoints it exercises.

```
tests/
  user.json            shared — committed, run by CI
  departments.json
  drafts/              gitignored — yours until proven
    user.json
```

The console's **Save as test…** dialog defaults the section to the tag of the
operation you just called, and shows the exact file it will write to.

## Sections vs labels

They answer different questions, so both exist.

| | what it is | question it answers |
|---|---|---|
| **section** (file) | the module — from the spec's tags | *where does this test live?* |
| **label** (`tags`) | `smoke`, `regression`, `negative`, `edge`, `sanity` | *do I want to run it now?* |

A label cuts across sections, which is the point: `--tag smoke` is the quick run
before a merge, `--tag negative` is the failure paths, and neither cares which
module a test is filed under.

```bash
python tests.py run --env dev --suite user        # one section
python tests.py run --env dev --tag smoke         # one label, every section
python tests.py run --env dev --kind e2e          # the sanity flows
```

## Inside a file

```json
{
  "name": "user",
  "description": "optional",
  "data":      { "pageSize": 10 },
  "cases":     [ { "id": "...", "tags": ["smoke"], "request": {...}, "assertions": [...] } ],
  "scenarios": [ { "id": "...", "kind": "api" | "e2e", "steps": [...] } ]
}
```

`data` is the section's own variables. An environment's `data` block overrides
them per server, and `--var` overrides both.

## Draft, then shared

New tests are written to `drafts/`. They run locally, CI ignores them, and they
can only be promoted into the committed section once they have actually passed:

```bash
python tests.py run     --env mock --drafts
python tests.py promote --suite user --id user-list-200
```

That ordering matters most for tests nobody hand-wrote — imported or generated
ones land in the same quarantine.
