/**
 * load.js — running the tests under load, from the Tests screen.
 *
 * Always against the mock: it is local, so the load goes nowhere. The suite
 * opens the panel, starts a short run, watches it count, and reads the result
 * — a sentence and one row per endpoint.
 */
const { chromium } = require("playwright");

let pass = 0, fail = 0; const errs = [];
const check = (n, ok, d) => { ok ? pass++ : fail++;
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${n}${ok || !d ? "" : "  — " + String(d).slice(0, 300)}`); };

(async () => {
  const b = await chromium.launch();
  const p = await b.newPage();
  p.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  p.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text()); });
  await p.goto("http://localhost:4100", { waitUntil: "networkidle" });
  const flat = (s) => (s || "").replace(/\s+/g, " ").trim();

  await p.locator('nav.side a[data-view="tests"]').click();
  await p.waitForFunction(() => document.querySelectorAll("#libList .librow").length > 0, null, { timeout: 15000 }).catch(() => {});
  await p.locator("#libEnv").selectOption("mock");

  check("Tests has one button for running under load", await p.locator("#loadOpen").isVisible());
  check("and nothing else about it until asked", await p.locator("#loadPanel").isHidden());
  await p.locator("#loadOpen").click();
  check("the panel asks two things: how many callers, and for how long",
        await p.locator("#loadPanel").isVisible()
        && await p.locator("#loadPanel input").count() === 2);
  const note = flat(await p.locator("#loadNote").textContent());
  check("and says which tests it will use and where", /only read, on mock/.test(note)
        && /create or change things are left out/.test(note), note);
  check("the mock needs no second thought", !/real load/.test(note));

  await p.locator("#libModule").selectOption("baseline");
  await p.locator("#loadUsers").fill("3");
  await p.locator("#loadSeconds").fill("4");
  await p.locator("#loadStart").click();
  await p.waitForFunction(() => /calls so far/.test((document.querySelector("#loadProgress") || {}).textContent || ""),
                          null, { timeout: 10000 }).catch(() => {});
  check("Start shows it counting", /calls so far on mock/.test(await p.locator("#loadOut").textContent()),
        flat(await p.locator("#loadOut").textContent()));
  check("and the button becomes Stop", (await p.locator("#loadStart").textContent()).trim() === "Stop");

  await p.waitForFunction(() => !!document.querySelector("#loadSentence"), null, { timeout: 40000 }).catch(() => {});
  const said = flat(await p.locator("#loadSentence").textContent().catch(() => ""));
  check("when it ends, the result is a sentence",
        /mock: [\d,]+ calls in [\d.]+ s with 3 at once/.test(said) && /for 95% of calls/.test(said), said);
  check("which says that nothing failed", /None failed/.test(said), said);
  const rows = await p.locator("#loadOut table tr").count();
  check("with one row per endpoint", rows >= 3, String(rows));
  const head = flat(await p.locator("#loadOut table tr").first().textContent());
  check("giving the usual time and the time 95% of calls came in under",
        /Usual \(ms\)/.test(head) && /95% within \(ms\)/.test(head), head);
  const cells = await p.locator("#loadOut table tr").nth(1).locator("td").allTextContents();
  check("in numbers", cells.length === 6 && cells.slice(1).every((c) => /^[\d,]+$/.test(c.trim())), cells.join(" | "));
  check("only reads were repeated", !(await p.locator("#loadOut table code").allTextContents())
        .some((c) => /^(POST|PUT|PATCH|DELETE) /.test(c)));
  check("the button is Start again", (await p.locator("#loadStart").textContent()).trim() === "Start");

  await p.locator("#loadClose").click();
  check("Close puts the panel away", await p.locator("#loadPanel").isHidden());

  console.log(`\n${pass} passed, ${fail} failed`);
  console.log(errs.length ? "JS ERRORS:\n  " + errs.join("\n  ") : "no JS errors");
  await b.close(); process.exit(fail || errs.length ? 1 : 0);
})();
