/**
 * projectspec.js — one document for the whole system, overridable per module.
 *
 * Before this, thirteen places decided which spec independently: six CLIs
 * defaulted to a hardcoded apis.json and the console fell back to whatever the
 * mock happened to be running. "Which document am I judged against?" depended
 * on which door you came through.
 */
const { chromium } = require("playwright");
const { execFileSync } = require("child_process");
const path = require("path");

let pass = 0, fail = 0; const errs = [];
const check = (n, ok, d) => { ok ? pass++ : fail++;
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${n}${ok || !d ? "" : "  — " + d}`); };

const DIR = process.env.MOCKD_DIR || path.resolve(__dirname, "..");
const cli = (...a) => execFileSync("python3", [path.join(DIR, "project.py"), ...a],
                                   { cwd: DIR, encoding: "utf8" });

(async () => {
  cli("use", "apis.json");
  for (const m of ["verify", "tests"]) { try { cli("clear", "--for", m); } catch {} }

  const b = await chromium.launch();
  const p = await b.newPage();
  p.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  p.on("console", (m) => { if (m.type() === "error" && !/status of 400/.test(m.text()))
                             errs.push("console: " + m.text()); });
  await p.goto("http://localhost:4100", { waitUntil: "networkidle" });
  await p.waitForTimeout(1800);
  await p.locator('nav.side a[data-view="source"]').click();
  await p.waitForTimeout(900);

  const tagNow = (await p.locator("#projSpecTag").textContent()).trim();
  const modsNow = (await p.locator("#projModules").textContent()).trim();
  check("the project spec is named", tagNow === "apis.json",
        `tag=${JSON.stringify(tagNow)} mods=${JSON.stringify(modsNow.slice(0,60))}`);
  const rows = await p.locator("#projModules table tr").count();
  check("every module's document is shown", rows >= 7, String(rows));
  check("with nothing pinned to start",
        /Everything follows the project spec/.test(
          await p.locator("#projModules").textContent()));

  // change it for the whole system
  await p.locator("#projSpec").selectOption("specs/fetched.json");
  await p.locator("#btnProjSave").click();
  await p.waitForTimeout(1200);
  check("choosing one sets it everywhere",
        (await p.locator("#projSpecTag").textContent()).trim() === "specs/fetched.json");
  check("and says to commit the decision",
        /commit mockd\.json/.test(await p.locator("#toast").textContent()),
        await p.locator("#toast").textContent());

  // the CLI agrees — this is the point of the whole change
  const shown = cli("show");
  check("the CLI resolves to the same document", /specs\/fetched\.json/.test(shown),
        shown.split("\n")[0]);

  // The fault this pins: setting the project spec used to leave the running
  // mock alone, so Overview kept reporting the old document's coverage.
  const covOf = async () => await p.evaluate(async () => {
    const d = await (await fetch("/api/coverage")).json();
    return `${d.safe_to_build_on}/${d.total}`;
  });
  await p.evaluate(async () => { await fetch("/api/project", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ spec: "apis.json" }) }); });
  await p.waitForTimeout(6000);
  const before = await covOf();
  await p.evaluate(async () => { await fetch("/api/project", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ spec: "specs/23_sept.json" }) }); });
  await p.waitForTimeout(6000);
  const after = await covOf();
  check("changing the project spec changes what Overview reports",
        before !== after, `${before} -> ${after}`);
  check("and the running mock follows it", await p.evaluate(async () =>
        (await (await fetch("/api/state")).json()).options.spec === "specs/23_sept.json"));
  await p.evaluate(async () => { await fetch("/api/project", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ spec: "apis.json" }) }); });
  await p.waitForTimeout(6000);

  // a module pinned elsewhere shows as an exception, and can be released
  cli("use", "apis.json", "--for", "tests");
  await p.reload({ waitUntil: "networkidle" });
  await p.waitForTimeout(1500);
  await p.locator('nav.side a[data-view="source"]').click();
  await p.waitForTimeout(900);
  check("a pinned module is visible as an exception",
        /1 module\(s\) deliberately pinned/.test(
          await p.locator("#projModules").textContent()),
        await p.locator("#projModules").textContent());
  check("and offers to unpin it",
        await p.locator('#projModules [data-unpin="tests"]').count() === 1);
  await p.locator('#projModules [data-unpin="tests"]').click();
  await p.waitForTimeout(1200);
  check("unpinning returns it to the project spec",
        /Everything follows the project spec/.test(
          await p.locator("#projModules").textContent()));

  cli("use", "apis.json");
  console.log(`\n${pass} passed, ${fail} failed`);
  console.log(errs.length ? "JS ERRORS:\n  " + errs.join("\n  ") : "no JS errors");
  await b.close(); process.exit(fail || errs.length ? 1 : 0);
})();
