/**
 * toast.js — feedback must be visible on the view that raised it.
 *
 * The bug this pins down: banner() used to write into #respHead, which lives
 * inside the Explore view. Every message raised from Source, Tests, Server or
 * Environments rendered into a hidden element on a page the user was not on,
 * so a failed spec fetch looked like the button did nothing at all.
 */
const { chromium } = require("playwright");

const http = require("http");

// A reachable host is needed to prove a fetch result reaches the screen. It
// used to be a public sample API, which made this suite fail whenever that
// service was slow — a test that depends on someone else's uptime reports
// their outage as your bug. Serve it here instead.
const FIXTURE_SPEC = JSON.stringify({
  openapi: "3.1.0",
  info: { title: "Toast fixture", version: "1.0.0" },
  paths: { "/ping": { get: { responses: { 200: { description: "ok" } } } } },
});

let pass = 0, fail = 0; const errs = [];
const check = (n, ok, d) => { ok ? pass++ : fail++;
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${n}${ok || !d ? "" : "  — " + d}`); };

(async () => {
  const fixture = await new Promise((resolve) => {
    const srv = http.createServer((req, res) => {
      if (req.url.split("?")[0] === "/openapi.json") {
        res.writeHead(200, { "Content-Type": "application/json" });
        return res.end(FIXTURE_SPEC);
      }
      res.writeHead(404, { "Content-Type": "application/json" });
      res.end('{"detail":"nope"}');
    });
    srv.listen(0, "127.0.0.1", () =>
      resolve({ srv, base: `http://127.0.0.1:${srv.address().port}` }));
  });
  const HOST = fixture.base;

  const b = await chromium.launch();
  const p = await b.newPage();
  p.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  // this suite fetches bad URLs on purpose — a 400 from the console is the
  // behaviour under test, not a page fault
  p.on("console", (m) => {
    if (m.type() === "error" && !/status of 400/.test(m.text())) {
      errs.push("console: " + m.text());
    }
  });
  await p.goto("http://localhost:4100", { waitUntil: "networkidle" });
  await p.waitForTimeout(2000);

  // the toast lives in the shell, so it is reachable from every view
  for (const v of ["overview", "authoring", "source", "connect", "explore", "tests",
                   "environments", "server"]) {
    await p.locator(`nav.side a[data-view="${v}"]`).click();
    await p.waitForTimeout(250);
    await p.evaluate(() => banner("ok", "probe"));
    const seen = await p.locator("#toast").isVisible();
    check(`feedback is visible on ${v}`, seen);
    await p.evaluate(() => hideBanner());
  }

  // --- the actual reported symptom, on the view it was reported from ---
  await p.locator('nav.side a[data-view="source"]').click();
  await p.waitForTimeout(600);

  await p.locator("#specUrl").fill(HOST.replace(/^https?:\/\//, "") + "/openapi.json");
  await p.locator("#btnSpecFetch").click(); await p.waitForTimeout(900);
  check("a URL with no scheme says so, on screen",
        /scheme/i.test(await p.locator("#toast").textContent()),
        await p.locator("#toast").textContent());
  check("the button is still usable", !(await p.locator("#btnSpecFetch").isDisabled()));

  await p.locator("#specUrl").fill(HOST + "/definitely-not-here");
  await p.locator("#btnSpecFetch").click(); await p.waitForTimeout(6000);
  // which status the upstream gives varies; that it reaches the screen must not
  check("an upstream failure is reported on screen",
        /HTTP \d{3}|not a usable|failed/i.test(await p.locator("#toast").textContent()),
        await p.locator("#toast").textContent());
  check("the button recovers after a failure",
        !(await p.locator("#btnSpecFetch").isDisabled()));

  // a real fetch: the candidate card appears AND the message is readable
  await p.locator("#specUrl").fill(HOST + "/openapi.json");
  await p.locator("#specSaveAs").fill("toast-probe.json");
  await p.locator("#btnSpecFetch").click(); await p.waitForTimeout(9000);
  check("a good fetch reports operations",
        /operation/.test(await p.locator("#toast").textContent()),
        await p.locator("#toast").textContent());
  check("and shows the candidate card", await p.locator("#candidateCard").isVisible());

  // errors persist, confirmations do not
  await p.evaluate(() => banner("err", "sticky"));
  await p.waitForTimeout(7000);
  check("an error stays until dismissed", await p.locator("#toast").isVisible());
  await p.locator("#toastClose").click(); await p.waitForTimeout(200);
  check("the close button dismisses it", !(await p.locator("#toast").isVisible()));

  console.log(`\n${pass} passed, ${fail} failed`);
  console.log(errs.length ? "JS ERRORS:\n  " + errs.join("\n  ") : "no JS errors");
  await b.close(); fixture.srv.close();
  process.exit(fail || errs.length ? 1 : 0);
})();
