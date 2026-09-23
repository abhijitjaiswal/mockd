/* mockd console — UI logic. Every call here is same-origin to console.py,
   which proxies to the mock it started. */
const $ = (id) => document.getElementById(id);
let ROUTES = [];
let SELECTED = null;
let RUNNING = false;
let COVERAGE = null;
let ENVS = [];
let TAG_FILTER = "";

const api = async (path, opts = {}) => {
  const res = await fetch(path, {
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    ...opts,
  });
  const text = await res.text();
  try { return { ok: res.ok, status: res.status, data: JSON.parse(text) }; }
  catch { return { ok: res.ok, status: res.status, data: { raw: text } }; }
};

/* ------------------------------------------------------------------- nav */

function showView(name) {
  hideBanner();                       // a message is about the view it was raised on
  document.querySelectorAll("main .view").forEach((v) =>
    v.classList.toggle("on", v.dataset.view === name));
  document.querySelectorAll("nav.side a").forEach((a) =>
    a.classList.toggle("on", a.dataset.view === name));
  location.hash = name;
  window.scrollTo(0, 0);
  if (name === "tests") loadTests();
  if (name === "source") loadProjectSpec();
  if (name === "explore" && !ROUTES.length && RUNNING) loadRoutes();
  if (name === "authoring" && !GUIDE) { loadRules(); loadGuide(); }
  if (name === "source") loadSpecStatus();
  if (name === "environments") renderOpsPicker();
  if (name === "environments") loadEnvironments();
}

document.querySelectorAll("nav.side a").forEach((a) =>
  a.addEventListener("click", () => showView(a.dataset.view)));
window.addEventListener("hashchange", () => {
  const want = location.hash.replace("#", "");
  if (want && document.querySelector(`.view[data-view="${want}"]`)) showView(want);
});

/* ---------------------------------------------------------------- server */

function options() {
  const spec = $("spec").value.trim();
  const o = {
    spec,
    overlay: $("overlay").value.trim(),
    port: Number($("port").value) || 4010,
    stateful: $("stateful").checked,
    require_auth: $("requireAuth").checked,
    allow_undocumented: $("allowUndoc").checked,
    validation_mode: $("validationMode").value,
    array_items: Number($("arrayItems").value) || 2,
  };
  if (/^https?:\/\//i.test(spec)) {
    o.poll = Number($("pollSecs").value) || 60;
    o.headers = $("specHeaders").value;
  }
  return o;
}

function setRunning(on, baseUrl) {
  RUNNING = on;
  TARGET_BASE.mock = baseUrl || "";
  $("pill").className = "pill " + (on ? "on" : "off");
  $("pilltext").textContent = on ? "running" : "stopped";
  $("baseurl").textContent = on && baseUrl ? baseUrl : "";
  $("btnStop").disabled = !on;
  $("btnRestart").disabled = !on;
  $("btnStart").disabled = on;
  $("btnSend").disabled = false;   // a real environment works without the mock
}

async function refreshState() {
  const { data } = await api("/api/state");
  setRunning(data.running, data.base_url);
  if (data.running) {
    renderDrift(data.drift);
    loadIntegration();
    if (!ROUTES.length) loadRoutes();
  }
}

async function start() {
  $("btnStart").disabled = true;
  $("pilltext").textContent = "starting…";
  const { data } = await api("/api/start", { method: "POST", body: JSON.stringify(options()) });
  $("stdout").textContent = data.stdout || "";
  if (!data.ok) {
    setRunning(false);
    banner("err", "Could not start: " + data.message);
    return;
  }
  banner("ok", "Mock server started.");
  await refreshState();
  await loadCoverage();
  await loadRules();
  await loadSpecStatus();
  await loadRoutes();
  await loadIntegration();
}

async function stop() {
  await api("/api/stop", { method: "POST" });
  ROUTES = [];
  $("ops").innerHTML = '<div class="empty">Server stopped.</div>';
  $("opcount").textContent = "0";
  $("drift").innerHTML = '<span class="hint">Server stopped.</span>';
  $("integ").hidden = true;
  setRunning(false);
}

/* ---------------------------------------------------------------- routes */

const MARK = { complete: "\u2705", partial: "\u26a0\ufe0f", undocumented: "\u2b55" };
const MARK_LABEL = {
  complete: ["fully specified",
             "response schema declared \u2014 every field, type and status is the real contract"],
  partial: ["partly specified",
            "an example but no schema \u2014 readable, not checkable"],
  undocumented: ["response shape not declared",
                 "the mock's body is inferred; explore it, but don't assert on its shape"],
};
let STATE_FILTER = "";

async function loadRoutes() {
  const { ok, data } = await api("/api/routes");
  if (!ok) { $("ops").innerHTML = '<div class="empty">' + (data.error || "no routes") + "</div>"; return; }
  ROUTES = data;
  // fold in the per-operation contract detail so the list can show and filter on it
  const cov = COVERAGE || (await api("/api/coverage")).data;
  const byKey = {};
  (cov.operations || []).forEach((o) => { byKey[o.operation] = o; });
  ROUTES.forEach((r) => {
    const o = byKey[`${r.method} ${r.path}`];
    r.state = o ? o.state : "undocumented";
    r.detail = o ? o.detail : null;
    r.trust = o ? o.trust : null;
  });
  $("opcount").textContent = data.length;
  $("navOps").textContent = data.length;
  renderCollectionHint(cov);
  renderOps();
}

function renderCollectionHint(cov) {
  const st = (cov && cov.states) || {};
  const total = (cov && cov.total) || ROUTES.length;
  const rows = ["complete", "partial", "undocumented"].map((k) => {
    const [label, what] = MARK_LABEL[k];
    const n = st[k] || 0;
    return `<div class="row2">
      <span class="mark">${MARK[k]}</span>
      <span><b>${n}</b> ${esc(label)} <span class="what">\u2014 ${what}</span></span>
    </div>`;
  }).join("");
  $("collHint").innerHTML = `
    <p class="hint" style="margin-top:9px">Each operation is marked by how much contract it
      actually has — the same marks travel into the Postman collection:</p>
    <div class="legend2">${rows}</div>
    <div class="quick" style="margin-top:8px">
      <button class="sm filterbtn" data-state="">all</button>
      <button class="sm filterbtn" data-state="complete">${MARK.complete} fully specified</button>
      <button class="sm filterbtn" data-state="undocumented">${MARK.undocumented} undeclared</button>
    </div>`;
  $("collHint").querySelectorAll(".filterbtn").forEach((b) =>
    b.addEventListener("click", () => {
      STATE_FILTER = b.dataset.state;
      $("collHint").querySelectorAll(".filterbtn")
        .forEach((x) => x.classList.toggle("on", x.dataset.state === STATE_FILTER));
      renderOps();
    }));
}

function renderOps() {
  const q = $("filter").value.toLowerCase().trim();
  const list = ROUTES.filter((r) =>
    (!q || (r.method + " " + r.path + " " + (r.tags || []).join(" ")).toLowerCase().includes(q))
    && (!STATE_FILTER || r.state === STATE_FILTER));
  if (!list.length) { $("ops").innerHTML = '<div class="empty">Nothing matches.</div>'; return; }
  $("ops").innerHTML = list.map((r) => {
    const d = r.detail || {};
    const tip = [MARK_LABEL[r.state] ? MARK_LABEL[r.state][0] : "",
                 d.fields_documented ? `${d.body_required} required / ${d.body_optional} optional body fields` : "",
                 d.forceable_statuses ? "statuses: " + d.forceable_statuses.join(", ") : ""]
                .filter(Boolean).join(" \u00b7 ");
    const fields = d.fields_documented
      ? `<span class="tag" title="${d.body_required} mandatory / ${d.body_optional} optional`
        + ` body fields">${d.body_required}+${d.body_optional}</span>` : "";
    // the path is the thing being scanned; everything else yields space to it
    return `<div class="op" data-i="${ROUTES.indexOf(r)}" title="${esc(tip)}">
      <span class="mark">${MARK[r.state] || ""}</span>
      <span class="m ${r.method}">${r.method}</span>
      <span class="p" dir="rtl" title="${esc(r.summary || r.path)}">${esc(r.path)}</span>
      ${fields}
      <button class="sm curl" title="copy a ready-to-paste curl">curl</button>
    </div>`;
  }).join("");
  [...$("ops").querySelectorAll(".op")].forEach((el) => {
    const route = ROUTES[Number(el.dataset.i)];
    el.addEventListener("click", (ev) => {
      if (ev.target.classList.contains("curl")) return;
      selectOp(route, el);
    });
    el.querySelector(".curl").addEventListener("click", async (ev) => {
      ev.stopPropagation();
      await copyCurlFor(route.method, route.path, ev.target);
    });
  });
}

function selectOp(route, el) {
  SELECTED = route;
  [...document.querySelectorAll(".op")].forEach((n) => n.classList.remove("sel"));
  if (el) el.classList.add("sel");
  $("method").value = route.method;
  // fill {param} placeholders with a value the stateful store / examples use
  $("path").value = route.path.replace(/\{[^}]+\}/g, "3fa85f64-5717-4562-b3fc-2c963f66afa6");
  $("query").value = "";
  $("body").value = "";
  const note = [];
  if (route.state) note.push(MARK[route.state] + " " + MARK_LABEL[route.state][0]);
  if (route.detail && route.detail.fields_documented)
    note.push(`${route.detail.body_required} required / ${route.detail.body_optional} optional fields`);
  if (route.statuses?.length) note.push("documented: " + route.statuses.join(", "));
  if (route.scenarios?.length) note.push("scenarios: " + route.scenarios.join(", "));
  $("bodyNote").textContent = note.length ? "— " + note.join("  ·  ") : "";
  if (["POST", "PUT", "PATCH"].includes(route.method)) fillSample();
}

/* ---------------------------------------------------------------- tester */

/* Which spec operation does the form currently describe?

   Matching on what is TYPED, not on the row last clicked: a path edited by hand
   (or with a real id pasted into it) used to leave "Fill sample body" doing
   nothing at all, silently. */
function routeForForm() {
  const method = $("method").value;
  const path = ($("path").value || "").split("?")[0];
  const exact = (ROUTES || []).find((r) => r.method === method && r.path === path);
  if (exact) return exact;
  // a concrete id in place of {param}
  const shaped = (ROUTES || []).find((r) => {
    if (r.method !== method) return false;
    const rx = new RegExp("^" + r.path.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")
      .replace(/\\\{[^}]+\\\}/g, "[^/]+") + "$");
    return rx.test(path);
  });
  if (shaped) return shaped;
  if (SELECTED && SELECTED.method === method) return SELECTED;
  return null;
}

async function fillSample() {
  const route = routeForForm();
  if (!route) {
    banner("err", `No operation in the spec matches ${$("method").value} ${$("path").value}.`);
    return;
  }
  const q = new URLSearchParams({ method: route.method, path: route.path });
  const { data } = await api("/api/sample?" + q);
  if (data.body == null) {
    banner("ok", `${route.method} ${route.path} takes no JSON body.`);
    $("body").value = "";
    return;
  }
  $("body").value = JSON.stringify(data.body, null, 2);
}

function breakBody() {
  let obj;
  try { obj = JSON.parse($("body").value || "{}"); }
  catch { $("body").value = "{ this is not json }"; return; }
  if (!obj || typeof obj !== "object") { $("body").value = '"not an object"'; return; }
  const keys = Object.keys(obj);
  let emptied = false;
  for (const k of keys) {
    if (!emptied && typeof obj[k] === "string") { obj[k] = ""; emptied = true; continue; }
    if (emptied) { obj[k] = 12345; break; }
  }
  if (!emptied && keys.length) obj[keys[0]] = 12345;
  $("body").value = JSON.stringify(obj, null, 2);
}

async function send() {
  $("btnSend").disabled = true;
  $("resp").textContent = "…";
  $("respHead").hidden = true;
  const payload = {
    target: exploreTarget(),
    method: $("method").value,
    path: $("path").value,
    query: $("query").value,
    headers: $("headers").value,
    body: $("body").value,
  };
  const { data } = await api("/api/send", { method: "POST", body: JSON.stringify(payload) });
  $("btnSend").disabled = !RUNNING;

  if (data.error) {
    $("resp").textContent = data.error;
    $("respMeta").textContent = data.ms + " ms";
    return;
  }
  const cls = "s" + String(data.status)[0];
  const src = data.headers["X-Mock-Source"] || data.headers["x-mock-source"] || "—";
  const op = data.headers["X-Mock-Operation"] || data.headers["x-mock-operation"] || "";
  $("respHead").hidden = false;
  $("respHead").className = "banner";
  $("respHead").innerHTML =
    `<span class="status ${cls}">${data.status}</span>` +
    `<span class="meta" style="display:inline-flex;margin-left:12px">` +
    `<span>${data.ms} ms</span><span>${data.bytes} bytes</span>` +
    `<span>target: <b>${esc(data.target || "?")}</b></span>` +
    `${src !== "—" ? `<span>source: <b>${esc(src)}</b></span>` : ""}` +
    `${op ? `<span>${esc(op)}</span>` : ""}</span>`;
  $("respMeta").textContent = data.status + " · " + data.ms + " ms";
  $("resp").textContent = data.body || "(empty body)";
  loadRequests();
}

/* ---------------------------------------------------------------- export */

async function copyText(text, button) {
  try { await navigator.clipboard.writeText(text); }
  catch { banner("err", "Clipboard blocked — the text is in the response panel instead.");
          $("respHead").hidden = true; $("resp").textContent = text; return false; }
  if (button) {
    const old = button.textContent;
    button.textContent = "copied"; button.classList.add("copied");
    setTimeout(() => { button.textContent = old; button.classList.remove("copied"); }, 1200);
  }
  return true;
}

/* A curl for one operation, straight from the spec: valid body, required query
   params filled in. Paste it in a terminal, or into Postman's Import > Raw text. */
async function copyCurlFor(method, path, button, reveal) {
  const q = new URLSearchParams({ method, path, target: exploreTarget() });
  if (reveal) q.set("reveal", "1");
  const { data } = await api("/api/curl?" + q);
  if (data.error) { banner("err", data.error); return; }
  await copyText(data.curl, button);
  if (reveal) {
    banner("err", "That command contains a real credential — paste it into your own "
                + "terminal, not into a ticket or a chat.");
  }
}

function exploreTarget() { return $("exploreTarget").value || "mock"; }

/* A curl for exactly what is in the request form right now, headers and all —
   against whichever target is selected, not always the mock. */
async function copyCurlFromForm(reveal) {
  const method = $("method").value;
  const query = $("query").value.trim();
  const base = TARGET_BASE[exploreTarget()] || "http://localhost:4010";
  let url = base + $("path").value + (query ? "?" + query.replace(/^\?/, "") : "");
  const parts = [`curl -X ${method} '${url}'`, "-H 'Accept: application/json'"];
  $("headers").value.split("\n").forEach((line) => {
    if (line.includes(":")) parts.push(`-H '${line.trim()}'`);
  });
  const body = $("body").value.trim();
  if (body && ["POST", "PUT", "PATCH", "DELETE"].includes(method)) {
    parts.push("-H 'Content-Type: application/json'");
    parts.push("-d '" + body.replace(/'/g, "'\\''") + "'");
  }
  const label = exploreTarget();
  // the environment's own credentials come from the server — the form only
  // knows the headers typed into it
  let prelude = "", extra = "";
  if (label !== "mock") {
    const q = new URLSearchParams({ target: label });
    if (reveal) q.set("reveal", "1");
    const { data } = await api("/api/curl/auth?" + q);
    if (data && !data.error) { prelude = data.prelude || ""; extra = data.extra || ""; }
  }
  if (extra) parts.push(extra);
  const head = label === "mock" ? "" : `# target: ${label} (${base})\n`;
  await copyText(head + prelude + parts.join(" \\\n  "),
                 reveal ? $("btnCurlReal") : $("btnCurl"));
  if (reveal && (extra || prelude)) {
    banner("err", "That command contains a real credential — paste it into your own "
                + "terminal, not into a ticket or a chat.");
  }
}

const TARGET_BASE = {};

function fillExploreTargets() {
  const opts = [`<option value="mock">mock — ${esc(TARGET_BASE.mock || "not running")}</option>`]
    .concat((ENVS || []).filter((e) => e.name !== "mock").map((e) => {
      TARGET_BASE[e.name] = e.base_url;
      return `<option value="${esc(e.name)}"${e.ready ? "" : " disabled"}>`
        + `${esc(e.name)} — ${esc(e.base_url || "?")}${e.ready ? "" : "  (unset vars)"}</option>`;
    }));
  const keep = $("exploreTarget").value;
  $("exploreTarget").innerHTML = opts.join("");
  if (keep) $("exploreTarget").value = keep;
  describeTarget();
}

function describeTarget() {
  const t = exploreTarget();
  // only offer the revealing copy where there is a credential to reveal
  $("btnCurlReal").hidden = t === "mock";
  $("targetTag").textContent = t;
  $("targetTag").style.color = t === "mock" ? "" : "var(--warn)";
  $("targetHint").innerHTML = t === "mock"
    ? `Requests go to the local mock at <code>${esc(TARGET_BASE.mock || "—")}</code>.`
    : `<b style="color:var(--warn)">Real environment.</b> Requests go to
       <code>${esc(TARGET_BASE[t] || "?")}</code> and are authenticated as that
       environment. <code>X-Mock-*</code> headers are ignored there, and writes are real.`;
}

async function getCollection() {
  const { data } = await api("/api/postman");
  if (data.error) { banner("err", data.error); return null; }
  return data;
}

/* ------------------------------------------------------- assertion editor */

/** Run whatever is in an editor right now, and show each assertion's verdict. */
async function tryTest(host, test, button, ctx = {}) {
  host.hidden = false;
  host.innerHTML = '<span class="hint" style="margin:0">running…</span>';
  const old = button.textContent;
  button.disabled = true; button.textContent = "running…";
  const { data } = await api("/api/tests/try", {
    method: "POST",
    body: JSON.stringify({ target: exploreTarget(), test,
                           suite: ctx.suite, stage: ctx.stage }),
  });
  button.disabled = false; button.textContent = old;

  if (data.error) { host.innerHTML = `<span class="hint">${esc(data.error)}</span>`; return; }
  if (data.ok === false) {
    host.innerHTML = `<div class="hint" style="color:var(--err);margin:0">Not valid yet:</div>`
      + data.errors.map((e) => `<div class="tryline no"><span class="v">bad</span>`
        + `<span>${esc(e)}</span></div>`).join("");
    return;
  }
  const r = data.result;
  const steps = r.steps || [];
  host.innerHTML =
    `<div class="hint" style="margin:0 0 6px">against <b>${esc(r.target || "")}</b>`
    + ` — <b style="color:${r.outcome === "pass" ? "var(--ok)" : "var(--err)"}">`
    + `${esc(r.outcome)}</b></div>`
    + steps.map((st) => `
        ${steps.length > 1 ? `<div class="hint" style="margin:6px 0 2px"><b>${esc(st.name)}</b>
           — ${st.status || "—"} ${st.ms || 0}ms</div>` : ""}
        ${(st.checks || []).map((c) => `
          <div class="tryline ${c.ok ? "ok" : "no"}">
            <span class="v">${c.ok ? "ok" : "fail"}</span>
            <span>${esc(c.label)}</span>
            ${c.ok ? "" : `<span class="d">${esc(c.detail)}</span>`}
          </div>`).join("")}
        ${steps.length === 1 ? `<div class="hint" style="margin-top:5px">`
          + `${st.status || "—"} · ${st.ms || 0}ms</div>` : ""}`).join("");
}

const A_TYPES = ["status", "schema", "jsonpath", "header", "responseTime", "body_contains"];
const A_OPS = ["equals", "not_equals", "exists", "not_exists", "is_null", "not_null",
               "type", "contains", "not_contains", "matches", "in", "gt", "gte", "lt",
               "lte", "length", "length_gte", "empty", "not_empty"];
const NO_VALUE = new Set(["exists", "not_exists", "is_null", "not_null", "empty", "not_empty"]);
const A_TYPE_HELP = {
  status: "the HTTP status. Use a list for “one of”.",
  schema: "validate the whole body against the schema the spec declares. No fields needed.",
  jsonpath: "a value inside the body — data.items[0].id",
  header: "a response header",
  responseTime: "how long it took, in ms",
  body_contains: "raw text anywhere in the body",
};

/** Render an editable list of assertions into `host`; read them back with readAssertions. */
function assertionEditor(host, assertions) {
  host.innerHTML = `<div class="alist"></div>
    <div class="btnrow" style="margin-top:8px">
      <button type="button" class="sm add-assert">+ Add assertion</button>
      <span class="hint" style="margin:0;align-self:center">every one must hold for the
        test to pass</span>
    </div>`;
  const list = host.querySelector(".alist");
  (assertions && assertions.length ? assertions : []).forEach((a) => addRow(list, a));
  host.querySelector(".add-assert").addEventListener("click", () =>
    addRow(list, { type: "jsonpath", path: "", op: "exists" }));
  return host;
}

function addRow(list, a) {
  const row = document.createElement("div");
  row.className = "arow2";
  row.innerHTML = `
    <select class="a-type" title="what to check">
      ${A_TYPES.map((t) => `<option value="${t}"${t === (a.type || "jsonpath") ? " selected" : ""}>`
        + `${t}</option>`).join("")}
    </select>
    <input class="a-path" placeholder="data.items[0].id">
    <select class="a-op">
      ${A_OPS.map((o) => `<option value="${o}"${o === (a.op || "equals") ? " selected" : ""}>`
        + `${o}</option>`).join("")}
    </select>
    <input class="a-value" placeholder="value">
    <button type="button" class="sm a-del danger" title="remove">remove</button>`;
  list.appendChild(row);

  const type = row.querySelector(".a-type");
  const path = row.querySelector(".a-path");
  const op = row.querySelector(".a-op");
  const val = row.querySelector(".a-value");

  // seed the fields from the assertion we were given
  if (a.type === "status") {
    val.value = a.in ? JSON.stringify(a.in) : JSON.stringify(a.equals ?? a.value ?? 200);
  } else if (a.type === "responseTime") {
    val.value = JSON.stringify(a.value ?? 2000);
  } else if (a.type === "header") {
    path.value = a.name || a.header || "";
    val.value = a.value === undefined ? "" : JSON.stringify(a.value);
  } else if (a.type === "body_contains") {
    val.value = a.value === undefined ? "" : JSON.stringify(a.value);
  } else if (a.type !== "schema") {
    path.value = a.path || "";
    val.value = a.value === undefined ? "" : JSON.stringify(a.value);
  }

  const sync = () => {
    const t = type.value;
    path.hidden = !["jsonpath", "header"].includes(t);
    op.hidden = !["jsonpath", "header", "responseTime"].includes(t);
    val.hidden = t === "schema" || (op.hidden === false && NO_VALUE.has(op.value));
    path.placeholder = t === "header" ? "Content-Type" : "data.items[0].id";
    row.title = A_TYPE_HELP[t] || "";
    if (t === "responseTime" && !A_OPS.slice(11, 15).includes(op.value)) op.value = "lt";
  };
  type.addEventListener("change", sync);
  op.addEventListener("change", sync);
  row.querySelector(".a-del").addEventListener("click", () => row.remove());
  sync();
}

/** A value typed in the box is JSON when it parses, a string otherwise —
    so 200, true and ["a","b"] work without anyone escaping anything. */
function parseValue(text) {
  const t = (text || "").trim();
  if (t === "") return undefined;
  try { return JSON.parse(t); } catch { return t; }
}

function readAssertions(host) {
  return [...host.querySelectorAll(".arow2")].map((row) => {
    const t = row.querySelector(".a-type").value;
    const path = row.querySelector(".a-path").value.trim();
    const op = row.querySelector(".a-op").value;
    const value = parseValue(row.querySelector(".a-value").value);
    if (t === "schema") return { type: "schema" };
    if (t === "status") return Array.isArray(value)
      ? { type: "status", in: value } : { type: "status", equals: value ?? 200 };
    if (t === "responseTime") return { type: "responseTime", op, value: value ?? 2000 };
    if (t === "header") return { type: "header", name: path, op,
                                 ...(NO_VALUE.has(op) ? {} : { value }) };
    if (t === "body_contains") return { type: "body_contains", value };
    return { type: "jsonpath", path, op, ...(NO_VALUE.has(op) ? {} : { value }) };
  }).filter((a) => a.type !== "jsonpath" || a.path);
}

/* ------------------------------------------------------------------- jobs */

/* A run prints one line per operation. Parsing them back into rows turns a wall
   of text into something you can watch — and makes "which call is it on?"
   answerable while it is still running. */
const VERIFY_LINE =
  /^\s*(PASS|WARN|FAIL|SKIP|ERR)\s+(\d{3}|---)\s+(\d+)ms\s+(\S+ \S+?)(?:\s{3,}(.*))?$/;
const TESTS_LINE = /^\s*(PASS|FAIL|BLOCK|SKIP|ERR)\s+(.+?)\s{2,}(\d{3}|---)\s+(\d+)ms\s*$/;
const TESTS_PLAIN = /^\s*(PASS|FAIL|BLOCK|SKIP|ERR)\s+(.+?)\s*(\[(?:api|e2e)\])?\s*$/;
const PHASE = /^\s*(phase \d.*|SUITE .*|={10,})\s*$/;
const STARTED = /^\s*\.\.\.\.\s+(.+)$/;
const SKIPPED = /^\s*SKIP\s+(\S+ \S+)\s{2,}(.*)$/;
const TOTAL = /^\s*running now: (\d+)/;

const CLASS = { PASS: "pass", FAIL: "fail", ERR: "fail", WARN: "warn",
                BLOCK: "block", SKIP: "warn" };

function renderRun(text, rowsEl, progressEl, finished) {
  const rows = [];
  let done = 0, expected = 0;
  const inflight = new Map();          // started but not yet reported
  let announced = false;               // the run states its own total up front
  for (const line of (text || "").split("\n")) {
    const total = line.match(TOTAL);
    if (total) { expected = Number(total[1]); announced = true; continue; }

    const phase = line.match(PHASE);
    if (phase && !line.startsWith("=")) {
      rows.push(`<div class="runphase">${esc(phase[1])}</div>`);
      const n = line.match(/phase \d: (\d+)/);
      if (n && !announced) expected += Number(n[1]);
      continue;
    }
    let m = line.match(SKIPPED);
    if (m) { rows.push(row("SKIP", "", "", m[1], m[2])); continue; }

    m = line.match(STARTED);
    if (m) { inflight.set(m[1].trim(), rows.length); rows.push(null); continue; }

    m = line.match(VERIFY_LINE);
    if (m) {
      done += 1;
      const at = inflight.get(m[4].trim());
      const html = row(m[1], m[2], m[3], m[4], m[5]);
      if (at !== undefined) { rows[at] = html; inflight.delete(m[4].trim()); }
      else rows.push(html);
      continue;
    }
    m = line.match(TESTS_LINE);
    if (m) { done += 1; rows.push(row(m[1], m[3], m[4], m[2])); continue; }
    m = line.match(TESTS_PLAIN);
    if (m && CLASS[m[1]]) { done += 1; rows.push(row(m[1], "", "", m[2])); continue; }
    if (/^\s{4,}\S/.test(line) && rows.length) {      // indented detail under a row
      rows.push(`<div class="runnote">${esc(line.trim())}</div>`);
    }
  }
  // anything announced but never reported is still in flight — name it, so a
  // run that stalls says which call it is waiting on. Once the run is over
  // nothing can still be running, so say what it actually is.
  for (const [what, at] of inflight) {
    rows[at] = finished
      ? `<div class="runrow warn">
          <span class="v">?</span><span class="code"></span><span class="ms"></span>
          <span class="what" title="${esc(what)}">${esc(what)}</span>
          <span class="why2">no result line — see the raw log</span></div>`
      : `<div class="runrow inflight">
          <span class="v">···</span><span class="code"></span><span class="ms">running</span>
          <span class="what" title="${esc(what)}">${esc(what)}</span>
          <span class="why2">waiting for this one</span></div>`;
  }
  const html = rows.filter(Boolean).join("");
  rowsEl.innerHTML = html ? RUN_LEGEND + html
                          : '<div class="runnote">starting…</div>';
  rowsEl.scrollTop = rowsEl.scrollHeight;
  if (progressEl) {
    const waiting = inflight.size && !finished ? `  ·  ${inflight.size} in flight` : "";
    progressEl.textContent = (expected ? `${done} / ${expected}` : `${done} done`) + waiting;
  }
}

function row(verdict, code, ms, what, why) {
  return `<div class="runrow ${CLASS[verdict] || ""}">
    <span class="v">${esc(verdict)}</span>
    <span class="code">${esc(code || "")}</span>
    <span class="ms">${ms ? esc(ms) + "ms" : ""}</span>
    <span class="what" title="${esc(what)}">${esc(what)}</span>
    ${why ? `<span class="why2" title="${esc(why)}">${esc(why)}</span>` : ""}
  </div>`;
}

/* WARN is not a failure, and a wall of amber with no explanation reads like one */
const RUN_LEGEND = `
  <div class="runlegend">
    <span><b class="pass">PASS</b> the response matched what the spec declares</span>
    <span><b class="warn">WARN</b> nothing could be checked — usually the spec declares
      no response schema. Not a failure.</span>
    <span><b class="fail">FAIL</b> an undocumented status, or the body breaks its own
      schema</span>
  </div>`;

/** Poll a background job, streaming its output into `pre` as it arrives. */
async function followJob(jobId, pre, elapsed, cancelBtn, onDone, rowsEl, progressEl) {
  let stop = false;
  if (cancelBtn) {
    cancelBtn.hidden = false;
    cancelBtn.onclick = async () => {
      await api(`/api/job/${jobId}/cancel`, { method: "POST" });
      if (elapsed) elapsed.textContent = "cancelling…";
    };
  }
  let misses = 0;
  while (!stop) {
    let data;
    try {
      ({ data } = await api(`/api/job/${jobId}`));
    } catch (err) {
      // a dropped poll used to kill the loop silently, freezing the timer at
      // whatever it last read — keep trying, and say so if it stays broken
      misses += 1;
      if (elapsed) elapsed.textContent = `lost contact with the run (${misses})`;
      if (misses > 8) { if (cancelBtn) cancelBtn.hidden = true; break; }
      await new Promise((r) => setTimeout(r, 1500));
      continue;
    }
    misses = 0;
    if (data.error) { pre.textContent = data.error; break; }
    // output arrives line by line, so a slow run looks different from a stuck one
    pre.textContent = data.output || "starting…";
    pre.scrollTop = pre.scrollHeight;
    // a rendering bug must not stop the polling
    try { if (rowsEl) renderRun(data.output, rowsEl, progressEl, data.done); }
    catch (err) { console.error("renderRun", err); }
    if (elapsed) {
      elapsed.textContent = data.done
        ? `finished in ${data.seconds}s` + (data.cancelled ? " (cancelled)" : "")
        : `running — ${data.seconds}s`;
    }
    if (data.done) { stop = true; if (cancelBtn) cancelBtn.hidden = true;
                     if (onDone) await onDone(data); break; }
    await new Promise((r) => setTimeout(r, 1200));
  }
}

/* ------------------------------------------------------------ saved tests */

let SUGGESTED = null;

async function loadTests() {
  const { data } = await api("/api/tests");
  const suites = data.suites || [];
  const total = suites.reduce((n, s) => n + s.cases.length + s.scenarios.length, 0);
  $("testCount").textContent = total ? `${total} in ${suites.length} section(s)` : "none yet";
  $("navTests").textContent = total || "";

  // labels cut across sections; sections are modules
  const labels = new Set();
  suites.forEach((s) => [...s.cases, ...s.scenarios]
    .forEach((t) => (t.tags || []).forEach((x) => labels.add(x))));
  $("tagFilter").innerHTML = labels.size
    ? `<button type="button" class="chip${TAG_FILTER ? "" : " on"}" data-tag="">all</button>`
      + [...labels].sort().map((t) =>
          `<button type="button" class="chip${TAG_FILTER === t ? " on" : ""}"
             data-tag="${esc(t)}">${esc(t)}</button>`).join("")
    : "";
  $("tagFilter").querySelectorAll(".chip").forEach((c) =>
    c.addEventListener("click", () => { TAG_FILTER = c.dataset.tag; loadTests(); }));

  if (TAG_FILTER) {
    suites = suites.map((s) => ({
      ...s,
      cases: s.cases.filter((c) => (c.tags || []).includes(TAG_FILTER)),
      scenarios: s.scenarios.filter((c) => (c.tags || []).includes(TAG_FILTER)),
    })).filter((s) => s.cases.length || s.scenarios.length);
  }
  $("suiteList").innerHTML = suites.map((s) => `<option value="${esc(s.name)}">`).join("");
  if (!suites.length) {
    $("testTree").innerHTML = '<div class="empty">No suites yet — send a request and '
      + 'use “Save as test…”.</div>';
    return;
  }
  const OUT = (h) => {
    const k = (h && h.last_outcome) || "never";
    if (k === "never") return '<span class="outcome never">never run</span>';
    // an outcome is meaningless without the environment it came from
    const env = (h && (h.last_env || h.last_base_url)) || "?";
    const title = `last run ${h.last_run || ""} against ${h.last_base_url || ""}`;
    return `<span class="outcome ${esc(k)}" title="${esc(title)}">${esc(k)}</span>`
         + `<span class="tag" title="${esc(title)}">on ${esc(env)}</span>`;
  };

  const acts = (suite, id, stage, h) => {
    const canPromote = stage === "draft";
    const proven = h && h.last_outcome === "pass";
    return `<span class="tacts">
      <button class="sm tact" data-act="edit" data-suite="${esc(suite)}"
              data-stage="${esc(stage)}" data-id="${esc(id)}">edit</button>
      ${canPromote ? `<button class="sm tact" data-act="promote" data-suite="${esc(suite)}"
          data-stage="${esc(stage)}" data-id="${esc(id)}" ${proven ? "" : "disabled"}
          title="${proven ? "move into the shared suite"
                          : "must pass at least once before it can be shared"}">promote</button>` : ""}
      <button class="sm tact danger" data-act="delete" data-suite="${esc(suite)}"
              data-stage="${esc(stage)}" data-id="${esc(id)}">delete</button>
    </span>`;
  };

  const caseBlock = (s, c) => `
    <div class="titem">
      <div class="head">
        <span class="kind case">case</span>
        <span class="label">
          <div class="t">${esc(c.name || c.id)}</div>
          <div class="sub">${esc(c.method)} ${esc(c.path)}</div>
        </span>
        <span class="tag">${c.assertions} assertion${c.assertions === 1 ? "" : "s"}</span>
        ${OUT(c.history)}
        ${acts(s.name, c.id, s.stage, c.history)}
      </div>
    </div>`;

  const KIND_NOTE = {
    api: "pre-data, then the endpoint under test",
    e2e: "end-to-end flow, run as sanity",
  };

  const scenarioBlock = (s, sc) => `
    <div class="titem">
      <div class="head">
        <span class="kind ${esc(sc.kind)}">${esc(sc.kind)}</span>
        <span class="label">
          <div class="t">${esc(sc.name || sc.id)}</div>
          <div class="sub">${sc.steps.length} step${sc.steps.length === 1 ? "" : "s"}
            · ${esc(KIND_NOTE[sc.kind] || "")}</div>
        </span>
        ${OUT(sc.history)}
        ${acts(s.name, sc.id, s.stage, sc.history)}
      </div>
      <div class="steps">
        ${sc.steps.map((st, i) => `
          <div class="stepline">
            <span class="role ${esc(st.role || "step")}">${esc(st.role || "step")}</span>
            <span class="m ${esc(st.method)}">${esc(st.method)}</span>
            <span class="t">${esc(st.name || st.path)}</span>
            ${st.captures.length
              ? `<span class="arrow">captures ${st.captures.map(esc).join(", ")}</span>` : ""}
            <span class="tag">${st.assertions}</span>
          </div>`).join("")}
      </div>
    </div>`;

  // shared first: that is the repo's definition of working, drafts are in progress
  const order = { shared: 0, draft: 1 };
  suites.sort((a, b) => (order[a.stage] - order[b.stage]) || a.name.localeCompare(b.name));

  $("testTree").innerHTML = suites.map((s) => `
    <div class="suite">
      <header>
        <span class="name">${esc(s.name)}</span>
        <span class="stage ${esc(s.stage)}">${esc(s.stage)}</span>
        <span class="grow"></span>
        <span class="tag">${s.cases.length + s.scenarios.length} test(s)</span>
      </header>
      ${s.cases.map((c) => caseBlock(s, c)).join("")}
      ${s.scenarios.map((sc) => scenarioBlock(s, sc)).join("")}
      ${!s.cases.length && !s.scenarios.length
        ? '<div class="empty">empty suite</div>' : ""}
    </div>`).join("");

  $("testTree").querySelectorAll(".tact").forEach((b) =>
    b.addEventListener("click", () =>
      testAction(b.dataset.act, b.dataset.suite, b.dataset.id, b.dataset.stage)));
}

/* -- edit / promote / delete -------------------------------------------- */

async function testAction(act, suite, id, stage) {
  if (act === "delete") {
    const { data } = await api("/api/tests/delete",
      { method: "POST", body: JSON.stringify({ suite, id, stage }) });
    if (data.error) { banner("err", data.error); return; }
    banner("ok", `Deleted ${id} from ${suite}.`);
    await loadTests();
    return;
  }
  if (act === "promote") {
    const { data } = await api("/api/tests/promote",
      { method: "POST", body: JSON.stringify({ suite, id }) });
    if (!data.ok) { banner("err", data.error); return; }
    banner("ok", `${id}: ${data.message} (${data.file})`);
    await loadTests();
    return;
  }
  const { data } = await api(`/api/tests/one?suite=${encodeURIComponent(suite)}`
                             + `&id=${encodeURIComponent(id)}`
                             + `&stage=${encodeURIComponent(stage || "")}`);
  if (data.error) { banner("err", data.error); return; }
  EDITING = { suite, id, stage: data.stage, kind: data.kind, test: data.test };
  $("editTitle").textContent = `${suite} / ${id}  [${data.stage}]`;
  renderEditor(data);
  $("editTry").hidden = true;
  $("editDlg").showModal();
}

let EDITING = null;

function renderEditor(data) {
  const raw = $("editRawWrap");
  const built = $("editBuilt");
  if (data.kind === "scenario") {
    $("editHint").innerHTML = "Each step has its own assertions. Add or remove them below; "
      + "use <b>Raw JSON</b> to reorder steps or change requests.";
    built.innerHTML = (data.test.steps || []).map((st, i) => `
      <div class="stepedit">
        <h5><span class="role ${esc(st.role || "step")}">${esc(st.role || "step")}</span>
          <span class="m ${esc((st.request || {}).method || "GET")}">
            ${esc((st.request || {}).method || "GET")}</span>
          ${esc(st.name || (st.request || {}).path || "step " + (i + 1))}</h5>
        <div class="aedit" data-step="${i}"></div>
      </div>`).join("");
    (data.test.steps || []).forEach((st, i) =>
      assertionEditor(built.querySelector(`.aedit[data-step="${i}"]`), st.assertions || []));
  } else {
    $("editHint").innerHTML = `<code>${esc((data.test.request || {}).method || "")} `
      + `${esc((data.test.request || {}).path || "")}</code> — add, change or remove `
      + "assertions. Use <b>Raw JSON</b> for the request body or captures.";
    built.innerHTML = '<div class="aedit" data-step="case"></div>';
    assertionEditor(built.querySelector('.aedit[data-step="case"]'),
                    data.test.assertions || []);
  }
  $("editBody").value = JSON.stringify(data.test, null, 2);
  raw.hidden = true;
  built.hidden = false;
  $("btnEditRaw").textContent = "Raw JSON";
}

$("btnEditRaw").addEventListener("click", () => {
  const showingRaw = !$("editRawWrap").hidden;
  if (!showingRaw) {
    // carry whatever is in the builder into the JSON before showing it
    $("editBody").value = JSON.stringify(collectEdited(), null, 2);
  } else {
    try { renderEditor({ ...EDITING, test: JSON.parse($("editBody").value) }); return; }
    catch (e) { $("editHint").textContent = "Not valid JSON: " + e.message; return; }
  }
  $("editRawWrap").hidden = showingRaw;
  $("editBuilt").hidden = !showingRaw;
  $("btnEditRaw").textContent = showingRaw ? "Raw JSON" : "Back to the editor";
});

function collectEdited() {
  const test = JSON.parse(JSON.stringify(EDITING.test));
  if (EDITING.kind === "scenario") {
    [...$("editBuilt").querySelectorAll(".aedit")].forEach((host) => {
      const i = Number(host.dataset.step);
      if (test.steps && test.steps[i]) test.steps[i].assertions = readAssertions(host);
    });
  } else {
    test.assertions = readAssertions($("editBuilt").querySelector(".aedit"));
  }
  return test;
}

$("editSave").addEventListener("click", async () => {
  let body;
  if ($("editRawWrap").hidden) {
    body = collectEdited();
  } else {
    try { body = JSON.parse($("editBody").value); }
    catch (e) { $("editHint").textContent = "Not valid JSON: " + e.message; return; }
  }
  const { data } = await api("/api/tests/update", {
    method: "POST",
    body: JSON.stringify({ suite: EDITING.suite, id: EDITING.id,
                           stage: EDITING.stage, test: body }),
  });
  if (data.error) { $("editHint").textContent = data.error; return; }
  $("editDlg").close();
  banner("ok", `Updated ${EDITING.id} in ${data.file}.`);
  await loadTests();
});

$("editCancel").addEventListener("click", () => $("editDlg").close());

$("editRun").addEventListener("click", () => {
  let test;
  try { test = $("editRawWrap").hidden ? collectEdited() : JSON.parse($("editBody").value); }
  catch (e) { $("editHint").textContent = "Not valid JSON: " + e.message; return; }
  tryTest($("editTry"), test, $("editRun"), EDITING);
});

function fillTestEnvs() {
  $("testEnv").innerHTML = (ENVS || []).map((e) =>
    `<option value="${esc(e.name)}"${e.name === "mock" ? " selected" : ""}>` +
    `${esc(e.name)} — ${esc(e.base_url || "?")}${e.ready ? "" : "  ⚠"}</option>`).join("");
}

async function runTests(kinds) {
  $("testOutCard").hidden = false;
  $("testOut").textContent = "running…";
  const { data } = await api("/api/tests/run", {
    method: "POST",
    body: JSON.stringify({ env: $("testEnv").value, kinds, verbose: false,
                           drafts: $("testDrafts").checked,
                           tags: TAG_FILTER ? [TAG_FILTER] : [] }),
  });
  if (data.error) { $("testOut").textContent = data.error; return; }
  await followJob(data.job, $("testOut"), $("testElapsed"), $("btnCancelTests"),
                  null, $("testRows"), $("testProgress"));
  const rep = (await api(`/api/job/${data.job}/report`)).data || {};
  const s = rep.summary || {};
  const bits = Object.entries(s).map(([k, v]) => `${v} ${k}`).join("  ");
  const spec = (rep.spec || {});
  $("testOutHead").hidden = false;
  $("testOutHead").innerHTML =
    `<span class="meta"><span>environment: <b>${esc(rep.env || $("testEnv").value)}</b></span>` +
    `<span><code>${esc(rep.base_url || "")}</code></span>` +
    `<span>spec <code>${esc((spec.digest || "").slice(0, 12))}…</code>` +
    `${spec.state === "match" ? " (pinned)"
      : spec.state ? ` <b style="color:var(--err)">(${esc(spec.state)})</b>` : ""}</span>` +
    `<span>${esc(rep.ran_at || "")}</span></span>`;
  banner(s.fail || s.error || s.blocked ? "err" : "ok",
         `${rep.env || $("testEnv").value}: ${bits || "nothing ran"}`
         + (s.blocked ? " — blocked means the endpoint under test never ran; its setup failed."
                      : ""));
}

/* -- authoring ---------------------------------------------------------- */

function assertionLabel(a) {
  if (a.type === "status") return `status ${a.in ? "in " + JSON.stringify(a.in) : "= " + a.equals}`;
  if (a.type === "schema") return "body matches the spec schema";
  if (a.type === "responseTime") return `responds ${a.op} ${a.value}ms`;
  if (a.type === "header") return `header ${a.name} ${a.op} ${a.value ?? ""}`;
  if (a.type === "body_contains") return `body contains ${JSON.stringify(a.value)}`;
  return `${a.path || "(root)"} ${a.op}${a.value === undefined ? "" : " " + JSON.stringify(a.value)}`;
}

async function openSaveDialog() {
  const { data } = await api("/api/tests/suggest",
    { method: "POST", body: JSON.stringify({}) });
  if (data.error) { banner("err", data.error); return; }
  SUGGESTED = data;

  const method = data.request.method, path = data.request.path;
  // A test files under the module it exercises. The spec already groups
  // operations by tag, so that is the section — not a name someone invents.
  const suite = data.suite_hint || "regression";
  $("suiteList").innerHTML = (data.suites || []).concat(
    data.suite_hint && !(data.suites || []).includes(data.suite_hint) ? [data.suite_hint] : []
  ).map((n) => `<option value="${esc(n)}">`).join("");
  $("dlgSuite").value = suite;

  const slug = (method + path).toLowerCase().replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "").slice(0, 48);
  $("dlgId").value = slug;
  $("dlgName").value = data.summary
    ? `${data.summary} returns ${data.status}`
    : `${method} ${path} returns ${data.status}`;

  const preset = ["smoke", "regression", "negative", "edge", "sanity"];
  const guess = data.status >= 400 ? "negative" : "smoke";
  $("dlgTags").innerHTML = preset.map((t) =>
    `<button type="button" class="chip${t === guess ? " on" : ""}" data-tag="${t}">${t}</button>`
  ).join("");
  $("dlgTags").querySelectorAll(".chip").forEach((c) =>
    c.addEventListener("click", () => c.classList.toggle("on")));
  $("dlgTagsExtra").value = "";
  describeDest();
  // suggestions start in the editor, where they can be changed or removed and
  // anything the mock did not think of can be added
  assertionEditor($("dlgAsserts"), data.assertions.map((a) => {
    const { _suggested, name, ...rest } = a; return rest;
  }));
  const caps = Object.entries(data.capture || {});
  $("dlgCaptures").innerHTML = caps.length ? caps.map(([name, p]) => `
    <label class="arow">
      <input type="checkbox" data-name="${esc(name)}" data-path="${esc(p)}">
      <code>{{${esc(name)}}} = ${esc(p)}</code>
    </label>`).join("") : '<div class="hint">No ids in this response to capture.</div>';
  $("dlgHint").textContent = `${method} ${path} → ${data.status}`;
  $("dlgTry").hidden = true;
  $("saveDlg").showModal();
}

function describeDest() {
  const suite = $("dlgSuite").value.trim() || "regression";
  const scenario = $("dlgTarget").value !== "case";
  $("dlgWhere").innerHTML =
    `Writes to <code>tests/drafts/${esc(suite)}.json</code>`
    + (scenario ? ` as a step of scenario <code>${esc($("dlgScenario").value.trim()
        || $("dlgId").value.trim() || "…")}</code>` : " as a case")
    + `. Drafts are gitignored — promote it once it passes and it moves to `
    + `<code>tests/${esc(suite)}.json</code>.`;
}
$("dlgSuite").addEventListener("input", describeDest);
$("dlgScenario").addEventListener("input", describeDest);

$("dlgTarget").addEventListener("change", () => {
  const scenario = $("dlgTarget").value !== "case";
  $("dlgScenarioWrap").hidden = !scenario;
  $("dlgRoleWrap").hidden = $("dlgTarget").value !== "api";
  describeDest();
});

$("dlgCancel").addEventListener("click", () => $("saveDlg").close());

/* A step that needs pre-data cannot be judged on its own: running it alone just
   reports an undefined variable. So for a scenario step, splice it into the
   scenario it is being added to and run the chain. */
$("dlgRun").addEventListener("click", async () => {
  const entry = buildEntry();
  if (!entry) return;
  const kind = $("dlgTarget").value;
  const suite = $("dlgSuite").value.trim();

  if (kind === "case") { tryTest($("dlgTry"), entry, $("dlgRun"), { suite }); return; }

  const scenarioId = $("dlgScenario").value.trim() || entry.id;
  entry.role = kind === "api" ? $("dlgRole").value : "step";
  let steps = [];
  const { data } = await api(`/api/tests/one?suite=${encodeURIComponent(suite)}`
                             + `&id=${encodeURIComponent(scenarioId)}`);
  if (data && data.test && data.test.steps) {
    steps = data.test.steps.filter((s) => s.name !== entry.name);
  }
  // a setup step joins the others in order; the target always runs last
  if (entry.role === "target") steps = [...steps.filter((s) => s.role !== "target"), entry];
  else steps = [...steps, entry];

  $("dlgTry").hidden = false;
  $("dlgTry").innerHTML = `<span class="hint" style="margin:0">running ${steps.length} step(s)`
    + `${steps.length > 1 ? " — the earlier ones first, so this one has its data" : ""}…</span>`;
  tryTest($("dlgTry"), { id: scenarioId, name: scenarioId, kind, steps },
          $("dlgRun"), { suite });
});

/* The entry the dialog currently describes — used by both Save and Run now, so
   what you test is exactly what you would save. */
function buildEntry() {
  if (!SUGGESTED) return null;
  const picked = readAssertions($("dlgAsserts"));
  if (!picked.length) { banner("err", "A test needs at least one assertion."); return null; }
  const capture = {};
  $("dlgCaptures").querySelectorAll("input:checked")
    .forEach((el) => { capture[el.dataset.name] = el.dataset.path; });

  const req = SUGGESTED.request;
  const headers = {};
  (req.headers || "").split("\n").forEach((line) => {
    if (line.includes(":")) {
      const [k, ...rest] = line.split(":");
      headers[k.trim()] = rest.join(":").trim();
    }
  });
  const tags = [...$("dlgTags").querySelectorAll(".chip.on")].map((c) => c.dataset.tag)
    .concat($("dlgTagsExtra").value.split(",").map((t) => t.trim()).filter(Boolean));
  const entry = {
    id: $("dlgId").value.trim(),
    name: $("dlgName").value.trim(),
    ...(tags.length ? { tags } : {}),
    request: {
      method: req.method, path: req.path,
      ...(Object.keys(req.query || {}).length ? { query: req.query } : {}),
      ...(Object.keys(headers).length ? { headers } : {}),
      ...(req.body ? { body: JSON.parse(req.body) } : {}),
    },
    assertions: picked,
    ...(Object.keys(capture).length ? { capture } : {}),
  };

  return entry;
}

$("dlgSave").addEventListener("click", async () => {
  const entry = buildEntry();
  if (!entry) return;
  const target = $("dlgTarget").value;
  const payload = { suite: $("dlgSuite").value.trim(), case: entry };
  if (target !== "case") {
    entry.role = target === "api" ? $("dlgRole").value : undefined;
    payload.scenario = { id: $("dlgScenario").value.trim() || entry.id,
                         name: $("dlgScenario").value.trim() || entry.name,
                         kind: target };
  }
  const { data } = await api("/api/tests/save",
    { method: "POST", body: JSON.stringify(payload) });
  if (data.error) { banner("err", data.error); return; }
  $("saveDlg").close();
  banner("ok", `Saved to ${data.file} — ${data.cases} case(s), ${data.scenarios} scenario(s).`);
  await loadTests();
});

/* ------------------------------------------------------- import / generate */

$("btnImport").addEventListener("click", () => {
  $("impOperation").innerHTML = '<option value="">— pick an operation —</option>'
    + (ROUTES || []).map((r) =>
        `<option value="${esc(r.method + " " + r.path)}">${esc(r.method)} ${esc(r.path)}</option>`)
      .join("");
  $("impErrors").innerHTML = "";
  $("importDlg").showModal();
});

$("impCancel").addEventListener("click", () => $("importDlg").close());

$("btnCopyPrompt").addEventListener("click", async () => {
  const op = $("impOperation").value;
  const { data } = await api("/api/tests/prompt?operation=" + encodeURIComponent(op));
  if (data.error) { $("impErrors").innerHTML = `<p class="hint">${esc(data.error)}</p>`; return; }
  const copied = await copyText(data.prompt, $("btnCopyPrompt"));
  $("impErrors").innerHTML = copied
    ? `<p class="hint">Brief copied${op ? " for " + esc(op) : ""} — paste it into whichever
       assistant your team uses, then paste the JSON it returns back here.</p>`
    : `<p class="hint">Clipboard blocked; the brief is in the response panel.</p>`;
});

$("impSave").addEventListener("click", async () => {
  const { data } = await api("/api/tests/import", {
    method: "POST",
    body: JSON.stringify({ suite: $("impSuite").value.trim(), tests: $("impBody").value }),
  });
  if (!data.ok) {
    $("impErrors").innerHTML = `<p class="hint" style="color:var(--err)">Refused — nothing
      was written:</p><ul class="hint" style="margin-top:4px">`
      + (data.errors || []).map((e) => `<li>${esc(e)}</li>`).join("") + "</ul>";
    return;
  }
  $("importDlg").close();
  banner("ok", `Imported ${data.imported} test(s) into ${data.file}`
             + (data.replaced ? ` (${data.replaced} replaced)` : "")
             + " — run them, then promote the ones that pass.");
  await loadTests();
});

/* ----------------------------------------------------------- integration */

async function loadIntegration() {
  const { data } = await api("/api/integration");
  if (!data.running) { $("integ").hidden = true; $("integOff").hidden = false; return; }
  $("integ").hidden = false; $("integOff").hidden = true;
  const base = data.base_url;
  const path = data.sample_path || "/";
  $("integMode").textContent = data.validation_mode === "spec"
    ? "bad requests -> 422 + detail[]" : "bad requests -> 400 + field list";

  const snip = (title, code, id) => `
    <div class="snip"><h4>${title}<span class="grow"></span>
      <button class="sm copy" data-copy="${esc(code)}">copy</button></h4>
      <pre>${esc(code)}</pre></div>`;

  const snippets = [
    snip(".env — Vite / React / Next",
`VITE_API_BASE_URL=${base}
REACT_APP_API_BASE_URL=${base}
NEXT_PUBLIC_API_BASE_URL=${base}`),
    snip("axios",
`const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL,  // ${base}
});
// no token needed unless the mock runs with --require-auth`),
    snip("Angular — environment.ts",
`export const environment = {
  production: false,
  apiBaseUrl: '${base}',
};`),
    snip("curl",
`curl ${base}${path}
curl ${base}${path} -H 'X-Mock-Scenario: empty'
curl ${base}${path} -H 'X-Mock-Status: 500'`),
    snip("Dev-server proxy (Vite)",
`server: {
  proxy: { '/api': { target: '${base}', changeOrigin: true } },
}`),
    snip("Playwright / Cypress",
`// point the app at the mock, then force an error path per test
await page.route('**/api/**', (route) =>
  route.continue({ headers: { ...route.request().headers(),
                              'X-Mock-Status': '500' } }));`),
  ].join("");

  const facts = [
    ["Reachable at", `<code>${esc(base)}</code>${data.lan_url
        ? `<br><span class="hint" style="margin:4px 0 0">teammates on your network: <code>${esc(data.lan_url)}</code></span>` : ""}`],
    ["CORS", "wide open — any origin, any header. No proxy needed for local dev."],
    ["Auth", data.require_auth
        ? "<b>required</b> — requests without an <code>Authorization</code> header get the documented 401."
        : "not enforced. Send any token or none. Start with <i>Require Authorization</i> to test 401 handling."],
    ["State", data.stateful
        ? "<b>stateful</b> — what you POST comes back from GET, and DELETE makes it 404."
        : "stateless — every GET returns the same payload. Turn on <i>Stateful CRUD</i> for create→read→delete flows."],
  ].map(([k, v]) => `<div class="fact"><b>${k}</b>${v}</div>`).join("");

  $("integBody").innerHTML = `
    <div class="url">
      <span class="lbl">Base URL</span>
      <code id="baseUrlText">${esc(base)}</code>
      <button class="sm copy" data-copy="${esc(base)}">copy</button>
      <span class="grow"></span>
      <span class="hint" style="margin:0">serving <code>${esc(data.spec || "")}</code></span>
    </div>
    <div class="facts">${facts}</div>
    <div class="snips">${snippets}</div>

    <details open>
      <summary>What happens when the request is wrong</summary>
      <div class="snips" style="margin-top:9px">
        ${snip("Missing or invalid fields -> 422 (same shape as the real API)",
`POST ${base}/api/v1/account/create   {"username": "x"}

422 Unprocessable Entity
{"detail": [
  {"loc": ["body","username"], "msg": "'x' is too short", "type": "string_too_short"},
  {"loc": ["body","email"],    "msg": "Field required",   "type": "missing"}
]}`)}
        ${snip("Bad query param -> 422",
`GET ${base}/api/v1/account/list?status=Bogus

422 {"detail": [{"loc": ["query","status"],
     "msg": "'Bogus' not in allowed values ['Online','Offline','Away']",
     "type": "enum"}]}`)}
        ${snip("Missing required query param -> 422",
`GET ${base}/api/v1/reference/cities

422 {"detail": [
  {"loc": ["query","country_id"], "msg": "required parameter missing", "type": "missing"},
  {"loc": ["query","state_id"],   "msg": "required parameter missing", "type": "missing"}]}`)}
        ${snip("Path not in the spec -> 404 with a hint",
`GET ${base}/api/v1/nope

404 {"mock_error": "no operation GET /api/v1/nope in spec",
     "hint": "path not in spec — check /_mock/routes"}

POST ${base}/api/v1/account/list
404 {"hint": "path exists but not for POST; documented: ['/api/v1/account/list']"}`)}
      </div>
      <p class="hint">Every rejection is logged with the full field-by-field reason — see
        <b>Recent requests</b> below, or <code>${esc(base)}/_mock/log</code>. Switch
        <i>Bad requests answer with</i> to the 400 form if you want that detail inline
        while debugging.</p>
    </details>

    <details>
      <summary>Headers a dev or tester can send to steer the mock</summary>
      <div class="snip" style="margin-top:9px"><pre>${esc(
`X-Mock-Status: 500          return a specific status for this call
X-Mock-Scenario: empty      a named scenario — empty list, page_full, ...
X-Mock-Nulls: on            null every nullable field, keeping the structure
X-Mock-Delay: 1500          delay the response, for loaders and timeouts
X-Mock-Example: cancelled   pick a named example from the spec

Responses come back tagged so you know where the body came from:
X-Mock-Source: overlay | spec:example | generated | synthesized | stateful
X-Mock-Operation: GET /api/v1/account/list`)}</pre></div>
    </details>`;

  document.querySelectorAll(".copy").forEach((b) =>
    b.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(b.dataset.copy); }
      catch { return; }
      const old = b.textContent; b.textContent = "copied"; b.classList.add("copied");
      setTimeout(() => { b.textContent = old; b.classList.remove("copied"); }, 1200);
    }));
}

/* ---------------------------------------------------------------- source */

const LOCK_STATE = {
  match: ["pass", "pinned", "this is the document the team agreed on"],
  drift: ["fail", "not the pinned document", "results below are about something else"],
  unlocked: ["blocked", "not pinned", "nothing ties a result to a document yet"],
};

async function loadSpecStatus() {
  const { data } = await api("/api/spec/status?spec=" + encodeURIComponent($("spec").value.trim()));
  if (data.error) {
    $("specStatus").innerHTML = `<span class="hint">${esc(data.error)}</span>`; return;
  }
  const st = data.state || {};
  const [cls, label, note] = LOCK_STATE[st.state] || LOCK_STATE.unlocked;
  $("navLock").innerHTML = st.state === "match" ? "pinned"
    : `<span style="color:var(--err)">${esc(st.state || "?")}</span>`;

  const sum = data.summary || {};
  const lock = data.lock;
  $("specStatus").innerHTML = `
    <div class="url" style="margin-bottom:13px">
      <span class="lbl">Loaded</span>
      <code style="font-size:14px">${esc(data.source)}</code>
      <span class="outcome ${cls}">${esc(label)}</span>
      <span class="grow"></span>
      <code style="font-size:12px;color:var(--muted)">${esc((st.digest || "").slice(0, 16))}…</code>
    </div>
    <p class="hint" style="margin-top:0">${esc(st.message || note)}</p>
    <div class="dims">
      ${dimTable("This document", [
        ["title", esc(sum.title || "—")],
        ["version", esc(sum.version || "—")],
        ["OpenAPI", esc(sum.openapi || "—")],
        ["operations", sum.operations ?? "—"],
        ["component schemas", sum.schemas ?? "—"],
        ["fetched", esc(data.fetched_at || "—")],
      ])}
      ${dimTable("spec.lock.json", lock ? [
        ["digest", `<code>${esc((lock.digest || "").slice(0, 16))}…</code>`],
        ["source", `<code>${esc(lock.source)}</code>`],
        ["operations", lock.operations],
        ["locked at", esc(lock.locked_at)],
        ["locked by", esc(lock.locked_by)],
        ["note", esc(lock.note || "—")],
      ] : [["", "nothing pinned yet"]])}
    </div>
    <div class="btnrow">
      <button class="sm" id="btnLockThis">${lock ? "Re-pin this document" : "Pin this document"}</button>
      ${st.state === "drift"
        ? '<button class="sm" id="btnDiffLock">What changed?</button>' : ""}
    </div>
    <p class="hint">Pinning rewrites <code>spec.lock.json</code>. Commit it — that is what
      makes a spec change visible to everyone else.</p>`;

  const pin = $("btnLockThis");
  if (pin) pin.addEventListener("click", () => lockSpec(data.source));
  const dd = $("btnDiffLock");
  if (dd) dd.addEventListener("click", () => showDiff(data.source));
}

function dimTable(title, rows) {
  return `<div class="dim"><h4>${esc(title)}</h4><table>` +
    rows.map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join("") +
    "</table></div>";
}

async function lockSpec(path) {
  const note = prompt("Why this version? (recorded in the lockfile)", "") ?? "";
  const { data } = await api("/api/spec/lock",
    { method: "POST", body: JSON.stringify({ spec: path, note }) });
  if (!data.ok) { banner("err", data.error); return; }
  banner("ok", data.message);
  await loadSpecStatus();
}

async function showDiff(path) {
  const { data } = await api("/api/spec/diff",
    { method: "POST", body: JSON.stringify({ spec: path }) });
  if (data.error) { banner("err", data.error); return; }
  renderCandidate(path, null, null, data);
}

function renderCandidate(name, summary, state, diff) {
  $("candidateCard").hidden = false;
  $("candName").textContent = name;
  const st = state || {};
  const rows = summary ? dimTable("The candidate", [
    ["title", esc(summary.title || "—")],
    ["version", esc(summary.version || "—")],
    ["operations", summary.operations],
    ["component schemas", summary.schemas],
  ]) : "";
  const d = diff ? `
    <div class="dim" style="margin-top:11px">
      <h4>Against the pinned source</h4>
      <table>
        <tr><td>unchanged</td><td>${diff.common}</td></tr>
        <tr><td>added</td><td style="color:var(--ok)">${diff.added.length}</td></tr>
        <tr><td>removed</td><td style="color:var(--err)">${diff.removed.length}</td></tr>
      </table>
      ${diff.removed.length && !diff.added.length
        ? `<p class="hint" style="color:var(--err)">Only removals — that is what loading an
           <b>older</b> export looks like.</p>` : ""}
      ${(diff.added.length || diff.removed.length) ? `<div class="offenders" style="margin-top:9px">
        ${diff.added.map((o) => `<div style="color:var(--ok)">+ ${esc(o)}</div>`).join("")}
        ${diff.removed.map((o) => `<div style="color:var(--err)">- ${esc(o)}</div>`).join("")}
      </div>` : ""}
    </div>` : "";

  $("candBody").innerHTML = `
    <p class="hint" style="margin-top:0">${esc(st.message || "Saved as a candidate. Nothing "
      + "is using it yet.")}</p>
    <div class="dims">${rows}</div>${d}
    <div class="btnrow">
      <button class="sm" id="btnCandUse">Load it into the mock</button>
      <button class="sm" id="btnCandDiff">Compare with the pinned source</button>
      <button class="sm" id="btnCandLock">Adopt and pin it</button>
    </div>
    <p class="hint">Loading it runs the mock against this document without changing what the
      team is pinned to — which is how you compare old against new on purpose.</p>`;

  $("btnCandUse").addEventListener("click", async () => {
    $("spec").value = name;
    showView("server");
    banner("ok", `Spec set to ${name}. Press Restart to load it.`);
  });
  $("btnCandDiff").addEventListener("click", () => showDiff(name));
  $("btnCandLock").addEventListener("click", () => lockSpec(name));
}

/* ------------------------------------------------- the project's document */

async function loadProjectSpec() {
  const { data } = await api("/api/project");
  if (!data || data.error) return;
  $("projSpecTag").textContent = data.spec;
  $("projSpec").innerHTML = (data.specs || [])
    .map((sp) => `<option value="${esc(sp)}"${sp === data.spec ? " selected" : ""}>`
               + `${esc(sp)}</option>`).join("");
  renderModulePins(data.modules || [], data.spec);
  fillCompare(data.specs, data.spec);
}

/* A module pinned elsewhere is an exception worth seeing, not a hidden setting. */
function renderModulePins(modules, projectSpec) {
  const pinned = modules.filter((m) => m.overridden);
  const missing = modules.filter((m) => !m.exists);
  $("projModules").innerHTML = `
    <div class="dim" style="margin-top:11px">
      <h4>What each part uses</h4>
      <table>${modules.map((m) => `
        <tr>
          <td style="width:90px">${esc(m.module)}</td>
          <td><code>${esc(m.spec)}</code></td>
          <td style="color:var(--dim)">${esc(m.from)}</td>
          <td style="width:80px;text-align:right">${m.overridden
            ? `<button class="sm" data-unpin="${esc(m.module)}">Unpin</button>` : ""}</td>
        </tr>`).join("")}
      </table>
      ${missing.length ? `<p class="hint" style="color:var(--err)">
        ${missing.map((m) => esc(m.spec)).join(", ")} does not exist.</p>` : ""}
      <p class="hint">${pinned.length
        ? `${pinned.length} module(s) deliberately pinned away from `
          + `<code>${esc(projectSpec)}</code>.`
        : "Everything follows the project spec."}
        Pin one for a single run with <code>MOCKD_SPEC_VERIFY=…</code>.</p>
    </div>`;
  $("projModules").querySelectorAll("[data-unpin]").forEach((b) =>
    b.addEventListener("click", () => setProjectSpec(null, b.dataset.unpin, true)));
}

async function setProjectSpec(spec, module, clear) {
  const { data } = await api("/api/project", {
    method: "POST",
    body: JSON.stringify({ spec, module: module || null, clear: !!clear }),
  });
  if (!data.ok) { banner("err", data.error); return; }
  banner("ok", data.message || "updated");
  // everything downstream was computed from the old document
  await loadProjectSpec();
  await loadSpecStatus();
  await refreshState();
  await loadCoverage();
  ROUTES = [];                      // the explorer's list belongs to the old spec
  GUIDE = "";                       // so the authoring grade is re-read too
}

$("btnProjSave").addEventListener("click", () => setProjectSpec($("projSpec").value));

/* ------------------------------------------------------- comparing specs */

const SEV = {
  breaking: { label: "Breaking", colour: "var(--err)",
              blurb: "existing callers or consumers stop working" },
  note:     { label: "Worth seeing", colour: "var(--warn)",
              blurb: "harmful only in particular readings" },
  additive: { label: "Additive", colour: "var(--ok)",
              blurb: "new surface; nothing that worked before stops" },
};

function fillCompare(specs, active) {
  const options = (specs || []).map((sp) => `<option value="${esc(sp)}">${esc(sp)}</option>`)
    .join("");
  $("cmpFrom").innerHTML = options;
  $("cmpTo").innerHTML = options;
  // the useful default: what we are pinned to, against what was fetched last
  if (active && specs.includes(active)) $("cmpFrom").value = active;
  const other = (specs || []).find((sp) => sp !== $("cmpFrom").value);
  if (other) $("cmpTo").value = other;
}

async function runCompare() {
  const from = $("cmpFrom").value, to = $("cmpTo").value;
  $("btnCompare").disabled = true;
  $("cmpOut").innerHTML = '<p class="hint">Comparing every field in both documents…</p>';
  try {
    const { data } = await api("/api/spec/compare",
      { method: "POST", body: JSON.stringify({ from, to }) });
    if (!data.ok) { $("cmpOut").innerHTML = ""; banner("err", data.error); return; }
    renderCompare(data);
  } catch (err) {
    $("cmpOut").innerHTML = "";
    banner("err", `Could not compare: ${err.message}`);
  } finally {
    $("btnCompare").disabled = false;
  }
}

function renderCompare(d) {
  $("cmpExport").hidden = d.identical;
  $("cmpTag").textContent = `${d.from.operations} -> ${d.to.operations} operations`;
  if (d.identical) {
    $("cmpOut").innerHTML = '<p class="hint" style="color:var(--ok)">'
      + "No differences in shape. The two documents describe the same API.</p>";
    return;
  }
  const tiles = Object.keys(SEV).map((k) => `
    <div class="tile"><div class="n" style="color:${SEV[k].colour}">${d.counts[k] || 0}</div>
      <div class="l">${SEV[k].label}</div></div>`).join("");

  // grouped by operation so one endpoint's story reads together
  const byOp = {};
  d.changes.forEach((c) => (byOp[c.operation] = byOp[c.operation] || []).push(c));
  const worst = (list) => list.some((c) => c.severity === "breaking") ? 0
                        : list.some((c) => c.severity === "note") ? 1 : 2;
  const ops = Object.keys(byOp).sort((a, b) => worst(byOp[a]) - worst(byOp[b])
                                            || a.localeCompare(b));

  $("cmpOut").innerHTML = `
    <div class="dims" style="margin-top:11px">${tiles}</div>
    <p class="hint">${esc(d.from.name)} → ${esc(d.to.name)}.
      Breaking first; within an operation, worst first.</p>
    <div class="ops" style="max-height:440px">
      ${ops.map((op) => `
        <details class="op" style="display:block">
          <summary style="cursor:pointer">
            <b>${esc(op)}</b>
            ${Object.keys(SEV).map((k) => {
              const n = byOp[op].filter((c) => c.severity === k).length;
              return n ? `<span class="tag" style="color:${SEV[k].colour}">${n} ${k}</span>` : "";
            }).join("")}
          </summary>
          <table style="margin:6px 0 10px 0">
            ${byOp[op].map((c) => `
              <tr>
                <td style="width:96px;color:${SEV[c.severity].colour}">${esc(SEV[c.severity].label)}</td>
                <td style="width:250px">${esc(c.what)}</td>
                <td><code>${esc(c.detail || "")}</code>
                    <div class="hint" style="margin:2px 0 0 0">${esc(c.why)}</div></td>
              </tr>`).join("")}
          </table>
        </details>`).join("")}
    </div>`;
}

async function exportCompare(download) {
  const body = JSON.stringify({
    from: $("cmpFrom").value, to: $("cmpTo").value,
    format: $("cmpFormat").value, breaking_only: $("cmpBreakingOnly").checked,
  });
  const { data } = await api("/api/spec/compare/export", { method: "POST", body });
  if (!data.ok) { banner("err", data.error); return; }
  if (!download) { await copyText(data.text, $("btnCmpCopy")); return; }
  // a real file, so it can be attached to a ticket or sent on
  const blob = new Blob([data.text], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = data.filename;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
  banner("ok", `Saved ${data.filename}`);
}

$("btnCmpCopy").addEventListener("click", () => exportCompare(false));
$("btnCmpDownload").addEventListener("click", () => exportCompare(true));
$("btnCompare").addEventListener("click", runCompare);

$("toastClose").addEventListener("click", hideBanner);

$("specPick").addEventListener("change", () => {
  const picked = $("specPick").value;
  if (picked === "__custom__") { showSpecPath(true); $("spec").focus(); return; }
  $("spec").value = picked;
  showSpecPath(false);
  $("urlOpts").hidden = true;
  loadSpecStatus();
});

$("spec").addEventListener("change", () => {
  const typed = $("spec").value.trim();
  $("specPick").value = SPEC_CHOICES.includes(typed) ? typed : "__custom__";
});
$("btnSpecStatus").addEventListener("click", loadSpecStatus);

$("btnSpecFetch").addEventListener("click", async () => {
  const url = $("specUrl").value.trim();
  if (!url) { banner("err", "Give a Swagger/OpenAPI URL."); return; }
  if (!/^https?:\/\//i.test(url)) {
    banner("err", `A spec URL needs its scheme — try https://${url}`);
    return;
  }
  $("btnSpecFetch").disabled = true;
  banner("ok", `Fetching ${url}…`);
  try {
    const { data } = await api("/api/spec/fetch", {
      method: "POST",
      body: JSON.stringify({ url, headers: $("specUrlHeaders").value,
                             save_as: $("specSaveAs").value.trim() }),
    });
    if (!data.ok) { banner("err", data.error || "the fetch failed"); return; }
    banner("ok", data.resolved_from
      ? `${data.resolved_from} is a documentation page — read the spec it points at `
        + `(${data.url}): ${data.summary.operations} operation(s) into ${data.file}`
      : `Fetched ${data.summary.operations} operation(s) into ${data.file}`);
    renderCandidate(data.file, data.summary, data.state, null);
  } catch (err) {
    // a thrown request must not leave the button dead
    banner("err", `Could not reach the console: ${err.message}`);
  } finally {
    $("btnSpecFetch").disabled = false;
  }
});

$("btnSpecUseUrl").addEventListener("click", () => {
  const url = $("specUrl").value.trim();
  if (!url) { banner("err", "Give a Swagger/OpenAPI URL."); return; }
  $("spec").value = url;
  $("specHeaders").value = $("specUrlHeaders").value;
  $("urlOpts").hidden = false;
  showView("server");
  banner("ok", "Spec set to the URL. Press Restart — the mock will re-check it on a poll.");
});

$("specFile").addEventListener("change", async (ev) => {
  const f = ev.target.files[0];
  if (!f) return;
  $("specPaste").value = await f.text();
  banner("ok", `${f.name} read — press “Import as candidate”.`);
  $("specPaste").dataset.name = f.name;
});

$("btnSpecUpload").addEventListener("click", async () => {
  const content = $("specPaste").value.trim();
  if (!content) { banner("err", "Choose a file or paste a document."); return; }
  const name = $("specPaste").dataset.name || "uploaded.json";
  const { data } = await api("/api/spec/upload",
    { method: "POST", body: JSON.stringify({ content, name }) });
  if (!data.ok) { banner("err", data.error); return; }
  banner("ok", `Imported ${data.summary.operations} operation(s) as ${data.file}`);
  renderCandidate(data.file, data.summary, data.state, null);
});

/* ------------------------------------------------------------- authoring */

async function loadRules() {
  const q = new URLSearchParams({ spec: $("spec").value.trim() });
  const { data } = await api("/api/spec-report?" + q);
  if (data.error) { $("ruleBody").innerHTML = `<span class="hint">${esc(data.error)}</span>`; return; }
  $("ruleSpec").textContent = `${data.operations} operations`;
  $("navScore").textContent = data.score + "%";

  const bar = `
    <div class="tiles" style="margin-bottom:14px">
      <div class="tile ${data.score >= 80 ? "good" : data.score >= 50 ? "mid" : "bad"}">
        <div class="n">${data.score}%</div>
        <div class="k">rule compliance</div>
        <div class="d">across ${data.rules.length} rules, weighted by how many operations
          each one applies to</div>
      </div>
      <div class="tile info"><div class="n">${data.operations}</div>
        <div class="k">operations graded</div>
        <div class="d">${esc(data.title || "")} · OpenAPI ${esc(data.version || "")}</div></div>
    </div>`;

  $("ruleBody").innerHTML = bar + data.rules.map((r) => {
    const passed = r.fail === 0;
    return `<div class="rule ${passed ? "pass" : "fail"}">
      <div class="head">
        <span class="code">${esc(r.rule)}</span>
        <span class="t">${esc(r.title)}<span class="why">${esc(r.why)}</span></span>
        <span class="score">${passed ? "\u2713 pass"
          : `${r.fail} of ${r.total} fail`}</span>
      </div>
      <div class="detail">${esc(r.detail)}</div>
      ${r.offenders.length ? `<details style="margin:0 12px 10px;border:0;padding:0">
          <summary>Show ${r.fail} failing</summary>
          <div class="offenders">${r.offenders.map((o) =>
            `<div>${esc(o)}</div>`).join("")}
            ${r.fail > r.offenders.length
              ? `<div>… and ${r.fail - r.offenders.length} more</div>` : ""}</div>
        </details>` : ""}
    </div>`;
  }).join("");
}

async function loadGuide() {
  const { data } = await api("/api/spec-guide");
  $("guideText").textContent = data.markdown || data.error || "";
  GUIDE = data.markdown || "";
}
let GUIDE = "";

/* -------------------------------------------------------------- coverage */

async function loadCoverage() {
  const q = new URLSearchParams({ spec: $("spec").value.trim(),
                                  overlay: $("overlay").value.trim() });
  const { data } = await api("/api/coverage?" + q);
  if (data.error) { $("covBody").innerHTML = `<span class="hint">${esc(data.error)}</span>`; return; }
  COVERAGE = data;
  $("covSpec").textContent = data.total + " operations";
  $("navCov").textContent = `${data.safe_to_build_on}/${data.total}`;

  const st = data.states, d = data.dimensions;
  const pct = (n) => Math.round((n / data.total) * 100);
  const sr = d.success_response, rb = d.request_body, fr = d.failure_responses;

  const tr = data.trust || {};
  const tiles = [
    ["good", data.safe_to_build_on + " / " + data.total, "safe to build on",
     "declared by the spec, agreed by the team, or verified against a real environment"],
    ["good", st.complete, "fully documented",
     "success response shape, request schema and failure responses all declared"],
    ["bad", st.undocumented, "no response shape",
     "the spec says nothing about what these return"],
    ["mid", tr.guess || 0, "still a guess",
     "mockd inferred the payload and nobody has confirmed it yet"],
  ].map(([cls, n, k, dd]) =>
    `<div class="tile ${cls}"><div class="n">${n}</div><div class="k">${k}</div>
     <div class="d">${dd}</div></div>`).join("");

  const seg = (n, color, title) => n
    ? `<span style="width:${pct(n)}%;background:${color}" title="${title}: ${n}"></span>` : "";

  const dim = (title, rows) =>
    `<div class="dim"><h3>${title}</h3><table>` +
    rows.map(([k, v]) => `<tr><td>${k}</td><td>${v}</td></tr>`).join("") +
    "</table></div>";

  $("covBody").innerHTML = `
    <div class="tiles">${tiles}</div>
    <div class="bar">
      ${seg(st.complete, "var(--ok)", "fully documented")}
      ${seg(st.partial, "var(--warn)", "partly documented")}
      ${seg(st.undocumented, "var(--err)", "no response shape")}
    </div>
    <div class="legend">
      <span><span class="sw" style="background:var(--ok)"></span>
        <b>${st.complete}</b> fully documented (${pct(st.complete)}%)</span>
      ${st.partial ? `<span><span class="sw" style="background:var(--warn)"></span>
        <b>${st.partial}</b> partial (${pct(st.partial)}%)</span>` : ""}
      <span><span class="sw" style="background:var(--err)"></span>
        <b>${st.undocumented}</b> no response shape (${pct(st.undocumented)}%)</span>
    </div>

    <div class="dim" style="margin-top:13px">
      <h3>Source of truth — where each operation's payload comes from</h3>
      <table>
        ${["spec", "verified", "agreed", "proposed", "guess"].map((t) => `
          <tr><td><span class="sw" style="background:${TRUST_COLOR[t]}"></span>
            <b>${t}</b> — ${esc((data.trust_meaning || {})[t] || "")}</td>
            <td>${tr[t] || 0}</td></tr>`).join("")}
      </table>
      <p class="hint">Only <b>spec</b>, <b>verified</b> and <b>agreed</b> are safe to build
        against. Move an operation up the ladder from the list below, or by editing
        <code>mock_overlay.json</code> — either way the decision lands in git, not in a
        chat thread.</p>
    </div>

    <div class="dims">
      ${dim("Success response", [
        ["declared by a <b>schema</b> — checkable by machine", sr.by_kind.schema],
        ["<b>example</b> only — readable, not checkable",
         `<span style="color:${sr.by_kind["example only"] ? "var(--warn)" : "var(--ink)"}">`
         + sr.by_kind["example only"] + "</span>"],
        ["<b>nothing declared</b>", `<span style="color:var(--err)">${sr.missing}</span>`],
      ])}
      ${dim("Request payload", [
        ["operations that take a body", rb.applicable],
        ["with a schema", rb.documented],
        ["<b>without a schema</b>",
         `<span style="color:${rb.missing ? "var(--err)" : "var(--ok)"}">${rb.missing}</span>`],
        ["take no body", rb.no_body],
      ])}
      ${dim("Error envelopes (422)", [
        ...Object.entries(d.error_envelopes.shapes).map(([shape, n]) =>
          [`<code>${esc(shape)}</code>`,
           shape === "not documented" ? `<span style="color:var(--warn)">${n}</span>` : n]),
        [d.error_envelopes.distinct > 1
          ? '<b style="color:var(--err)">distinct shapes</b>'
          : "<b>distinct shapes</b>",
         `<span style="color:${d.error_envelopes.distinct > 1 ? "var(--err)" : "var(--ok)"}">`
         + d.error_envelopes.distinct + "</span>"],
      ])}
      ${dim("Failure responses", [
        ["401 / 403 / 404 / 409 / 500 declared", fr.documented],
        ["<b>only 422</b>",
         `<span style="color:var(--err)">${fr.validation_only}</span>`],
        ["none at all", fr.none],
        ["every failure has an example", fr.with_examples],
      ])}
    </div>

    <details>
      <summary>Show the ${st.undocumented} operations the spec does not describe</summary>
      <div class="oplist">${opRows(data.operations.filter((o) => o.state !== "complete"), true)}</div>
    </details>
    <details>
      <summary>Show the ${st.complete} fully documented operations</summary>
      <div class="oplist">${opRows(data.operations.filter((o) => o.state === "complete"))}</div>
    </details>`;

  document.querySelectorAll("select.trust").forEach((sel) =>
    sel.addEventListener("change", () => setTrust(sel.dataset.op, sel.value)));
}

const TRUST_COLOR = {
  spec: "var(--ok)", verified: "var(--ok)", agreed: "var(--accent)",
  proposed: "var(--warn)", guess: "var(--err)",
};

function opRows(ops, actions) {
  if (!ops.length) return '<div class="empty">None.</div>';
  return ops.map((o) => {
    const [method, ...rest] = o.operation.split(" ");
    const act = actions && o.trust !== "spec"
      ? `<select class="trust" data-op="${esc(o.operation)}"
           style="width:auto;padding:2px 5px;font-size:11px">
           ${["guess", "proposed", "agreed", "verified"].map((t) =>
             `<option value="${t}"${t === o.trust ? " selected" : ""}>${t}</option>`).join("")}
         </select>` : "";
    return `<div class="op">
      <span class="m ${method}">${method}</span>
      <span class="p" title="${esc(o.summary || "")}">${esc(rest.join(" "))}</span>
      <span class="tag" style="color:${TRUST_COLOR[o.trust]};border-color:currentColor"
            title="${esc(o.trust_meaning || "")}">${esc(o.trust)}</span>
      ${o.missing.length ? `<span class="why">missing: ${o.missing.join(", ")}</span>` : ""}
      ${act}
    </div>`;
  }).join("");
}

async function setTrust(operation, status) {
  const { data } = await api("/api/agree",
    { method: "POST", body: JSON.stringify({ operation, status }) });
  if (data.error) { banner("err", data.error); return; }
  await loadCoverage();
}

/* ---------------------------------------------------------------- panels */

function renderDrift(d) {
  if (!d) return;
  const bySource = {};
  ROUTES.forEach((r) => { bySource[r.body_source] = (bySource[r.body_source] || 0) + 1; });
  const rows = [
    ["operations", d.spec_operations],
    ["overlay entries", d.overlay_operations],
    ["uncurated", (d.uncurated || []).length],
    ["synthesised", (d.synthesized_operations || []).length],
    ["spec generation", d.generation],
    ["source", d.spec_source ? d.spec_source.kind + " · " + short(d.spec_source.location) : "—"],
  ];
  const added = d.last_change && d.last_change.added && d.last_change.added.length
    ? `<p class="hint">Added on the last reload: ${d.last_change.added.map(esc).join(", ")}</p>` : "";
  const stale = (d.overlay_entries_no_longer_in_spec || []).length
    ? `<p class="hint">${d.overlay_entries_no_longer_in_spec.length} overlay entr(ies) no longer in the spec.</p>` : "";
  $("drift").innerHTML =
    "<table>" + rows.map(([k, v]) => `<tr><td>${k}</td><td>${esc(String(v))}</td></tr>`).join("") +
    "</table>" + added + stale;
}

async function loadRequests() {
  const { ok, data } = await api("/api/requests");
  if (!ok || !Array.isArray(data) || !data.length) {
    $("reqlog").innerHTML = '<div class="empty">Nothing yet.</div>'; return;
  }
  $("reqlog").innerHTML = "<table>" + data.slice(-12).reverse().map((e) => {
    const cls = "s" + String(e.status)[0];
    const v = (e.validation_errors || []).length;
    return `<tr><td><span class="${cls}" style="font-weight:700">${e.status}</span></td>
      <td><span class="m ${e.method}" style="min-width:auto">${e.method}</span>
          <code>${esc(e.path)}</code>
          ${e.source ? `<span class="tag">${esc(e.source)}</span>` : ""}
          ${v ? `<span class="tag" style="color:var(--err)">${v} violation(s)</span>` : ""}</td></tr>`;
  }).join("") + "</table>";
}

async function refreshStdout() {
  const { data } = await api("/api/stdout");
  $("stdout").textContent = data.stdout || "not started";
  $("stdout").scrollTop = $("stdout").scrollHeight;
}

/* ---------------------------------------------------------------- helpers */

const esc = (s) => String(s).replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const short = (s) => (String(s).length > 34 ? "…" + String(s).slice(-33) : String(s));

/* Feedback goes to the shell, not into the Explore response header — every view
   calls this, and a message rendered into another view's panel is a message
   nobody sees. Errors stay until dismissed; confirmations fade. */
let TOAST_TIMER = null;

function banner(kind, text) {
  const el = $("toast");
  $("toastText").textContent = text;
  el.className = kind;
  el.hidden = false;
  clearTimeout(TOAST_TIMER);
  if (kind !== "err") TOAST_TIMER = setTimeout(hideBanner, 6000);
}

function hideBanner() {
  clearTimeout(TOAST_TIMER);
  $("toast").hidden = true;
}

function addHeader(line) {
  const cur = $("headers").value.split("\n").filter((l) => l.trim());
  const name = line.split(":")[0].toLowerCase();
  const kept = cur.filter((l) => l.split(":")[0].toLowerCase() !== name);
  kept.push(line);
  $("headers").value = kept.join("\n");
}

/* ---------------------------------------------------------------- wiring */

$("btnStart").addEventListener("click", start);
$("btnStop").addEventListener("click", stop);
$("btnRestart").addEventListener("click", async () => { await stop(); await start(); });
$("btnSend").addEventListener("click", send);
$("btnSample").addEventListener("click", fillSample);
$("btnCurl").addEventListener("click", () => copyCurlFromForm(false));
$("btnCurlReal").addEventListener("click", () => copyCurlFromForm(true));

/* the parsed rows are the default; the raw log is one click away */
for (const [btn, pre, rows] of [["testRaw", "testOut", "testRows"],
                                ["liveRaw", "liveOut", "liveRows"]]) {
  $(btn).addEventListener("click", () => {
    const showingRaw = !$(pre).hidden;
    $(pre).hidden = showingRaw;
    $(rows).hidden = !showingRaw;
    $(btn).textContent = showingRaw ? "Raw log" : "Show rows";
  });
}
$("exploreTarget").addEventListener("change", describeTarget);
$("btnSaveTest").addEventListener("click", openSaveDialog);
$("btnTestsRefresh").addEventListener("click", loadTests);
$("btnRunTests").addEventListener("click", () =>
  runTests($("testKind").value ? [$("testKind").value] : []));
$("btnRunSanity").addEventListener("click", () => runTests(["e2e"]));
$("btnCI").addEventListener("click", async () => {
  const { data } = await api("/api/ci");
  $("testOutCard").hidden = false;
  $("testOut").textContent = data.snippet || data.error || "";
});

$("btnCollection").addEventListener("click", () => {
  // served with Content-Disposition, so the browser saves it
  window.location.href = "/api/postman?download=1";
});

$("btnCollectionCopy").addEventListener("click", async () => {
  const data = await getCollection();
  if (!data) return;
  const ok = await copyText(JSON.stringify(data.collection, null, 2), $("btnCollectionCopy"));
  if (ok) {
    $("collHint").innerHTML =
      `Copied <b>${data.requests}</b> requests in <b>${data.folders}</b> folders ` +
      `(<code>baseUrl=${esc(data.base_url)}</code>). In Postman: Import &gt; Raw text.`;
  }
});
$("btnBreak").addEventListener("click", breakBody);
$("btnStdout").addEventListener("click", refreshStdout);
$("btnCov").addEventListener("click", loadCoverage);
$("btnRules").addEventListener("click", loadRules);
$("btnGuideCopy").addEventListener("click", () => copyText(GUIDE, $("btnGuideCopy")));
$("btnReqs").addEventListener("click", loadRequests);
$("filter").addEventListener("input", renderOps);
$("clearHeaders").addEventListener("click", () => { $("headers").value = ""; });

$("spec").addEventListener("input", () => {
  $("urlOpts").hidden = !/^https?:\/\//i.test($("spec").value.trim());
});

document.querySelectorAll("button[data-h]").forEach((b) =>
  b.addEventListener("click", () => addHeader(b.dataset.h)));

$("btnReload").addEventListener("click", async () => {
  const { data } = await api("/api/reload", { method: "POST" });
  renderDrift(data);
  await loadRoutes();
  await loadCoverage();
  banner("ok", "Spec re-read. " + (data.last_change?.added?.length
    ? "New: " + data.last_change.added.join(", ") : "No new operations."));
});

$("btnReset").addEventListener("click", async () => {
  await api("/api/reset", { method: "POST" });
  await loadRequests();
  banner("ok", "State and request log cleared.");
});

$("btnOverlay").addEventListener("click", async () => {
  showView("explore");
  $("resp").textContent = "building overlay…";
  const { data } = await api("/api/overlay",
    { method: "POST", body: JSON.stringify({ report: false }) });
  $("respHead").hidden = true;
  $("resp").textContent = data.output || "(no output)";
});

$("btnVerify").addEventListener("click", async () => {
  showView("explore");
  $("resp").textContent = "self-check: running every operation against this mock…";
  $("respHead").hidden = true;
  const { data } = await api("/api/verify",
    { method: "POST", body: JSON.stringify({ negative: false }) });
  $("resp").textContent = data.output || "(no output)";
  banner("ok", "Self-check of the mock, writes included — its store is in memory. "
             + "'unverifiable' is not a failure: the spec declares no response schema "
             + "for those, so nothing could be checked against.");
});

async function loadEnvironments() {
  const { data } = await api("/api/environments");
  const sel = $("liveEnv");
  const list = data.environments || [];
  $("navEnvs").textContent = list.length || "";
  $("envList").innerHTML = list.length ? `<table>${list.map((e) => `
    <tr>
      <td><b>${esc(e.name)}</b></td>
      <td><code>${esc(e.base_url || "—")}</code></td>
      <td><span class="tag">auth: ${esc(e.auth_mode)}</span></td>
      <td>${e.ready
        ? '<span class="outcome pass">ready</span>'
        : `<span class="outcome fail">needs ${esc(e.unresolved.join(", "))}</span>`}</td>
      <td><button class="sm env-config" data-env="${esc(e.name)}">Configure</button></td>
    </tr>
    ${e.description ? `<tr><td></td><td colspan="3" class="hint"
       style="margin:0">${esc(e.description)}</td></tr>` : ""}`).join("")}</table>
    <p class="hint">Use <b>Configure</b> to fill these in from here. Anything shown as
      <code>${"${VAR}"}</code> is resolved from your shell,
      <code>.env</code>, or <code>environments.local.json</code> — the last two are
      gitignored. Per-environment test data goes in that environment's
      <code>data</code> block.</p>`
    : '<div class="empty">No environments defined.</div>';
  $("envList").querySelectorAll(".env-config").forEach((b) =>
    b.addEventListener("click", () => configureEnv(b.dataset.env)));
  sel.innerHTML = '<option value="">— pick one —</option>' + list.map((e) =>
    `<option value="${esc(e.name)}"${e.ready ? "" : " data-missing=\"1\""}>` +
    `${esc(e.name)} — ${esc(e.base_url || "?")} (${esc(e.auth_mode)})` +
    `${e.ready ? "" : "  ⚠ needs " + e.unresolved.join(", ")}</option>`).join("");
  ENVS = list;
  fillExploreTargets();   // after ENVS, or the first load builds an empty list
}

$("liveEnv").addEventListener("change", () => {
  const env = (ENVS || []).find((e) => e.name === $("liveEnv").value);
  if (!env) { $("envHint").textContent = ""; return; }
  $("liveUrl").placeholder = env.base_url || "https://…";
  $("envHint").innerHTML = env.ready
    ? `${esc(env.description || "")} <b>Ready</b> — auth: ${esc(env.auth_mode)}.`
    : `<span style="color:var(--err)">Not ready: ${esc(env.unresolved.join(", "))} `
      + `${env.unresolved.length > 1 ? "are" : "is"} unset.</span> Export `
      + `${esc(env.unresolved.join(", "))} in your shell, or put ${env.unresolved.length > 1
          ? "them" : "it"} in <code>environments.local.json</code>.`;
});

/* ---------------------------------------------------- environment values */

let ENV_EDITING = null;

async function configureEnv(name) {
  const { data } = await api(`/api/environments/${encodeURIComponent(name)}/vars`);
  if (data.error) { banner("err", data.error); return; }
  ENV_EDITING = name;
  $("envDlgName").textContent = name;
  $("envDlgAbout").textContent = data.description || "";
  $("envDlgWhere").textContent = data.writes_to;
  $("envDlgMsg").innerHTML = "";
  $("envDlgFields").innerHTML = data.fields.length ? data.fields.map((f) => `
    <div class="varrow">
      <div class="n">
        <code>${esc(f.name)}</code>
        <span class="state ${f.set ? "set" : "unset"}">${f.set ? "set" : "not set"}</span>
        <span class="why">${esc(f.purpose || "")}</span>
      </div>
      <input type="${f.secret ? "password" : "text"}" data-var="${esc(f.name)}"
             placeholder="${f.set ? (f.secret ? "already set — type to replace"
                                              : esc(f.hint || "")) : "not set"}">
    </div>`).join("")
    : '<div class="runnote">This environment needs no variables.</div>';
  $("envDlg").showModal();
}

async function saveEnvVars() {
  const values = {};
  $("envDlgFields").querySelectorAll("input").forEach((el) => {
    if (el.value.trim()) values[el.dataset.var] = el.value.trim();
  });
  if (!Object.keys(values).length) {
    $("envDlgMsg").innerHTML = '<p class="hint">Nothing typed in — nothing written.</p>';
    return false;
  }
  const { data } = await api("/api/environments/vars",
    { method: "POST", body: JSON.stringify({ values }) });
  if (!data.ok) {
    $("envDlgMsg").innerHTML = `<p class="hint" style="color:var(--err)">${esc(data.error)}</p>`;
    return false;
  }
  $("envDlgMsg").innerHTML = `<p class="hint" style="color:var(--ok)">${esc(data.message)}</p>`;
  await loadEnvironments();
  return true;
}

$("envDlgSave").addEventListener("click", async () => {
  if (await saveEnvVars()) setTimeout(() => $("envDlg").close(), 700);
});

$("envDlgTest").addEventListener("click", async () => {
  if (!(await saveEnvVars())) return;
  const { data } = await api("/api/env-login",
    { method: "POST", body: JSON.stringify({ name: ENV_EDITING }) });
  $("envDlgMsg").innerHTML = data.ok
    ? `<p class="hint" style="color:var(--ok)">${esc(data.note)} — `
      + `${esc(Object.entries(data.headers || {}).map(([k, v]) => k + ": " + v).join("  ")
              || "no headers needed")}</p>`
    : `<p class="hint" style="color:var(--err)">${esc(data.error)}</p>`;
});

$("envDlgCancel").addEventListener("click", () => $("envDlg").close());

$("btnEnvLogin").addEventListener("click", async () => {
  const name = $("liveEnv").value;
  if (!name) { banner("err", "Pick an environment first."); return; }
  $("btnEnvLogin").disabled = true;
  const { data } = await api("/api/env-login",
    { method: "POST", body: JSON.stringify({ name }) });
  $("btnEnvLogin").disabled = false;
  if (!data.ok) { banner("err", data.error); return; }
  const shown = Object.entries(data.headers || {})
    .map(([k, v]) => `${k}: ${v}`).join("   ");
  banner("ok", `${name}: ${data.note}. ${shown || "no headers needed"}`);
});

/* Which operations a live run should call. Deselection travels as exact keys,
   not a regex, so unticking one thing cannot accidentally drop another. */
const STREAMY = /stream|sse|watch|subscribe|events/i;
let OPS_PICKED = null;               // null = everything

function renderOpsPicker() {
  const list = ROUTES || [];
  if (!list.length) {
    $("opsPick").innerHTML = '<div class="runnote">Start the mock to load the operations.</div>';
    return;
  }
  if (OPS_PICKED === null) {
    OPS_PICKED = new Set(list.map((r) => `${r.method} ${r.path}`));
  }
  const q = ($("opsFilter").value || "").toLowerCase().trim();
  const shown = list.filter((r) =>
    !q || `${r.method} ${r.path}`.toLowerCase().includes(q));
  $("opsPick").innerHTML = shown.map((r) => {
    const key = `${r.method} ${r.path}`;
    const streamy = STREAMY.test(r.path + " " + (r.summary || ""));
    return `<label title="${esc(r.summary || "")}">
      <input type="checkbox" data-key="${esc(key)}"
             ${OPS_PICKED.has(key) ? "checked" : ""}>
      <span class="m ${esc(r.method)}">${esc(r.method)}</span>
      <span class="p">${esc(r.path)}</span>
      ${streamy ? '<span class="stream">stream</span>' : ""}
    </label>`;
  }).join("");
  $("opsPick").querySelectorAll("input").forEach((el) =>
    el.addEventListener("change", () => {
      el.checked ? OPS_PICKED.add(el.dataset.key) : OPS_PICKED.delete(el.dataset.key);
      updateOpsCount();
    }));
  updateOpsCount();
}

function updateOpsCount() {
  const total = (ROUTES || []).length;
  const n = OPS_PICKED ? OPS_PICKED.size : total;
  $("opsCount").textContent = `${n} of ${total} selected`;
}

function setOps(pred) {
  OPS_PICKED = new Set((ROUTES || []).filter(pred).map((r) => `${r.method} ${r.path}`));
  renderOpsPicker();
}

$("opsAll").addEventListener("click", () => setOps(() => true));
$("opsNone").addEventListener("click", () => setOps(() => false));
$("opsReads").addEventListener("click", () => setOps((r) => r.method === "GET"));
$("opsFilter").addEventListener("input", renderOpsPicker);

$("liveAuthMode").addEventListener("change", () => {
  const m = $("liveAuthMode").value;
  $("liveCookieWrap").hidden = m !== "cookie";
  $("liveBearerWrap").hidden = m !== "bearer";
});

$("btnVerifyLive").addEventListener("click", async () => {
  const envName = $("liveEnv").value;
  const base = $("liveUrl").value.trim();
  if (!envName && !base) {
    banner("err", "Pick an environment, or type a base URL."); return; }
  const writes = $("liveWrites").checked;
  $("btnVerifyLive").disabled = true;
  // the result stays on this view — sending the user to Explore hid what they asked for
  $("liveOutCard").hidden = false;
  $("liveOutHead").hidden = true;
  $("liveOut").textContent = `checking ${base || envName}…`
    + (writes ? " including writes — this creates and deletes real rows." : " read-only.");
  const { data } = await api("/api/verify-live", {
    method: "POST",
    body: JSON.stringify({
      env: envName,
      base_url: base,
      token: $("liveAuthMode").value === "bearer" ? $("liveToken").value.trim() : "",
      headers: [
        $("liveAuthMode").value === "cookie" && $("liveCookie").value.trim()
          ? "Cookie: " + $("liveCookie").value.trim() : "",
        $("liveHeaders").value,
      ].filter(Boolean).join("\n"),
      only: $("liveOnly").value.trim(),
      negative: $("liveNegative").checked,
      skip: OPS_PICKED
        ? (ROUTES || []).map((r) => `${r.method} ${r.path}`)
                        .filter((k) => !OPS_PICKED.has(k))
        : [],
      skip_streaming: $("liveSkipStream").checked,
      allow_writes: writes,
      learn_overlay: $("liveLearn").checked,
    }),
  });
  if (data.error) { $("btnVerifyLive").disabled = false;
                    $("liveOut").textContent = data.error; return; }
  $("liveOutHead").hidden = false;
  $("liveOutHead").innerHTML =
    `<span class="meta"><span>target: <b>${esc(data.target || "")}</b></span>` +
    `<span>auth: ${esc(data.auth || "none")}</span></span>`;
  await followJob(data.job, $("liveOut"), $("liveElapsed"), $("btnCancelLive"),
                  async () => { $("btnVerifyLive").disabled = false; },
                  $("liveRows"), $("liveProgress"));
});


/* The spec list is a real control, not a datalist: a datalist only appears once
   you already know what to type, which reads as "there is nothing to choose". */
let SPEC_CHOICES = [];

function fillSpecPicker(specs, current) {
  SPEC_CHOICES = specs || [];
  const value = current || $("spec").value.trim();
  const known = SPEC_CHOICES.includes(value);
  $("specPick").innerHTML = SPEC_CHOICES
    .map((s) => `<option value="${esc(s)}"${s === value ? " selected" : ""}>${esc(s)}</option>`)
    .concat(`<option value="__custom__"${known ? "" : " selected"}>Another file or a URL…</option>`)
    .join("");
  showSpecPath(!known);
}

function showSpecPath(show) {
  $("spec").hidden = !show;
  $("specPathLabel").hidden = !show;
}

function rememberedHint(remembered) {
  const spec = remembered && remembered.spec;
  $("specDefaultHint").innerHTML = spec
    ? `Defaults to <code>${esc(spec)}</code> — the project spec, set under Source. `
      + `Choosing something else here starts the mock on it just this once.`
    : "Set the project spec under Source to change this for everything.";
}

(async function init() {
  const { data } = await api("/api/defaults");
  const remembered = data.remembered || {};
  if (remembered.spec) $("spec").value = remembered.spec;
  if (remembered.overlay !== undefined) $("overlay").value = remembered.overlay;
  if (remembered.port) $("port").value = remembered.port;
  fillSpecPicker(data.specs, remembered.spec);
  rememberedHint(remembered);
  $("specs").innerHTML = (data.specs || []).map((s) => `<option value="${esc(s)}">`).join("");
  $("overlays").innerHTML = (data.overlays || []).map((s) => `<option value="${esc(s)}">`).join("");
  await refreshState();
  await refreshStdout();
  await loadCoverage();
  await loadEnvironments();
  fillTestEnvs();
  await loadTests();
  setInterval(() => { if (RUNNING) refreshStdout(); }, 5000);
})();
