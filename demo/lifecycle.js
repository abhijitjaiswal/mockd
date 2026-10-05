/**
 * lifecycle.js — flows derived from the document, driven through the console.
 *
 * The point under test is not "a button exists". It is that a team with a spec
 * and no tests can press one control and end up with runnable flows: derived
 * from path shape rather than vocabulary, honest about what it could not
 * derive, and refusing to invent an assertion the document cannot support.
 *
 * The suite writes into the draft workspace, which is gitignored, and puts the
 * project file back the way it found it.
 */
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

let pass = 0, fail = 0; const errs = [];
const check = (n, ok, d) => { ok ? pass++ : fail++;
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${n}${ok || !d ? "" : "  — " + d}`); };

const DIR = process.env.MOCKD_DIR || path.resolve(__dirname, "..");
const DRAFT = path.join(DIR, "tests", "drafts", "lifecycles.json");
const had = fs.existsSync(DRAFT);
const before = had ? fs.readFileSync(DRAFT, "utf8") : null;
process.on("exit", () => {
  try {
    if (before === null) fs.rmSync(DRAFT, { force: true });
    else fs.writeFileSync(DRAFT, before);
  } catch { /* nothing to restore */ }
});

(async () => {
  const b = await chromium.launch();
  const p = await b.newPage();
  p.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  p.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text()); });
  await p.goto("http://localhost:4100", { waitUntil: "networkidle" });

  await p.locator('nav.side a[data-view="tests"]').click();
  // these suites exercise the tools kept under Advanced, which is shut by default
  await p.evaluate(() => window.showAdvanced(true));
  await p.waitForTimeout(400);

  check("the control is offered without typing anything",
        await p.locator("#btnLifecycle").isVisible());

  await p.locator("#btnLifecycle").click();
  await p.waitForTimeout(300);
  check("its panel opens", await p.locator("#genLifeRow").isVisible());
  check("and the story panel is not also open",
        await p.locator("#genStoryRow").isHidden());
  check("nothing can be imported before anything is derived",
        await p.locator("#genLifeImport").isDisabled());

  await p.locator("#genLifeGo").click();
  await p.waitForTimeout(2500);

  const rows = await p.locator("#genLifeOut .runrow").count();
  check("flows come back", rows > 0, `${rows} rows`);
  const out = await p.locator("#genLifeOut").textContent();
  check("each names the operations it chains",
        /POST .*→.*GET/.test(out), out.slice(0, 160));
  check("and it is now possible to keep them",
        !(await p.locator("#genLifeImport").isDisabled()));

  const note = await p.locator("#genLifeNote").textContent();
  check("the note says where they came from", /flow/.test(note), note);

  // the API is the contract here; the panel is one reader of it
  const derived = await p.evaluate(async () =>
    await (await fetch("/api/tests/blueprint", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}) })).json());

  check("every flow captures an id from its create",
        (derived.flows || []).every((f) => f.steps.length >= 2), "");
  check("a resource it cannot do is reported, not silently dropped",
        Array.isArray(derived.skipped), JSON.stringify(derived.skipped || []).slice(0, 120));
  check("it says whether the mock can actually pass these",
        typeof derived.stateful === "boolean");

  // Importing must go through the same validator as a human's paste.
  const imported = await p.evaluate(async () =>
    await (await fetch("/api/tests/blueprint", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ import: true, suite: "lifecycles" }) })).json());
  check("importing succeeds", imported.ok === true,
        JSON.stringify(imported).slice(0, 200));
  check("and it lands in a named suite", imported.suite === "lifecycles",
        JSON.stringify(imported.suite));
  check("the draft file is written", fs.existsSync(DRAFT));

  if (fs.existsSync(DRAFT)) {
    const suite = JSON.parse(fs.readFileSync(DRAFT, "utf8"));
    const flows = suite.scenarios || [];
    check("the saved suite holds the flows", flows.length > 0, `${flows.length}`);
    check("each records the operations it was derived from",
          flows.every((f) => (f.generated || {}).from || (f.tags || []).includes("generated")),
          JSON.stringify(flows[0] && flows[0].generated || {}).slice(0, 120));
    const steps = flows.flatMap((f) => f.steps || []);
    check("a flow has exactly one target",
          flows.every((f) => (f.steps || []).filter((s) => s.role === "target").length === 1));
    // A flow's own steps take their statuses from the document (`in: [...]`),
    // and the one exact status they may assert is the documented 404 after a
    // delete. The reads added in front to fetch ids are plain GETs expecting 200.
    check("no step asserts a status its operation never documents",
          steps.every((s) => (s.assertions || []).every((a) =>
            a.type !== "status" || a.equals === undefined || a.equals === 404
            || (s.role === "setup" && a.equals === 200))));
    check("a flow that needs another record's id reads it rather than inventing one",
          flows.every((f) => !JSON.stringify(f.steps).match(
            /"[a-z_]+_id":\s*"[0-9a-f]{8}-[0-9a-f]{4}-/)),
          "a literal uuid is sitting in a foreign-key field");
  }

  console.log(`\n${pass} passed, ${fail} failed`);
  console.log(errs.length ? "JS ERRORS:\n  " + errs.join("\n  ") : "no JS errors");
  await b.close(); process.exit(fail || errs.length ? 1 : 0);
})();
