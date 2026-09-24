/**
 * wbasserts.js — a step must be able to assert more than "it answered".
 *
 * The workbench gave every step a default status check and no way to add
 * another, which makes a flow that proves the endpoint is reachable and
 * nothing about what it returned. The editor here is the SAME component the
 * save dialog uses, so an assertion means the same thing wherever it is
 * written — a second, subtly different editor is how two places drift.
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
  await p.waitForTimeout(1200);
  await p.locator("#wbToggle").click();
  await p.waitForTimeout(1500);

  check("every step has an assertions section",
        await p.locator('[data-asserts="0"]').count() === 1);
  await p.locator('[data-asserts="0"] summary').click();
  await p.waitForTimeout(400);
  check("it uses the same editor as the save dialog",
        await p.locator('[data-ahost="0"] .add-assert').count() === 1);

  // choosing an operation should propose what the spec documents
  const listOp = await p.evaluate(async () => {
    const d = await (await fetch("/api/routes")).json();
    const r = (d.routes || d).find((x) => x.method === "GET" && !/\{/.test(x.path));
    return r ? r.path : "";
  });
  await p.locator('[data-method="0"]').selectOption("GET"); await p.waitForTimeout(600);
  await p.locator('[data-op="0"]').selectOption(listOp); await p.waitForTimeout(1800);
  const proposed = await p.evaluate(() => WB.steps[0].assertions);
  check("picking an operation proposes a status the spec documents",
        JSON.stringify(proposed).includes("status"), JSON.stringify(proposed));

  // add a real assertion of our own
  await p.locator('[data-asserts="0"] summary').click(); await p.waitForTimeout(400);
  await p.locator('[data-ahost="0"] .add-assert').click(); await p.waitForTimeout(500);
  const rows = await p.locator('[data-ahost="0"] .arow2').count();
  check("an assertion can be added", rows >= 2, String(rows));

  const last = p.locator('[data-ahost="0"] .arow2').last();
  await last.locator(".a-type").selectOption("jsonpath");
  await last.locator(".a-path").fill("status_code");
  await last.locator(".a-op").selectOption("equals");
  await last.locator(".a-value").fill("200");
  await p.waitForTimeout(300);

  await p.locator('[data-path="0"]').fill(listOp);
  await p.locator('[data-run="0"]').click();
  await p.waitForTimeout(3000);

  const kept = await p.evaluate(() => WB.steps[0].assertions);
  check("what was typed is what runs",
        JSON.stringify(kept).includes("status_code"), JSON.stringify(kept));
  const panel = await p.locator('.wbstep[data-i="0"]').textContent();
  check("both assertions are reported", (panel.match(/PASS/g) || []).length >= 2,
        String((panel.match(/PASS/g) || []).length));

  // a failing assertion must show as failing, not be quietly dropped
  await p.locator('[data-ahost="0"] .arow2').last().locator(".a-value").fill("999");
  await p.waitForTimeout(300);
  await p.locator('[data-run="0"]').click();
  await p.waitForTimeout(3000);
  const after = await p.locator('.wbstep[data-i="0"]').textContent();
  check("a wrong assertion fails the step", /FAIL/.test(after),
        after.replace(/\s+/g, " ").slice(-130));
  check("and the step is marked failed",
        await p.locator(".wbstep.ran-fail").count() === 1);

  // --- asserting on a value should not require knowing the path ---
  await p.locator('[data-method="0"]').selectOption("GET"); await p.waitForTimeout(500);
  const listPath = await p.evaluate(async () => {
    const d = await (await fetch("/api/routes")).json();
    const r = (d.routes || d).find((x) => x.method === "GET" && /list|countries|cities/i.test(x.path)
                                          && !/\{/.test(x.path));
    return r ? r.path : "";
  });
  if (listPath) {
    await p.locator('[data-path="0"]').fill(listPath);
    await p.locator('[data-run="0"]').click();
    await p.waitForTimeout(3000);

    check("every value offers to be asserted on",
          await p.locator("[data-assert]").count() > 0);
    const countBtn = p.locator('.wbfield:has(.id:text("COUNT")) [data-assert]').first();
    if (await countBtn.count()) {
      // the field list has its own scroll box, so the row may sit out of view
      await countBtn.scrollIntoViewIfNeeded();
      await countBtn.click();
      await p.waitForTimeout(1200);
      const added = await p.evaluate(() => WB.steps[0].assertions);
      const lengthOne = added.find((a) => a.op === "length_gte");
      check("asserting on a COUNT writes length_gte, which nobody guesses",
            !!lengthOne, JSON.stringify(added));
      check("and it defaults to at least one",
            lengthOne && lengthOne.value === 1, JSON.stringify(lengthOne || {}));
      await p.locator('[data-run="0"]').click();
      await p.waitForTimeout(2500);
      // ask the step itself, not a page-wide class count that an earlier
      // deliberate failure can still be colouring
      const checks = await p.evaluate(() => (WB.ran[0] || {}).checks || []);
      const lengthCheck = checks.find((c) => /length_gte/.test(c.label));
      check("and that assertion passes against the real response",
            !!lengthCheck && lengthCheck.ok === true, JSON.stringify(checks));
    }
  }

  // and they survive into the saved flow
  await p.locator('[data-ahost="0"] .arow2').last().locator(".a-value").fill("200");
  await p.waitForTimeout(300);
  const id = "wbassert-" + Date.now().toString(36);
  await p.locator("#wbModule").fill("workbench-probe");
  await p.locator("#wbId").fill(id);
  await p.locator("#wbSave").click();
  await p.waitForTimeout(2000);
  const saved = await p.evaluate(async (args) => {
    const q = new URLSearchParams({ suite: args.suite, id: args.id, stage: "draft" });
    return await (await fetch("/api/tests/one?" + q)).json();
  }, { suite: "workbench-probe", id });
  const def = saved.test || saved;
  check("the saved step keeps every assertion",
        JSON.stringify(def).includes("status_code"),
        JSON.stringify(def).slice(0, 150));

  console.log(`\n${pass} passed, ${fail} failed`);
  console.log(errs.length ? "JS ERRORS:\n  " + errs.join("\n  ") : "no JS errors");
  await b.close(); process.exit(fail || errs.length ? 1 : 0);
})();
