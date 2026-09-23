# Writing the spec so the framework can mock it

For whoever owns the API. Everything here is plain OpenAPI — no vendor
extensions, no custom keywords, nothing that only this toolchain understands. A
spec that follows it mocks exactly, validates exactly, and generates its own
tests. A spec that doesn't still works, but the mock has to *infer*, and an
inference is a guess with good manners.

The rules are ordered by how much they buy you.

---

## R1 — Declare a schema for every success response

**The single highest-value rule.** Without it there is nothing to generate from,
nothing to validate against, and nothing for a test to assert on.

```yaml
responses:
  "200":
    description: Departments fetched successfully
    content:
      application/json:
        schema:
          $ref: "#/components/schemas/StandardResponseModel_Page_DepartmentOut_"
```

Not this — which is what FastAPI emits when a route has no `response_model`:

```yaml
responses:
  "200":
    description: Successful Response
    content:
      application/json:
        schema: {}          # <- says nothing
```

**FastAPI:**

```python
@router.get("/departments", response_model=StandardResponseModel[Page[DepartmentOut]])
async def list_departments(...): ...
```

What it unlocks: real payloads instead of inferred ones, `{"type": "schema"}`
assertions in tests, and `verify.py` being able to prove the live API matches.

---

## R2 — Declare the failures you actually return

If the endpoint can 401, say so. A status the spec does not document cannot be
requested from the mock, cannot be asserted on, and is reported as a contract
violation when the real API returns it.

```yaml
responses:
  "200": { ... }
  "401": { description: Missing or invalid token, content: { application/json: { example: { ... } } } }
  "403": { description: Permission denied,       content: { application/json: { example: { ... } } } }
  "404": { description: Department not found,    content: { application/json: { example: { ... } } } }
  "409": { description: Title already exists,    content: { application/json: { example: { ... } } } }
  "500": { description: Internal server error,   content: { application/json: { example: { ... } } } }
```

**FastAPI:** `@router.get(..., responses={401: {...}, 404: {...}})`, or a shared
`responses=COMMON_ERRORS` dict so every route gets them without repetition.

---

## R3 — One error envelope for the whole API

Pick one shape and use it everywhere. Two shapes means the UI needs two error
parsers and neither team remembers which module returns which.

Either FastAPI's default:

```json
{ "detail": [ { "loc": ["body", "title"], "msg": "Field required", "type": "missing" } ] }
```

…or your own, declared as a component and `$ref`d from every error response:

```yaml
components:
  schemas:
    ApiError:
      type: object
      required: [status_code, message]
      properties:
        status_code: { type: integer }
        message:     { type: string }
        error:
          type: array
          items: { $ref: "#/components/schemas/FieldError" }
```

The mock mirrors whichever shape each operation declares, so a mixed API
produces a mixed mock — faithfully, and unhelpfully.

---

## R4 — Put an `example` on the responses that matter

A schema tells the mock what shape to generate. An example tells it exactly what
to return, which makes the payload stable across restarts and readable in review.

```yaml
content:
  application/json:
    schema: { $ref: "#/components/schemas/StandardResponseModel_DepartmentOut_" }
    example:
      status_code: 200
      message: Department created successfully
      data: { id: "3fa85f64-5717-4562-b3fc-2c963f66afa6", title: Engineering, level_count: 3 }
```

An example **without** a schema is worth much less: a person can read it, but
nothing automated can check a response against it.

**FastAPI:** `Field(examples=[...])` on the model, or
`responses={200: {"content": {"application/json": {"example": {...}}}}}`.

---

## R5 — Name your types in `components`, and `$ref` them

Inline schemas duplicated across operations drift apart. A named component is
resolved once, generated consistently, and shows up in the Postman collection
and the test briefs under its own name.

```yaml
components:
  schemas:
    DepartmentOut:
      type: object
      required: [id, title]
      properties:
        id:    { type: string, format: uuid }
        title: { type: string, minLength: 1, maxLength: 200 }
```

---

## R6 — Be exact about required, types, formats and bounds

Everything here drives both generation and validation. Vague schemas produce
vague mocks.

| Declare | Get |
|---|---|
| `required: [id, title]` | the right 422 when a field is missing; correct `n+m` counts in the console |
| `enum: [Draft, Sent]` | valid values generated, invalid ones rejected |
| `format: uuid \| date-time \| date \| email \| uri` | realistic values instead of `sample-string` |
| `minLength` / `maxLength` / `minimum` / `maximum` | values inside the real range |
| `default:` | the value the server would have used |

Use the **standard** `format` values. A made-up format is ignored and you fall
back to a generic string.

---

## R7 — Say what may be null, in your version's spelling

Optional-and-absent is not the same as present-and-null, and a UI that gets it
wrong crashes on the first null.

```yaml
# OpenAPI 3.1
description: { anyOf: [ { type: string }, { type: "null" } ] }

# OpenAPI 3.0
description: { type: string, nullable: true }
```

Both are understood. `X-Mock-Nulls: on` then returns every nullable leaf as
null, which is the payload that breaks interfaces.

---

## R8 — Give every operation an `operationId` and a `tag`

```yaml
get:
  tags: [departments]
  operationId: list_departments
  summary: List departments
```

`tags` become Postman folders and console grouping; `operationId` and `summary`
become the generated test names. Without them everything is called
`GET /api/v1/…`.

---

## R9 — Declare `servers`, and keep secrets out

```yaml
servers:
  - url: https://api.dev.example.com
  - url: https://api.example.com
```

No tokens, no internal hostnames, no credentials anywhere in the document.
Environments belong in `environments.json` with `${VAR}` placeholders.

---

## R10 — Publish the spec at a URL

If the service serves `/openapi.json`, the mock can follow it live:

```bash
python mockd.py --spec https://api.dev.example.com/openapi.json --poll 30
```

Endpoints added this morning are mocked this afternoon with nobody re-exporting
a file.

---

## A complete operation

Everything above, on one endpoint:

```yaml
paths:
  /api/v1/departments:
    post:
      tags: [departments]
      operationId: create_department
      summary: Create a department
      requestBody:
        required: true
        content:
          application/json:
            schema: { $ref: "#/components/schemas/DepartmentCreate" }
      responses:
        "201":
          description: Department created successfully
          content:
            application/json:
              schema: { $ref: "#/components/schemas/StandardResponseModel_DepartmentOut_" }
              example:
                status_code: 201
                message: Department created successfully
                data: { id: "3fa85f64-5717-4562-b3fc-2c963f66afa6", title: Engineering, level_count: 0 }
        "401": { description: Missing or invalid token, content: { application/json: { schema: { $ref: "#/components/schemas/ApiError" } } } }
        "403": { description: Permission denied,        content: { application/json: { schema: { $ref: "#/components/schemas/ApiError" } } } }
        "409": { description: Title already exists,     content: { application/json: { schema: { $ref: "#/components/schemas/ApiError" } } } }
        "422": { description: Validation error,         content: { application/json: { schema: { $ref: "#/components/schemas/ApiError" } } } }
        "500": { description: Internal server error,    content: { application/json: { schema: { $ref: "#/components/schemas/ApiError" } } } }

components:
  schemas:
    DepartmentCreate:
      type: object
      required: [title]
      properties:
        title:       { type: string, minLength: 1, maxLength: 200 }
        description: { anyOf: [ { type: string }, { type: "null" } ] }
    DepartmentOut:
      allOf:
        - $ref: "#/components/schemas/DepartmentCreate"
        - type: object
          required: [id]
          properties:
            id:          { type: string, format: uuid }
            level_count: { type: integer, minimum: 0, default: 0 }
```

---

## What the framework does *not* need

- No vendor extensions (`x-mock-*`). Payload overrides live in
  `mock_overlay.json`, outside the spec, so the spec stays portable.
- No special ordering, file layout or naming convention.
- No changes to application code — only to what the route declares.

## Checking yourself

```bash
python mockd.py --spec your_spec.json          # prints the coverage split at startup
curl localhost:4010/_mock/coverage             # per-operation, with the reason
```

The console's **Authoring** view grades the loaded spec against these rules and
names every operation that fails each one.
