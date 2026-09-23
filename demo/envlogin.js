/**
 * envlogin.js — "Test login" must lead somewhere.
 *
 * It used to report the first unresolved variable as a raw resolver message
 * telling you to export a shell variable or hand-edit a file — while the
 * button that writes that file sat on the same page. And it named one variable
 * at a time, so a five-variable environment meant five round trips for one
 * piece of information.
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
  await p.locator('nav.side a[data-view="environments"]').click();
  await p.waitForTimeout(1200);

  // every variable the login needs, in one answer
  const api = await p.evaluate(async () => (await (await fetch("/api/env-login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: "dev" }) })).json()));
  check("the failure names every missing variable at once",
        (api.unresolved || []).length >= 2, JSON.stringify(api.unresolved));
  check("including the password", (api.unresolved || []).includes("DEV_PASSWORD"));
  check("and the username", (api.unresolved || []).includes("DEV_USERNAME"));
  check("but NOT the test-data ids, which logging in does not need",
        !(api.unresolved || []).some((v) => /USER_ID|DEPARTMENT_ID/.test(v)),
        JSON.stringify(api.unresolved));
  check("the message points at Configure, not at a text editor",
        /Configure/.test(api.error) && !/Export it in your shell/.test(api.error),
        api.error);

  // pressing the button opens the dialog that fixes it
  await p.locator("#liveEnv").selectOption("dev");
  await p.waitForTimeout(400);
  await p.locator("#btnEnvLogin").click();
  await p.waitForTimeout(1500);
  check("pressing Test login opens Configure", await p.locator("#envDlg").isVisible());
  check("with fields for the missing values",
        await p.locator("#envDlgFields input").count() > 0);
  check("and says where they are written",
        /\.env/.test(await p.locator("#envDlgWhere").textContent()));
  await p.locator("#envDlgCancel").click();

  // an environment needing nothing still reports success
  const ok = await p.evaluate(async () => (await (await fetch("/api/env-login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: "mock" }) })).json()));
  check("an environment with nothing to resolve still passes", ok.ok === true,
        JSON.stringify(ok).slice(0, 80));

  console.log(`\n${pass} passed, ${fail} failed`);
  console.log(errs.length ? "JS ERRORS:\n  " + errs.join("\n  ") : "no JS errors");
  await b.close(); process.exit(fail || errs.length ? 1 : 0);
})();
