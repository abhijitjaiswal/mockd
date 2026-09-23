/**
 * dotenv.js — a suite that touches .env puts it back the way it found it.
 *
 * Two suites write to .env: envvars.js through the console's own UI, and
 * reveal.js directly so it has an environment that resolves. Neither should
 * leave residue behind for the next run, or for the person whose machine it is.
 */
const fs = require("fs");
const path = require("path");

// the suites live in <project>/demo, so the project is one level up
const ENV_FILE = path.join(
  process.env.CLAVIS_DIR || path.resolve(__dirname, ".."), ".env");

/** Snapshot .env and restore it when the process exits. Returns nothing. */
function guard(extra) {
  const had = fs.existsSync(ENV_FILE);
  const before = had ? fs.readFileSync(ENV_FILE, "utf8") : null;
  if (extra) fs.writeFileSync(ENV_FILE, (before || "") + extra);
  process.on("exit", () => {
    if (had) fs.writeFileSync(ENV_FILE, before);
    else fs.rmSync(ENV_FILE, { force: true });
  });
}

module.exports = { guard, ENV_FILE };
