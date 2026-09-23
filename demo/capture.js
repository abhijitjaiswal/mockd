/**
 * capture.js — drive the mockd console and capture the product in use.
 *
 * Each frame is a single panel, captured as an element rather than a viewport,
 * so every slide is tight and readable instead of showing whatever happened to
 * be on screen. The PNGs become the pages of the deck troupe narrates.
 */
const { chromium } = require("playwright");
const EP = require("./endpoints");
const fs = require("fs");
const path = require("path");

const CONSOLE_URL = process.env.CONSOLE_URL || "http://localhost:4100";
const OUT = process.env.OUT || path.join(__dirname, "shots");

const shots = [];
let n = 0;

/** Capture one element on a padded, page-coloured backdrop. */
async function view(page, name) {
  await page.locator(`nav.side a[data-view="${name}"]`).click();
  await page.waitForTimeout(700);
}

async function card(page, selector, name, note, opts = {}) {
  n += 1;
  const file = path.join(OUT, `${String(n).padStart(2, "0")}-${name}.png`);
  const el = page.locator(selector).first();
  await el.scrollIntoViewIfNeeded();
  await page.waitForTimeout(opts.settle || 250);
  await el.screenshot({ path: file });
  shots.push({ page: n, name, note, file });
  console.log(`  p${String(n).padStart(2, "0")}  ${name.padEnd(22)} ${note}`);
}

/** A rendered title/section card, so the film has punctuation. */
async function titleCard(page, name, heading, sub) {
  n += 1;
  const file = path.join(OUT, `${String(n).padStart(2, "0")}-${name}.png`);
  await page.setContent(`<!doctype html><meta charset="utf-8">
    <div id="c" style="width:1240px;height:520px;display:flex;flex-direction:column;
         justify-content:center;gap:18px;padding:0 90px;box-sizing:border-box;
         background:#fdfdfe;font:16px -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
         color:#14181b;border:1px solid #d7dce2;border-radius:12px">
      <div style="font-size:13px;font-weight:700;letter-spacing:2px;text-transform:uppercase;
                  color:#6b7580">mockd</div>
      <div style="font-size:52px;font-weight:700;line-height:1.1;letter-spacing:-1px">${heading}</div>
      <div style="font-size:21px;line-height:1.5;color:#6b7580;max-width:46ch">${sub}</div>
    </div>`);
  await page.waitForTimeout(150);
  await page.locator("#c").screenshot({ path: file });
  shots.push({ page: n, name, note: heading, file });
  console.log(`  p${String(n).padStart(2, "0")}  ${name.padEnd(22)} (title) ${heading}`);
}

(async () => {
  fs.rmSync(OUT, { recursive: true, force: true });
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch();
  const context = await browser.newContext({
    viewport: { width: 1500, height: 1000 },
    deviceScaleFactor: 2,
    colorScheme: "light",
  });
  const page = await context.newPage();

  console.log(`capturing ${CONSOLE_URL}`);
  await page.goto(CONSOLE_URL, { waitUntil: "networkidle" });
  await page.waitForTimeout(1500);

  await titleCard(page, "title", "One spec.<br>Three teams.",
    "A mock server, a contract verifier and a test runner, all driven by the same OpenAPI document.");
  await page.goto(CONSOLE_URL, { waitUntil: "networkidle" });
  await page.waitForTimeout(1200);

  // --- what the document actually says -------------------------------------
  await view(page, "overview");
  await card(page, "#cov", "coverage", "31 of 96 operations are fully documented");

  const gaps = page.locator("#cov details summary").first();
  await gaps.click();
  await card(page, "#cov details", "coverage-gaps", "every gap, named", { settle: 500 });
  await gaps.click();

  // --- what a dev team plugs into ------------------------------------------
  await titleCard(page, "t-ui", "For the UI team",
    "Point the app at one URL. Every endpoint answers, including the 65 the spec never describes.");
  await page.goto(CONSOLE_URL, { waitUntil: "networkidle" });
  await page.waitForTimeout(1200);

  await view(page, "connect");
  await card(page, "#integ", "integration", "base URL and copy-ready snippets");
  await card(page, "#integ details", "errors", "bad requests answer like the real API does",
             { settle: 400 });

  // --- exploring ------------------------------------------------------------
  await view(page, "explore");
  await card(page, "#collHint", "marks", "which endpoints are worth exploring");

  await page.locator("#method").selectOption("GET");
  await page.locator("#path").fill(EP.COLLECTION);
  await page.locator("#query").fill("page=1&page_size=10");
  await page.locator("#btnSend").click();
  await page.waitForTimeout(1000);
  await card(page, "#resp", "request", "a real payload, not a placeholder");

  await page.locator('button[data-h="X-Mock-Status: 500"]').click();
  await page.locator("#btnSend").click();
  await page.waitForTimeout(900);
  await card(page, "#respHead", "forced-500", "any documented status, on demand");
  await page.locator("#clearHeaders").click();

  await page.locator('button[data-h="X-Mock-Scenario: empty"]').click();
  await page.locator("#btnSend").click();
  await page.waitForTimeout(900);
  await card(page, "#resp", "scenario-empty", "the empty state, without emptying a database");
  await page.locator("#clearHeaders").click();
  await page.locator("#btnSend").click();
  await page.waitForTimeout(900);

  // --- QA -------------------------------------------------------------------
  await titleCard(page, "t-qa", "For QA",
    "Turn a response you just looked at into assertions that anyone can replay, anywhere.");
  await page.goto(CONSOLE_URL, { waitUntil: "networkidle" });
  await page.waitForTimeout(1200);
  await view(page, "explore");
  await page.locator("#method").selectOption("GET");
  await page.locator("#path").fill(EP.COLLECTION);
  await view(page, "explore");
  await page.locator("#btnSend").click();
  await page.waitForTimeout(1000);

  await page.locator("#btnSaveTest").click();
  await page.waitForTimeout(1000);
  await card(page, "#saveDlg", "save-as-test", "assertions read off the real response",
             { settle: 400 });
  await page.locator("#dlgCancel").click();
  await page.waitForTimeout(300);

  await view(page, "tests");
  await card(page, "#testTree", "tests", "suites, their stage, and how each last ran");

  await view(page, "tests");
  await page.locator("#btnRunTests").click();
  await page.waitForTimeout(7000);
  await card(page, "#testOutCard", "tests-run", "one button, any environment, same test data");

  await page.locator("#btnImport").click();
  await page.waitForTimeout(800);
  await card(page, "#importDlg", "import", "one validated door for generated tests",
             { settle: 400 });
  await page.locator("#impCancel").click();
  await page.waitForTimeout(300);

  // --- the rest of the team -------------------------------------------------
  await titleCard(page, "t-ci", "For the backend, and for CI",
    "The same spec, the same suites, run against a real environment on every merge.");
  await page.goto(CONSOLE_URL, { waitUntil: "networkidle" });
  await page.waitForTimeout(1200);

  await view(page, "environments");
  const live = page.locator(".card").filter({ hasText: "Check a real environment" }).first();
  await live.scrollIntoViewIfNeeded();
  await page.waitForTimeout(300);
  n += 1;
  const envFile = path.join(OUT, `${String(n).padStart(2, "0")}-environments.png`);
  await live.screenshot({ path: envFile });
  shots.push({ page: n, name: "environments", note: "named environments; no credential in the repo",
               file: envFile });
  console.log(`  p${String(n).padStart(2, "0")}  environments           named environments`);

  fs.writeFileSync(path.join(OUT, "shots.json"), JSON.stringify(shots, null, 2));
  await context.close();
  await browser.close();
  console.log(`\n${shots.length} pages in ${OUT}`);
})().catch((err) => { console.error(err); process.exit(1); });
