/**
 * envalign.js — one test, many environments.
 *
 * The engine could already vary a test by environment; the console could not.
 * Three things had to exist for that to be true in practice: somewhere to say
 * what a value IS on each server, a way to scope an assertion to the servers
 * it makes sense on, and a result badge per environment — because one "last
 * outcome" meant running against dev erased the fact that it passes on mock.
 */
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

/* environments.json is COMMITTED. This suite edits it to prove the editor
   works, so it snapshots the file and puts it back — a test that leaves its
   probe values in a shared file has quietly changed the project. */
const ENV_FILE = path.join(
  process.env.MOCKD_DIR || path.resolve(__dirname, ".."), "environments.json");
const BEFORE = fs.readFileSync(ENV_FILE, "utf8");
process.on("exit", () => {
  try { fs.writeFileSync(ENV_FILE, BEFORE); } catch { /* nothing to restore */ }
});

let pass = 0, fail = 0; const errs = [];
const check = (n, ok, d) => { ok ? pass++ : fail++;
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${n}${ok || !d ? "" : "  — " + d}`); };

(async () => {
  const b = await chromium.launch();
  const p = await b.newPage();
  p.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  p.on("console", (m) => { if (m.type() === "error" && !/status of 4\d\d/.test(m.text()))
                             errs.push("console: " + m.text()); });
  await p.goto("http://localhost:4100", { waitUntil: "networkidle" });
  await p.waitForTimeout(1800);

  // ------------------------------------------- 1. per-environment test data
  await p.locator('nav.side a[data-view="environments"]').click();
  await p.waitForTimeout(1400);
  check("each environment offers its test data",
        await p.locator(".env-data").count() > 0);
  await p.locator('.env-data[data-env="mock"]').click();
  await p.waitForTimeout(1200);
  check("the editor opens", await p.locator("#envDataCard").isVisible());
  check("and names the environment",
        (await p.locator("#envDataName").textContent()) === "mock");

  await p.locator("#envDataAdd").click(); await p.waitForTimeout(400);
  const rows = await p.locator("[data-dname]").count();
  await p.locator(`[data-dname="${rows - 1}"]`).fill("probeCount");
  await p.locator(`[data-dvalue="${rows - 1}"]`).fill("7");
  await p.locator("#envDataSave").click();
  await p.waitForTimeout(1500);

  const stored = await p.evaluate(async () =>
    await (await fetch("/api/environments/mock/data")).json());
  const probe = (stored.data || []).find((r) => r.name === "probeCount");
  check("a value is saved for that environment", !!probe, JSON.stringify(stored.data));
  check("and a number stays a number, not a string", probe && probe.value === 7,
        JSON.stringify(probe));

  // something sensitive must not land in the committed file
  await p.locator("#envDataAdd").click(); await p.waitForTimeout(400);
  const n = await p.locator("[data-dname]").count();
  await p.locator(`[data-dname="${n - 1}"]`).fill("secretRow");
  await p.locator(`[data-dvalue="${n - 1}"]`).fill("real-id-123");
  await p.locator(`[data-dsecret="${n - 1}"]`).check();
  await p.locator("#envDataSave").click();
  await p.waitForTimeout(1500);
  const after = await p.evaluate(async () =>
    await (await fetch("/api/environments/mock/data")).json());
  const secret = (after.data || []).find((r) => r.name === "secretRow");
  check("a value marked sensitive is kept out of the committed file",
        secret && !secret.literal, JSON.stringify(secret));
  check("and the note says where it went",
        /\.env/.test(await p.locator("#envDataNote").textContent()),
        await p.locator("#envDataNote").textContent());

  // --------------------------------------- 2. an assertion scoped to an env
  await p.locator('nav.side a[data-view="tests"]').click();
  await p.waitForTimeout(1600);
  await p.locator("#wbToggle").click(); await p.waitForTimeout(1400);
  await p.locator('[data-asserts="0"] summary').click(); await p.waitForTimeout(400);
  await p.locator('[data-ahost="0"] .add-assert').click(); await p.waitForTimeout(500);
  const where = p.locator('[data-ahost="0"] .a-where').last();
  check("an assertion can be scoped to environments", await where.count() === 1);
  const options = await where.textContent();
  check("it offers the real environments", /mock/.test(options), options.slice(0, 80));
  check("and defaults to everywhere", (await where.inputValue()) === "");

  await where.selectOption({ index: 1 });
  const row = p.locator('[data-ahost="0"] .arow2').last();
  await row.locator(".a-path").fill("data.total");
  await row.locator(".a-op").selectOption("exists");
  await p.waitForTimeout(400);
  const read = await p.evaluate(() => readAssertions(
    document.querySelector('[data-ahost="0"]')));
  const scoped = read.find((a) => a.path === "data.total");
  check("the scope survives being read back",
        !!scoped && (scoped.only_on || scoped.except_on), JSON.stringify(scoped));

  // ------------------------------------------- 3. a result badge per env
  const withHistory = await p.evaluate(async () => {
    const d = await (await fetch("/api/tests")).json();
    for (const s of (d.suites || [])) {
      for (const t of [...(s.cases || []), ...(s.scenarios || [])]) {
        if (t.history && t.history.by_env) return Object.keys(t.history.by_env);
      }
    }
    return null;
  });
  check("history is kept per environment", !!withHistory, JSON.stringify(withHistory));
  if (withHistory) {
    const tree = await p.locator("#testTree").textContent();
    check("and the badge names the environment",
          tree.includes(withHistory[0]), withHistory.join(","));
  }

  console.log(`\n${pass} passed, ${fail} failed`);
  console.log(errs.length ? "JS ERRORS:\n  " + errs.join("\n  ") : "no JS errors");
  await b.close(); process.exit(fail || errs.length ? 1 : 0);
})();
