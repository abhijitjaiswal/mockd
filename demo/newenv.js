/**
 * newenv.js — creating an environment, and production refusing writes.
 *
 * Adding a target used to mean hand-editing environments.json. The risk in
 * automating it is obvious: somebody types a real URL or password into a form
 * that writes a COMMITTED file. So the form writes only the shape — ${VAR}
 * placeholders — and the values go to the gitignored .env.
 */
const { chromium } = require("playwright");
const EP = require("./endpoints");

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

  check("there is a way to add one", await p.locator("#btnEnvNew").count() === 1);
  await p.locator("#btnEnvNew").click(); await p.waitForTimeout(400);
  check("the form opens", await p.locator("#envNewCard").isVisible());
  check("a login route is asked for only when logging in",
        await p.locator("#envNewLoginWrap").isHidden());
  await p.locator("#envNewMode").selectOption("login"); await p.waitForTimeout(300);
  check("and appears when it is", await p.locator("#envNewLoginWrap").isVisible());

  await p.locator("#envNewName").fill("prod");
  await p.locator("#envNewDesc").fill("Production — reads only");
  await p.locator("#envNewMode").selectOption("cookie"); await p.waitForTimeout(300);
  await p.locator("#envNewReadonly").check();
  await p.locator("#envNewSave").click();
  await p.waitForTimeout(2200);

  const envs = await p.evaluate(async () =>
    (await (await fetch("/api/environments")).json()).environments);
  const prod = (envs || []).find((e) => e.name === "prod");
  check("the environment now exists", !!prod, JSON.stringify(envs || []).slice(0, 80));
  check("and reports what it still needs",
        prod && (prod.unresolved || []).includes("PROD_BASE_URL"),
        JSON.stringify(prod || {}).slice(0, 110));

  // the committed file must hold placeholders only
  const written = await p.evaluate(async () => {
    const r = await fetch("/api/environments/prod/vars");
    return await r.json();
  });
  check("its values are ${VAR} placeholders, not literals",
        (written.fields || []).some((f) => /PROD_/.test(f.name)),
        JSON.stringify(written).slice(0, 120));

  // --- the point of read-only ---
  // EP lives in node; the callback runs in the browser, so pass it across
  const refused = await p.evaluate(async (collection) => {
    const steps = [{ role: "target", name: "create something",
      request: { method: "POST", path: collection,
                 body: { title: "should never happen" } },
      assertions: [{ type: "status", in: [200, 201] }] }];
    return await (await fetch("/api/tests/chain", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target: "prod", steps, upto: 0 }) })).json();
  }, EP.COLLECTION);
  const detail = JSON.stringify(refused);
  check("a write against production is refused", /read-only/.test(detail),
        detail.slice(0, 150));

  console.log(`\n${pass} passed, ${fail} failed`);
  console.log(errs.length ? "JS ERRORS:\n  " + errs.join("\n  ") : "no JS errors");
  await b.close(); process.exit(fail || errs.length ? 1 : 0);
})();
