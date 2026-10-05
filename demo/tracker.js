/**
 * tracker.js — sending a bug report to where the team tracks work.
 *
 * The suite makes a test that fails, runs it, and then does what a person
 * would on the failing row: Send it somewhere… — paste an address — connect —
 * sent. The "tracker" is a small local stand-in that answers like a Jira the
 * team hosts itself, so nothing outside this machine is touched; the suite
 * reads what the stand-in was actually sent.
 *
 * The connection file, the values file and the probe test are put back.
 */
const { chromium } = require("playwright");
const http = require("http");
const fs = require("fs");
const path = require("path");

let pass = 0, fail = 0; const errs = [];
const check = (n, ok, d) => { ok ? pass++ : fail++;
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${n}${ok || !d ? "" : "  — " + String(d).slice(0, 300)}`); };

const DIR = process.env.MOCKD_DIR || path.resolve(__dirname, "..");
const KEEP = ["connections.json", ".env"].map((name) => {
  const file = path.join(DIR, name);
  return { file, before: fs.existsSync(file) ? fs.readFileSync(file) : null };
});
const SUITE = path.join(DIR, "tests", "drafts", "tracker-probe.json");
process.on("exit", () => {
  for (const { file, before } of KEEP) {
    try { if (before === null) fs.rmSync(file, { force: true }); else fs.writeFileSync(file, before); }
    catch { /* nothing to restore */ }
  }
  try { fs.rmSync(SUITE, { force: true }); } catch { /* gone */ }
});

(async () => {
  const got = [];
  const tracker = http.createServer((req, res) => {
    let raw = "";
    req.on("data", (c) => { raw += c; });
    req.on("end", () => {
      let body = {}; try { body = JSON.parse(raw || "{}"); } catch { /* not json */ }
      got.push({ path: req.url, auth: req.headers.authorization || "", body });
      const refused = req.headers.authorization !== "Bearer probe-pat-123";
      res.writeHead(refused ? 401 : 201, { "Content-Type": "application/json" });
      res.end(JSON.stringify(refused ? { message: "no" } : { key: `PRB-${70 + got.length}` }));
    });
  });
  await new Promise((r) => tracker.listen(0, "127.0.0.1", r));
  const SITE = `http://127.0.0.1:${tracker.address().port}`;

  for (const { file } of KEEP.slice(0, 1)) fs.rmSync(file, { force: true });   // start unconnected
  fs.mkdirSync(path.dirname(SUITE), { recursive: true });
  fs.writeFileSync(SUITE, JSON.stringify({ name: "tracker-probe", data: {}, cases: [{
    id: "tracker-probe-case", name: "a probe that fails on purpose", levels: ["negative"], priority: "P1",
    description: "Asks the mock for its routes and expects a status it will not give.",
    request: { method: "GET", path: "/_mock/routes" },
    assertions: [{ type: "status", equals: 418 }] }], scenarios: [] }, null, 2));

  const b = await chromium.launch();
  const p = await b.newPage();
  p.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  p.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text()); });
  await p.goto("http://localhost:4100", { waitUntil: "networkidle" });
  const toast = async () => ((await p.locator("#toast").textContent().catch(() => "")) || "").trim();
  const flat = (s) => (s || "").replace(/\s+/g, " ").trim();

  try {
    await p.locator('nav.side a[data-view="tests"]').click();
    await p.waitForTimeout(1500);
    await p.locator("#libSearch").fill("fails on purpose");
    await p.waitForTimeout(600);
    await p.locator("#libEnv").selectOption("mock");
    const row = p.locator("#libList .librow").first();
    await row.locator("[data-librun]").click();
    await p.waitForFunction(() => /does not pass/.test(
      (document.querySelector("#libList .librow .top .libres") || {}).textContent || ""), null, { timeout: 60000 }).catch(() => {});
    check("the probe fails, as it is meant to", /does not pass/.test(await row.locator(".top .libres").textContent()));

    if (!(await row.locator('[data-libact="send"]').count())) await row.locator(".top .nm").click();
    await p.waitForTimeout(400);
    const send = p.locator('#libList .librow [data-libact="send"]').first();
    check("a failing test offers to send its report somewhere", await send.count() === 1
          && /Send it somewhere/.test(await send.textContent()), flat(await p.locator("#libList .librow").first().textContent()).slice(-200));
    await send.click();
    await p.waitForTimeout(300);
    const form = p.locator("[data-trackerform]");
    check("with nothing connected, it asks one thing: the address", await form.count() === 1
          && await form.locator("input").count() === 1, String(await form.locator("input").count()));

    // --------------------------------------------- an address, and what it is
    await form.locator("[data-traddr]").fill("yourteam.atlassian.net");
    await p.waitForFunction(() => /https:\/\//.test((document.querySelector("[data-trmore]") || {}).textContent || ""),
                            null, { timeout: 8000 }).catch(() => {});
    check("an address without https:// is explained", /starting with https:\/\//.test(await form.textContent()),
          flat(await form.textContent()));
    check("and cannot be connected", await form.locator("[data-trgo]").isDisabled());

    await form.locator("[data-traddr]").fill("https://yourteam.atlassian.net/browse/SHOP-12");
    await p.waitForFunction(() => /Jira SHOP/.test((document.querySelector("[data-trmore]") || {}).textContent || ""),
                            null, { timeout: 8000 }).catch(() => {});
    check("a Jira Cloud address is recognised, project and all",
          /This is Jira SHOP/.test(flat(await form.locator("[data-trmore]").textContent())),
          flat(await form.locator("[data-trmore]").textContent()));
    check("and it asks for exactly what Jira Cloud needs: an email and a token",
          await form.locator("[data-trneed]").count() === 2);

    await form.locator("[data-traddr]").fill(`${SITE}/browse/PRB-1`);
    await p.waitForFunction(() => /Jira PRB/.test((document.querySelector("[data-trmore]") || {}).textContent || ""),
                            null, { timeout: 8000 }).catch(() => {});
    check("a Jira the team hosts itself asks for a token alone",
          await form.locator("[data-trneed]").count() === 1
          && await form.locator('[data-trneed="token"]').getAttribute("type") === "password");

    // ---------------------------------------------------- a wrong token
    await form.locator('[data-trneed="token"]').fill("not-the-token");
    await form.locator("[data-trgo]").click();
    await p.waitForFunction(() => /refused the credential/.test(document.querySelector("#toast").textContent),
                            null, { timeout: 20000 }).catch(() => {});
    check("a token the tracker refuses is said in words", /refused the credential/.test(await toast()), await toast());
    check("and nothing is linked to the test",
          !(JSON.parse(fs.readFileSync(SUITE, "utf8")).cases[0].links || []).length);

    // ------------------------------------------------------- the real thing
    await p.locator("[data-trchange]").first().click();
    await p.waitForTimeout(300);
    await p.locator("[data-traddr]").fill(`${SITE}/browse/PRB-1`);
    await p.waitForFunction(() => !!document.querySelector('[data-trneed="token"]'), null, { timeout: 8000 }).catch(() => {});
    await p.locator('[data-trneed="token"]').fill("probe-pat-123");
    await p.locator("[data-trgo]").click();
    await p.waitForFunction(() => !!document.querySelector("[data-trsent]"), null, { timeout: 20000 }).catch(() => {});
    const last = got[got.length - 1] || {};
    check("Connect and send posts the report as an issue in that project",
          last.path === "/rest/api/2/issue" && ((last.body.fields || {}).project || {}).key === "PRB"
          && /a probe that fails on purpose/.test((last.body.fields || {}).summary || ""), JSON.stringify(last).slice(0, 300));
    check("with what was expected and what happened in it",
          /418/.test((last.body.fields || {}).description || "") && /curl/.test((last.body.fields || {}).description || ""));
    check("and no credential of the server under test", !/Authorization|Bearer|Cookie/.test((last.body.fields || {}).description || ""));
    const sent = flat(await p.locator("[data-trsent]").textContent());
    check("the screen says where it went and what it is called", /Sent to Jira PRB as PRB-\d+/.test(sent), sent || await toast());
    check("with a link to open it", await p.locator("[data-trsent] a").count() === 1
          && /\/browse\/PRB-\d+$/.test(await p.locator("[data-trsent] a").getAttribute("href")));
    const linked = (JSON.parse(fs.readFileSync(SUITE, "utf8")).cases[0].links || []);
    check("the issue is kept on the test", linked.length === 1 && /^PRB-\d+$/.test(linked[0]), JSON.stringify(linked));
    check("and shows on its row", /Linked: PRB-\d+/.test(flat(await p.locator("#libList .librow").first().textContent())));
    const shape = fs.readFileSync(path.join(DIR, "connections.json"), "utf8");
    check("the token is not in the connection file", !shape.includes("probe-pat-123") && /\$\{TRACKER_TOKEN\}/.test(shape), shape);

    // ------------------------------------------------- next time: one press
    const again = p.locator('#libList .librow [data-libact="send"]').first();
    check("from now on the button names where it goes", /Send to Jira PRB/.test(await again.textContent()),
          await again.textContent());
    const before = got.length;
    await again.click();
    await p.waitForFunction(() => /already raised as/.test((document.querySelector("#libList") || {}).textContent || ""),
                            null, { timeout: 15000 }).catch(() => {});
    check("sending the same failure twice is caught: it says it was already raised",
          /already raised as PRB-\d+/.test(flat(await p.locator("#libList").textContent())) && got.length === before,
          flat(await p.locator("#libList").textContent()).slice(-200));
    await p.locator("[data-tragain]").click();
    await p.waitForFunction((n) => true, null).catch(() => {});
    await p.waitForTimeout(2500);
    check("unless you say to send it again", got.length === before + 1, `${got.length} vs ${before}`);
  } finally {
    tracker.close();
  }

  console.log(`\n${pass} passed, ${fail} failed`);
  console.log(errs.length ? "JS ERRORS:\n  " + errs.join("\n  ") : "no JS errors");
  await b.close(); process.exit(fail || errs.length ? 1 : 0);
})();
