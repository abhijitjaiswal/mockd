# mockd — reference

Everything in detail. For the short version, see the [README](../README.md).


Point it at an OpenAPI 3.0/3.1 document — a file or a live `/openapi.json` URL —
and every operation becomes a working endpoint with a realistic payload. It
validates incoming requests against the spec, so a UI sending the wrong shape
gets the same rejection the real backend would send, instead of a silent 200
that hides the integration bug.

The problem it exists to solve is not "we need fake data". It is that three
teams hold three different ideas of what the API is — the backend has the code,
the UI has whatever it saw when it integrated, QA has its fixtures — and the
document meant to reconcile them describes only a third of the surface. So the
tools here track, per operation, **how much the spec really says** and **who has
confirmed what it does not**.

Three programs, one spec:

| | |
|---|---|
| `mockd.py` | the server UI devs point at |
| `build_overlay.py` | turns generated payloads into agreed, editable, version-controlled ones |
| `verify.py` | drives the **real** API and reports where it disagrees with the doc |
| `console.py` | a local control panel: start/stop the mock and fire requests at it from the browser |
| `postman.py` | exports the spec as a Postman collection — hand testing today, `newman` in CI tomorrow |
| `environments.py` | named targets (base URL + how to log in) that every tool takes as `--env NAME` |
| `SPEC_GUIDE.md` | the authoring standard for whoever owns the API — plain OpenAPI, graded live |
| `speclock.py` | pins which document the results are about, so a spec swap can't be silent |
| `tests.py` | saved, replayable assertions — cases, scenarios, and the runner CI calls |
| `blueprint.py` | the baseline: tests derived from the document, so every endpoint is checked before anyone writes one |
| `mcp_server.py` | mockd as tools an AI assistant can call, so it can write and run tests itself |

## Setup

The only prerequisite is **Python 3.9 or newer**. One script does the rest on
every platform: it builds a private virtual environment beside the code,
installs the four dependencies into it, and opens the console. It changes
nothing outside this folder and installs nothing system-wide.

### macOS

```bash
python3 --version                  # 3.9+? if not: brew install python@3.12
git clone git@github.com:abhijitjaiswal/mockd.git && cd mockd
python3 bootstrap.py
```

### Linux

```bash
python3 --version
# Debian/Ubuntu also need the venv module:
sudo apt install python3-venv          # Fedora: sudo dnf install python3-virtualenv

git clone git@github.com:abhijitjaiswal/mockd.git && cd mockd
python3 bootstrap.py
```

### Windows

Install Python from [python.org](https://www.python.org/downloads/) and **tick
"Add python.exe to PATH"** in the installer — without it the `python` command
will not be found.

```powershell
python --version
git clone git@github.com:abhijitjaiswal/mockd.git
cd mockd
python bootstrap.py
```

Then open **http://127.0.0.1:4100**. That is the whole setup.

| Command | What it does |
|---|---|
| `python bootstrap.py` | set up, then run the console |
| `python bootstrap.py --no-start` | set up only |
| `python bootstrap.py --with-demo` | also install Playwright for the browser suites |
| `python bootstrap.py --port 8080` | serve the console elsewhere |

Re-running it is safe: it reuses the environment and installs only what is
missing.

<details><summary>Doing it by hand, or in CI</summary>

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python console.py                  # then open http://localhost:4100
```
</details>

### First run

A fresh clone ships `sample_spec.yaml`, a four-operation example, so there is
something to look at before you supply anything. The console opens on **Home**,
which always says the one thing to do next. To point it at your own API:

1. **Add your API document.** Paste a link — your `/openapi.json`, or the
   address of the Swagger page; it follows the page to the document behind it —
   or choose a file. **Load** shows what was found; nothing is replaced until
   you press **Use this document**.
2. **Start the mock.** It comes up on port 4010 on that document, checks itself
   against it, and a baseline of tests is made for you.
3. **Run the baseline.** Every endpoint, checked against what the document says.

Then add your dev or staging server under **Servers** and run the same tests
there, and describe what else you want proven under **Create tests**.

From the command line instead:

```bash
python project.py use specs/your-api.json          # the whole system
python build_overlay.py --out mock_overlay.json    # optional: editable payloads
python mockd.py --spec specs/your-api.json --port 4010
```

With a URL rather than a file, the server fetches the document itself,
re-checks it every `--poll` seconds (ETag, so an unchanged spec costs a 304),
and caches the last good copy under `logs/` so it still boots when the upstream
is down:

```bash
python mockd.py --spec https://api.dev.example.com/openapi.json \
                --header 'Authorization: Bearer ...' --poll 30
```

CORS is open, including the permission request a browser sends before a
cross-origin write and requests made with credentials, so an app on another
port can call the mock as it would the real API. `mock_overlay.json` is picked
up automatically if it sits next to the spec.

### What is not in this repository

No API document, overlay or environment secret is committed — those belong to
whoever runs it. `specs/`, `.env`, `environments.local.json` and
`tests/drafts/` are all ignored by git, so your API's shape never leaves your
machine by accident. `environments.json` **is** committed, and holds only
`${VAR}` placeholders resolved from your shell, `.env`, or
`environments.local.json`.

CORS is open. `mock_overlay.json` is picked up automatically if it sits next to
the spec.

```bash
python mockd.py --spec https://api.dev.example.com/openapi.json \
                --header 'Authorization: Bearer ...' --poll 30
```

With a URL the server fetches the document itself, re-checks it every `--poll`
seconds (ETag, so an unchanged spec costs a 304), and caches the last good copy
under `logs/` so it still boots when the upstream is down.

## What the spec gives you, and what it doesn't

The engine works this out itself, on whatever spec you point it at. It grades
every operation on three things a document can omit — what comes back on
success, what the request body must contain, and what the failure paths are —
and reports the split at startup, at `GET /_mock/coverage`, and in the band at
the top of the console:

```
mockd: spec coverage — 30 fully documented, 1 partial, 65 with no response shape
```

For `apis.json` (a FastAPI export: OpenAPI 3.1, 76 paths, 96 operations, 92
component schemas) that split is clean:

| | operations | success response | request body | failure responses |
|---|---|---|---|---|
| **fully documented** — the tags whose routes declare a response model | **30** | `$ref` to a `StandardResponseModel_*` + a hand-written `example` | schema | 401/403/404/409/500, each with an example |
| **partial** | **1** | an `example` but no schema — a person can read it, a test cannot check against it | schema | documented |
| **no response shape** — every other tag | **65** | `"schema": {}` — the route has no `response_model` | schema (where a body is taken) | only 422 |

Per dimension, across all 96: success response declared for 31 — but only **30
by schema**; the one that carries an `example` and no schema is readable by a
person and usable by the mock, yet nothing automated can check a response
against it, so it grades as partial. Every one of the 28 operations that takes a
body declares a schema for it; 31 document real failure paths, 54 document only
422, and 11 document none at all.

Those 65 operations declare nothing about what they return. No mock can read a
shape out of `{}`, which is why a naive spec-driven mock answers them with
`{"message": "Successful Response"}`.

The grading is derived, never hardcoded — add a fully documented endpoint to the
spec and the complete count goes to 32 on the next reload; add a bare one and
the undocumented count goes to 66. `GET /_mock/coverage` names each operation
and exactly what it is missing, so it reads as a worklist for the API team.

mockd closes that gap in three layers, most-specific first:

1. **overlay** — a payload someone wrote down and reviewed (`mock_overlay.json`)
2. **spec** — the `example` or `schema` in the document
3. **synthesis** — inferred from the operation's request schema, a component
   schema matching the resource name, the path verb and the spec's enums,
   wrapped in this API's `{status_code, message, data}` envelope

Layer 3 is what makes the mock survive a swagger update: an endpoint added this
morning answers with a shaped payload this afternoon, before anyone curates
anything. `GET /_mock/routes` labels every operation with the layer it is being
served from, and `GET /_mock/drift` lists the ones still uncurated.

## When a new API shows up

Nothing to restart, nothing to regenerate:

```
GET /api/v1/quote/list  ->  404          # not in the spec yet
                                          # backend adds it to apis.json / redeploys
GET /api/v1/quote/list  ->  200  X-Mock-Source: synthesized
{"status_code":200,"message":"List Offers successful",
 "data":{"items":[{"id":"...","customer_name":"Aarav Sharma","amount":2400000,
                   "status":"Draft","valid_until":"2026-03-13", ...}],
         "total":2,"page":1,"page_size":10}}
```

The new operation is validated from the moment it appears — a POST with a bad
enum or a missing required field is rejected against the newly added schema.

Then, when the payload is worth pinning down:

```bash
python build_overlay.py --spec apis.json --out mock_overlay.json
#   + GET /api/v1/quote/list          <- new operations get an entry
#   hand-edited bodies are preserved; removed operations are flagged, not deleted
```

`--check` exits non-zero when the overlay has drifted from the spec, which is
the CI gate that stops the mock quietly falling behind.

## Verifying the real API against the doc

```bash
python verify.py --spec apis.json \
    --base-url https://api.dev.example.com \
    --header 'Authorization: Bearer eyJ...'
```

Read-only by default. For each operation it checks that the status came back
**documented**, that the body **validates against the schema declared for that
status**, that the content type matches, and how long it took.

It fills path params intelligently: collection endpoints run first and real ids
are harvested from their responses, so `/account/read/{account_id}` is exercised with
an id that exists rather than a random uuid that 404s.

```
96 operations: 12 ok  37 warning  0 error  47 skipped

UNDOCUMENTED STATUS (1) — the API returned something the swagger doc does not mention:
  500  GET /api/v1/report/dashboard  (documented: 200, 422)

CONTRACT VIOLATIONS (2) — the body does not match its own schema:
  GET /api/v1/widget-links
      data/items/0/level_title: None is not of type 'string'

UNVERIFIABLE (37) — the spec declares no response shape, so nothing could be checked.
```

| flag | |
|---|---|
| `--target mock\|live` | override the auto-detection |
| `--allow-writes` | also run POST/PUT/PATCH/DELETE — on a live target **this changes real data**, so it is opt-in; on a mock it is the default |
| `--no-writes` | read-only, even against a mock |
| `--negative` | probe documented failure paths: strip auth where 401/403 is documented, drop a required field where 422 is |
| `--only` / `--exclude` | regex over `METHOD /path` |
| `--fixtures ids.json` | pin path params to known-good ids |
| `--report` / `--junit` | JSON and JUnit XML for CI, stamped with environment and spec digest |
| `--learn-overlay FILE` | write what the real API actually returned as a mockd overlay |
| `--fail-on error` | exit non-zero for the pipeline |

`--learn-overlay` closes the loop: capture a real environment's responses once,
and the mock replays them — including for the 65 operations the spec never
described. Review before committing; captured bodies contain real data.

## The console

`console.py` serves a single local page that starts and stops `mockd.py` as a
child process and talks to it on your behalf. It has to be local and it has to
serve its own UI: the page spawns a process on this machine and calls
`http://localhost:<port>`, and every request it sends goes through the console
server, so the browser only ever makes same-origin calls.

Five screens do the work, and each opens on the simple thing with the technical
tools folded under **Advanced**:

| screen | what it is for |
|---|---|
| **Home** | where things stand — document, mock, baseline, your tests, servers — and the one thing to do next, with a button that does it |
| **Create tests** | say what you want tested in a sentence; tests come back validated, saved and already tried |
| **Tests** | every test in one list: search, filter by module, type, priority or result, pick a server, run; a failing row has **Copy bug report** |
| **API document** | which document is in use, and one box to bring in another from a link or a file |
| **Servers** | the mock and your real servers; **Add a server** asks for a name, an address and how you sign in, then tests the connection |

Under **More**: *Try a request* (fire one request at the mock, with sample and
deliberately broken bodies), *Spec coverage* and *Spec quality* (how much the
document really says), *Use the mock in an app*, and *Mock settings* (port,
overlay, stateful, auth simulation).

When the mock starts it runs `verify.py` against itself — the mock checking its
own answers against the document, writes included — and shows the result beside
"running". Everything downstream assumes the mock is faithful, so that is
established first rather than discovered later.

`CONSOLE_PORT=4200 python console.py` moves it off 4100. Closing the console
stops the mock it started.

## What a dev team points at, and what they get back

The mock listens on `http://localhost:4010` (`--port` to move it). CORS is wide
open, so no dev-server proxy is needed:

```
VITE_API_BASE_URL=http://localhost:4010
NEXT_PUBLIC_API_BASE_URL=http://localhost:4010
REACT_APP_API_BASE_URL=http://localhost:4010
```

The console prints this, plus copy-ready snippets for axios, Angular
`environment.ts`, a Vite proxy and Playwright, in its **Point your app here**
band — along with the LAN address, if one machine is hosting the mock for the
whole team.

**Bad requests come back in the shape the real API uses.** This matters more
than the shape being descriptive: a UI writes its error handling once, against
whatever the mock returns. FastAPI answers a bad request with 422 and
`{"detail": [{"loc", "msg", "type"}]}`, so mockd does too:

```
POST /api/v1/account/create   {"username": "x"}

422 Unprocessable Entity
{"detail": [
  {"loc": ["body","username"], "msg": "'x' is too short", "type": "string_too_short"},
  {"loc": ["body","email"],    "msg": "Field required",   "type": "missing"}
]}
```

Missing required query params, bad enums and wrong types all answer the same
way, with `loc` naming the field so a form can map the error back to its input.

The shape is per-operation, not per-API, because this spec carries **two
different 422 bodies**: FastAPI's `{"detail": [...]}` on 57 operations, and a
custom `{"status_code", "message", "error": [...]}` on 27 — with 12 documenting
none. The mock mirrors whichever each operation declares, so a UI dev is never
surprised by which module they happened to call. That the API has two error
contracts at all is itself a finding; the console reports the split under
**Error envelopes (422)**.
A path that is not in the spec is a 404 that says so, and a path that exists for
a different method says which methods are documented.

The full field-by-field reason is always in `/_mock/log` and in the console's
**Recent requests** panel. `--validation-mode debug` puts it inline in a 400
instead, when you would rather read it than parse it.

## Handing it to QA

Two ways out of the console, both aimed at the same file eventually running in CI.

**One request.** Hover any operation in the console and hit **curl**, or use
**Copy as cURL** in the request panel to capture exactly what is in the form —
headers, query, body and all. The result runs as pasted, in a terminal or
through Postman's *Import > Raw text*:

```bash
curl -X POST 'http://localhost:4010/api/v1/widgets' \
  -H 'Accept: application/json' \
  -H 'Content-Type: application/json' \
  -d '{
  "description": "Builds and maintains the product",
  "title": "Engineering"
}'
```

**Every request.** **Download Postman collection** in the console, or:

```bash
python postman.py --spec apis.json --base-url http://localhost:4010 \
    --out mockd.postman_collection.json
```

96 requests across 16 folders. Each one carries a body generated from its own
schema (so it passes), required query params filled and optional ones present
but disabled, path params as real Postman path variables, and the `X-Mock-*`
headers disabled and ready to enable — a tester forces a 500 or an empty list by
ticking a checkbox rather than reading documentation.

**Not every operation is worth the same attention, and the collection says so.**
Each request is marked in its name and sorted to the top of its folder if it is
fully specified:

```
✅ 30 fully specified — response schema declared; every field, type and status is the real contract.
⚠️  1 partly specified — an example but no schema; readable, not checkable.
⭕ 65 response shape not declared — the mock's body is inferred. Explore them, but
      do not assert on their shape yet.
```

Open one and the description leads with the verdict and the counts that matter:

```
✅ FULLY SPECIFIED — the response schema is declared, so every field and type
   below is the real contract.

body: 1 required / 1 optional · statuses you can force: 200, 201, 401, 403, 409, 422, 500
```

versus

```
⭕ RESPONSE SHAPE NOT DECLARED — the spec says nothing about what this returns,
   so the body you get back is INFERRED by the mock. Do not build assertions on
   its shape yet.

body: 5 required / 18 optional · statuses you can force: 200, 422
```

The console's Operations list carries the same marks, a `required+optional` field
count and a status count per row, and buttons to filter down to just the fully
specified ones — which is the shortlist a tester should start from.

Each request also ships tests derived from the spec, so the collection is the
start of the automated suite rather than a placeholder for it:

```js
pm.test('status is documented in the spec', () => pm.expect(documented).to.include(pm.response.code));
pm.test('responds within 2s', () => pm.expect(pm.response.responseTime).to.be.below(2000));
pm.test('success body has the documented top-level fields', () => { ... });
```

Nothing in it is mock-specific except the `baseUrl` variable, so the same file
runs against a real environment:

```bash
newman run mockd.postman_collection.json --env-var baseUrl=https://api.dev.example.com
```

That is the handoff: Postman by hand now, `newman` in the pipeline later, and
`verify.py` alongside it for the schema-level checks a collection cannot express.

## Saved tests: assertions QA writes once and anyone can replay

The console lets you fire a request and look at the answer. That is exploration.
`tests.py` turns an exploration into something a colleague re-runs six months
later against a different server without asking how it worked.

Three ideas, and they are the whole design:

1. A **case** is one request plus its assertions, saved rather than re-typed.
2. Everything that differs between servers or runs is a **variable**. No test
   file names a host or holds a token.
3. A **scenario** is ordered steps where later steps use values **captured** from
   earlier ones — which is how you test an endpoint that needs data to exist.

### The two kinds of scenario

They fail differently, so they are reported differently.

**`kind: "api"`** — the pre-data steps exist only to make the endpoint under test
reachable. The last step is the one being tested. If a setup step fails, the
target never ran, so it is **blocked**, not failed — nobody should spend an
afternoon debugging an endpoint that was never called:

```
BLOCK  Create a level — needs a department to exist first [api]
       target never ran — blocked by: create the parent department
       FAIL  setup   create the parent department       500    14ms
            x status in [200, 201] — 500 not one of [200, 201]
            x capture departmentId — data.id not found; later steps cannot run
       BLOCK target  create a level under it            ---     0ms
```

**`kind: "e2e"`** — every step is the point: a flow a human would perform, run as
sanity. A failure anywhere fails the flow.

```json
{
  "id": "level-needs-a-department",
  "kind": "api",
  "steps": [
    { "role": "setup", "name": "create the parent department",
      "request": { "method": "POST", "path": "/api/v1/widgets",
                   "body": { "title": "{{departmentTitle}}" } },
      "assertions": [ { "type": "status", "in": [200, 201] } ],
      "capture": { "departmentId": "data.id" } },
    { "role": "target", "name": "create a level under it",
      "request": { "method": "POST",
                   "path": ".../departments/{{departmentId}}/levels",
                   "body": { "title": "{{levelTitle}}", "level_order": 1 } },
      "assertions": [ { "type": "status", "in": [200, 201] }, { "type": "schema" } ] }
  ]
}
```

### Authoring without a blank page

Blank-page authoring is why test suites do not get written. In the console, send
a request and press **Save as test…** — it reads the response that just came
back and proposes the assertions, each with a checkbox:

```
status is 200                                            status
body matches the schema the spec declares                schema
responds under 2s                                        responseTime
status_code equals 200                                   jsonpath
data.items type array                                    jsonpath
data.items not_empty                                     jsonpath
data.items[0].id exists                                  jsonpath
```

It also spots ids worth capturing (`{{firstId}} = data.items[0].id`) so the next
step of a scenario has something to use.

### Adding assertions

The suggestions are a starting point, not the ceiling. Both dialogs use the same
row editor — **type · path · operator · value** — with **+ Add assertion** and
**remove** on every row:

```
schema                                                    remove
responseTime   lt        2000                             remove
jsonpath       status_code        equals   200            remove
jsonpath       data.items         type     "array"        remove
jsonpath       data.items         not_empty               remove
jsonpath       data.total         gte      1              remove     <- added by hand
```

The fields follow the type: `schema` needs nothing, `status` takes a number or a
list, `header` takes a name, and operators that ask no question (`exists`,
`not_empty`, `is_null`) hide the value box. A value is parsed as JSON when it
parses, so `200`, `true` and `["a","b"]` work without escaping, and anything
else is a string.

**Save as test…** opens it seeded from the response you just saw. **edit** on any
saved test opens the same editor with what is already there — and for a scenario,
one editor per step, so you can assert on the setup as well as the target.
**Raw JSON** toggles to the underlying definition for the things the rows do not
cover (request bodies, captures, reordering steps) and carries your edits both
ways.

**Run now** runs what is in the editor before you save it, and reports each
assertion separately:

```
against mock — pass
create the parent department — 200 2ms
  ok   status in [200, 201]
  ok   data.id exists
create a level under it — 200 1ms
  ok   status is 200
  ok   matches the spec schema
  fail data.nope exists — not present in the response
```

For a step of a scenario it runs **the steps before it too**, in order, so a
target that needs pre-data gets it — running such a step alone would only ever
report an undefined variable. It also uses the section's own `data`, so
`{{departmentTitle}}` resolves the same way it will at run time.

Assertion types: `status` (equals / in), `schema` (validate against the spec —
free coverage, nobody restates the contract), `jsonpath` with 19 operators,
`header`, `responseTime`, `body_contains`. Variables include `{{$uuid}}`,
`{{$randomEmail}}`, `{{$timestamp}}` for data that must be unique per run.

### How tests are organised

**A section is a file, and a file is a module** — and the sections come from the
spec's own `tags`, so nobody invents a taxonomy. `tests/user.json` holds the
tests for operations tagged `user`. **Save as test…** defaults the section to the
tag of the operation you just called and shows the exact file it will write:

```
Section — one file per module      Test id
[ user                        ]    [ get-api-v1-user-list ]
Writes to tests/drafts/user.json as a case. Drafts are gitignored — promote it
once it passes and it moves to tests/user.json.
```

**Labels** (`smoke`, `regression`, `negative`, `edge`, `sanity`, or your own) cut
across sections. Sections answer *where does this live*; labels answer *do I want
to run it now*:

```bash
python tests.py run --env dev --suite user     # one section
python tests.py run --env dev --tag smoke      # one label, every section
python tests.py run --env dev --kind e2e       # the sanity flows
```

The Tests screen filters by the same things above its list. `tests/README.md`
has the full layout.

### Draft first, shared when proven

QA should not push an assertion they are not sure about into the common repo.
So there are two places a test can live:

| | where | committed | run by CI |
|---|---|---|---|
| **draft** | `tests/drafts/` | no — gitignored | no |
| **shared** | `tests/` | yes | yes |

The console saves to drafts. Once a test has actually gone green, **promote**
lights up; until then it is refused:

```
$ python tests.py promote --suite checkout --id create-order
'create-order' has not passed yet (last outcome: blocked). Run it green first,
or pass --force if the endpoint is the thing that is wrong.
```

Every run records its outcome per test, so the console shows `pass` / `fail` /
`blocked` / `never run` beside each one, and **promote** is only enabled on a
test that has passed. Nothing is frozen: **edit** opens the full definition to
change assertions, add or reorder scenario steps, or rename; **del** removes it.

### The baseline: tests nobody writes

As soon as the mock is running on the project's document, `blueprint.py`
derives a suite called `baseline` from it:

| kind | what it checks | priority |
|---|---|---|
| contract | every read that needs no id answers with the documented status and shape | P0 |
| lifecycle | each resource can be created, read back, changed, found in its list and removed — every step held to its documented shape | P1 |
| omission | a request missing a required field is refused, as documented | P2 |
| missing | asking for an id nothing has is answered with 404, where the document says so | P2 |
| parameter | a query parameter outside its documented range is refused | P3 |

Ids a request needs are captured from the API itself — a book's id from the
list of books — never invented. Regenerating never overwrites work: a generated
test somebody edited is theirs from then on, and is kept. A not-found check is
kept only where the mock itself answers 404, so the baseline can always pass
on the mock; run it there first, then on any real server.

```bash
python tests.py run --env mock --drafts --suite baseline
python tests.py run --env dev  --drafts --suite baseline
```

### A test is a record, not only something to run

Each test carries a **priority** (`P0` must never break … `P3` edges), a
**status** (`ready`, `blocked`, `retired` — retired tests are kept and never
run), an **owner**, **links** (a ticket key, a URL) and a one-sentence
**description**. All of it is edited from the Tests list without opening the
steps, shows in the HTML report, and steers runs:

```bash
python tests.py run --env dev --priority P0 --priority P1
```

### From a sentence to tests

**Create tests** takes what you want proven, in your own words, and offers who
writes it: Claude or Codex on this machine if either is installed; any AI tool
of your own, through a prompt to copy and an answer to paste back; or an AI
tool connected directly ([MCP](#letting-an-ai-tool-write-the-tests-mcp)). The
prompt is built from the document — the endpoints the sentence is about, every
field and limit, and where each id comes from. A request that is not about
this API is refused rather than answered with invented endpoints.

What comes back is validated, saved as drafts, and tried on the mock at once,
with each result in plain words. The mock answers in the documented shape but
knows none of the API's own rules — what a total comes to, what is refused the
second time — so where a failure may be the mock's ignorance rather than the
test's fault, the screen says so and offers **Try them on** a real server.

### A value only you know

Sometimes nothing in the API can supply a value a test needs — a warehouse that
must already exist, a coupon somebody issued. The test then carries it as a
placeholder, such as `<a real warehouse id>`, rather than an invented value
that would fail for no visible reason. Such a test is marked **needs a value**
in the Tests list; open it, type the value, **Save**. The Create tests review
screen asks for it the same way, and an assistant connected over MCP is told
which tests are waiting and for what, so it asks you instead of making one up.
A value that differs per server belongs in that server's test data
(Servers → Advanced), which wins over the test's own at run time.

### A failure as a bug report

A failing row in the Tests list has **Copy bug report**: what was expected,
what happened, which side the evidence points at, the steps, the failing
request as a `curl` that can be re-run, and the response. It is plain Markdown
for any tracker, and it never contains a token, cookie or password.

**Download report** gives the whole run as HTML to read or send, JUnit XML for
a CI server, or JSON for a script.

### Generated tests, and the door they come through

Tests will increasingly arrive as pasted JSON — from a teammate, a script, or an
assistant. The design accounts for that rather than bolting it on later:

**One validated entry point.** `tests.py import` and the console's
**Import / paste…** (Tests → Advanced tools) accept a single test, an array, or a whole suite, and
check every field before anything is written. Plausible-looking output fails
loudly instead of silently evaluating to false at runtime:

```
refused — nothing was written:
  - dept-create: `path` must be a string starting with /
  - dept-create.assertions[0]: unknown operator 'equalz'
  - checkout-flow.steps[1]: uses {{orderId}} but nothing before it captures that,
    and it is not in the suite's `data`
```

That last one is the classic generated-scenario bug — a step referring to data no
earlier step produces — and it is caught at import, not at 3am in CI.

**A brief, not an integration.** `tests.py prompt --operation 'POST /api/v1/...'`
prints everything needed to write tests for one operation: the test grammar, that
operation's parameters and schemas, its documented statuses, a real response
fetched from the mock, and the ids already covered so nothing is duplicated. Paste
it into whichever assistant the company already uses and paste the JSON back.
Vendor-neutral on purpose — no key, no SDK, no lock-in, and the same validator
guards the result either way.

```bash
python tests.py prompt --operation 'POST /api/v1/...' --base-url http://localhost:4010
python tests.py import --suite ai-generated --file generated.json
python tests.py run --env mock --drafts --only ai-generated
python tests.py promote --suite ai-generated --id dept-create-201
```

**Quarantine by default.** Imports land in `tests/drafts/` — gitignored, not run
by CI — and the promotion gate still applies. A generated assertion cannot reach
the shared repo until a human has seen it go green. That ordering matters more
for generated tests than for hand-written ones.

Swapping the paste step for a direct API call later changes one function; the
grammar, the validator, the draft quarantine and the promotion gate all stay.

### Which environment am I running against?

Every layer answers it, because an outcome without a target is not a result.

**Before** — *Try a request* (under More) has a **Send to** selector (the mock, or any
defined environment) and warns in place when it is pointed at a real one.
The Tests screen has **Run on**.

**During** — a run in the console is a background job: each operation appears as
its own row the moment it answers, with a `n / total` counter, elapsed seconds,
and a **Cancel** button. A slow run and a stuck run no longer look the same.

```
RESULT                                              24 / 34   [Raw log]
target: http://127.0.0.1:4010   auth: none
  WARN  200  306ms  GET /api/v1/reference/phonecodes
  WARN  200  310ms  GET /api/v1/reference/all-cities
  PASS  200  405ms  GET /api/v1/forms/sections
```

**Raw log** toggles to the unparsed output. On the command line the same lines
print as they happen; the child runs unbuffered, because Python block-buffers
stdout to a pipe and that is what made progress arrive in lumps.

The CLI also names the target on the way in and again in the summary, so it
survives a long scroll:

```
env: mock -> http://localhost:4010 (no authentication), 2 env value(s)
running against mock (http://localhost:4010)
...
========================================================================
target: mock  http://localhost:4010
7 test(s): 7 pass
```

**After** — the result carries it. The console's result panel shows environment,
base URL, spec digest and time; the JSON report has `env`, `base_url` and the
spec provenance; and JUnit records them as properties so CI shows them too:

```xml
<properties>
  <property name="environment"  value="mock"/>
  <property name="base_url"     value="http://localhost:4010"/>
  <property name="spec_digest"  value="24b2fca175139737"/>
</properties>
```

**Later** — each test's badge in the console says `pass` **`on mock`**. The
environment *name* is recorded, not just the URL, because two environments can
share one (`mock` and `mock-auth` both point at `localhost:4010` and behave
differently).

### Running them

```bash
python tests.py run --env mock                     # everything shared
python tests.py run --env mock --drafts            # plus my own drafts
python tests.py run --env dev --kind e2e           # the sanity run
python tests.py run --env staging --suite checkout --junit results.xml
```

Or press **Run tests** in the console, pick the environment from the dropdown,
and anyone gets the same assertions with the same test data against whichever
server they chose. Exit code is non-zero on failure, blocked or error.

## Letting an AI tool write the tests (MCP)

`mcp_server.py` exposes mockd to an AI assistant as a set of tools, over the
Model Context Protocol. There is no AI in it and it needs no key: it answers
function calls with the same code the console uses. The assistant is whatever
tool the tester already has — Claude Code, Claude Desktop, Cursor.

Connect it once. The console's **Create tests** screen has the exact text to
paste for each tool ("Show me how"); for Claude Code it is one command:

    claude mcp add mockd -- /path/to/.venv/bin/python /path/to/mcp_server.py

Then ask the assistant in its own window — "write tests for cancelling an
order" — and it works through these itself:

| Tool | What the assistant gets |
|---|---|
| `get_test_format` | the JSON a test is written in, and the rules |
| `find_operations` | which endpoints a request is about |
| `get_operation` | one endpoint's fields, limits and documented answers, with a real response from the mock |
| `where_does_this_id_come_from` | which call supplies an id, and the path to capture it from |
| `list_tests` | what already exists |
| `list_servers` | where tests can run — names and addresses only |
| `validate_tests` | every problem with its tests, without saving |
| `save_tests` | validates, then adds them to the draft workspace |
| `set_value` | fills in a value only the user knows, where a test is waiting on one |
| `run_tests` | each result on the mock or a named server, with the failing step and response |

What it cannot do, by construction: see a token, password or cookie (a server
is named, and signed in to here); write to a server marked read only; change
or remove an existing test; or write tests for an endpoint that is not in the
project's API document. What does leave the machine is what the tools return —
endpoint and field names, sample responses, and the response of a failing test.

## Wiring it into CI

`.github/workflows/api-contract.yml` and `ci/gitlab-ci.yml` are ready to copy —
no application changes, only this repo. Three checks, cheapest first, each
answering a different question:

| | needs | runs on |
|---|---|---|
| `build_overlay.py --check` | nothing | every PR — fails when someone adds an endpoint nobody curated |
| `verify.py` against a local mock | nothing | every PR — catches a spec change breaking the mock |
| `tests.py run` against that mock | nothing | every PR — every assertion the team wrote |
| `verify.py --env dev` + `tests.py --kind e2e` | a real URL and secrets | main + nightly |

The first three need no environment and no credentials, which is what makes them
safe on a pull request from a fork. Secrets reach the live job through
`${{ secrets.* }}` and `environments.json`'s `${VAR}` placeholders — never
through a file in the repo. Every job publishes JUnit XML, so failures land in
the PR's checks rather than in a log somebody has to open.

## Which endpoints are safe to build against

For the 65 operations the spec does not describe, mockd's payload is an
inference. That is useful on day one and dangerous on day thirty, so every
operation carries a status that says how much weight it holds:

| status | meaning | who moves it |
|---|---|---|
| `spec` | the document declares the response; nothing was invented | backend, by adding `response_model` |
| `verified` | captured from a real environment and validated against the spec | CI, via `verify.py --learn-overlay` |
| `agreed` | a human wrote it and signed it off | UI + backend, in review |
| `proposed` | edited but not signed off | anyone |
| `guess` | mockd inferred it; nobody has confirmed the shape | nobody yet |

Only `spec`, `verified` and `agreed` are safe to build against — the console
shows that count as **safe to build on** and `GET /_mock/coverage` returns it.
The status lives in `mock_overlay.json`, so the decision lands in git next to
the code rather than in a chat thread, and a rebuild never demotes it: a human
owns that field. Today `apis.json` starts at 31 `spec` and 65 `guess`.

## Environments — set the URL and credentials once

Nobody should be pasting a base URL and a token into three tools. An environment
is declared by name in `environments.json`, and `verify.py`, `postman.py` and the
console all take `--env NAME`.

```bash
python environments.py list            # what exists, and whether its secrets resolve
python environments.py login dev       # prove the credentials before running anything
python verify.py --spec apis.json --env dev
python postman.py --spec apis.json --env dev     # also writes dev.postman_environment.json
```

**Secrets never live in that file.** Any value may be written as `${VAR}` and is
resolved from the shell, from `.env`, or from `environments.local.json` — the
last two are gitignored.

**Setting them from the console.** The **Servers** screen lists the mock and
your servers, each with its state in words — *Ready*, *Connected*, or *Needs a
password, a username*. **Add a server** asks for a name, an address, how you
sign in (nothing, a cookie from your browser, a token, or a username and
password) and whether it is read only, then signs in and makes one real call,
so a wrong credential is caught there rather than halfway through a run.

What you type is written to `.env`, which is gitignored; `environments.json`
gets only the *shape* — `${DEV_BASE_URL}`, `${DEV_TOKEN}` — so it is safe to
commit and share. Values already set are never sent back to the browser.
`.env` is read when a value is needed, so nothing restarts.

The file ships with only the mock in it. Your team's servers are added from
the Servers screen, or kept on one machine in `environments.local.json`
(`environments.local.example.json` shows one in full). The mock's own address
is `${MOCK_BASE_URL}`, which resolves by itself to the port the mock was last
started on — start it on 4020 and tests "on the mock" go to 4020.

```
dev          ${DEV_BASE_URL}                 auth=login  [unresolved: DEV_BASE_URL, DEV_PASSWORD, DEV_USERNAME]
mock         http://localhost:4010           auth=none
```

Three ways to authenticate:

| mode | for |
|---|---|
| `none` | the mock, unless it was started with `--require-auth` |
| `token` | a token you already hold: sent as a header, `Bearer` by default |
| `login` | call a login endpoint and use what comes back |

`login` handles this API's actual shape, which is worth knowing because it is not
the usual one. Probing the dev server directly:

```
Authorization: Bearer <anything>   ->  {"detail":"Authorization token missing"}
Cookie: access_token=<anything>    ->  {"detail":"Invalid token"}
```

The second read the credential; the first was ignored. **This API authenticates
by cookie**, and its 401 message says "Authorization token missing" regardless —
which makes a bearer token look like a rejected token rather than an ignored one.
So the `dev` environment uses `send: "query"` with `use_cookies: true` and keeps
the jar for the rest of the run, and `dev-cookie` exists for when you have a
session from the browser but not the password. `token_path` covers the ordinary
case of digging a bearer out of the JSON.

In the console, the **Check a real environment** panel lists them in a dropdown,
flags any that are missing variables, and has a **Test login** button that proves
the credentials and shows the resulting headers with the secret masked. No
credential is typed into the browser or stored by the console — `verify.py`
performs the login itself.

`postman.py --env dev` also writes `dev.postman_environment.json` for Postman,
with `baseUrl` filled in and `token` deliberately **empty** — that file gets
shared, the token should not.

## Two different questions verify.py answers

They are easy to conflate, so `verify.py` names which one it is running.

**Self-check — does the mock answer its own spec?** Every operation runs, writes
included: a mock's store is in memory, so there is nothing to protect and no
reason to leave half the spec unchecked. This validates the mock and the
overlay, and says nothing about the backend. It is the console's
**Self-check the mock** button, or:

```bash
python verify.py --spec apis.json --base-url http://localhost:4010
# target: http://localhost:4010 — detected to be a mockd instance (self-check)
```

**Live check — does the deployed backend answer the same spec?** Read-only
unless you ask otherwise, because here a POST creates a row somebody owns. It is
**Check a real environment** (Servers → Advanced), or:

```bash
python verify.py --spec apis.json --base-url https://api.dev.example.com \
    --header 'Authorization: Bearer eyJ...'
```

The target is detected by probing `/_mock/routes` — only mockd answers it —
and `--target mock|live` overrides the guess. `--no-writes` makes even a
self-check read-only.

## Reading a verify run

Either way, the run answers a narrower question than the coverage band: not
"does this endpoint work" but "can I **prove** it is right". Those differ, so
the summary spells it out:

```
96 operations in the spec
  49 exercised
      12 verified      status is documented AND the body validates against the declared schema
      37 unverifiable  the spec declares no response schema — nothing to check the body against
         of those, 37 answered with a success status anyway; they are not broken, just unprovable
       0 failed        undocumented status, or the body breaks its own schema
  47 not run       POST/PUT/PATCH/DELETE change real data; pass --allow-writes to include them
```

Three things to read carefully:

- **unverifiable is not a failure.** Those 37 endpoints answered with a success
  status. The spec simply declares no schema, so the run could prove nothing
  about the bodies. They become `verified` the day the routes get a
  `response_model` — not by changing anything here.
- **not run** appears only on a live check, and only for write methods. A mock
  self-check runs all 96 and reports **30 verified / 66 unverifiable / 0
  failed**.
- **30, not 31.** Of the 31 operations the spec describes, one carries an
  `example` and no schema — readable by a person, useless to a validator — so 30
  are provable. A read-only run reaches only the 12 of those that are GETs.

## Control headers

| header | effect |
|---|---|
| `X-Mock-Status: 404` | return a specific documented status |
| `X-Mock-Example: cancelled` | pick a named example from the spec |
| `X-Mock-Scenario: empty` | pick a named scenario from the overlay (`empty`, `page_full` are generated for every list endpoint) |
| `X-Mock-Nulls: on` | null every nullable leaf, keeping the structure — the payload that breaks UIs |
| `X-Mock-Delay: 1200` | delay the response by N ms, for loaders and timeouts |

Responses carry `X-Mock-Source` (`overlay` / `spec:example` / `generated` /
`synthesized` / `stateful`), `X-Mock-Operation` and `X-Mock-Generation`.

Because 65 operations document only 200 and 422, `X-Mock-Status: 500` is
refused there by default with the list of what *is* documented. Start the server
with `--allow-undocumented-status` to let QA force it anyway.

## Stateful mode

```bash
python mockd.py --spec apis.json --stateful
```

`POST` creates, `GET /…/{id}` returns it, `PUT/PATCH` update, `DELETE` removes,
missing ids 404 — so create → verify → cleanup flows work against the mock. The
console starts the mock this way.

It does not invent: what you create is what you read back. A created row is
given the fields the document requires and a server would add itself — an id, a
`created_at` — once, and keeps them. An id that refers to nothing is answered
with the documented 404 (in a path) or 422 (in a body), and documented query
filters are honoured, so a test that passes here is not passing on the mock's
imagination. Every answer is checked against the operation's own schema first;
where a stored row would not validate, the mock falls back to the documented
sample rather than serve the wrong shape.

It understands this API's verb-in-path shape: `/api/v1/account/create`,
`/api/v1/account/list`, `/api/v1/account/read/{id}`, `/api/v1/account/update/{id}` and
`/api/v1/account/delete/{id}` all resolve to one `/api/v1/account` collection. The
store is seeded from the overlay at startup, so lists are populated before
anything has been created; `--no-seed` starts empty.

## Auth simulation

```bash
python mockd.py --spec apis.json --require-auth
```

Any request without an `Authorization` header gets the documented 401 for that
operation. Off by default.

## Introspection

```
GET  /_mock/routes   every operation + which layer serves its body + its scenarios
GET  /_mock/coverage how completely the spec documents each operation, and the totals
GET  /_mock/drift    uncurated operations, spec/overlay generation, what changed on the last reload
GET  /_mock/log      last 200 requests (also appended to logs/requests.jsonl)
GET  /_mock/state    in-memory data (stateful mode)
POST /_mock/reload   force a spec re-read now
POST /_mock/reset    wipe state + log, re-seed
```

Every request is logged to `logs/requests.jsonl` with the matched operation, any
validation violations, the status and the body source.

## Test it

```bash
./smoke_test.sh                                          # 14 scenarios, end to end
python build_overlay.py --check                          # overlay drift
python verify.py --spec apis.json --base-url http://localhost:4010   # mock self-check
python tests.py run --base-url http://localhost:4010     # the saved suites
python selftest.py                                       # the code itself, no server needed
```

All four are what CI runs, and all four are green: 96/96 operations answer,
30 verified / 0 contract failures, 7/7 saved tests, 96/96 Postman requests pass
their generated assertions.

## Files

```
apis.json            the swagger document (untouched by any of this)
mockd.py             the server
console.py           local control panel (serves console.html + console.js)
mcp_server.py        mockd as tools an AI assistant can call (MCP, over stdio)
console.html         the panel's markup
console.js           the panel's logic
generator.py         JSON Schema -> example value
synth.py             payload inference for operations the spec leaves undefined
build_overlay.py     generate / merge / check the overlay
mock_overlay.json    the editable payloads (315 response bodies across 96 operations)
verify.py            contract verification against a live API
postman.py           spec -> Postman collection (importable, newman-runnable)
tests.py             saved assertions: cases, scenarios, runner, promotion
blueprint.py         the baseline suite, derived from the document
bindings.py          where each id comes from: which call returns it, and the path
verdict.py           whose problem a failure looks like, with the evidence
project.py           which document the project is about (mockd.json)
speclock.py          pins the agreed version of the document; specdiff.py compares two
selftest.py          checks of the code itself — python selftest.py
demo/                browser suites that click through the console (Playwright)
docs/recordings/     the two recordings shown at the top of this file
tests/               shared suites — committed, run by CI
tests/drafts/        the draft workspace — gitignored, yours until proven; also
                     where imported and generated tests land
.github/workflows/   ready-to-copy GitHub Actions pipeline
ci/gitlab-ci.yml     the same for GitLab
environments.py      named targets: base URL + how to authenticate
environments.json    the committed shapes — ${VAR} placeholders, never secrets; ships with the mock only
environments.local.example.json   copy to environments.local.json (gitignored) for real values
smoke_test.sh        end-to-end walkthrough
sample_spec.yaml     a tiny REST spec, for checking mockd against a non-FastAPI shape
```

## Known limits

Tested hard against `apis.json` (OpenAPI 3.1, FastAPI) and `sample_spec.yaml`,
and spot-checked against a hand-written 3.0 spec. What that probe found, and
what is still open:

- **JSON only.** A response declared as `text/csv` or `application/xml` is still
  answered with JSON — the wrong content type, not just a thin body. A
  `multipart/form-data` request body is accepted but never validated. Nothing in
  `apis.json` needs either.
- **`security` / `securitySchemes` are ignored.** `--require-auth` checks that an
  `Authorization` header is present; it does not verify a token, read scopes, or
  model per-role permissions.
- **`servers`, `style`/`explode` on parameters, callbacks, webhooks and links are
  not interpreted.**
- **Recursive schemas terminate but render poorly** — a self-referencing model
  generates `[null, null]` where a real API would return `[]`.
- Stateful mode is in-memory; a restart wipes it. Use `POST /_mock/reset`.
- Stateful mode keeps one collection per resource, so an API whose list-item and
  detail shapes differ cannot serve both from the store. The store checks its
  answer against the documented schema and defers to the spec/overlay when it
  would not validate.
- Synthesised payloads are *inferred*. Right far more often than not, but a guess
  until the owning team confirms them — which is what the `guess` status and
  `/_mock/drift` are for.
- **The mock knows the document, not the business.** It answers in the right
  shape with the right statuses, but it does not compute a total, track stock
  or refuse something the second time. A test of such a rule can only be
  settled on a real server; the console says so when that is the likely reason
  for a failure.
- **Some not-found checks are left out of the baseline.** Where the mock serves
  a read from a hand-written sample it answers every id with that sample, so a
  "404 for an id nothing has" check could never pass there and is not
  generated — though it would be valid against a real server.
- **"Is this request about this API?" is decided by words.** A request that
  shares a resource's name by accident — "book a flight" against an API of
  books — is let through; the endpoints offered will plainly not fit.
- **An assistant connected over MCP can run tests on any server that is set
  up**, including ones it may write to. It never sees a credential and read-only
  servers stay read only, but there is no per-server allow-list for it.
- The browser suites under `demo/` need Playwright (`cd demo && npm install`)
  and a running console; they are not part of the CI templates.

## Licence

[Apache License 2.0](../LICENSE). Use it, change it, ship it, sell it — the one
condition is attribution, and that condition holds for commercial use too.

Section 4(d) of the licence requires that any derivative work you distribute
carries a readable copy of the [NOTICE](../NOTICE) file, which names the author
and this repository. So if you build a product on this, the credit travels with
it: in your documentation, your about screen, or your third-party notices.

You may add your own notices alongside; you may not remove the existing one.
