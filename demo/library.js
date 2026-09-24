/**
 * library.js — a growing suite must stay readable, and must be able to leave.
 *
 * Three things a list of tests needs once there are more than a handful:
 * collapse, so one big suite does not push everything else off the screen;
 * pagination, so a hundred tests are not all drawn at once; and export, or the
 * tool is somewhere tests go to be trapped.
 */
const { chromium } = require("playwright");

let pass = 0, fail = 0; const errs = [];
const check = (n, ok, d) => { ok ? pass++ : fail++;
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${n}${ok || !d ? "" : "  — " + d}`); };

(async () => {
  const b = await chromium.launch();
  const p = await b.newPage();
  p.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  p.on("console", (m) => { if (m.type() === "error" && !/status of 4\d\d/.test(m.text()))
                             errs.push("console: " + m.text()); });

  // a suite big enough to need both collapsing and paging
  const many = [];
  for (let i = 0; i < 30; i++) {
    many.push({ id: `bulk-${i}`, name: `bulk case ${i}`, levels: ["regression"],
                request: { method: "GET", path: "/api/v1/user/list" },
                assertions: [{ type: "status", equals: 200 }] });
  }
  await p.goto("http://localhost:4100", { waitUntil: "networkidle" });
  await p.waitForTimeout(1500);
  await p.evaluate(async (tests) => {
    await fetch("/api/tests/import", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tests, suite: "bulk-probe", stage: "draft" }) });
  }, many);

  await p.locator('nav.side a[data-view="tests"]').click();
  await p.waitForTimeout(1800);

  const bulk = p.locator('.suite:has(.name:text("bulk-probe"))');
  check("the big suite is listed", await bulk.count() === 1);
  check("and starts collapsed, not filling the page",
        await bulk.locator(".titem").count() === 0,
        String(await bulk.locator(".titem").count()));
  check("its header says how many it holds",
        /30 test\(s\)/.test(await bulk.locator("header").textContent()));

  await bulk.locator("header.tfold").click();
  await p.waitForTimeout(900);
  const shown = await bulk.locator(".titem").count();
  check("opening it shows tests", shown > 0, String(shown));
  check("but only one page of them", shown <= 25, `${shown} drawn`);
  check("and it says which page", /showing 1–25 of 30/.test(await bulk.textContent()),
        (await bulk.textContent()).slice(-90));

  await bulk.locator('[data-page]').last().click();
  await p.waitForTimeout(900);
  check("the next page shows the rest",
        /showing 26–30 of 30/.test(await p.locator('.suite:has(.name:text("bulk-probe"))')
          .textContent()));

  const small = p.locator('.suite:has(.name:text("departments"))');
  if (await small.count()) {
    check("a small suite is open by default, needing no clicks",
          await small.locator(".titem").count() > 0);
  }

  // ------------------------------------------------------------- export
  check("there is a way to export", await p.locator("#btnExport").count() === 1);
  const exported = await p.evaluate(async () =>
    await (await fetch("/api/tests/export?suite=bulk-probe")).json());
  check("an export carries the suite", (exported.suites || []).length === 1,
        JSON.stringify(Object.keys(exported)));
  check("with every test in it",
        (exported.suites[0].cases || []).length === 30,
        String((exported.suites[0].cases || []).length));
  check("and says which spec it belongs to", !!exported.spec, String(exported.spec));
  check("and how to bring it back", /import/i.test(exported._how || ""));

  // the round trip is the point: what comes out must go back in
  const back = await p.evaluate(async (doc) =>
    await (await fetch("/api/tests/import", { method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tests: doc, suite: "bulk-return", stage: "draft" }) })).json(),
    exported);
  check("an exported file imports straight back", back.ok === true,
        JSON.stringify(back).slice(0, 110));
  check("with nothing lost", back.imported === 30, String(back.imported));

  // ------------------------------------------------- more like these
  await p.locator("#btnStory").click(); await p.waitForTimeout(400);
  await p.locator("#genMoreGo").click(); await p.waitForTimeout(2500);
  const brief = await p.locator("#genOut").textContent();
  check("a brief can ask for more like the existing tests",
        /WHAT THIS PROJECT'S TESTS LOOK LIKE/.test(brief));
  check("it shows real examples", /"assertions"/.test(brief));
  check("it names the gap rather than asking vaguely for more",
        /NOTHING COVERS YET|Every operation is touched/.test(brief));
  check("and warns which ids are taken", /IDS ALREADY TAKEN/.test(brief));

  // delete removes one test at a time, so sweep each suite's ids
  await p.evaluate(async () => {
    const all = await (await fetch("/api/tests")).json();
    for (const suite of (all.suites || [])) {
      if (!/^bulk-(probe|return)$/.test(suite.name)) continue;
      const ids = [...(suite.cases || []), ...(suite.scenarios || [])].map((t) => t.id);
      for (const id of ids) {
        await fetch("/api/tests/delete", { method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ suite: suite.name, stage: "draft", id }) });
      }
    }
  });

  console.log(`\n${pass} passed, ${fail} failed`);
  console.log(errs.length ? "JS ERRORS:\n  " + errs.join("\n  ") : "no JS errors");
  await b.close(); process.exit(fail || errs.length ? 1 : 0);
})();
