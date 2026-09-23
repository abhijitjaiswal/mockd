/**
 * compare.js — comparing two documents by shape.
 *
 * The point being pinned: direction decides severity. A field added to a
 * response is additive; the same field added to a request as required breaks
 * every existing caller. A text diff cannot tell those apart, which is why
 * this is not a text diff.
 */
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

let pass = 0, fail = 0; const errs = [];
const check = (n, ok, d) => { ok ? pass++ : fail++;
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${n}${ok || !d ? "" : "  — " + d}`); };

const DIR = process.env.MOCKD_DIR || path.resolve(__dirname, "..");
const SPECS = path.join(DIR, "specs");

function spec({ reqRequired = ["name"], reqProps = { name: { type: "string" } },
                respProps = { id: { type: "string" }, name: { type: "string" } },
                statuses = ["200"] } = {}) {
  const responses = {};
  statuses.forEach((s) => {
    responses[s] = { description: "r", content: { "application/json": {
      schema: { type: "object", properties: respProps } } } };
  });
  return JSON.stringify({
    openapi: "3.1.0",
    info: { title: "Fixture", version: "1.0.0" },
    paths: { "/thing": { post: {
      requestBody: { content: { "application/json": {
        schema: { type: "object", required: reqRequired, properties: reqProps } } } },
      responses } } },
  }, null, 2);
}

const A = path.join(SPECS, "cmp-a.json");
const B = path.join(SPECS, "cmp-b.json");

(async () => {
  fs.mkdirSync(SPECS, { recursive: true });
  fs.writeFileSync(A, spec());
  // B: a required request field added, a response field removed, a status added,
  //    and an optional request field added
  fs.writeFileSync(B, spec({
    reqRequired: ["name", "email"],
    reqProps: { name: { type: "string" }, email: { type: "string" },
                nickname: { type: "string" } },
    respProps: { id: { type: "string" }, createdAt: { type: "string" } },
    statuses: ["200", "404"],
  }));
  process.on("exit", () => { for (const f of [A, B]) fs.rmSync(f, { force: true }); });

  const b = await chromium.launch();
  const p = await b.newPage();
  p.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  p.on("console", (m) => { if (m.type() === "error" && !/status of 400/.test(m.text()))
                             errs.push("console: " + m.text()); });
  await p.goto("http://localhost:4100", { waitUntil: "networkidle" });
  await p.waitForTimeout(1800);
  await p.locator('nav.side a[data-view="source"]').click();
  await p.waitForTimeout(1500);

  check("both pickers are populated",
        (await p.locator("#cmpFrom option").count()) > 1
        && (await p.locator("#cmpTo option").count()) > 1);

  await p.locator("#cmpFrom").selectOption("specs/cmp-a.json");
  await p.locator("#cmpTo").selectOption("specs/cmp-b.json");
  await p.locator("#btnCompare").click();
  await p.waitForTimeout(3000);

  const out = await p.locator("#cmpOut").textContent();
  check("a required request field added is breaking",
        /required request field added/.test(out), out.slice(0, 120));
  check("a response field removed is breaking",
        /response field removed/.test(out), out.slice(0, 120));
  check("an optional request field added is additive",
        /optional request field added/.test(out));
  check("a response field added is additive", /response field added/.test(out));
  check("a new status is reported", /response status added/.test(out));
  check("it explains who each change hurts",
        /existing callers do not send it/.test(out));

  // the asymmetry, asserted directly against the API
  const api = await p.evaluate(async () => (await (await fetch("/api/spec/compare", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ from: "specs/cmp-a.json", to: "specs/cmp-b.json" }),
  })).json()));
  const sev = (what) => (api.changes.find((c) => c.what === what) || {}).severity;
  check("required request field added -> breaking",
        sev("required request field added") === "breaking", sev("required request field added"));
  check("optional request field added -> additive",
        sev("optional request field added") === "additive", sev("optional request field added"));
  check("response field added -> additive",
        sev("response field added (200)") === "additive", sev("response field added (200)"));
  check("response field removed -> breaking",
        sev("response field removed (200)") === "breaking", sev("response field removed (200)"));

  // identical documents say so
  const same = await p.evaluate(async () => (await (await fetch("/api/spec/compare", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ from: "specs/cmp-a.json", to: "specs/cmp-a.json" }),
  })).json()));
  check("comparing a document with itself is refused", same.ok === false, same.error);

  console.log(`\n${pass} passed, ${fail} failed`);
  console.log(errs.length ? "JS ERRORS:\n  " + errs.join("\n  ") : "no JS errors");
  await b.close(); process.exit(fail || errs.length ? 1 : 0);
})();
