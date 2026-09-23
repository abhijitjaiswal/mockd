/**
 * params.js — clicking an operation must produce a request that can succeed.
 *
 * Two faults, one cause: the explorer ignored the parameter schemas. It pasted
 * a single hardcoded uuid into every path parameter, so an integer id got a
 * uuid; and it blanked the query string, so a REQUIRED query parameter was
 * never sent. Both produced a 422 before the request left the browser, and the
 * API's complaint read as the API's fault.
 */
const { chromium } = require("playwright");

let pass = 0, fail = 0; const errs = [];
const check = (n, ok, d) => { ok ? pass++ : fail++;
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${n}${ok || !d ? "" : "  — " + d}`); };

const ask = (p, method, path) => p.evaluate(async ([m, pa]) => {
  const q = new URLSearchParams({ method: m, path: pa });
  return await (await fetch("/api/sample-request?" + q)).json();
}, [method, path]);

(async () => {
  const b = await chromium.launch();
  const p = await b.newPage();
  p.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  p.on("console", (m) => { if (m.type() === "error" && !/status of 4\d\d/.test(m.text()))
                             errs.push("console: " + m.text()); });
  await p.goto("http://localhost:4100", { waitUntil: "networkidle" });
  await p.waitForTimeout(1800);

  // find operations in whatever spec is loaded: one with a required query
  // parameter, and one whose path parameter is an integer
  const routes = await p.evaluate(async () => {
    const d = await (await fetch("/api/routes")).json();
    return (d.routes || d).map((r) => ({ method: r.method, path: r.path }));
  });
  const withParam = routes.filter((r) => r.method === "GET" && /\{/.test(r.path));
  check("the spec has operations with path parameters", withParam.length > 0);

  // every path parameter must be substituted — no braces may survive
  let leftover = [];
  for (const r of withParam.slice(0, 12)) {
    const s = await ask(p, r.method, r.path);
    if (s.path && /\{|\}/.test(s.path)) leftover.push(r.path);
  }
  check("no path parameter is left unsubstituted", leftover.length === 0,
        leftover.join(" "));

  // required query parameters are present
  const needQuery = [];
  for (const r of routes.filter((x) => x.method === "GET" && !/\{/.test(x.path)).slice(0, 40)) {
    const s = await ask(p, r.method, r.path);
    if ((s.required_query || []).length) needQuery.push({ r, s });
  }
  check("at least one operation requires a query parameter", needQuery.length > 0);
  const missing = needQuery.filter(({ s }) =>
    s.required_query.some((name) => !(s.query || "").includes(name + "=")));
  check("every required query parameter is filled in", missing.length === 0,
        missing.map((m) => m.r.path).join(" "));

  // and the generated request actually succeeds against the mock
  if (needQuery.length) {
    const { r, s } = needQuery[0];
    const live = await p.evaluate(async ([path, query]) => {
      const base = document.getElementById("baseurl").textContent.trim();
      const one = await fetch(`${base}${path}` + (query ? "?" + query : ""));
      const none = await fetch(`${base}${path}`);
      return { withQuery: one.status, withoutQuery: none.status, path };
    }, [s.path, s.query]);
    check("the generated request succeeds", live.withQuery === 200,
          `${live.path} -> ${live.withQuery}`);
    check("and omitting the required parameter would have failed",
          live.withoutQuery !== 200, `without it -> ${live.withoutQuery}`);
  }

  // clicking the row in the UI fills the form, not just the API
  await p.locator('nav.side a[data-view="explore"]').click();
  await p.waitForTimeout(1200);
  if (needQuery.length) {
    // Drive the selection the way a click does, without depending on that row
    // being scrolled into view or surviving whatever filter is active.
    const target = needQuery[0].r;
    await p.evaluate((t) => {
      const route = ROUTES.find((r) => r.method === t.method && r.path === t.path);
      if (route) selectOp(route, null);
    }, target);
    await p.waitForTimeout(1500);
    check("selecting the operation fills the query box",
          (await p.locator("#query").inputValue()).length > 0,
          `${target.path} -> ${await p.locator("#query").inputValue()}`);
    check("and the path has no unsubstituted parameter",
          !/\{|\}/.test(await p.locator("#path").inputValue()),
          await p.locator("#path").inputValue());
  }

  console.log(`\n${pass} passed, ${fail} failed`);
  console.log(errs.length ? "JS ERRORS:\n  " + errs.join("\n  ") : "no JS errors");
  await b.close(); process.exit(fail || errs.length ? 1 : 0);
})();
