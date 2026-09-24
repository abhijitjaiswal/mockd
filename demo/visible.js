/**
 * visible.js — the things that were computed but never shown.
 *
 * Two of them. Every failure already carried an attribution — whose problem it
 * is — computed on every run and printed only to the terminal, so the console
 * showed a red row and swallowed the most useful sentence. And the values that
 * resolve at run time existed only as a placeholder hint, which is the same as
 * not existing: people wrote constants, and the second run collided with the
 * first.
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
  await p.goto("http://localhost:4100", { waitUntil: "networkidle" });
  await p.waitForTimeout(1800);
  await p.locator('nav.side a[data-view="tests"]').click();
  await p.waitForTimeout(1600);

  // ---------------------------------------------------- fault attribution
  await p.locator("#btnRunTests").click();
  await p.waitForTimeout(18000);

  const report = await p.evaluate(async () => {
    const jobs = await (await fetch("/api/tests")).json();
    return jobs ? true : false;
  });
  check("a run completes", report);

  const judged = await p.evaluate(() => !document.getElementById("testVerdicts").hidden);
  const anyVerdict = await p.evaluate(async () => {
    // is there anything to attribute at all in this project right now?
    const r = await (await fetch("/api/tests")).json();
    return (r.suites || []).some((s) => [...(s.cases || []), ...(s.scenarios || [])]
      .some((t) => (t.history || {}).last_outcome !== "pass"));
  });
  if (anyVerdict) {
    check("a failure's attribution is shown, not only printed", judged,
          "testVerdicts stayed hidden");
    if (judged) {
      const text = await p.locator("#testVerdicts").textContent();
      check("it says whose problem it is", /Whose problem is it/.test(text));
      check("and names a category",
            /spec|backend|environment|assumes/i.test(text), text.slice(0, 120));
      check("and what to do next", /Next:/.test(text) || text.length > 40,
            text.slice(0, 120));
    }
  } else {
    check("nothing failed, so nothing is attributed", !judged);
  }

  // ------------------------------------------------------ dynamic values
  await p.locator("#wbToggle").click(); await p.waitForTimeout(1600);
  await p.locator('[data-method="0"]').selectOption("POST");
  await p.waitForTimeout(800);

  const chips = await p.locator("[data-insert]").count();
  check("run-time values are offered, not just hinted at", chips > 0, String(chips));
  const labels = await p.locator("[data-insert]").allTextContents();
  check("including a per-run id", labels.some((l) => /runId/.test(l)), labels.join(" "));
  check("and a fresh uuid", labels.some((l) => /uuid/.test(l)), labels.join(" "));
  const tip = await p.locator('[data-insert]').first().getAttribute("title");
  check("each says what it is for", !!tip && tip.length > 20, String(tip));

  await p.locator('[data-body="0"]').fill('{"title": ""}');
  await p.locator('[data-body="0"]').click();
  // put the cursor inside the quotes
  await p.evaluate(() => {
    const f = document.querySelector('[data-body="0"]');
    const at = f.value.indexOf('""') + 1;
    f.setSelectionRange(at, at);
  });
  await p.locator('[data-insert$="|$uuid"]').first().click();
  await p.waitForTimeout(600);
  const body = await p.locator('[data-body="0"]').inputValue();
  check("clicking one inserts it where the cursor was",
        body.includes('{{$uuid}}') && body.includes('"title"'), body);

  console.log(`\n${pass} passed, ${fail} failed`);
  console.log(errs.length ? "JS ERRORS:\n  " + errs.join("\n  ") : "no JS errors");
  await b.close(); process.exit(fail || errs.length ? 1 : 0);
})();
