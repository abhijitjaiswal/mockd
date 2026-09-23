/**
 * discover.js — a documentation page URL is what people actually paste.
 *
 * The reported symptom: pasting the docs URL gave
 *   "not a usable OpenAPI document: mapping values are not allowed here ...
 *    line 16, column 12: url: '/openapi.json'"
 * — the YAML parser choking on a Swagger UI page. The page names its own spec,
 * so the tool follows it instead of asking anyone to read the Network tab.
 */
const { chromium } = require("playwright");
const http = require("http");

let pass = 0, fail = 0; const errs = [];
const check = (n, ok, d) => { ok ? pass++ : fail++;
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${n}${ok || !d ? "" : "  — " + d}`); };

const SPEC = JSON.stringify({
  openapi: "3.1.0",
  info: { title: "Fixture API", version: "9.9.9" },
  paths: { "/ping": { get: { responses: { 200: { description: "ok" } } } } },
});

// the exact shape from the reported error: `url: '/openapi.json'` on line 16
const SWAGGER_UI = `<!DOCTYPE html>
<html>
  <head>
    <title>Swagger UI</title>
    <link rel="stylesheet" href="./swagger-ui.css">
  </head>
  <body>
    <div id="swagger-ui"></div>
    <script src="./swagger-ui-bundle.js"></script>
    <script>
      window.onload = function() {
        window.ui = SwaggerUIBundle({
          url: '/openapi.json',
          dom_id: '#swagger-ui',
          presets: [SwaggerUIBundle.presets.apis],
        });
      };
    </script>
  </body>
</html>`;

const REDOC = `<!DOCTYPE html><html><head><title>docs</title></head>
<body><redoc spec-url="/openapi.json"></redoc></body></html>`;

// a docs page that names nothing and whose host serves no spec anywhere
const BLANK = `<!DOCTYPE html><html><body><h1>API documentation</h1></body></html>`;

(async () => {
  const origin = await new Promise((resolve) => {
    const s = http.createServer((req, res) => {
      const url = req.url.split("?")[0];
      if (url === "/docs") { res.writeHead(200, {"Content-Type":"text/html"}); return res.end(SWAGGER_UI); }
      if (url === "/redoc") { res.writeHead(200, {"Content-Type":"text/html"}); return res.end(REDOC); }
      if (url === "/openapi.json") { res.writeHead(200, {"Content-Type":"application/json"}); return res.end(SPEC); }
      res.writeHead(404, {"Content-Type":"application/json"}); res.end('{"detail":"nope"}');
    });
    s.listen(0, "127.0.0.1", () => resolve({ srv: s, base: `http://127.0.0.1:${s.address().port}` }));
  });
  const base = origin.base;

  // a second host that serves ONLY the page — none of the well-known spec
  // paths exist there, so discovery genuinely has nowhere to go
  const barren = await new Promise((resolve) => {
    const s2 = http.createServer((req, res) => {
      if (req.url.split("?")[0] === "/blank") {
        res.writeHead(200, {"Content-Type":"text/html"}); return res.end(BLANK);
      }
      res.writeHead(404, {"Content-Type":"application/json"}); res.end('{"detail":"nope"}');
    });
    s2.listen(0, "127.0.0.1", () => resolve({ srv: s2, base: `http://127.0.0.1:${s2.address().port}` }));
  });

  const b = await chromium.launch();
  const p = await b.newPage();
  p.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  p.on("console", (m) => {
    if (m.type() === "error" && !/status of 400/.test(m.text())) errs.push("console: " + m.text());
  });
  await p.goto("http://localhost:4100", { waitUntil: "networkidle" });
  await p.waitForTimeout(1800);
  await p.locator('nav.side a[data-view="source"]').click();
  await p.waitForTimeout(600);

  async function fetchSpec(path, saveAs, host) {
    await p.locator("#specUrl").fill((host || base) + path);
    await p.locator("#specSaveAs").fill(saveAs);
    await p.locator("#btnSpecFetch").click();
    await p.waitForTimeout(2500);
    return (await p.locator("#toast").textContent()).trim();
  }

  const ui = await fetchSpec("/docs", "disc-ui.json");
  check("a Swagger UI page resolves to its spec", /1 operation/.test(ui), ui);
  check("and says which page it followed", /documentation page/.test(ui), ui);
  check("naming the document it actually read", /openapi\.json/.test(ui), ui);

  const rd = await fetchSpec("/redoc", "disc-redoc.json");
  check("a ReDoc page resolves too", /1 operation/.test(rd), rd);

  const direct = await fetchSpec("/openapi.json", "disc-direct.json");
  check("a direct spec URL is unaffected", /1 operation/.test(direct), direct);
  check("and claims no redirection", !/documentation page/.test(direct), direct);

  const blank = await fetchSpec("/blank", "disc-blank.json", barren.base);
  check("a page naming no spec fails honestly",
        /documentation page, not a spec/.test(blank), blank);
  check("and points at the Network tab", /Network tab/.test(blank), blank);

  console.log(`\n${pass} passed, ${fail} failed`);
  console.log(errs.length ? "JS ERRORS:\n  " + errs.join("\n  ") : "no JS errors");
  await b.close(); origin.srv.close(); barren.srv.close();
  process.exit(fail || errs.length ? 1 : 0);
})();
