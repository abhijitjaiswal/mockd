/**
 * mcp.js — connecting an AI tool to mockd, from the screen.
 *
 * The connection itself is made in the AI tool, so what this screen owes
 * somebody is exactly what to paste, for the tool they use — and that what
 * they copy actually starts a working server. The suite copies it and starts
 * it, rather than trusting that it looks right.
 */
const { chromium } = require("playwright");
const { spawn } = require("child_process");
const os = require("os");
const path = require("path");

let pass = 0, fail = 0; const errs = [];
const check = (n, ok, d) => { ok ? pass++ : fail++;
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${n}${ok || !d ? "" : "  — " + String(d).slice(0, 300)}`); };

/** Start what was copied and ask it to introduce itself. */
function introduce(command, args) {
  return new Promise((resolve) => {
    const child = spawn(command, args, { stdio: ["pipe", "pipe", "ignore"] });
    let out = "";
    const done = (value) => { try { child.kill(); } catch { /* gone */ } resolve(value); };
    const timer = setTimeout(() => done(null), 20000);
    child.on("error", () => { clearTimeout(timer); done(null); });
    child.stdout.on("data", (chunk) => {
      out += chunk;
      const lines = out.split("\n").filter(Boolean);
      if (lines.length >= 2) {
        clearTimeout(timer);
        try { done(lines.map((l) => JSON.parse(l))); } catch { done(null); }
      }
    });
    child.stdin.write(JSON.stringify({ jsonrpc: "2.0", id: 1, method: "initialize",
      params: { protocolVersion: "2025-06-18", capabilities: {},
                clientInfo: { name: "suite", version: "0" } } }) + "\n");
    child.stdin.write(JSON.stringify({ jsonrpc: "2.0", id: 2, method: "tools/list" }) + "\n");
  });
}

(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext({ permissions: ["clipboard-read", "clipboard-write"] });
  const p = await ctx.newPage();
  p.on("pageerror", (e) => errs.push("pageerror: " + e.message));
  p.on("console", (m) => { if (m.type() === "error") errs.push("console: " + m.text()); });
  await p.goto("http://localhost:4100", { waitUntil: "networkidle" });
  const toast = async () => ((await p.locator("#toast").textContent().catch(() => "")) || "").trim();
  const clip = () => p.evaluate(() => navigator.clipboard.readText()).catch(() => "");

  await p.locator('nav.side a[data-view="create"]').click();
  await p.waitForTimeout(1200);
  check("Create tests offers to connect an AI tool directly", await p.locator("#czMcpOpen").isVisible());
  await p.locator("#czMcpOpen").click();
  await p.waitForFunction(() => document.querySelectorAll("#czMcpWays .mcpway").length > 0,
                          null, { timeout: 15000 }).catch(() => {});
  check("Show me how opens the instructions", await p.locator("#czMcp").isVisible());
  check("and puts the form away", await p.locator("#czAsk").isHidden());
  const tools = await p.locator("#czMcpWays .mcpway b").allTextContents();
  check("for Claude Code, Claude Desktop and Cursor",
        ["Claude Code", "Claude Desktop", "Cursor"].every((t) => tools.includes(t)), tools.join(", "));
  const seen = (await p.locator("#czMcp").textContent()).replace(/\s+/g, " ");
  check("it says no AI or key is needed on this side", /contains no AI and needs no key/.test(seen));
  check("and what the tool is never given", /never given a token, password or cookie/.test(seen));
  check("what is shown does not carry this computer's account name",
        !seen.includes(os.userInfo().username), os.userInfo().username);
  check("it is shortened with ~ instead", /~\//.test(seen));

  // ------------------------------------------------------------ Claude Code
  await p.locator('.mcpway[data-mcp="Claude Code"] [data-mcpcopy]').click();
  await p.waitForTimeout(500);
  const command = await clip();
  check("Copy for Claude Code gives the command to run",
        /^claude mcp add mockd -- /.test(command) && /mcp_server\.py"?$/.test(command), command);
  check("with the full path, which a terminal elsewhere needs", command.includes(os.homedir()) || !/~/.test(command), command);
  check("and says what to do with it", /Run this once in a terminal/.test(await toast()), await toast());

  // --------------------------------------------------------- Claude Desktop
  await p.locator('.mcpway[data-mcp="Claude Desktop"] [data-mcpcopy]').click();
  await p.waitForTimeout(500);
  let config = null;
  try { config = JSON.parse(await clip()); } catch { /* checked below */ }
  const entry = ((config || {}).mcpServers || {}).mockd;
  check("Copy for Claude Desktop gives settings it can read",
        !!entry && typeof entry.command === "string" && Array.isArray(entry.args), await clip());
  check("pointing at this project's server",
        !!entry && path.basename(entry.args[0] || "") === "mcp_server.py" && path.isAbsolute(entry.args[0]));

  // what was copied, started for real
  const answers = entry ? await introduce(entry.command, entry.args) : null;
  check("what was copied starts a server that answers",
        !!answers && ((answers[0] || {}).result || {}).serverInfo && answers[0].result.serverInfo.name === "mockd",
        JSON.stringify(answers).slice(0, 200));
  check("and offers its tools",
        !!answers && (((answers[1] || {}).result || {}).tools || []).length === 9,
        JSON.stringify((answers || [])[1] || {}).slice(0, 200));

  await p.locator("#czMcpBack").click();
  await p.waitForTimeout(300);
  check("Back returns to describing a test", await p.locator("#czAsk").isVisible()
        && await p.locator("#czMcp").isHidden());

  console.log(`\n${pass} passed, ${fail} failed`);
  console.log(errs.length ? "JS ERRORS:\n  " + errs.join("\n  ") : "no JS errors");
  await b.close(); process.exit(fail || errs.length ? 1 : 0);
})();
