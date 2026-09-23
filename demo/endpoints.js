/**
 * endpoints.js — the endpoints the browser suites drive, in one place.
 *
 * The suites need concrete paths to type into the explorer, but those paths
 * belong to whatever API you point the tool at — they are not part of the tool.
 * So the repository carries neutral placeholders, and you point them at your
 * own without editing any suite:
 *
 *   1. drop a demo/endpoints.local.js next to this file (gitignored):
 *          module.exports = { COLLECTION: "/api/v1/your/thing", ... };
 *   2. or set the environment variables below for a single run.
 *
 * Whatever you choose, no API's real shape ends up in a commit.
 */
const path = require("path");

// COLLECTION  a GET that lists things, and a POST that creates one
// NESTED      a child collection under an item, for the setup-then-target chain
// LIST        any second collection, used where two differing paths are needed
// GROUPS      a third, for the copy-as-curl checks
const DEFAULTS = {
  COLLECTION: "/api/v1/widgets",
  NESTED: "/api/v1/widgets/{{widgetId}}/parts",
  LIST: "/api/v1/accounts/list",
  GROUPS: "/api/v1/forms/sections",
};

let local = {};
try {
  local = require(path.join(__dirname, "endpoints.local.js"));
} catch {
  local = {};                       // no local override, which is the normal case
}

const resolved = {};
for (const key of Object.keys(DEFAULTS)) {
  resolved[key] = process.env[`CLAVIS_${key}`] || local[key] || DEFAULTS[key];
}

module.exports = resolved;
