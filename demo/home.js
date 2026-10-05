/**
 * home.js — the first screen, and the sidebar around it.
 *
 * Somebody opening this for the first time should be able to tell, without
 * reading documentation, whether things are set up and what to do next. So the
 * suite checks the screen says exactly that, that each line's button goes
 * somewhere useful, and that the sidebar stays short.
 */
const { chromium } = require("playwright");

let pass = 0, fail = 0; const errs = [];
const check = (n, ok, d) => { ok ? pass++ : fail++;
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${n}${ok || !d ? "" : "  — " + d}`); };

(async () => {
  const b = await chromium.launch();
  const p = await b.newPage();
  p.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  p.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text()); });
  await p.goto("http://localhost:4100", { waitUntil: "networkidle" });
  await p.waitForFunction(() => document.querySelectorAll("#homeList .homerow").length > 0,
                          null, { timeout: 20000 }).catch(() => {});

  // ------------------------------------------------------------ sidebar
  const visibleLinks = await p.evaluate(() =>
    [...document.querySelectorAll("nav.side a")].filter((a) => a.offsetParent !== null)
      .map((a) => a.textContent.trim().replace(/\s+\S*$/, (m) => /\d|%|pinned/.test(m) ? "" : m)));
  check("the sidebar shows five places and no more", visibleLinks.length === 5,
        visibleLinks.join(" | "));
  check("they are named for what you do there",
        ["Home", "Create tests", "Tests"].every((n) => visibleLinks.some((v) => v.startsWith(n))),
        visibleLinks.join(" | "));
  check("the rest is folded away", await p.locator("#navMoreItems").isHidden());

  // --------------------------------------------------------------- home
  check("the console opens on Home", await p.locator('.view[data-view="home"]').isVisible());
  check("it says where each thing stands", await p.locator("#homeList .homerow").count() === 5);
  const rows = await p.locator("#homeList .homerow").allTextContents();
  check("the API document is named", /API document loaded/.test(rows[0]) && /operations|\.json/.test(rows[0]),
        rows[0].replace(/\s+/g, " ").slice(0, 90));
  check("the mock's state is stated", /Mock server is (running|stopped)/.test(rows[1]),
        rows[1].replace(/\s+/g, " ").slice(0, 90));
  check("the baseline is counted", /\d+ baseline tests ready|not made yet/.test(rows[2]),
        rows[2].replace(/\s+/g, " ").slice(0, 90));
  check("every line has one button", await p.locator("#homeList .homerow button").count() === 5);
  const next = (await p.locator("#homeNext").textContent()).replace(/\s+/g, " ").trim();
  check("one next step is singled out", /Next:|Everything is in place/.test(next), next.slice(0, 100));
  check("with a single main button for it",
        await p.locator("#homeNext button.primary").count() === 1);

  // ------------------------------------------------- the buttons go places
  await p.locator('#homeList .homerow[data-home="tests"] button').click();
  await p.waitForTimeout(600);
  check("the tests line leads to Create tests",
        await p.locator('.view[data-view="create"]').isVisible());

  await p.locator('nav.side a[data-view="home"]').click();
  await p.waitForFunction(() => document.querySelectorAll("#homeList .homerow").length > 0,
                          null, { timeout: 15000 }).catch(() => {});
  await p.locator('#homeList .homerow[data-home="servers"] button').click();
  await p.waitForTimeout(600);
  check("the servers line leads to Servers",
        await p.locator('.view[data-view="environments"]').isVisible());

  await p.locator('nav.side a[data-view="home"]').click();
  await p.waitForTimeout(800);
  await p.locator('#homeList .homerow[data-home="spec"] button').click();
  await p.waitForTimeout(600);
  check("the document line leads to the API document",
        await p.locator('.view[data-view="source"]').isVisible());

  // ------------------------------------------- the two buttons that act
  await p.locator('nav.side a[data-view="home"]').click();
  await p.waitForFunction(() => document.querySelectorAll("#homeList .homerow").length > 0,
                          null, { timeout: 15000 }).catch(() => {});
  if (/baseline tests ready/.test(await p.locator('#homeList .homerow[data-home="baseline"]').textContent())) {
    await p.locator('#homeList .homerow[data-home="baseline"] button').click();
    await p.waitForFunction(() => /passed on/.test(
      (document.querySelector("#baseCount") || {}).textContent || ""),
      null, { timeout: 90000 }).catch(() => {});
    check("Run them takes you to Tests and runs the baseline",
          await p.locator('.view[data-view="tests"]').isVisible()
          && /\d+ of \d+ passed on/.test(await p.locator("#baseCount").textContent()),
          await p.locator("#baseCount").textContent());
  }

  // stop the mock, and Home should make starting it the next step
  await p.evaluate(async () => { await fetch("/api/stop", { method: "POST" }); });
  await p.locator('nav.side a[data-view="home"]').click();
  await p.waitForFunction(() => /stopped/.test(
    (document.querySelector('#homeList .homerow[data-home="mock"]') || {}).textContent || ""),
    null, { timeout: 15000 }).catch(() => {});
  check("with the mock stopped, Home says so",
        /Mock server is stopped/.test(await p.locator('#homeList .homerow[data-home="mock"]').textContent()));
  check("and makes starting it the next step",
        /Start the mock/.test(await p.locator("#homeNext").textContent()),
        (await p.locator("#homeNext").textContent()).replace(/\s+/g, " ").slice(0, 80));
  await p.locator("#homeNext button.primary").click();
  await p.waitForFunction(() => /Mock server is running/.test(
    (document.querySelector('#homeList .homerow[data-home="mock"]') || {}).textContent || ""),
    null, { timeout: 60000 }).catch(() => {});
  check("pressing it starts the mock, and Home shows it running",
        /Mock server is running/.test(await p.locator('#homeList .homerow[data-home="mock"]').textContent()),
        (await p.locator('#homeList .homerow[data-home="mock"]').textContent()).replace(/\s+/g, " ").slice(0, 90));

  // ---------------------------------------------------------------- More
  await p.locator("#navMore").click();
  await p.waitForTimeout(300);
  check("More opens the lesser screens", await p.locator('nav.side a[data-view="explore"]').isVisible());
  await p.locator('nav.side a[data-view="explore"]').click();
  await p.waitForTimeout(600);
  check("and they still work", await p.locator('.view[data-view="explore"]').isVisible());
  await p.locator("#navMore").click();
  await p.waitForTimeout(300);
  check("More folds away again", await p.locator("#navMoreItems").isHidden());

  // a deep link to a folded screen must not strand the reader
  await p.goto("http://localhost:4100/#server", { waitUntil: "networkidle" });
  await p.waitForTimeout(1200);
  check("opening a folded screen by its address shows it",
        await p.locator('.view[data-view="server"]').isVisible());
  check("and unfolds More so it is marked as the current one",
        await p.locator('nav.side a[data-view="server"]').isVisible());

  console.log(`\n${pass} passed, ${fail} failed`);
  console.log(errs.length ? "JS ERRORS:\n  " + errs.join("\n  ") : "no JS errors");
  await b.close(); process.exit(fail || errs.length ? 1 : 0);
})();
