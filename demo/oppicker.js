/**
 * oppicker.js — the step's path comes from the spec, not from memory.
 *
 * Choosing a method must narrow the operation list to that method, and
 * choosing an operation must fill everything the document can supply: a path
 * with its parameters typed correctly, any required query, and a valid body.
 * Typing a path from memory is how you end up testing an endpoint that does
 * not exist, and discovering that only when the run says 404.
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

  // what the spec actually holds, to check the list against
  const counts = await p.evaluate(async () => {
    const d = await (await fetch("/api/routes")).json();
    const routes = d.routes || d;
    const by = {};
    routes.forEach((r) => (by[r.method] = (by[r.method] || 0) + 1));
    return by;
  });

  // ------------------------------------------------------------- GET
  await p.locator('[data-method="0"]').selectOption("GET");
  await p.waitForTimeout(700);
  const getOpts = await p.locator('[data-op="0"] option').count();
  check("choosing GET lists every GET in the spec",
        getOpts === counts.GET + 1, `${getOpts - 1} listed, spec has ${counts.GET}`);
  const getText = await p.locator('[data-op="0"]').textContent();
  // every listed path must really be a GET in the document — a count alone
  // would pass even if the list held the wrong operations
  const listed = await p.evaluate(() =>
    [...document.querySelectorAll('[data-op="0"] option')]
      .map((o) => o.value).filter(Boolean));
  const stray = await p.evaluate(async (paths) => {
    const d = await (await fetch("/api/routes")).json();
    const gets = new Set((d.routes || d).filter((r) => r.method === "GET")
                                        .map((r) => r.path));
    return paths.filter((x) => !gets.has(x));
  }, listed);
  check("and every one of them really is a GET", stray.length === 0, stray.join(" "));
  check("each option carries its summary", /—/.test(getText), getText.slice(0, 80));

  // ------------------------------------------------------------- POST
  await p.locator('[data-method="0"]').selectOption("POST");
  await p.waitForTimeout(700);
  const postOpts = await p.locator('[data-op="0"] option').count();
  check("switching to POST re-lists for POST",
        postOpts === counts.POST + 1, `${postOpts - 1} listed, spec has ${counts.POST}`);
  check("the two lists differ", postOpts !== getOpts, `${postOpts} vs ${getOpts}`);

  // --------------------------------------------- selecting fills the rest
  const target = await p.evaluate(async () => {
    const d = await (await fetch("/api/routes")).json();
    const r = (d.routes || d).find((x) => x.method === "POST" && !/\{/.test(x.path));
    return r ? r.path : "";
  });
  await p.locator('[data-op="0"]').selectOption(target);
  await p.waitForTimeout(2000);

  check("the path is filled", (await p.locator('[data-path="0"]').inputValue()) === target,
        await p.locator('[data-path="0"]').inputValue());
  const body = await p.locator('[data-body="0"]').inputValue();
  check("a valid sample body is filled", body.length > 2 && body.trim().startsWith("{"),
        body.slice(0, 70));
  check("the body is real JSON", (() => { try { JSON.parse(body); return true; }
                                          catch { return false; } })());
  const name = await p.locator('[data-name="0"]').inputValue();
  check("the step is named from the spec's summary", name.length > 0, name);
  check("and what it documents is reported",
        /documents/.test(await p.locator("#toast").textContent()),
        await p.locator("#toast").textContent());

  // ------------------------------------- an operation with a required query
  const needsQuery = await p.evaluate(async () => {
    const d = await (await fetch("/api/routes")).json();
    for (const r of (d.routes || d)) {
      if (r.method !== "GET" || /\{/.test(r.path)) continue;
      const q = new URLSearchParams({ method: "GET", path: r.path });
      const s = await (await fetch("/api/sample-request?" + q)).json();
      if ((s.required_query || []).length) return { path: r.path, query: s.query };
    }
    return null;
  });
  if (needsQuery) {
    await p.locator('[data-method="0"]').selectOption("GET"); await p.waitForTimeout(600);
    await p.locator('[data-op="0"]').selectOption(needsQuery.path);
    await p.waitForTimeout(1800);
    check("a required query parameter is filled in too",
          (await p.locator('[data-query="0"]').inputValue()) === needsQuery.query,
          `${await p.locator('[data-query="0"]').inputValue()} vs ${needsQuery.query}`);
  }

  // ---------------------------- the path stays editable, for {{variables}}
  await p.locator('[data-path="0"]').fill("/api/v1/thing/{{someId}}/parts");
  await p.locator('[data-path="0"]').dispatchEvent("change");
  await p.waitForTimeout(700);
  check("the path can still be edited by hand",
        (await p.locator('[data-path="0"]').inputValue()).includes("{{someId}}"));
  check("and an edited path does not wrongly claim an operation",
        (await p.locator('[data-op="0"]').inputValue()) === "",
        await p.locator('[data-op="0"]').inputValue());

  console.log(`\n${pass} passed, ${fail} failed`);
  console.log(errs.length ? "JS ERRORS:\n  " + errs.join("\n  ") : "no JS errors");
  await b.close(); process.exit(fail || errs.length ? 1 : 0);
})();
