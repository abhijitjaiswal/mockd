/**
 * record.js — a test as something to manage, not only something to run.
 *
 * Priority, status, owner, a linked ticket and a plain-words description are
 * what let a team answer "which of these matter before a release?" and "what
 * was this one for?". The point under test is that all of it can be set from
 * the list, without opening the test's steps, and that it then steers runs.
 *
 * Works in its own draft suite and removes it afterwards.
 */
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

let pass = 0, fail = 0; const errs = [];
const check = (n, ok, d) => { ok ? pass++ : fail++;
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${n}${ok || !d ? "" : "  — " + d}`); };

const DIR = process.env.MOCKD_DIR || path.resolve(__dirname, "..");
const FILE = path.join(DIR, "tests", "drafts", "record-probe.json");
process.on("exit", () => { try { fs.rmSync(FILE, { force: true }); } catch { /* gone */ } });

(async () => {
  const b = await chromium.launch();
  const p = await b.newPage();
  p.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  p.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text()); });
  await p.goto("http://localhost:4100", { waitUntil: "networkidle" });

  // a test of our own to manage
  const saved = await p.evaluate(async () =>
    await (await fetch("/api/tests/save-flow", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ suite: "record-probe", stage: "draft", flow: {
        id: "record-probe-flow", name: "a probe to manage", kind: "e2e",
        levels: ["smoke"],
        steps: [{ role: "target", name: "ask for the routes",
                  request: { method: "GET", path: "/_mock/routes" },
                  assertions: [{ type: "status", equals: 200 }] }] } }) })).json());
  check("a probe test exists to manage", saved.ok === true, JSON.stringify(saved).slice(0, 120));

  await p.locator('nav.side a[data-view="tests"]').click();
  await p.waitForTimeout(1500);

  // ------------------------------------------------------------ the picker
  const prioText = await p.locator("#testPriorities").textContent();
  check("priority is something a run can be narrowed by",
        await p.locator("#testPriorities option").count() === 4);
  check("each priority says what it means and how many tests hold it",
        /P0 \(\d+\)/.test(prioText) && /must|unusable|every change/i.test(prioText),
        prioText.slice(0, 90));

  // --------------------------------------------------------------- the row
  const row = p.locator('.titem:has([data-id="record-probe-flow"])').first();
  check("a test shows its priority in the list",
        await row.locator(".prio").count() === 1);
  check("an untouched test sits at the default priority",
        (await row.locator(".prio").textContent()).trim() === "P2");

  // ------------------------------------------------------------ the editor
  await row.locator('[data-act="record"]').click();
  await p.waitForTimeout(300);
  const editor = row.locator(".receditor");
  check("details open without opening the steps", await editor.isVisible());
  check("and the workbench was not touched",
        !(await p.locator("#wbId").inputValue()).includes("record-probe"));

  await editor.locator('[data-f="priority"]').selectOption("P0");
  await editor.locator('[data-f="owner"]').fill("sam");
  await editor.locator('[data-f="links"]').fill("ABC-123");
  await editor.locator('[data-f="description"]').fill("The mock lists its routes.");
  await editor.locator('[data-level="regression"]').check();
  await editor.locator("[data-recsave]").click();
  await p.waitForTimeout(1500);

  const after = p.locator('.titem:has([data-id="record-probe-flow"])').first();
  const meta = await after.locator(".recmeta").allTextContents();
  check("the new priority shows", (await after.locator(".prio").textContent()).trim() === "P0");
  check("so do the owner and the ticket",
        /sam/.test(meta.join(" ")) && /ABC-123/.test(meta.join(" ")), meta.join(" | "));
  check("and what the test is for", /lists its routes/.test(meta.join(" ")));
  check("a test can hold more than one type",
        /smoke/.test(meta.join(" ")) && /regression/.test(meta.join(" ")), meta.join(" | "));

  const onDisk = JSON.parse(fs.readFileSync(FILE, "utf8")).scenarios[0];
  check("it is written to the test's own file",
        onDisk.priority === "P0" && onDisk.owner === "sam"
        && (onDisk.links || []).includes("ABC-123"), JSON.stringify(onDisk).slice(0, 160));
  check("and the steps were left exactly as they were",
        onDisk.steps.length === 1 && onDisk.steps[0].request.path === "/_mock/routes");

  // --------------------------------------------------- it steers what runs
  const counts = await p.evaluate(async () => {
    const ask = async (extra) => (await (await fetch("/api/tests/pipeline", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ env: "mock", format: "github", drafts: true,
                             modules: ["record-probe"], ...extra }) })).json());
    return { p0: await ask({ priorities: ["P0"] }), p3: await ask({ priorities: ["P3"] }) };
  });
  check("choosing its priority selects it", counts.p0.selects === 1,
        JSON.stringify(counts.p0.selects));
  check("choosing another does not", counts.p3.selects === 0,
        JSON.stringify(counts.p3.selects));
  check("the pipeline carries the same choice",
        /--priority P0/.test(counts.p0.command || ""), counts.p0.command);

  // ------------------------------------------------------- refusing nonsense
  const bad = await p.evaluate(async () =>
    await (await fetch("/api/tests/record", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ suite: "record-probe", stage: "draft",
                             id: "record-probe-flow", fields: { priority: "P9" } }) })).json());
  check("a priority that does not exist is refused", bad.ok === false,
        JSON.stringify(bad).slice(0, 120));
  check("and nothing was written",
        JSON.parse(fs.readFileSync(FILE, "utf8")).scenarios[0].priority === "P0");

  // retiring takes it out of runs without deleting it
  await p.evaluate(async () =>
    await fetch("/api/tests/record", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ suite: "record-probe", stage: "draft",
                             id: "record-probe-flow", fields: { status: "retired" } }) }));
  const retired = await p.evaluate(async () =>
    (await (await fetch("/api/tests/pipeline", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ env: "mock", format: "github", drafts: true,
                             modules: ["record-probe"] }) })).json()).selects);
  check("a retired test is kept but no longer runs", retired === 0, String(retired));
  check("and is still on disk", fs.existsSync(FILE));

  console.log(`\n${pass} passed, ${fail} failed`);
  console.log(errs.length ? "JS ERRORS:\n  " + errs.join("\n  ") : "no JS errors");
  await b.close(); process.exit(fail || errs.length ? 1 : 0);
})();
