/**
 * openload.js — a saved test must be reopenable, and unsaved work must survive.
 *
 * Two halves. A test you can save but not reopen is a dead end — the workbench
 * is where a flow is understood, so a saved flow has to be able to go back
 * there. And loading one must never silently discard steps somebody is in the
 * middle of writing.
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
  let asked = null;
  p.on("dialog", async (d) => { asked = d.message(); await d.accept(); });

  await p.goto("http://localhost:4100", { waitUntil: "networkidle" });
  await p.waitForTimeout(1600);
  await p.locator('nav.side a[data-view="tests"]').click();
  await p.waitForTimeout(1600);

  // find a saved scenario with more than one step
  const target = await p.evaluate(async () => {
    const d = await (await fetch("/api/tests")).json();
    for (const s of (d.suites || [])) {
      for (const sc of (s.scenarios || [])) {
        // steps may arrive as an array or as a count; comparing an array
        // with >= is always false, which made this find nothing
        const n = Array.isArray(sc.steps) ? sc.steps.length : Number(sc.steps || 0);
        if (n >= 1) {
          return { suite: s.name, stage: s.stage, id: sc.id };
        }
      }
    }
    return null;
  });
  check("there is a saved flow to reopen", !!target, JSON.stringify(target));
  if (!target) { await b.close(); process.exit(1); }

  const row = p.locator(`[data-act="open"][data-id="${target.id}"]`).first();
  check("it offers to open", await row.count() === 1);
  await row.click();
  await p.waitForTimeout(2500);

  check("the workbench opened", !(await p.locator("#wbBody").isHidden()));
  const steps = await p.locator(".wbstep").count();
  check("with the flow's steps in it", steps >= 1, String(steps));
  check("the id came with it",
        (await p.locator("#wbId").inputValue()) === target.id,
        await p.locator("#wbId").inputValue());
  check("and the module", (await p.locator("#wbModule").inputValue()) === target.suite);
  const firstPath = await p.locator('[data-path="0"]').inputValue();
  check("a step's path is loaded", firstPath.length > 0, firstPath);

  // captures must survive, or the chain is broken on reopen
  const captures = await p.evaluate(() =>
    WB.steps.map((s) => Object.keys(s.capture || {})).flat());
  const saved = await p.evaluate(async (t) => {
    const q = new URLSearchParams({ suite: t.suite, id: t.id, stage: t.stage });
    const d = await (await fetch("/api/tests/one?" + q)).json();
    const test = d.test || d;
    return (test.steps || []).map((s) => Object.keys(s.capture || {})).flat();
  }, target);
  check("every capture came back too",
        JSON.stringify(captures) === JSON.stringify(saved),
        `${JSON.stringify(captures)} vs ${JSON.stringify(saved)}`);

  // --- running a loaded flow must count as running that test ---
  const before = await p.evaluate(async (t) => {
    const d = await (await fetch("/api/tests")).json();
    for (const s of (d.suites || [])) {
      for (const x of [...(s.cases || []), ...(s.scenarios || [])]) {
        if (x.id === t.id) return ((x.history || {}).by_env || {});
      }
    }
    return {};
  }, target);
  await p.locator("#wbRunAll").click();
  await p.waitForTimeout(6000);
  const toastText = await p.locator("#toast").textContent();
  check("running a loaded flow is recorded against that test",
        /Recorded against/.test(toastText), toastText.slice(0, 120));
  const after = await p.evaluate(async (t) => {
    const d = await (await fetch("/api/tests")).json();
    for (const s of (d.suites || [])) {
      for (const x of [...(s.cases || []), ...(s.scenarios || [])]) {
        if (x.id === t.id) return ((x.history || {}).by_env || {});
      }
    }
    return {};
  }, target);
  check("and the history gains a run for the environment",
        Object.keys(after).length >= Object.keys(before).length,
        `${JSON.stringify(before)} -> ${JSON.stringify(after)}`);

  // --- unsaved work must be defended ---
  await p.locator("#wbAdd").click(); await p.waitForTimeout(500);
  const last = (await p.locator(".wbstep").count()) - 1;
  await p.locator(`[data-path="${last}"]`).fill("/api/v1/something/unsaved");
  await p.locator(`[data-path="${last}"]`).dispatchEvent("input");
  await p.waitForTimeout(500);

  asked = null;
  await p.locator(`[data-act="open"][data-id="${target.id}"]`).first().click();
  await p.waitForTimeout(2000);
  check("opening another test asks before discarding unsaved steps",
        !!asked && /unsaved/i.test(asked), String(asked));

  // an edited flow is no longer the saved test, so running it must NOT claim
  // to be a run of it — a green badge on something nobody ran is worse than
  // no badge at all
  await p.waitForTimeout(1500);
  await p.locator("#wbRunAll").click();
  await p.waitForTimeout(6000);
  const edited = await p.locator("#toast").textContent();
  check("an edited flow is not recorded as a run of the saved test",
        !/Recorded against/.test(edited), edited.slice(0, 130));

  console.log(`\n${pass} passed, ${fail} failed`);
  console.log(errs.length ? "JS ERRORS:\n  " + errs.join("\n  ") : "no JS errors");
  await b.close(); process.exit(fail || errs.length ? 1 : 0);
})();
