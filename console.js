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
  if (name === "environments") watchLoad();
  if (name === "tests") { loadTests(); fillTestSelectors(); fillTypeList();
                         loadBindings(); fillBindSuites(); loadBaseline(); }
  if (name === "home") homeLoad();
  // a screen kept under More still has to show as the current one
  const link = document.querySelector(`nav.side a[data-view="${name}"]`);
  if (link && link.closest("#navMoreItems")) showMore(true);
  if (name === "create") czLoad();
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

/* Everything downstream assumes the mock is faithful to the document. When it
   is not, every test written against it measures the mock's imagination, so the
   answer belongs next to "running" rather than somewhere you have to go look. */
async function watchSelfcheck() {
  const el = $("selfcheck");
  el.hidden = false;
  el.className = "tag";
  el.textContent = "checking the mock against its spec…";
  for (let i = 0; i < 90; i++) {
    let d;
    try { ({ data: d } = await api("/api/mock/selfcheck")); } catch { return; }
    if (d.state === "done") {
      const s = d.summary || {};
      const failed = d.failed_count || 0;
      el.className = "tag " + (failed ? "err" : "ok");
      el.textContent = failed
        ? `${failed} operation(s) do not match the spec`
        : `${s.ok || 0} verified against the spec`;
      el.title = failed
        ? "The mock contradicts its own document: " + (d.failed || []).join(", ")
        : `${s.ok || 0} verified, ${s.warning || 0} unverifiable (the spec `
          + `declares no response schema for those)`;
      return;
    }
    if (d.state && d.state !== "running") { el.hidden = true; return; }
    await new Promise((r) => setTimeout(r, 1000));
  }
  el.hidden = true;
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
  if (!on) $("selfcheck").hidden = true;
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
  watchSelfcheck();
  await refreshState();
  loadEnvironments();                 // the mock's address follows the port it started on
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
  // A first guess that is valid on its own: the spec knows each parameter's
  // type and which query parameters are required, so ask rather than paste one
  // uuid into everything and send no query string.
  $("path").value = route.path.replace(/\{[^}]+\}/g, "3fa85f64-5717-4562-b3fc-2c963f66afa6");
  $("query").value = "";
  $("body").value = "";
  fillSampleRequest(route);
  const note = [];
  if (route.state) note.push(MARK[route.state] + " " + MARK_LABEL[route.state][0]);
  if (route.detail && route.detail.fields_documented)
    note.push(`${route.detail.body_required} required / ${route.detail.body_optional} optional fields`);
  if (route.statuses?.length) note.push("documented: " + route.statuses.join(", "));
  if (route.scenarios?.length) note.push("scenarios: " + route.scenarios.join(", "));
  $("bodyNote").textContent = note.length ? "— " + note.join("  ·  ") : "";
  if (["POST", "PUT", "PATCH"].includes(route.method)) fillSample();
}

/* Path parameters typed correctly and required query parameters present — the
   difference between a request that can succeed and one that cannot. */
async function fillSampleRequest(route) {
  const q = new URLSearchParams({ method: route.method, path: route.path });
  try {
    const { data } = await api("/api/sample-request?" + q);
    if (!data || data.error) return;
    if (SELECTED !== route) return;          // a later click already won
    if (data.path) $("path").value = data.path;
    $("query").value = data.query || "";
    if ((data.required_query || []).length) {
      $("bodyNote").textContent +=
        `${$("bodyNote").textContent ? "  ·  " : "— "}required query: `
        + data.required_query.join(", ");
    }
  } catch {
    /* the placeholder path already filled in stays */
  }
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
      // Not disabled: an environment missing its values is one you are about to
      // fill in, and a dead option gives you nowhere to do that. Selecting it
      // opens Configure instead.
      return `<option value="${esc(e.name)}">`
        + `${esc(e.name)} — ${esc(e.base_url || "needs values")}`
        + `${e.ready ? "" : "  — needs " + (e.unresolved || []).length + " value(s)"}</option>`;
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
/* Which suites are open, and which page of each is showing. Kept outside the
   render so opening a suite survives a refresh — a list that snaps shut every
   time the tests reload is a list you stop using. */
const TREE_OPEN = {};
const TREE_PAGE = {};
const COLLAPSE_OVER = 6;        // bigger than this and it starts closed
const PAGE_SIZE = 25;

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
/* Every type an assertion may name — JSON's own, the formats we can check, and
   any this project already uses. Refreshed with the rest of the Tests view, so
   a type that appears in a saved test appears here too. */
async function fillTypeList() {
  const { data } = await api("/api/tests/taxonomy");
  const types = (data && data.types) || {};
  const groups = [["JSON types", types.json || []],
                  ["formats", types.formats || []],
                  ["used in this project", types.in_use || []]];
  $("typeList").innerHTML = groups
    .filter(([, list]) => list.length)
    .map(([label, list]) => list
      .map((x) => `<option value="${esc(x)}" label="${esc(label)}">`).join(""))
    .join("");
}

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
    <input class="a-value" placeholder="value" list="typeList">
    <select class="a-where" title="which environments this assertion applies to">
      <option value="">everywhere</option>
      <optgroup label="only on"></optgroup>
      <optgroup label="except on"></optgroup>
    </select>
    <button type="button" class="sm a-del danger" title="remove">remove</button>`;
  list.appendChild(row);

  const type = row.querySelector(".a-type");
  const path = row.querySelector(".a-path");
  const op = row.querySelector(".a-op");
  const val = row.querySelector(".a-value");
  const where = row.querySelector(".a-where");

  // Most assertions hold everywhere and say nothing. The exceptions — a count
  // only true on a seeded server, a field only production returns — are worth
  // naming, and naming them here beats copying the whole test per environment.
  const named = (ENVS || []).map((e) => e.name);
  where.querySelectorAll("optgroup")[0].innerHTML = named
    .map((n) => `<option value="only:${esc(n)}">only on ${esc(n)}</option>`).join("");
  where.querySelectorAll("optgroup")[1].innerHTML = named
    .map((n) => `<option value="except:${esc(n)}">except on ${esc(n)}</option>`).join("");
  if (a.only_on) {
    where.value = `only:${[].concat(a.only_on)[0]}`;
  } else if (a.except_on) {
    where.value = `except:${[].concat(a.except_on)[0]}`;
  }

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
    // the value field means something different for `type`: it names one of a
    // known set, so offer the set rather than leave people guessing
    const isType = t === "jsonpath" && op.value === "type";
    val.setAttribute("list", isType ? "typeList" : "");
    val.placeholder = isType ? "string, integer, uuid, date-time…" : "value";
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
    const whereSel = row.querySelector(".a-where");
    const [mode, envName] = (whereSel ? whereSel.value : "").split(":");
    const scope = envName
      ? (mode === "only" ? { only_on: [envName] } : { except_on: [envName] })
      : {};
    const path = row.querySelector(".a-path").value.trim();
    const op = row.querySelector(".a-op").value;
    const value = parseValue(row.querySelector(".a-value").value);
    if (t === "schema") return { type: "schema", ...scope };
    if (t === "status") return Array.isArray(value)
      ? { type: "status", in: value, ...scope }
      : { type: "status", equals: value ?? 200, ...scope };
    if (t === "responseTime")
      return { type: "responseTime", op, value: value ?? 2000, ...scope };
    if (t === "header") return { type: "header", name: path, op,
                                 ...(NO_VALUE.has(op) ? {} : { value }), ...scope };
    if (t === "body_contains") return { type: "body_contains", value, ...scope };
    return { type: "jsonpath", path, op, ...(NO_VALUE.has(op) ? {} : { value }),
             ...scope };
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
// "48 / 48" with nothing else said reads as full coverage. It is not: the
// writes and anything deselected never produce a result line at all.
const NOTRUN = /^\s*(\d+) not run\s*$/;

const CLASS = { PASS: "pass", FAIL: "fail", ERR: "fail", WARN: "warn",
                BLOCK: "block", SKIP: "warn" };

function renderRun(text, rowsEl, progressEl, finished) {
  const rows = [];
  let done = 0, expected = 0, notRun = 0;
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
    const nr = line.match(NOTRUN);
    if (nr) { notRun = Math.max(notRun, Number(nr[1])); continue; }

    let m = line.match(SKIPPED);
    if (m) { notRun++; rows.push(row("SKIP", "", "", m[1], m[2])); continue; }

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
    const held = notRun ? `  ·  ${notRun} not run` : "";
    progressEl.textContent = (expected ? `${done} / ${expected}` : `${done} done`)
                             + held + waiting;
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
  // side by side: the list should not wait on knowing where bug reports go
  const [{ data }] = await Promise.all([api("/api/tests"), trackerLoad()]);
  // reassigned below when a label is selected — `const` here threw on every
  // click, which aborted the redraw and made the filter look like it did nothing
  let suites = data.suites || [];
  TESTS_CACHE = suites;
  libRender();
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
    // One badge per environment. A single "last outcome" meant running against
    // dev erased the fact that it passes on the mock — and green-here-red-there
    // is the most useful thing a test can tell you.
    const byEnv = (h && h.by_env) || {};
    const names = Object.keys(byEnv);
    if (!names.length) {
      const env = (h && (h.last_env || h.last_base_url)) || "?";
      return `<span class="outcome ${esc(k)}">${esc(k)}</span>`
           + `<span class="tag">on ${esc(env)}</span>`;
    }
    const differs = new Set(names.map((n) => byEnv[n].last_outcome)).size > 1;
    return names.sort().map((name) => {
      const seen = byEnv[name];
      const title = `${seen.passes}/${seen.runs} passed · last ${seen.last_run || "?"}`;
      return `<span class="outcome ${esc(seen.last_outcome)}" title="${esc(title)}"`
           + ` style="margin-right:3px">${esc(name)} ${esc(seen.last_outcome)}</span>`;
    }).join("")
      + (differs ? '<span class="tag" style="color:var(--warn)" title="the test and the '
                 + 'spec are the same in both — look at the server and its data"'
                 + '>differs by environment</span>' : "");
  };

  const acts = (suite, id, stage, h) => {
    const canPromote = stage === "draft";
    const proven = h && h.last_outcome === "pass";
    return `<span class="tacts">
      <button class="sm tact" data-act="open" data-suite="${esc(suite)}"
              data-stage="${esc(stage)}" data-id="${esc(id)}"
              title="open it in the workbench, where its steps can be run and rewired"
        >open</button>
      <button class="sm tact" data-act="run" data-suite="${esc(suite)}"
              data-stage="${esc(stage)}" data-id="${esc(id)}"
              title="run just this one, against the environment chosen on the left"
        >run</button>
      <button class="sm tact" data-act="edit" data-suite="${esc(suite)}"
              data-stage="${esc(stage)}" data-id="${esc(id)}">edit</button>
      <button class="sm tact" data-act="record" data-suite="${esc(suite)}"
              data-stage="${esc(stage)}" data-id="${esc(id)}"
              title="priority, status, owner, linked ticket and what it is for"
        >details</button>
      ${canPromote ? `<button class="sm tact" data-act="promote" data-suite="${esc(suite)}"
          data-stage="${esc(stage)}" data-id="${esc(id)}" ${proven ? "" : "disabled"}
          title="${proven ? "move into the shared suite"
                          : "must pass at least once before it can be shared"}">promote</button>` : ""}
      <button class="sm tact danger" data-act="delete" data-suite="${esc(suite)}"
              data-stage="${esc(stage)}" data-id="${esc(id)}">delete</button>
    </span>`;
  };

  /* How a test is managed, at a glance: how much it matters, whether it is in
     use, whose it is and what it traces to. */
  const recordLine = (t) => `
    <div class="recmeta">
      <span class="prio ${esc(t.priority)}">${esc(t.priority)}</span>
      ${(t.levels || []).map(esc).join(" · ")}
      ${t.status && t.status !== "ready" ? ` · <b>${esc(t.status)}</b>` : ""}
      ${t.owner ? ` · ${esc(t.owner)}` : ""}
      ${(t.links || []).length ? ` · ${t.links.map(esc).join(", ")}` : ""}
    </div>
    ${t.description ? `<div class="recmeta">${esc(t.description)}</div>` : ""}`;
  const recordHost = (s, t) =>
    `<div data-rechost="${esc(s.name)}|${esc(s.stage)}|${esc(t.id)}"></div>`;

  const caseBlock = (s, c) => `
    <div class="titem">
      <div class="head">
        <span class="kind case">case</span>
        <span class="label">
          <div class="t">${esc(c.name || c.id)}</div>
          <div class="sub">${esc(c.method)} ${esc(c.path)}</div>
          ${recordLine(c)}
        </span>
        <span class="tag">${c.assertions} assertion${c.assertions === 1 ? "" : "s"}</span>
        ${OUT(c.history)}
        ${acts(s.name, c.id, s.stage, c.history)}
      </div>
      ${recordHost(s, c)}
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
          ${recordLine(sc)}
        </span>
        ${OUT(sc.history)}
        ${acts(s.name, sc.id, s.stage, sc.history)}
      </div>
      ${recordHost(s, sc)}
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

  // A suite is collapsed by default once it is big enough to push everything
  // else off the screen, and only a page of it is drawn at a time — a hundred
  // tests rendered at once is a list nobody can read and a page that stutters.
  $("testTree").innerHTML = suites.map((s) => {
    const items = [...s.cases.map((c) => ({ kind: "case", item: c })),
                   ...s.scenarios.map((sc) => ({ kind: "scenario", item: sc }))];
    const key = `${s.name}|${s.stage}`;
    const open = TREE_OPEN[key] !== undefined
      ? TREE_OPEN[key] : items.length <= COLLAPSE_OVER;
    const page = TREE_PAGE[key] || 0;
    const pages = Math.max(1, Math.ceil(items.length / PAGE_SIZE));
    const slice = items.slice(page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE);
    return `
    <div class="suite">
      <header class="tfold" data-fold="${esc(key)}" style="cursor:pointer">
        <span class="fold">${open ? "▾" : "▸"}</span>
        <span class="name">${esc(s.name)}</span>
        <span class="stage ${esc(s.stage)}">${esc(s.stage)}</span>
        <span class="grow"></span>
        <span class="tag">${items.length} test(s)</span>
        <button class="sm tact" data-act="run-suite" data-suite="${esc(s.name)}"
                data-stage="${esc(s.stage)}" data-id=""
                title="run this whole section against the environment chosen on the left"
          >run section</button>
      </header>
      ${open ? `
        ${slice.map(({ kind, item }) => kind === "case"
            ? caseBlock(s, item) : scenarioBlock(s, item)).join("")}
        ${!items.length ? '<div class="empty">empty suite</div>' : ""}
        ${pages > 1 ? `
          <div class="tpage">
            <button class="sm" data-page="${esc(key)}|${page - 1}"
              ${page === 0 ? "disabled" : ""}>← previous</button>
            <span class="hint">showing ${page * PAGE_SIZE + 1}–${
              Math.min((page + 1) * PAGE_SIZE, items.length)} of ${items.length}</span>
            <button class="sm" data-page="${esc(key)}|${page + 1}"
              ${page + 1 >= pages ? "disabled" : ""}>next →</button>
          </div>` : ""}
      ` : ""}
    </div>`;
  }).join("");

  $("testTree").querySelectorAll("[data-fold]").forEach((h) =>
    h.addEventListener("click", (ev) => {
      if (ev.target.closest(".tact")) return;        // a button, not the header
      const key = h.dataset.fold;
      const items = suites.find((x) => `${x.name}|${x.stage}` === key);
      const count = items ? items.cases.length + items.scenarios.length : 0;
      const now = TREE_OPEN[key] !== undefined ? TREE_OPEN[key] : count <= COLLAPSE_OVER;
      TREE_OPEN[key] = !now;
      loadTests();
    }));
  $("testTree").querySelectorAll("[data-page]").forEach((b) =>
    b.addEventListener("click", () => {
      const [name, stage, page] = b.dataset.page.split("|");
      TREE_PAGE[`${name}|${stage}`] = Math.max(0, +page);
      loadTests();
    }));

  $("testTree").querySelectorAll(".tact").forEach((b) =>
    b.addEventListener("click", () =>
      testAction(b.dataset.act, b.dataset.suite, b.dataset.id, b.dataset.stage)));
}

/* -- edit / promote / delete -------------------------------------------- */

/* A test id is matched by regex, so one whose id contains a dot or a dash must
   not quietly select its neighbours. */
function escapeRegex(text) {
  return String(text).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/* One section, or one test, against the chosen environment — and a report
   afterwards, because a run you cannot read is a run you have to repeat. */
async function runScoped({ suite, only, drafts, what }) {
  const env = $("testEnv").value || "mock";
  const chosen = (ENVS || []).find((e) => e.name === env);
  if (chosen && !chosen.ready) {
    banner("err", `${env} needs ${(chosen.unresolved || []).join(", ")} first.`);
    configureEnv(env);
    return;
  }
  $("testOutCard").hidden = false;
  $("testOut").textContent = `running ${what} against ${env}…`;
  $("testRows").innerHTML = "";
  const { data } = await api("/api/tests/run", {
    method: "POST",
    body: JSON.stringify({ env, suite, only, drafts: !!drafts, kinds: [], verbose: false }),
  });
  if (data.error) { banner("err", data.error); $("testOut").textContent = data.error; return; }
  LAST_RUN = data.job;
  await followJob(data.job, $("testOut"), $("testElapsed"), $("btnCancelTests"),
                  null, $("testRows"), $("testProgress"));
  await showRunReport(data.job, `${what} on ${env}`);
  await loadTests();
}

/* Whose problem is it?

   Every failure already carries an attribution — spec changed, backend broke,
   the test assumes something undocumented, the environment. It was computed on
   every run and shown only in the terminal, so the console displayed a red row
   and left the most useful sentence unread. */
const VERDICT_TONE = {
  "spec-changed": "warn",
  "backend-broke-contract": "err",
  "test-assumes-undocumented": "warn",
  "spec-silent": "warn",
  "environment-or-data": "warn",
  unknown: "warn",
};

function renderVerdicts(report) {
  const items = (report.suites || []).flatMap((s) =>
    (s.results || []).map((r) => ({ ...r, suite: s.name })));
  const judged = items.filter((i) => i.verdict);
  const host = $("testVerdicts");
  if (!host) return;
  if (!judged.length) { host.hidden = true; host.innerHTML = ""; return; }

  // group: one headline repeated twenty times is noise, twenty findings under
  // one headline is a finding
  const byKind = {};
  judged.forEach((i) => (byKind[i.verdict.kind] = byKind[i.verdict.kind] || []).push(i));
  const order = ["spec-changed", "backend-broke-contract", "test-assumes-undocumented",
                 "environment-or-data", "spec-silent", "unknown"];
  host.hidden = false;
  host.innerHTML = `<h4 style="margin:12px 0 6px">Whose problem is it?</h4>`
    + order.filter((k) => byKind[k]).map((kind) => {
      const group = byKind[kind];
      const v = group[0].verdict;
      return `
      <details class="dim" style="margin-bottom:8px" ${group.length <= 3 ? "open" : ""}>
        <summary style="cursor:pointer;color:var(--${VERDICT_TONE[kind] || "warn"})">
          <b>${esc(v.headline)}</b> — ${group.length} test(s)</summary>
        <p class="hint" style="margin:6px 0">${esc(v.guidance || "")}</p>
        ${group.map((i) => `
          <div style="font-size:12px;margin:5px 0 5px 10px">
            <code>${esc(i.suite)}/${esc(i.id || i.name)}</code>
            ${(i.verdict.evidence || []).map((e) =>
              `<div class="hint" style="margin:1px 0 0 12px">${esc(e)}</div>`).join("")}
          </div>`).join("")}
        ${v.next ? `<p class="hint" style="margin:6px 0 0 0"><b>Next:</b>
          ${esc(v.next)}</p>` : ""}
      </details>`;
    }).join("");
}

/* The same report for every way of running, so what you read does not depend
   on which button you pressed. */
async function showRunReport(job, label) {
  const rep = (await api(`/api/job/${job}/report`)).data || {};
  renderVerdicts(rep);
  const summary = rep.summary || {};
  const bits = Object.entries(summary).map(([k, v]) => `${v} ${k}`).join("   ");
  const failed = (summary.fail || 0) + (summary.error || 0);
  const blocked = summary.blocked || 0;
  $("testOutHead").hidden = false;
  $("testOutHead").className = "banner " + (failed ? "err" : blocked ? "warn" : "ok");
  $("testOutHead").innerHTML =
    `<b>${esc(label)}</b> — ${esc(bits || "nothing ran")}`
    + (rep.spec && rep.spec.digest
        ? `  ·  spec ${esc(String(rep.spec.digest).slice(0, 12))}…` : "")
    + (failed ? "  ·  look at the failures below before anything else" : "")
    + (blocked && !failed
        ? "  ·  blocked means the endpoint under test never ran — fix the setup" : "");
  return rep;
}

/* Load a saved test into the workbench so it can be run step by step, rebound
   and re-saved. Anything unsaved already in there is asked about first —
   silently replacing work somebody is in the middle of is the one thing this
   must never do. */
async function wbLoad(suite, id, stage) {
  showAdvanced(true);                 // the workbench is an advanced tool
  const q = new URLSearchParams({ suite, id, stage: stage || "draft" });
  const { data } = await api("/api/tests/one?" + q);
  const test = data.test || data;
  if (!test || data.error) { banner("err", data.error || "could not read that test"); return; }

  if (wbDirty()) {
    const keep = confirm(
      `The workbench has unsaved steps.\n\n`
      + `Open ${id} instead and lose them?\n\n`
      + `Cancel to keep what is there — save it first, then open this one.`);
    if (!keep) {
      banner("err", "Left the workbench as it was. Save it, then open the other test.");
      return;
    }
  }

  // a flow may declare values no step produces; keep them, or running the
  // test you just opened fails on a variable it was imported with
  WB.data = test.data || {};
  wbDataToText(WB.data);
  const steps = (test.steps || [test]).map((step) => ({
    role: step.role || (test.steps ? "step" : "target"),
    name: step.name || "",
    request: {
      method: (step.request || step).method || "GET",
      path: (step.request || step).path || "",
      query: wbQueryText(((step.request || step).query) || ""),
      body: wbBodyFrom((step.request || step).body),
    },
    assertions: step.assertions || [],
    capture: step.capture || {},
  }));
  (test.cleanup || []).forEach((step) => steps.push({
    role: "cleanup", name: step.name || "",
    request: { method: (step.request || step).method || "DELETE",
               path: (step.request || step).path || "",
               query: wbQueryText(((step.request || step).query) || ""),
               body: wbBodyFrom((step.request || step).body) },
    assertions: step.assertions || [], capture: {},
  }));

  WB.steps = steps.length ? steps : [wbBlankStep()];
  WB.ran = {};
  WB.loadedFrom = { suite, id, stage };
  $("wbBody").hidden = false;
  $("wbToggle").textContent = "Close";
  await wbInit();
  $("wbId").value = id;
  $("wbModule").value = suite;
  if (test.kind) $("wbKind").value = test.kind;
  const level = (test.levels || [])[0];
  if (level) $("wbLevel").value = level;
  wbRender();
  $("wbCard").scrollIntoView({ behavior: "smooth", block: "start" });
  banner("ok", `${id} is open — ${steps.length} step(s). Saving writes back to `
             + `${suite}, replacing it.`);
}

/* Unsaved means: there is something in there, and it did not come from a test
   we just loaded or saved. */
function wbDirty() {
  const real = WB.steps.filter((s) => (s.request.path || "").trim());
  if (!real.length) return false;
  return !WB.loadedFrom || WB.touched;
}

function wbQueryText(query) {
  if (!query || typeof query === "string") return query || "";
  return Object.entries(query).map(([k, v]) => `${k}=${v}`).join("&");
}

function wbBodyFrom(body) {
  if (body === undefined || body === null) return "";
  return typeof body === "string" ? body : JSON.stringify(body, null, 2);
}

async function testAction(act, suite, id, stage) {
  // Running one section, or one test, against whichever environment is chosen
  // on the left. Without this the only way to run anything was everything, and
  // the only server it could reach was the mock.
  if (act === "open") {
    await wbLoad(suite, id, stage);
    return;
  }
  if (act === "record") {
    openRecordEditor(suite, stage, id);
    return;
  }
  if (act === "run" || act === "run-suite") {
    // --only is matched against "id name", so anchoring both ends finds
    // nothing; anchor the start and require a boundary after the id, or
    // "dept-create" would also select "dept-create-conflict"
    await runScoped({ suite, only: act === "run"
                        ? `^${escapeRegex(id)}( |$)` : undefined,
                      drafts: stage === "draft" || $("testDrafts").checked,
                      what: act === "run" ? id : `section ${suite}` });
    return;
  }
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
    `${esc(e.name)} — ${esc(e.base_url || "needs values")}` +
    `${e.ready ? "" : "  — needs " + (e.unresolved || []).length + " value(s)"}</option>`)
    .join("");
}

/* Levels and modules come from the taxonomy the runner itself uses, counts and
   all — so what is offered here is exactly what can be selected. */
/* Change how a test is managed without opening what it does. Re-prioritising
   before a release should not mean editing steps. */
let TESTS_CACHE = [];
function openRecordEditor(suite, stage, id, where) {
  const host = where || $("testTree").querySelector(
    `[data-rechost="${CSS.escape(suite)}|${CSS.escape(stage)}|${CSS.escape(id)}"]`);
  if (!host) return;
  if (host.innerHTML) { host.innerHTML = ""; return; }      // second click closes
  const found = (TESTS_CACHE.find((s) => s.name === suite && s.stage === stage) || {});
  const t = [...(found.cases || []), ...(found.scenarios || [])]
    .find((x) => x.id === id) || {};
  const opt = (values, current) => values.map((v) =>
    `<option value="${v}"${v === current ? " selected" : ""}>${v}</option>`).join("");
  const LV = ["smoke", "sanity", "regression", "negative", "performance"];
  host.innerHTML = `
    <div class="receditor">
      <div><label>Priority</label>
        <select data-f="priority">${opt(["P0", "P1", "P2", "P3"], t.priority || "P2")}</select></div>
      <div><label>Status</label>
        <select data-f="status">${opt(["ready", "blocked", "retired"], t.status || "ready")}</select></div>
      <div><label>Owner</label>
        <input type="text" data-f="owner" value="${esc(t.owner || "")}" placeholder="who looks after it"></div>
      <div><label>Ticket or link</label>
        <input type="text" data-f="links" value="${esc((t.links || []).join(", "))}" placeholder="PROJ-123"></div>
      <div class="wide"><label>Types — a test can be several</label>
        ${LV.map((l) => `<label style="display:inline-flex;gap:4px;margin-right:12px;font-size:12px">
          <input type="checkbox" data-level="${l}"${(t.levels || []).includes(l) ? " checked" : ""}>${l}</label>`).join("")}</div>
      <div class="wide"><label>What this test is for, in plain words</label>
        <input type="text" data-f="description" value="${esc(t.description || "")}"
               placeholder="A new record can be created and shows up in the list"></div>
      <div class="wide btnrow">
        <button class="primary sm" data-recsave>Save</button>
        <button class="sm" data-reccancel>Cancel</button></div>
    </div>`;
  host.querySelector("[data-reccancel]").onclick = () => { host.innerHTML = ""; };
  host.querySelector("[data-recsave]").onclick = async () => {
    const fields = {};
    host.querySelectorAll("[data-f]").forEach((el) => { fields[el.dataset.f] = el.value.trim(); });
    fields.levels = [...host.querySelectorAll("[data-level]:checked")].map((el) => el.dataset.level);
    const { data } = await api("/api/tests/record", {
      method: "POST", body: JSON.stringify({ suite, stage, id, fields }) });
    if (!data.ok) { banner("err", (data.errors || [data.error]).join("  ·  ")); return; }
    banner("ok", `Saved ${id}.`);
    await loadTests(); fillTestSelectors();
  };
}

async function fillTestSelectors() {
  const { data } = await api("/api/tests/taxonomy");
  if (!data || !data.levels) return;
  $("testLevels").innerHTML = data.levels.map((l) =>
    `<option value="${esc(l.name)}"${l.tests ? "" : " disabled"}>`
    + `${esc(l.name)} (${l.tests}) — ${esc(l.means)}</option>`).join("");
  $("testModules").innerHTML = (data.modules || []).map((m) =>
    `<option value="${esc(m.name)}">${esc(m.name)} (${m.tests})</option>`).join("");
  $("testPriorities").innerHTML = (data.priorities || []).map((p) =>
    `<option value="${esc(p.name)}"${p.tests ? "" : " disabled"}>`
    + `${esc(p.name)} (${p.tests}) — ${esc(p.means)}</option>`).join("");
}

const pickedValues = (id) => [...$(id).selectedOptions].map((o) => o.value);

/* Choosing an environment that is not ready should lead somewhere. */
function offerConfigure(name, where) {
  const env = (ENVS || []).find((e) => e.name === name);
  if (!env || env.ready) return false;
  banner("err", `${name} needs ${(env.unresolved || []).join(", ")} before it can be used`
              + `${where ? " " + where : ""}. Opening Configure.`);
  configureEnv(name);
  return true;
}

async function runTests(kinds) {
  $("testOutCard").hidden = false;
  $("testOut").textContent = "running…";
  const { data } = await api("/api/tests/run", {
    method: "POST",
    body: JSON.stringify({ env: $("testEnv").value, kinds, verbose: false,
                           levels: pickedValues("testLevels"),
                           priorities: pickedValues("testPriorities"),
                           modules: pickedValues("testModules"),
                           only: $("testOnly").value.trim() || undefined,
                           drafts: $("testDrafts").checked,
                           tags: TAG_FILTER ? [TAG_FILTER] : [] }),
  });
  if (data.error) { $("testOut").textContent = data.error; return; }
  LAST_RUN = data.job;
  await followJob(data.job, $("testOut"), $("testElapsed"), $("btnCancelTests"),
                  null, $("testRows"), $("testProgress"));
  const rep = (await api(`/api/job/${data.job}/report`)).data || {};
  renderVerdicts(rep);
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
  // The badges on every test are its history, and the run just changed it.
  // Only the scoped run refreshed them, so a full run left every badge showing
  // whatever it said when the list was last drawn.
  await loadTests();
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
  // only worth a word in the sidebar when it differs from what the team locked
  $("navLock").innerHTML = st.state === "drift"
    ? '<span style="color:#b25e00">changed</span>' : "";
  docRender(data);

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
  // The mock's own setting follows the project. Left behind, this screen went
  // on describing the old document and Restart put the mock back onto it.
  if (!module && data.spec) $("spec").value = data.spec;
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

/* ========================================================================
   Workbench — assembling a flow from real responses
   ======================================================================== */

/* The model has always supported chaining: capture a value, use {{it}} later.
   What was missing was any way to DISCOVER the path — you had to know the
   response said `data.id` and type it correctly. So: run a step, look at what
   actually came back, click the value, and the binding writes itself. */

let WB = { steps: [], ran: {}, loadedFrom: null, touched: false, data: {} };

function wbBlankStep(role) {
  // A lone step is the thing being tested, not scaffolding for it. Defaulting
  // to "setup" made a one-step flow structurally unable to pass: its failure
  // reported as blocked, which tells you to fix a setup that IS the test.
  return { role: role || "target", name: "",
           request: { method: "POST", path: "", body: "" },
           assertions: [{ type: "status", in: [200, 201] }], capture: {} };
}

/* The operations this step could call, for the method it is set to. Typing a
   path from memory is how you end up testing an endpoint that does not exist;
   the spec is right here, so offer it. */
let WB_ROUTES = [];

function wbOperations(method) {
  return WB_ROUTES.filter((r) => r.method === (method || "GET"))
    .sort((a, b) => a.path.localeCompare(b.path));
}

/* The path as the spec writes it, so a path already filled with real ids or
   {{variables}} still matches the operation it came from. */
function wbBarePath(step) {
  const filled = step.request.path || "";
  if (!filled) return "";
  const exact = WB_ROUTES.find((r) => r.path === filled);
  if (exact) return exact.path;
  const shaped = filled.replace(/\{\{[^}]+\}\}/g, "{x}")
                       .replace(/\/[0-9a-f-]{8,}/gi, "/{x}");
  const match = WB_ROUTES.find((r) =>
    r.path.replace(/\{[^}]+\}/g, "{x}") === shaped);
  return match ? match.path : "";
}

/* Choosing an operation fills everything the spec can supply: a path with its
   parameters typed correctly, the required query, and a valid body. */
async function wbPickOperation(index, specPath) {
  const step = WB.steps[index];
  if (!specPath) return;
  const q = new URLSearchParams({ method: step.request.method, path: specPath });
  const { data } = await api("/api/sample-request?" + q);
  if (!data || data.error) { banner("err", (data && data.error) || "could not read the spec"); return; }
  step.request.path = data.path || specPath;
  step.request.query = data.query || "";
  if (data.body != null) step.request.body = JSON.stringify(data.body, null, 2);
  if (!step.name) step.name = data.summary || `${step.request.method} ${specPath}`;
  // the spec says which statuses are documented; a success assertion drawn
  // from that beats a guess at 200
  const good = (data.statuses || []).map(Number)
    .filter((c) => c >= 200 && c < 300);
  if (good.length) {
    step.assertions = [good.length === 1
      ? { type: "status", equals: good[0] }
      : { type: "status", in: good }];
  }
  wbRender();
  const notes = [];
  if ((data.required_query || []).length) notes.push(`required query: ${data.required_query.join(", ")}`);
  if (data.statuses && data.statuses.length) notes.push(`documents ${data.statuses.join(", ")}`);
  banner("ok", `Filled from the spec${notes.length ? " — " + notes.join("  ·  ") : ""}`
             + ". Replace any id with a {{variable}} from an earlier step.");
}

/* The values that resolve at run time. Discoverable only from a placeholder
   before this, which is the same as not existing: everybody wrote constants,
   and the second run collided with the first. */
let DYNAMIC_VALUES = [];

async function loadDynamicValues() {
  if (DYNAMIC_VALUES.length) return DYNAMIC_VALUES;
  try {
    const { data } = await api("/api/tests/values");
    DYNAMIC_VALUES = data.values || [];
  } catch { DYNAMIC_VALUES = []; }
  return DYNAMIC_VALUES;
}

function renderDynamicChips() {
  $("wbSteps").querySelectorAll("[data-values]").forEach((host) => {
    const i = +host.dataset.values;
    host.innerHTML = DYNAMIC_VALUES.map((v) =>
      `<button type="button" class="chip" data-insert="${i}|${esc(v.name)}"
         title="${esc(v.about)} — e.g. ${esc(v.example)}">{{${esc(v.name)}}}</button>`)
      .join("");
    host.querySelectorAll("[data-insert]").forEach((chip) =>
      chip.addEventListener("click", () => {
        const [at, name] = chip.dataset.insert.split("|");
        const field = $("wbSteps").querySelector(`[data-body="${at}"]`);
        if (!field) return;
        const start = field.selectionStart ?? field.value.length;
        const token = `{{${name}}}`;
        field.value = field.value.slice(0, start) + token + field.value.slice(start);
        WB.steps[+at].request.body = field.value;
        WB.touched = true;
        field.focus();
        field.setSelectionRange(start + token.length, start + token.length);
      }));
  });
}

/* A flow may carry values no step produces — an id the document cannot supply.
   The workbench dropped them silently on load and never saved them, so an
   imported test that declared `data` was refused the moment you ran it, while
   the hint told you to "add it to the flow's data" with nowhere to do so. */
function wbDataFromText() {
  const out = {};
  for (const line of ($("wbData").value || "").split("\n")) {
    const text = line.split("#")[0].trim();
    if (!text) continue;
    const at = text.indexOf("=");
    if (at < 1) continue;
    out[text.slice(0, at).trim()] = text.slice(at + 1).trim();
  }
  return out;
}

function wbDataToText(data) {
  $("wbData").value = Object.entries(data || {})
    .map(([k, v]) => `${k}=${v}`).join("\n");
}

/* Names the flow supplies itself, and the side effect of keeping WB.data current. */
function wbScope() {
  WB.data = wbDataFromText();
  return Object.keys(WB.data);
}

function wbRender() {
  if (!WB.steps.length) WB.steps.push(wbBlankStep());
  const produced = [wbScope()];                       // what is in scope by each step
  $("wbSteps").innerHTML = WB.steps.map((step, i) => {
    const ran = WB.ran[i];
    const consumes = wbVariablesUsed(step);
    const known = new Set(produced.flat());
    const missing = consumes.filter((v) => !known.has(v));
    const outs = Object.keys(step.capture || {});
    produced.push(outs);
    const isLast = i === WB.steps.length - 1;
    return `
    <div class="wbstep ${ran ? "ran-" + ran.outcome : ""}" data-i="${i}">
      <header>
        <span class="n">${i + 1}</span>
        <select data-role="${i}" style="width:auto">
          ${["setup", "target", "step", "cleanup"].map((r) =>
            `<option value="${r}"${step.role === r ? " selected" : ""}>${r}</option>`).join("")}
        </select>
        <input type="text" data-name="${i}" value="${esc(step.name || "")}"
               placeholder="what this step does" style="flex:1">
        ${ran ? `<span class="tag">${ran.status ?? "—"} · ${ran.ms ?? "?"}ms</span>` : ""}
        ${step.request.method === "POST" && Object.keys(step.capture || {}).length
          ? `<button class="sm" data-cleanup="${i}"
               title="add a step that deletes what this one creates">+ cleanup</button>` : ""}
        <button class="sm" data-run="${i}">Run to here</button>
        <button class="sm" data-del="${i}"${WB.steps.length < 2 ? " disabled" : ""}>×</button>
      </header>
      <div class="inner">
        <div class="row">
          <div style="max-width:120px"><label>Method</label>
            <select data-method="${i}">
              ${["GET", "POST", "PUT", "PATCH", "DELETE"].map((m) =>
                `<option${step.request.method === m ? " selected" : ""}>${m}</option>`).join("")}
            </select></div>
          <div style="flex:1"><label>Operation</label>
            <select data-op="${i}">
              <option value="">— pick one from the spec —</option>
              ${wbOperations(step.request.method).map((r) =>
                `<option value="${esc(r.path)}"${r.path === wbBarePath(step) ? " selected" : ""}>`
                + `${esc(r.path)}${r.summary ? "  —  " + esc(r.summary) : ""}</option>`).join("")}
            </select></div>
        </div>
        <label>Path <span class="hint">— edit it to use a captured value,
          e.g. {{departmentId}} in place of an id</span></label>
        <input type="text" data-path="${i}" value="${esc(step.request.path || "")}"
               placeholder="/api/v1/…">
        ${step.request.query ? `
        <label>Query</label>
        <input type="text" data-query="${i}" value="${esc(step.request.query)}">` : ""}
        ${["POST", "PUT", "PATCH"].includes(step.request.method) ? `
        <label>Body <span class="hint">— click a value to insert it; these resolve
          when the test runs, so two runs cannot collide</span></label>
        <div class="quick" data-values="${i}"></div>
        <textarea data-body="${i}" rows="4"
          placeholder='{"title": "{{$uuid}}"}'>${esc(wbBodyText(step))}</textarea>` : ""}

        <details class="wbasserts" data-asserts="${i}"${
            (step.assertions || []).length > 1 ? " open" : ""}>
          <summary style="cursor:pointer;font-size:12px;margin-top:8px">
            Assertions (${(step.assertions || []).length}) — what must hold for this
            step to pass</summary>
          <div class="wbahost" data-ahost="${i}" style="margin-top:7px"></div>
          ${ran && ran.bindable && ran.bindable.length ? `
            <p class="hint">Tip: a value below can be asserted on as well as bound —
              its path is what an assertion needs.</p>` : ""}
        </details>

        <div class="wbwire">
          <div><b>uses</b>${consumes.length
            ? consumes.map((v) => `<span class="wbchip ${missing.includes(v) ? "missing" : "in"}"
                >{{${esc(v)}}}</span>`).join("")
            : '<span class="hint">nothing from earlier steps</span>'}</div>
          <div><b>provides</b>${outs.length
            ? outs.map((v) => `<span class="wbchip out" data-unbind="${i}|${esc(v)}"
                title="click to remove">{{${esc(v)}}} ×</span>`).join("")
            : '<span class="hint">nothing yet</span>'}</div>
        </div>
        ${missing.length ? `<p class="hint" style="color:var(--err)">
          ${missing.map(esc).join(", ")} ${missing.length > 1 ? "are" : "is"} not provided by
          any earlier step — run an earlier step and bind ${missing.length > 1 ? "them" : "it"},
          or add ${missing.length > 1 ? "them" : "it"} to the flow's data.</p>` : ""}

        ${ran && ran.bindable && ran.bindable.length ? `
        <details${isLast ? " open" : ""} style="margin-top:9px">
          <summary style="cursor:pointer;font-size:12px">
            Values you can carry forward — click one
            (${ran.bindable.length})</summary>
          ${wbListNote(ran.bindable)}
          <div style="max-height:230px;overflow:auto;margin-top:6px">
            ${ran.bindable.map((b) => `
              <div class="wbfield">
                <span class="p">${esc(b.path)}</span>
                <span class="v">${esc(String(b.value))}</span>
                ${b.looks_like_id ? '<span class="id">ID</span>' : ""}
                ${b.of_list ? '<span class="id" style="color:var(--accent)">COUNT</span>' : ""}
                <button type="button" class="sm"
                        data-bind="${i}|${esc(b.path)}|${esc(b.suggested)}"
                  >use as {{${esc(b.suggested)}}}</button>
                <button type="button" class="sm"
                        data-assert="${i}|${esc(b.path)}|${esc(String(b.value))}|${
                          b.of_list ? "count" : "value"}"
                  >assert on it</button>
              </div>`).join("")}
          </div>
        </details>` : ""}
        ${ran && ran.response_json !== undefined && ran.response_json !== null ? `
        <details style="margin-top:6px">
          <summary style="cursor:pointer;font-size:12px">The whole response</summary>
          <pre style="max-height:260px;overflow:auto;margin-top:6px;font-size:11.5px"
            >${esc(JSON.stringify(ran.response_json, null, 2))}</pre>
        </details>` : ""}
        ${ran && ran.checks && ran.checks.length ? `
        <div style="margin-top:7px;font-size:12px">
          ${ran.checks.map((c) => `<div style="color:var(--${c.ok ? "ok" : "err"})">
            ${c.ok ? "PASS" : "FAIL"} ${esc(c.label)}${c.ok ? "" : " — " + esc(c.detail || "")}
          </div>
          ${c.rebind && (c.rebind.candidates || []).length ? `
            <div style="margin:4px 0 8px 18px">
              <div class="hint">The field moved or was renamed. Point
                <code>{{${esc(c.rebind.name)}}}</code> at what is there now:</div>
              ${c.rebind.candidates.map((cand) => `
                <div class="wbfield"
                     data-rebind="${i}|${esc(c.rebind.name)}|${esc(cand.path)}">
                  <span class="p">${esc(cand.path)}</span>
                  <span class="v">${esc(String(cand.value))}</span>
                  <span class="hint">${esc(cand.why)}</span>
                </div>`).join("")}
            </div>` : ""}`).join("")}
        </div>` : ""}
      </div>
    </div>`;
  }).join("");
  // the editors are built, not stringified: they are the same component the
  // save dialog uses, so an assertion means the same thing wherever it is
  // written
  WB.steps.forEach((step, i) => {
    const host = $("wbSteps").querySelector(`[data-ahost="${i}"]`);
    if (host) assertionEditor(host, step.assertions || []);
  });
  renderDynamicChips();
  wbWire();
}

/* Read every step's assertions out of its editor, so what runs is what is on
   screen rather than what was last stored. */
function wbSyncAssertions() {
  WB.steps.forEach((step, i) => {
    const host = $("wbSteps").querySelector(`[data-ahost="${i}"]`);
    if (!host) return;
    const read = readAssertions(host);
    step.assertions = read.length ? read : [{ type: "status", in: [200, 201] }];
  });
}

/* Every {{name}} a step refers to, so "uses" is read from the step itself and
   cannot fall out of step with what it actually needs. */
/* A list offers its first item and its length — say so, because a panel
   showing data.items[0] and nothing else reads as "the response held one
   thing", which is exactly the wrong conclusion. */
function wbListNote(fields) {
  const lists = fields.filter((f) => f.of_list);
  if (!lists.length) return "";
  return `<p class="hint" style="margin:5px 0 0 0">`
    + lists.map((l) => `<code>${esc(l.path.replace(/\.length$/, ""))}</code> holds `
        + `<b>${l.value}</b> item${l.value === 1 ? "" : "s"}`).join("; ")
    + `. Fields below come from the first one; bind <code>.length</code> to assert `
    + `how many, and open the whole response to see them all.</p>`;
}

function wbVariablesUsed(step) {
  const text = JSON.stringify(step.request || {});
  const found = new Set();
  for (const m of text.matchAll(/\{\{([^}]+)\}\}/g)) {
    const name = m[1].trim();
    if (!name.startsWith("$")) found.add(name);   // $uuid and friends are built in
  }
  return [...found];
}

function wbBodyText(step) {
  const b = step.request.body;
  if (b === undefined || b === null || b === "") return "";
  return typeof b === "string" ? b : JSON.stringify(b, null, 2);
}

/* Redraw only one step's uses/provides, leaving every other node — and every
   listener attached to it — alone. */
function wbRefreshWire(index) {
  const host = $("wbSteps").querySelector(`.wbstep[data-i="${index}"] .wbwire`);
  if (!host) return;
  const step = WB.steps[index];
  const known = new Set([...wbScope(), ...WB.steps.slice(0, index)
    .flatMap((s) => Object.keys(s.capture || {}))]);
  const consumes = wbVariablesUsed(step);
  const missing = consumes.filter((v) => !known.has(v));
  const outs = Object.keys(step.capture || {});
  host.innerHTML = `
    <div><b>uses</b>${consumes.length
      ? consumes.map((v) => `<span class="wbchip ${missing.includes(v) ? "missing" : "in"}"
          >{{${esc(v)}}}</span>`).join("")
      : '<span class="hint">nothing from earlier steps</span>'}</div>
    <div><b>provides</b>${outs.length
      ? outs.map((v) => `<span class="wbchip out" data-unbind="${index}|${esc(v)}"
          title="click to remove">{{${esc(v)}}} ×</span>`).join("")
      : '<span class="hint">nothing yet</span>'}</div>`;
  host.querySelectorAll("[data-unbind]").forEach((el) =>
    el.addEventListener("click", () => {
      const [i, name] = el.dataset.unbind.split("|");
      delete WB.steps[+i].capture[name];
      wbRender();
    }));
}

function wbWire() {
  const q = (sel, fn) => $("wbSteps").querySelectorAll(sel).forEach(fn);
  q("[data-role]", (el) => el.addEventListener("change", () => {
    WB.steps[+el.dataset.role].role = el.value; wbRender();
  }));
  q("[data-name]", (el) => el.addEventListener("input", () => {
    WB.steps[+el.dataset.name].name = el.value;
    WB.touched = true;
  }));
  q("[data-method]", (el) => el.addEventListener("change", () => {
    WB.steps[+el.dataset.method].request.method = el.value; wbRender();
  }));
  q("[data-path]", (el) => el.addEventListener("input", () => {
    const at = +el.dataset.path;
    WB.steps[at].request.path = el.value;
    WB.touched = true;
    // Update in place. A full re-render here replaced the DOM on blur —
    // including the Run button you were on your way to clicking, so the click
    // landed on a detached node and nothing happened.
    wbRefreshWire(at);
    // and keep the operation picker honest: a hand-edited path that no longer
    // matches any operation must stop claiming the one it used to be
    const picker = $("wbSteps").querySelector(`[data-op="${at}"]`);
    if (picker) picker.value = wbBarePath(WB.steps[at]);
  }));
  q("[data-query]", (el) => el.addEventListener("input", () => {
    WB.steps[+el.dataset.query].request.query = el.value;
  }));
  q("[data-op]", (el) => el.addEventListener("change", () =>
    wbPickOperation(+el.dataset.op, el.value)));
  q("[data-body]", (el) => el.addEventListener("input", () => {
    WB.steps[+el.dataset.body].request.body = el.value;
  }));
  q("[data-run]", (el) => el.addEventListener("click", () => wbRun(+el.dataset.run)));
  q("[data-del]", (el) => el.addEventListener("click", () => {
    const gone = +el.dataset.del;
    WB.steps.splice(gone, 1);
    // Keep what the other steps returned. Throwing all of it away meant
    // removing one step lost the responses you were binding from.
    const kept = {};
    Object.keys(WB.ran).map(Number).forEach((i) => {
      if (i < gone) kept[i] = WB.ran[i];
      else if (i > gone) kept[i - 1] = WB.ran[i];
    });
    WB.ran = kept;
    wbRender();
  }));
  q("[data-bind]", (el) => el.addEventListener("click", () => {
    const [i, path, suggested] = el.dataset.bind.split("|");
    wbBind(+i, path, suggested);
  }));
  q("[data-cleanup]", (el) => el.addEventListener("click", () => wbCleanup(+el.dataset.cleanup)));
  q("[data-assert]", (el) => el.addEventListener("click", () => {
    const [i, path, value, kind] = el.dataset.assert.split("|");
    wbAssertOn(+i, path, value, kind);
  }));
  q("[data-rebind]", (el) => el.addEventListener("click", () => {
    const [i, name, path] = el.dataset.rebind.split("|");
    WB.steps[+i].capture[name] = path;          // one click, one edit
    wbRender();
    banner("ok", `{{${name}}} now reads ${path}. Run the step again to confirm.`);
  }));
  q("[data-unbind]", (el) => el.addEventListener("click", () => {
    const [i, name] = el.dataset.unbind.split("|");
    delete WB.steps[+i].capture[name];
    wbRender();
  }));
}

/* Auto-named, with the chance to change it — the name is proposed from the
   field and the resource it came from, which is right often enough that
   typing one would be busywork, and wrong often enough to stay editable. */
function wbBind(index, path, suggested) {
  const taken = new Set(WB.steps.flatMap((s) => Object.keys(s.capture || {})));
  let name = suggested;
  let n = 2;
  while (taken.has(name)) name = `${suggested}${n++}`;
  const chosen = prompt(
    `Call this value what?\n\n${path}\n\nLater steps use it as {{name}}.`, name);
  if (!chosen) return;
  WB.steps[index].capture = WB.steps[index].capture || {};
  WB.steps[index].capture[chosen.trim()] = path;
  wbRender();
  banner("ok", `{{${chosen.trim()}}} is now available to every step after this one.`);
}

/* What a flow creates on a shared server stays there unless something removes
   it. The spec knows which endpoint deletes the thing, so offer the step
   rather than depend on remembering to write one. */
/* Writing an assertion is mostly getting the path right, and the path is on
   screen. For a list it is the COUNT that matters — "at least one came back"
   is the assertion people actually want, and `length_gte 1` is not something
   anybody guesses. */
function wbAssertOn(index, path, value, kind) {
  wbSyncAssertions();
  const step = WB.steps[index];
  step.assertions = (step.assertions || []).filter(
    (a) => !(a.type === "jsonpath" && a.path === path));
  if (kind === "count") {
    step.assertions.push({ type: "jsonpath", path: path.replace(/\.length$/, ""),
                           op: "length_gte", value: 1 });
  } else {
    const n = Number(value);
    step.assertions.push(Number.isFinite(n) && value !== ""
      ? { type: "jsonpath", path, op: "equals", value: n }
      : { type: "jsonpath", path, op: "exists" });
  }
  wbRender();
  const added = step.assertions[step.assertions.length - 1];
  banner("ok", kind === "count"
    ? `Asserting ${added.path} has at least 1 item. Change the operator to `
      + `length for an exact count, or length_gte for a floor.`
    : `Asserting ${added.path} ${added.op}${added.value !== undefined
        ? " " + added.value : ""}. Edit it below if that is not what you meant.`);
}

async function wbCleanup(index) {
  const step = WB.steps[index];
  const variable = Object.keys(step.capture || {})[0];
  if (!variable) { banner("err", "Bind the created id first, so cleanup knows what to remove."); return; }
  const q = new URLSearchParams({ path: step.request.path, var: variable });
  const { data } = await api("/api/tests/cleanup-for?" + q);
  if (data.error) { banner("err", data.error); return; }
  if (!data.found) {
    banner("err", `No cleanup added — ${data.why}. Add a step by hand if something else `
                + `removes it.`);
    return;
  }
  WB.steps.push({ role: "cleanup", name: data.step.name,
                  request: { method: "DELETE", path: data.step.request.path, body: "" },
                  assertions: data.step.assertions, capture: {} });
  wbRender();
  banner("ok", `Added a cleanup step using ${data.operation}. Cleanup is best-effort: `
             + `it runs even when the flow fails, and its own failure does not fail the flow.`);
}

/* "a=1&b=2" is what a person types; the runner wants it as pairs. */
function wbQueryObject(text) {
  const out = {};
  String(text || "").replace(/^\?/, "").split("&").forEach((pair) => {
    if (!pair) return;
    const [k, ...rest] = pair.split("=");
    if (k) out[k] = rest.join("=");
  });
  return out;
}

function wbCollect(includeCleanup = true) {
  return WB.steps.filter((s) => includeCleanup || s.role !== "cleanup").map((step) => {
    const out = { role: step.role, name: step.name || undefined,
                  request: { method: step.request.method, path: step.request.path,
                             ...(step.request.query
                                 ? { query: wbQueryObject(step.request.query) } : {}) },
                  assertions: step.assertions || [{ type: "status", in: [200, 201] }] };
    const text = (step.request.body || "").toString().trim();
    if (text) {
      try { out.request.body = JSON.parse(text); }
      catch { out.request.body = text; }        // a non-JSON body is still a body
    }
    if (Object.keys(step.capture || {}).length) out.capture = step.capture;
    return out;
  });
}

async function wbRun(upto) {
  wbSyncAssertions();
  const steps = wbCollect();
  if (!steps.some((s) => s.request.path)) { banner("err", "Give a step a path first."); return; }
  $("wbSteps").querySelectorAll("[data-run]").forEach((b) => (b.disabled = true));
  try {
    // Only claim this as a run of the saved test when it IS the saved test:
    // loaded from disk, not edited since, and run end to end. An experiment
    // that happens to pass must not put a green badge on something nobody ran.
    const faithful = WB.loadedFrom && !WB.touched && upto >= steps.length - 1;
    const { data } = await api("/api/tests/chain", {
      method: "POST",
      body: JSON.stringify({ target: $("wbEnv").value || "mock", upto,
                             kind: $("wbKind").value, steps,
                             data: wbDataFromText(),
                             ...(faithful ? { record: WB.loadedFrom } : {}) }),
    });
    if (data.error) { banner("err", data.error); return; }
    if (!data.ok) { banner("err", (data.errors || ["could not run"]).join("  ·  ")); return; }
    (data.result.steps || []).forEach((r, i) => (WB.ran[i] = r));
    wbRender();
    const last = (data.result.steps || [])[upto];
    banner(last && last.outcome === "pass" ? "ok" : "err",
           `Ran ${data.ran} of ${data.of} against ${data.target}`
           + (last ? ` — step ${upto + 1} ${last.outcome}` : "")
           + (data.recorded ? `. Recorded against ${data.recorded}.`
              : WB.loadedFrom ? ". Not recorded — this differs from what is saved."
              : ""));
    if (data.recorded) await loadTests();
  } catch (err) {
    banner("err", `Could not run: ${err.message}`);
  } finally {
    $("wbSteps").querySelectorAll("[data-run]").forEach((b) => (b.disabled = false));
  }
}

async function wbSave() {
  wbSyncAssertions();
  const id = $("wbId").value.trim();
  const module = $("wbModule").value.trim();
  if (!id) { banner("err", "Give the flow an id."); return; }
  if (!module) { banner("err", "Which module does this belong to?"); return; }
  const all = wbCollect();
  const scenario = {
    id, name: $("wbId").value.trim().replace(/-/g, " "),
    kind: $("wbKind").value, levels: [$("wbLevel").value],
    // cleanup is a separate list: it runs even when the flow fails, which is
    // the only way a failed run does not leave its rows behind
    steps: all.filter((s) => s.role !== "cleanup"),
    cleanup: all.filter((s) => s.role === "cleanup"),
  };
  if (!scenario.cleanup.length) delete scenario.cleanup;
  const flowData = wbDataFromText();
  if (Object.keys(flowData).length) scenario.data = flowData;
  const { data } = await api("/api/tests/save-flow", {
    method: "POST",
    body: JSON.stringify({ suite: module, stage: "draft", flow: scenario }),
  });
  if (!data.ok) { banner("err", (data.errors || [data.error || "refused"]).join("  ·  ")); return; }
  WB.loadedFrom = { suite: module, id, stage: "draft" };
  WB.touched = false;
  banner("ok", `Saved to ${data.file}. It is a draft until it passes.`);
  $("wbSaveNote").textContent =
    `Saved as ${id} in ${module} (${$("wbLevel").value}). Drafts are gitignored — `
    + `promote it once it passes and it joins the shared suite.`;
  loadTests();
}

function wbDescribeTarget() {
  const t = $("wbEnv").value || "mock";
  $("wbTarget").textContent = t;
  const live = t !== "mock" && t !== "mock-auth" && t !== "mock-login";
  $("wbLiveWarn").hidden = !live;
  $("wbLiveWarn").className = "banner warn";
  if (live) {
    const env = (ENVS || []).find((e) => e.name === t);
    const ro = env && env.readonly;
    $("wbLiveWarn").textContent = ro
      ? `Heads up: ${t} is read-only, so steps here may read but never create, `
        + `change or delete.`
      : `Heads up: steps run against ${t}, a real server. Anything a step creates is `
        + `really created — use {{$uuid}} in names so runs cannot collide, and add a `
        + `cleanup step for what you make.`;
  }
}

async function wbInit() {
  await loadDynamicValues();
  try {
    const routes = await api("/api/routes");
    WB_ROUTES = (routes.data.routes || routes.data || []).map((r) =>
      ({ method: r.method, path: r.path, summary: r.summary || "" }));
  } catch { WB_ROUTES = []; }
  const { data } = await api("/api/tests/taxonomy");
  if (data && data.levels) {
    $("wbLevel").innerHTML = data.levels
      .map((l) => `<option value="${esc(l.name)}"${l.name === "regression" ? " selected" : ""}>`
                + `${esc(l.name)} — ${esc(l.means)}</option>`).join("");
    $("wbModules").innerHTML = (data.modules || [])
      .map((m) => `<option value="${esc(m.name)}">`).join("");
  }
  $("wbEnv").innerHTML = ['<option value="mock">mock — the local mock</option>']
    .concat((ENVS || []).filter((e) => e.name !== "mock")
      .map((e) => `<option value="${esc(e.name)}">${esc(e.name)}`
                + `${e.ready ? "" : "  — needs " + (e.unresolved || []).length
                                  + " value(s)"}</option>`)).join("");
  wbDescribeTarget();
  wbRender();
}

$("wbToggle").addEventListener("click", () => {
  const open = $("wbBody").hidden;
  $("wbBody").hidden = !open;
  $("wbToggle").textContent = open ? "Close" : "Open";
  if (open) wbInit();
});
$("wbAdd").addEventListener("click", () => {
  // adding a second step makes the first one setup and the new one the target
  if (WB.steps.length === 1 && WB.steps[0].role === "target") {
    WB.steps[0].role = "setup";
  }
  WB.steps.push(wbBlankStep("target"));
  wbRender();
});
$("wbRunAll").addEventListener("click", () => wbRun(WB.steps.length - 1));
$("wbSave").addEventListener("click", wbSave);
$("wbEnv").addEventListener("change", () => {
  wbDescribeTarget();
  offerConfigure($("wbEnv").value, "to run steps against");
});

/* ------------------------------------------- per-environment test data */

let ENV_DATA = { name: null, rows: [] };

async function openEnvData(name) {
  const { data } = await api(`/api/environments/${encodeURIComponent(name)}/data`);
  if (data.error) { banner("err", data.error); return; }
  ENV_DATA = { name, rows: (data.data || []).map((r) => ({
    name: r.name,
    value: r.literal ? r.value : "",
    secret: !r.literal,
  })) };
  $("envDataName").textContent = name;
  $("envDataCard").hidden = false;
  $("envDataNote").textContent = data.note || "";
  renderEnvData();
  $("envDataCard").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function renderEnvData() {
  $("envDataRows").innerHTML = ENV_DATA.rows.length
    ? ENV_DATA.rows.map((row, i) => `
      <div class="row" style="align-items:flex-end">
        <div style="flex:1"><label>Name</label>
          <input type="text" data-dname="${i}" value="${esc(row.name)}"
                 placeholder="expectedCountries"></div>
        <div style="flex:1"><label>Value on this server</label>
          <input type="text" data-dvalue="${i}" value="${esc(row.value ?? "")}"
                 placeholder="${row.secret ? "set in .env — type to replace" : "250"}"></div>
        <div style="align-self:center">
          <label style="font-weight:400;white-space:nowrap">
            <input type="checkbox" data-dsecret="${i}"${row.secret ? " checked" : ""}>
            keep out of git</label></div>
        <div><button class="sm danger" data-ddel="${i}">remove</button></div>
      </div>`).join("")
    : '<p class="hint">Nothing yet. Add a value that differs between servers — a row id, '
      + 'an expected count.</p>';

  const q = (sel, fn) => $("envDataRows").querySelectorAll(sel).forEach(fn);
  q("[data-dname]", (el) => el.addEventListener("input", () => {
    ENV_DATA.rows[+el.dataset.dname].name = el.value; }));
  q("[data-dvalue]", (el) => el.addEventListener("input", () => {
    ENV_DATA.rows[+el.dataset.dvalue].value = el.value; }));
  q("[data-dsecret]", (el) => el.addEventListener("change", () => {
    ENV_DATA.rows[+el.dataset.dsecret].secret = el.checked; }));
  q("[data-ddel]", (el) => el.addEventListener("click", () => {
    ENV_DATA.rows.splice(+el.dataset.ddel, 1); renderEnvData(); }));
}

$("envDataAdd").addEventListener("click", () => {
  ENV_DATA.rows.push({ name: "", value: "", secret: false });
  renderEnvData();
});
$("envDataClose").addEventListener("click", () => { $("envDataCard").hidden = true; });
$("envDataSave").addEventListener("click", async () => {
  const { data } = await api(
    `/api/environments/${encodeURIComponent(ENV_DATA.name)}/data`,
    { method: "POST", body: JSON.stringify({ data: ENV_DATA.rows }) });
  if (!data.ok) { banner("err", data.error); return; }
  banner("ok", data.message);
  $("envDataNote").textContent = (data.kept_out_of_git || []).length
    ? `${data.kept_out_of_git.join(", ")} went to .env, not the committed file.`
    : "Saved to environments.json — commit it so the team shares these values.";
  await loadEnvironments();
});

/* ------------------------------------------------- adding an environment */

$("btnEnvNew").addEventListener("click", () => {
  $("envNewCard").hidden = !$("envNewCard").hidden;
  if (!$("envNewCard").hidden) $("envNewName").focus();
});
$("envNewCancel").addEventListener("click", () => { $("envNewCard").hidden = true; });
$("envNewMode").addEventListener("change", () => {
  $("envNewLoginWrap").hidden = $("envNewMode").value !== "login";
});

$("envNewSave").addEventListener("click", async () => {
  const name = $("envNewName").value.trim().toLowerCase();
  if (!name) { banner("err", "Give the environment a name."); return; }
  const { data } = await api("/api/environments/create", {
    method: "POST",
    body: JSON.stringify({
      name, description: $("envNewDesc").value.trim(),
      mode: $("envNewMode").value,
      readonly: $("envNewReadonly").checked,
      login_path: $("envNewLoginPath").value.trim() || undefined,
    }),
  });
  if (!data.ok) { banner("err", data.error); return; }
  banner("ok", data.message);
  $("envNewNote").textContent = data.needs.length
    ? `Now set ${data.needs.join(", ")} — opening Configure.`
    : "Nothing left to set.";
  await loadEnvironments();
  $("envNewCard").hidden = true;
  if (data.needs.length) configureEnv(name);
});

/* The last run, as a file. HTML to read or forward, JUnit for a CI server that
   already renders it, JSON for anything else — the same run, three ways out. */
let LAST_RUN = null;          // this tab's own run, so the download is yours

$("testReport").addEventListener("click", () => {
  // One press downloads the report a person can open. It used to ask you to
  // type "html", "xml" or "json" into a browser prompt first.
  const wanted = $("testReportKind").value || "html";
  // ask for THIS tab's run. Without the id you get whatever finished last,
  // which is somebody else's results whenever anything ran in between.
  window.location.href = `/api/tests/report.${wanted}`
    + (LAST_RUN ? `?job=${encodeURIComponent(LAST_RUN)}` : "");
});

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
$("exploreTarget").addEventListener("change", () => {
  describeTarget();
  offerConfigure($("exploreTarget").value, "to explore against");
});
$("btnSaveTest").addEventListener("click", openSaveDialog);
$("btnTestsRefresh").addEventListener("click", () => { loadTests(); loadBindings(); });
$("btnRunTests").addEventListener("click", () =>
  runTests($("testKind").value ? [$("testKind").value] : []));
$("btnRunSanity").addEventListener("click", () => runTests(["e2e"]));
/* ------------------------------------------ pipeline and story briefs */

/* Whatever is selected above IS the pipeline: the same flags, so a failure in
   CI can be reproduced locally by copying one line. */
function genOpen(kind) {
  $("genCard").hidden = false;
  $("genStoryRow").hidden = kind !== "story";
  $("genPipeRow").hidden = kind !== "pipeline";
  $("genLifeRow").hidden = kind !== "lifecycle";
  $("genOut").textContent = "";
  if (kind === "lifecycle") {
    $("genTitle").textContent = "Lifecycles derived from the spec";
    $("genNote").textContent =
      "No model and no story. A contract sweep proves each operation answers "
      + "correctly on its own; it cannot prove the thing you created can then be "
      + "read, changed and removed, because that is a sequence. These are derived "
      + "from the shape of your paths, so they work on any spec.";
    $("genLifeOut").innerHTML = "";
    $("genLifeNote").textContent = "";
    $("genLifeImport").disabled = true;
  } else if (kind === "story") {
    $("genTitle").textContent = "Tests from a user story";
    $("genNote").textContent =
      "This builds a brief: the story, the operations from your spec that look "
      + "relevant, the house format, and what is already covered. It calls no model "
      + "— paste it wherever your team already works.";
    $("genStory").focus();
    if ($("genVia")) {
      const label = () => ($("genVia").value
        ? `Generate with ${$("genVia").value} and import`
        : "Build the brief");
      $("genStoryGo").textContent = label();
      $("genVia").onchange = () => { $("genStoryGo").textContent = label(); };
    }
  } else {
    $("genTitle").textContent = "Pipeline for this selection";
    $("genNote").textContent =
      "Built from what is selected above, so CI runs exactly what you just ran.";
  }
  $("genCard").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

$("btnPipeline").addEventListener("click", () => genOpen("pipeline"));
$("btnStory").addEventListener("click", () => genOpen("story"));

/* Only offer what this machine can actually run. A generator is a convenience;
   without one the prompt is still the deliverable, exactly as before. */
let GENERATORS = {};
async function loadGenerators() {
  try {
    const { data } = await api("/api/generators");
    GENERATORS = data.available || {};
  } catch { GENERATORS = {}; }
  const sel = $("genVia");
  if (!sel) return;
  const names = Object.keys(GENERATORS).filter((k) => GENERATORS[k]);
  sel.innerHTML = '<option value="">nobody — just give me the prompt</option>'
    + names.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
  $("genRefine").disabled = !GENERATORS.claude;
}
loadGenerators();

/* Generate and import in one go. What comes back is validated exactly as a
   paste is — a generator is a faster way to reach the same door, not a way
   around it — and it lands in drafts, where it still has to pass. */
async function generateWith(via) {
  const story = $("genStory").value.trim();
  const module = $("genModule").value.trim();
  $("genStoryGo").disabled = true;
  $("genOut").textContent = via
    ? `asking ${via} for tests — this takes a moment…`
    : "building the brief…";
  try {
    const { data } = await api("/api/tests/generate", {
      method: "POST",
      body: JSON.stringify({ story, via, module, refine: $("genRefine").checked }),
    });
    if (!data.ok) { banner("err", data.error || "could not generate");
                    $("genOut").textContent = data.error || ""; return; }
    if (data.criteria) {
      $("genNote").textContent = "What the story implies: " + data.criteria;
    }
    if (!via) { $("genOut").textContent = data.brief || ""; return; }

    const briefShown = data.brief || "";
    $("genOut").textContent = briefShown;

    // Wait for the generator to actually finish. followJob wants DOM nodes to
    // stream into; handing it nulls threw, the throw was swallowed, and the
    // import ran instantly against a job that had not started producing yet —
    // which is what "still generating" was telling us.
    const started = Date.now();
    let finished = false;
    for (let i = 0; i < 1200; i++) {
      let status;
      try { ({ data: status } = await api(`/api/job/${data.job}`)); }
      catch { break; }
      if (status && status.done) { finished = true; break; }
      const secs = Math.round((Date.now() - started) / 1000);
      $("genOut").textContent =
        `${via} is writing the tests — ${secs}s`
        + ` (two to five minutes is normal; Cancel is in the toolbar)\n\n`
        + ((status && status.output) || "").slice(-2000);
      await new Promise((r) => setTimeout(r, 1500));
    }
    if (!finished) {
      banner("err", `${via} did not finish in 30 minutes. The prompt is in the `
                    + `box above — copy it and paste it wherever you like.`);
      $("genOut").textContent = briefShown;
      return;
    }

    const { data: got } = await api("/api/tests/generate/import", {
      method: "POST", body: JSON.stringify({ job: data.job, module }) });
    if (!got.ok) {
      banner("err", (got.errors || [got.error || "refused"]).join("  ·  "));
      $("genOut").textContent = (got.reply || "")
        + "\n\n--- refused ---\n" + (got.errors || [got.error]).join("\n");
      return;
    }
    banner("ok", `${via} wrote tests into ${got.suite} — they are drafts until they pass.`);
    $("genOut").textContent = got.reply || "";
    await loadTests();
  } finally { $("genStoryGo").disabled = false; }
}
$("btnLifecycle").addEventListener("click", () => genOpen("lifecycle"));

/* The ids a document cannot explain, and what we know about each. Unsettled
   first — those are the ones that end up as a placeholder in somebody's test
   and then as "invalid or no longer available" from a real server. */
async function loadBindings() {
  let d;
  try { ({ data: d } = await api("/api/bindings")); } catch { return; }
  const ids = d.ids || [];
  const open = ids.filter((r) => !r.settled).length;
  $("bindCount").textContent = ids.length
    ? `${ids.length - open} of ${ids.length} placed` : "none needed";
  $("bindLearned").className = "tag " + (d.learned_from === "mock" ? "warn" : "ok");
  $("bindLearned").textContent = `names from ${d.learned_from || "mock"}`;
  fillBindEnvs(d.learned_from);
  $("bindCount").className = "tag " + (open ? "warn" : "ok");
  if (!d.have_index) {
    $("bindList").innerHTML = '<div class="empty">Start the mock — the self-check '
      + 'it runs is what tells us which endpoint returns what.</div>';
    return;
  }
  $("bindList").innerHTML = ids.map((r) => {
    const rec = r.recorded;
    const best = (r.candidates || [])[0];
    const where = rec && rec.path ? `${esc(rec.path)} from ${esc(rec.from)}`
      : rec && rec.value_required ? "you supply a real value"
      : best ? `${esc(best.path)} from ${esc(best.operation)}` : "";
    const mark = rec ? "recorded" : (best ? best.strength : "nothing");
    const cls = rec ? "pass" : (best && best.strength !== "weak" ? "pass" : "warn");
    return `<div class="runrow ${cls}" data-bind="${esc(r.field)}">
      <span class="v">${esc(mark)}</span>
      <span class="what" title="${esc(r.where)} on ${esc(r.operation)}">
        <code>${esc(r.field)}</code></span>
      <span class="why2">${where || "no field in any response looks like it"}</span>
    </div>
    <div class="bindopts" data-opts="${esc(r.field)}" style="margin:0 0 9px 26px">
      ${(r.candidates || []).map((c) => `
        <button class="sm" data-use="${esc(r.field)}"
          data-from="${esc(c.operation)}" data-path="${esc(c.path)}"
          title="${esc(c.why)}">use ${esc(c.path)}${c.needs_id ? " ⚠" : ""}</button>`).join("")}
      <button class="sm" data-manual="${esc(r.field)}">I supply this value</button>
      ${rec ? `<button class="sm danger" data-forget="${esc(r.field)}">forget</button>` : ""}
      ${(r.candidates || []).some((c) => c.needs_id)
        ? '<span class="hint">⚠ that read needs an id of its own first</span>' : ""}
    </div>`;
  }).join("");

  const post = async (body) => {
    const { data } = await api("/api/bindings",
      { method: "POST", body: JSON.stringify(body) });
    if (!data.ok) { banner("err", data.error || "could not record"); return; }
    banner("ok", `Recorded where ${body.field} comes from.`);
    await loadBindings();
  };
  $("bindList").querySelectorAll("[data-use]").forEach((b) =>
    b.addEventListener("click", () => post({ field: b.dataset.use,
      from: b.dataset.from, path: b.dataset.path })));
  $("bindList").querySelectorAll("[data-manual]").forEach((b) =>
    b.addEventListener("click", () => post({ field: b.dataset.manual,
      value_required: true })));
  $("bindList").querySelectorAll("[data-forget]").forEach((b) =>
    b.addEventListener("click", () => post({ field: b.dataset.forget, forget: true })));
}
$("bindRefresh").addEventListener("click", loadBindings);

/* Fix a suite in place rather than regenerating it: the tests somebody already
   reviewed keep their shape, and only the placeholder ids change. */
/* ------------------------------------------------------------- document
   Which document is in use, and one way to bring in another: a link or a file.
   Loading only ever shows what was found; nothing is replaced until the person
   says so. Comparing versions, the lock and per-tool choices are Advanced. */
const DOC = { found: null };

function showSrcAdvanced(on) {
  const open = on === undefined ? $("srcAdvWrap").hidden : !!on;
  $("srcAdvWrap").hidden = !open;
  $("srcAdvToggle").textContent = open ? "Advanced ▾" : "Advanced ▸";
  $("srcAdvToggle").setAttribute("aria-expanded", String(open));
}
window.showSrcAdvanced = showSrcAdvanced;
$("srcAdvToggle").addEventListener("click", () => showSrcAdvanced());

const docCount = (n) => `${n} endpoint${n === 1 ? "" : "s"}`;

function docRender(data) {
  if (!$("docNow")) return;
  const sum = data.summary || {}, st = data.state || {}, lock = data.lock || {};
  DOC.now = { source: data.source, operations: sum.operations };
  $("docNow").innerHTML = `<div class="docnow">
    <b>${esc(sum.title || data.source)}</b>${sum.version ? ` · version ${esc(sum.version)}` : ""}
      · ${docCount(sum.operations ?? 0)}
    <div class="from">from ${esc(data.source)}</div>
    ${st.state === "drift" ? `<div class="warn">
      <span>This is not the version the team last locked${
        lock.operations != null ? ` — that one had ${docCount(lock.operations)}` : ""}.</span>
      <button class="sm" id="docDiff">What changed?</button></div>` : ""}
  </div>`;
  const diff = $("docDiff");
  if (diff) diff.addEventListener("click", async () => {
    showSrcAdvanced(true);
    await showDiff(data.source);
    $("candidateCard").scrollIntoView({ behavior: "smooth", block: "start" });
  });
}

function docName(raw) {
  let name = String(raw || "").split(/[\\/]/).pop().replace(/[^A-Za-z0-9._-]+/g, "-")
    .replace(/^[-.]+|[-.]+$/g, "") || "document";
  if (!/\.(json|ya?ml)$/i.test(name)) name += ".json";
  // never write over the document in use before the person has agreed to it
  if (DOC.now && DOC.now.source === `specs/${name}`) name = "new-" + name;
  return name;
}

$("docChoose").addEventListener("click", () => $("docFile").click());
$("docFile").addEventListener("change", (ev) => {
  const f = ev.target.files[0];
  if (!f) return;
  $("docUrl").value = "";
  $("docChosen").textContent = `${f.name} chosen — press Load.`;
});
$("docUrl").addEventListener("input", () => {
  if ($("docUrl").value.trim() && $("docFile").files.length) {
    $("docFile").value = "";
    $("docChosen").textContent = "Nothing changes until you have seen what was found.";
  }
});

$("docLoad").addEventListener("click", async () => {
  const url = $("docUrl").value.trim();
  const file = $("docFile").files[0];
  if (!url && !file) { banner("err", "Paste a link or choose a file first."); return; }
  if (url && !/^https?:\/\//i.test(url)) {
    banner("err", "The link should start with http:// or https://");
    return;
  }
  $("docLoad").disabled = true; $("docLoad").textContent = "Loading…";
  try {
    let data;
    if (file) {
      ({ data } = await api("/api/spec/upload", { method: "POST",
        body: JSON.stringify({ content: await file.text(), name: docName(file.name) }) }));
    } else {
      ({ data } = await api("/api/spec/fetch", { method: "POST",
        body: JSON.stringify({ url, headers: "", name_from_title: true }) }));
    }
    if (!data.ok) {
      banner("err", /not a usable/i.test(data.error || "")
        ? "That is not an API document this can read. It needs an OpenAPI (Swagger) file."
        : `Could not load it. ${String(data.error || "").split("\n")[0].slice(0, 220)}`);
      return;
    }
    DOC.found = data;
    const sum = data.summary || {};
    const was = DOC.now && DOC.now.operations;
    $("docFound").hidden = false;
    $("docFound").innerHTML = `
      <div>Found <b>${esc(sum.title || data.file)}</b>${sum.version ? ` · version ${esc(sum.version)}` : ""}
        · ${docCount(sum.operations ?? 0)}${
        was != null ? ` <span class="hint">— the one in use has ${was}</span>` : ""}</div>
      <div class="btnrow" style="margin-top:10px">
        <button class="primary sm" id="docUse">Use this document</button>
        <button class="sm" id="docCancel">Cancel</button>
        <span class="hint">The mock restarts on it and the baseline tests are remade.
          Tests you wrote are kept.</span>
      </div>
      <div class="docimpact" id="docImpact"><span class="hint">Working out what it would change…</span></div>`;
    docImpact(data.file);
    $("docCancel").onclick = () => {
      DOC.found = null; $("docFound").hidden = true; $("docFile").value = "";
      $("docChosen").textContent = "Nothing changes until you have seen what was found.";
    };
    $("docUse").onclick = docUse;
  } catch (err) {
    banner("err", `Could not load it: ${err.message}`);
  } finally {
    $("docLoad").disabled = false; $("docLoad").textContent = "Load";
  }
});

/* What using this document would change — worked out without being asked,
   because the moment before a contract is swapped is the one moment it is
   cheap to find out. One sentence; the detail is there for whoever wants it. */
async function docImpact(file) {
  const host = $("docImpact");
  let d;
  try { ({ data: d } = await api("/api/spec/impact", { method: "POST", body: JSON.stringify({ to: file }) })); }
  catch { if (host) host.textContent = ""; return; }
  if (!$("docImpact") || !DOC.found || DOC.found.file !== file) return;
  if (!d.ok) { host.textContent = ""; return; }
  const WORD = { breaking: "breaks", note: "look", additive: "adds" };
  const changes = d.changes || [], tests = (d.tests || {}).affected || [];
  host.innerHTML = `<div><b>Compared with the one in use:</b> ${esc(d.sentence)}</div>`
    + (changes.length ? `<details style="margin-top:5px"><summary style="cursor:pointer">See what changes</summary>
        ${changes.slice(0, 12).map((c) => `<div class="line"><span class="sev ${esc(c.severity)}">${
          esc(WORD[c.severity] || c.severity)}</span><code>${esc(c.operation)}</code> — ${esc(c.what)}${
          c.detail ? ` <span class="hint">(${esc(c.detail)})</span>` : ""}</div>`).join("")}
        ${changes.length > 12 ? `<div class="hint">…and ${changes.length - 12} more, under Advanced → Compare two documents.</div>` : ""}
        ${tests.length ? `<div style="margin-top:6px"><b>Tests that use what changed</b></div>`
          + tests.slice(0, 8).map((t) => `<div class="line"><span class="sev ${esc(t.severity)}">${
              esc(WORD[t.severity] || t.severity)}</span>${esc(t.name)} <span class="hint">· ${esc(t.suite)}</span></div>`).join("")
          + (tests.length > 8 ? `<div class="hint">…and ${tests.length - 8} more.</div>` : "") : ""}
      </details>` : "");
}

async function docUse() {
  if (!DOC.found) return;
  $("docUse").disabled = true; $("docUse").textContent = "Switching…";
  const { data } = await api("/api/project", { method: "POST",
    body: JSON.stringify({ spec: DOC.found.file, module: null, clear: false }) });
  if (!data.ok) {
    banner("err", data.error || "could not switch");
    $("docUse").disabled = false; $("docUse").textContent = "Use this document";
    return;
  }
  DOC.found = null;
  if (data.spec) $("spec").value = data.spec;      // see setProjectSpec
  // said at once: the reloads below take a moment and silence reads as failure
  banner("ok", data.mock_restarted
    ? "Now using this document. The mock restarted on it and the baseline tests are being remade."
    : data.mock_running ? "Now using this document."
    : "Now using this document. Start the mock from Home to try it.");
  $("docFound").hidden = true;
  $("docUrl").value = ""; $("docFile").value = "";
  $("docChosen").textContent = "Nothing changes until you have seen what was found.";
  await loadProjectSpec();
  await refreshState();
  await loadSpecStatus();
  await loadCoverage();
  ROUTES = []; GUIDE = "";
  if (data.mock_restarted && typeof watchSelfcheck === "function") watchSelfcheck();
}

/* -------------------------------------------------------------- servers
   A server is a name, an address, a way of signing in, and whether it is safe
   to write to. That is all anybody adding "dev" needs to say. Which file it is
   stored in, what the variables are called and how authentication is modelled
   are true and still here — under Advanced — but they are not the question. */
const SRV = { open: null, results: {} };

const SRV_FIELD = (name) => {
  const low = name.toLowerCase();
  if (low.endsWith("base_url")) return ["Address", "https://api.dev.example.com", false];
  if (low.endsWith("username")) return ["Username", "", false];
  if (low.endsWith("password")) return ["Password", "", true];
  if (low.endsWith("cookie")) return ["Cookie", "copy it from your browser after logging in", true];
  if (low.endsWith("token")) return ["Token", "", true];
  const words = low.replace(/^[a-z0-9]+_/, "").replace(/_/g, " ");
  return [words.charAt(0).toUpperCase() + words.slice(1), "", false];
};

function showEnvAdvanced(on) {
  const open = on === undefined ? $("envAdvWrap").hidden : !!on;
  $("envAdvWrap").hidden = !open;
  $("envAdvToggle").textContent = open ? "Advanced ▾" : "Advanced ▸";
  $("envAdvToggle").setAttribute("aria-expanded", String(open));
}
window.showEnvAdvanced = showEnvAdvanced;
$("envAdvToggle").addEventListener("click", () => showEnvAdvanced());

function srvRender(list) {
  if (!$("srvList")) return;
  const mine = (list || []).filter((e) => !e.technical);
  const hidden = (list || []).length - mine.length;
  mine.sort((a, b) => (b.builtin - a.builtin) || (b.ready - a.ready) || a.name.localeCompare(b.name));
  $("srvList").innerHTML = mine.map((e) => {
    const r = SRV.results[e.name];
    const state = r ? (r.ok ? ["ok", "Connected"] : ["bad", "Could not connect"])
      : e.ready ? ["ok", "Ready"] : ["todo", `Needs ${e.needs.join(", ")}`];
    return `<div class="srvrow" data-srv="${esc(e.name)}">
      <span class="who"><b>${esc(e.name)}</b>
        ${e.builtin ? '<span class="tag">built in</span>' : ""}
        ${e.readonly ? '<span class="tag" title="tests that create, change or delete are refused here">read only</span>' : ""}
        <div>${esc(e.base_url || "no address yet")}${
          e.signs_in_with && e.signs_in_with !== "nothing" ? ` · signs in with ${esc(e.signs_in_with)}` : ""}</div>
        ${r && !r.ok ? `<div style="color:var(--err)">${esc(r.words)}</div>` : ""}
        ${r && r.ok ? `<div>${esc(r.words)}</div>` : ""}
      </span>
      <span class="srvstate ${state[0]}">${esc(state[1])}</span>
      ${e.builtin ? "" : `<button class="sm" data-srvedit="${esc(e.name)}">${e.ready ? "Edit" : "Finish setup"}</button>`}
      <button class="sm" data-srvtest="${esc(e.name)}" ${e.ready ? "" : "disabled"}
              title="${e.ready ? "sign in and make one real call" : "finish setting it up first"}">Test connection</button>
      ${SRV.open === e.name ? `<div class="srvform" data-srvvars="${esc(e.name)}" style="flex:1 1 100%">loading…</div>` : ""}
    </div>`;
  }).join("") || '<div class="empty">No servers yet.</div>';
  $("srvNote").textContent = hidden
    ? `${hidden} technical server${hidden === 1 ? "" : "s"} for testing sign-in handling ${hidden === 1 ? "is" : "are"} listed under Advanced.` : "";

  $("srvList").querySelectorAll("[data-srvtest]").forEach((b) =>
    b.addEventListener("click", () => srvTest(b.dataset.srvtest, b)));
  $("srvList").querySelectorAll("[data-srvedit]").forEach((b) =>
    b.addEventListener("click", () => {
      SRV.open = SRV.open === b.dataset.srvedit ? null : b.dataset.srvedit;
      srvRender(list);
    }));
  if (SRV.open) srvVars(SRV.open);
}

async function srvTest(name, button) {
  if (button) { button.disabled = true; button.textContent = "Testing…"; }
  const { data } = await api("/api/env-login", { method: "POST", body: JSON.stringify({ name }) });
  SRV.results[name] = data.ok
    ? { ok: true, words: "Signed in and got an answer — tests can run here." }
    : { ok: false, words: String(data.error || "It did not answer.").split("\n")[0].slice(0, 260) };
  banner(data.ok ? "ok" : "err",
         data.ok ? `${name}: connected.` : `${name}: could not connect. ${SRV.results[name].words}`);
  await loadEnvironments();
}

async function srvVars(name) {
  const host = $("srvList").querySelector(`[data-srvvars="${CSS.escape(name)}"]`);
  if (!host) return;
  const { data } = await api(`/api/environments/${encodeURIComponent(name)}/vars`);
  if (data.error) { host.textContent = data.error; return; }
  host.innerHTML = (data.fields || []).map((f) => {
    const [label, hint, secret] = SRV_FIELD(f.name);
    return `<label>${esc(label)}${f.set ? ' <span class="hint">— already set; leave empty to keep it</span>' : ""}</label>
      <input type="${secret ? "password" : "text"}" data-var="${esc(f.name)}"
             placeholder="${esc(f.set && !secret ? (f.hint || "") : hint)}" autocomplete="off">`;
  }).join("") + `
    <div class="btnrow" style="margin-top:12px">
      <button class="primary sm" data-srvsave>Save and test</button>
      <button class="sm" data-srvcancel>Cancel</button>
      <span class="hint">Saved on this computer only, never in the shared files.</span>
    </div>`;
  host.querySelector("[data-srvcancel]").onclick = () => { SRV.open = null; loadEnvironments(); };
  host.querySelector("[data-srvsave]").onclick = async () => {
    const values = {};
    host.querySelectorAll("[data-var]").forEach((el) => {
      if (el.value.trim()) values[el.dataset.var] = el.value.trim(); });
    if (Object.keys(values).length) {
      const { data: saved } = await api("/api/environments/vars",
        { method: "POST", body: JSON.stringify({ values }) });
      if (!saved.ok) { banner("err", saved.error || "could not save"); return; }
    }
    SRV.open = null;
    await loadEnvironments();
    const now = (ENVS || []).find((e) => e.name === name);
    if (now && now.ready) await srvTest(name);
    else banner("err", `${name} still needs ${(now && now.needs || []).join(", ")}.`);
  };
}

$("srvAdd").addEventListener("click", () => {
  const form = $("srvForm");
  if (!form.hidden) { form.hidden = true; return; }
  form.hidden = false;
  form.innerHTML = `<div class="srvform">
    <b>Add a server</b>
    <label for="srvName">Name</label>
    <input type="text" id="srvName" placeholder="dev" autocomplete="off">
    <label for="srvUrl">Address</label>
    <input type="text" id="srvUrl" placeholder="https://api.dev.example.com" autocomplete="off">
    <label>How do you sign in?</label>
    <div class="opts">
      <label><input type="radio" name="srvMode" value="none" checked> No sign-in needed</label>
      <label><input type="radio" name="srvMode" value="cookie"> A cookie from my browser</label>
      <label><input type="radio" name="srvMode" value="token"> A token</label>
      <label><input type="radio" name="srvMode" value="login"> A username and password</label>
    </div>
    <div id="srvSecrets"></div>
    <div class="opts"><label><input type="checkbox" id="srvReadonly">
      Read only — never create, change or delete anything on this server</label></div>
    <div class="btnrow" style="margin-top:12px">
      <button class="primary sm" id="srvCreate">Add and test</button>
      <button class="sm" id="srvCancel">Cancel</button>
      <span class="hint">What you type is saved on this computer only.</span>
    </div></div>`;
  const secrets = () => {
    const mode = form.querySelector('input[name="srvMode"]:checked').value;
    $("srvSecrets").innerHTML = mode === "cookie"
      ? '<label>Cookie</label><input type="password" data-secret="COOKIE" placeholder="copy it from your browser after logging in" autocomplete="off">'
      : mode === "token" ? '<label>Token</label><input type="password" data-secret="TOKEN" autocomplete="off">'
      : mode === "login" ? '<label>Username</label><input type="text" data-secret="USERNAME" autocomplete="off">'
                           + '<label>Password</label><input type="password" data-secret="PASSWORD" autocomplete="off">'
      : "";
  };
  form.querySelectorAll('input[name="srvMode"]').forEach((r) => r.addEventListener("change", secrets));
  $("srvCancel").onclick = () => { form.hidden = true; };
  $("srvCreate").onclick = async () => {
    const name = $("srvName").value.trim().toLowerCase();
    const url = $("srvUrl").value.trim();
    if (!/^[a-z0-9][a-z0-9._-]{0,40}$/.test(name)) {
      banner("err", "Give it a short name using lower-case letters, numbers or dashes — for example dev.");
      return;
    }
    if (!/^https?:\/\//i.test(url)) {
      banner("err", "The address should start with http:// or https://");
      return;
    }
    const mode = form.querySelector('input[name="srvMode"]:checked').value;
    $("srvCreate").disabled = true;
    try {
      const { data } = await api("/api/environments/create", { method: "POST",
        body: JSON.stringify({ name, mode, readonly: $("srvReadonly").checked,
                               description: `${name}, added from the console` }) });
      if (!data.ok) { banner("err", data.error || "could not add it"); return; }
      const prefix = name.toUpperCase().replace(/[^A-Z0-9]+/g, "_").replace(/^_+|_+$/g, "") || "ENV";
      const values = { [`${prefix}_BASE_URL`]: url };
      $("srvSecrets").querySelectorAll("[data-secret]").forEach((el) => {
        if (el.value.trim()) values[`${prefix}_${el.dataset.secret}`] = el.value.trim(); });
      await api("/api/environments/vars", { method: "POST", body: JSON.stringify({ values }) });
      form.hidden = true;
      await loadEnvironments();
      fillTestEnvs && fillTestEnvs();
      const now = (ENVS || []).find((e) => e.name === name);
      if (now && now.ready) await srvTest(name);
      else banner("ok", `${name} added. It still needs ${(now && now.needs || []).join(", ")} — press Finish setup.`);
    } finally { if ($("srvCreate")) $("srvCreate").disabled = false; }
  };
});

/* ----------------------------------------------------------------- load
   The tests that only read, repeated with several callers at once for a set
   time. What comes back is each endpoint's usual time and the time 95% of
   calls came in under — the number worth watching, because it is the slow
   calls people notice. */
let LOAD_TIMER = null;

function loadNote() {
  const env = $("libEnv").value || "mock";
  const shown = libFiltered().length, all = libAll().length;
  $("loadNote").textContent = `Repeats ${shown === all ? "the tests" : `the ${shown} tests shown`} that only read, on `
    + `${env}. Tests that create or change things are left out.`
    + (env === "mock" ? "" : ` This puts real load on ${env}.`);
  $("loadStart").textContent = "Start"; delete $("loadStart").dataset.sure;
}

$("loadOpen").addEventListener("click", () => {
  $("loadPanel").hidden = !$("loadPanel").hidden;
  if (!$("loadPanel").hidden) { loadNote(); loadPoll(); }
});
$("loadClose").addEventListener("click", () => { $("loadPanel").hidden = true; });
$("libEnv").addEventListener("change", () => { if (!$("loadPanel").hidden) loadNote(); });

$("loadStart").addEventListener("click", async () => {
  const env = $("libEnv").value || "mock";
  if ($("loadStart").dataset.running) { await api("/api/perf/stop", { method: "POST" }); return; }
  if (env !== "mock" && !$("loadStart").dataset.sure) {       // one stray click must not load a real server
    $("loadStart").dataset.sure = "1";
    $("loadStart").textContent = `Yes, put load on ${env}`;
    return;
  }
  const shown = libFiltered(), all = libAll();
  const { data } = await api("/api/perf/start", { method: "POST", body: JSON.stringify({
    env, users: $("loadUsers").value, seconds: $("loadSeconds").value,
    only: shown.length === all.length ? null : shown.map((t) => `${t.suite}|${t.stage}|${t.id}`) }) });
  if (!data.ok) { banner("err", data.error || "could not start"); return; }
  loadPoll();
});

async function loadPoll() {
  clearTimeout(LOAD_TIMER);
  let d;
  try { ({ data: d } = await api("/api/perf")); } catch { return; }
  if (d.running) {
    const pr = d.progress || {};
    $("loadStart").dataset.running = "1"; $("loadStart").textContent = "Stop";
    $("loadOut").innerHTML = `<div class="loadbar"><i style="width:${Math.min(100, 100 * (pr.elapsed || 0) / (pr.seconds || 1))}%"></i></div>
      <div class="hint" style="margin-top:5px" id="loadProgress">${(pr.calls || 0).toLocaleString()} calls so far on ${esc(d.server)}…</div>`;
    if (!$("loadPanel").hidden) LOAD_TIMER = setTimeout(loadPoll, 700);
    return;
  }
  delete $("loadStart").dataset.running;
  if ($("loadStart").textContent === "Stop") $("loadStart").textContent = "Start";
  const r = d.result;
  if (!r) { $("loadOut").innerHTML = ""; return; }
  if (!r.ok) { $("loadOut").innerHTML = `<p class="hint" style="color:var(--err)">${esc(r.error)}</p>`; return; }
  $("loadOut").innerHTML = `<p style="margin:10px 0 0;font-size:13px" id="loadSentence"><b>${esc(r.server)}:</b> ${esc(r.sentence)}</p>
    <table><tr><th>Endpoint</th><th>Calls</th><th>Usual (ms)</th><th>95% within (ms)</th><th>Slowest (ms)</th><th>Failed</th></tr>
    ${(r.rows || []).slice(0, 40).map((x) => `<tr><td><code>${esc(x.operation)}</code></td><td>${x.calls.toLocaleString()}</td>
      <td>${x.median}</td><td class="${x.over_budget ? "over" : ""}">${x.p95}</td><td>${x.slowest}</td>
      <td class="${x.errors ? "over" : ""}">${x.errors}</td></tr>`).join("")}</table>
    ${(r.rows || []).length > 40 ? `<p class="hint">…and ${(r.rows || []).length - 40} more endpoints.</p>` : ""}`;
}

/* ------------------------------------------------------------- trackers
   A bug report that has to be copied into another window is sometimes not
   sent. Connecting the place it goes takes one thing: its address. What it is
   — Jira, GitHub, a Slack channel — is read from the address, and only what
   that kind of place needs is then asked for. */
let TRACKER = null;
async function trackerLoad() {
  try { TRACKER = ((await api("/api/tracker")).data || {}).connection || null; } catch { TRACKER = null; }
}

/* Where a row's extra content lives, looked up when it is needed: the list is
   redrawn whenever a run finishes, and an element held on to from before
   that is no longer on the page. */
const trackerHost = (test) => $("libList").querySelector(
  `[data-libhost="${CSS.escape(`${test.suite}|${test.stage}|${test.id}`)}"]`);

function trackerForm(test) {
  const host = trackerHost(test);
  if (!host) return;
  host.innerHTML = `<div class="czneed" data-trackerform data-keep>
    <b>Where should bug reports go?</b>
    <div class="hint" style="margin:3px 0 7px">Paste the address of your Jira project, GitHub or GitLab
      repository, or a Slack or Teams channel's webhook. This is asked once.</div>
    <input type="text" data-traddr placeholder="https://yourteam.atlassian.net/browse/ABC-1" autocomplete="off">
    <div data-trmore></div>
    <div class="btnrow" style="margin-top:9px">
      <button class="primary sm" data-trgo disabled>Connect and send</button>
      <button class="sm" data-trcancel>Cancel</button>
      <span class="hint">A token is kept on this computer only.</span>
    </div></div>`;
  const addr = host.querySelector("[data-traddr]"), more = host.querySelector("[data-trmore]");
  const go = host.querySelector("[data-trgo]");
  let chosen = "", timer = null;
  const look = async () => {
    const address = addr.value.trim();
    if (!address) { more.innerHTML = ""; go.disabled = true; return; }
    const { data } = await api("/api/tracker/detect", { method: "POST",
      body: JSON.stringify({ address, kind: chosen || undefined }) });
    if (addr.value.trim() !== address) return;                    // typed on since
    if (!data.ok) { more.innerHTML = `<div class="hint" style="margin-top:6px">${esc(data.error)}</div>`; go.disabled = true; return; }
    more.innerHTML = `
      <div style="margin-top:8px;font-size:13px">${data.sure ? "This is" : "This looks like"} <b>${esc(data.name)}</b>
        <span class="hint">— ${esc(data.what)}.</span>
        <span class="hint" style="margin-left:8px">Not right? It is
          <select data-trkind style="width:auto;display:inline-block;padding:2px 6px">${Object.entries(data.kinds).map(([k, v]) =>
            `<option value="${esc(k)}"${k === data.kind ? " selected" : ""}>${esc(v)}</option>`).join("")}</select></span></div>
      ${data.project_missing ? '<div class="hint" style="color:var(--err);margin-top:4px">That address does not say which project. Paste one like …/browse/ABC-1 or …/projects/ABC.</div>' : ""}
      ${(data.needs || []).map((n) => `<label style="margin-top:8px">${esc(n.label)}</label>
        <input type="${n.secret ? "password" : "text"}" data-trneed="${esc(n.field)}" autocomplete="off" style="max-width:460px">`).join("")}`;
    more.querySelector("[data-trkind]").onchange = (ev) => { chosen = ev.target.value; look(); };
    go.disabled = !!data.project_missing;
  };
  addr.addEventListener("input", () => { chosen = ""; clearTimeout(timer); timer = setTimeout(look, 350); });
  host.querySelector("[data-trcancel]").onclick = () => { const now = trackerHost(test); if (now) now.innerHTML = ""; };
  go.onclick = async () => {
    const secrets = {};
    more.querySelectorAll("[data-trneed]").forEach((el) => { secrets[el.dataset.trneed] = el.value.trim(); });
    go.disabled = true;
    const { data } = await api("/api/tracker/connect", { method: "POST",
      body: JSON.stringify({ address: addr.value.trim(), kind: chosen || undefined, secrets }) });
    if (!data.ok) { banner("err", data.error || "could not connect"); go.disabled = false; return; }
    TRACKER = data.connection;
    await trackerSend(test, false);
  };
  addr.focus();
}

async function trackerSend(test, again, button) {
  if (button) { button.disabled = true; button.textContent = "Sending…"; }
  const { data } = await api("/api/tests/bug/send", { method: "POST",
    body: JSON.stringify({ ...test, env: $("libEnv").value || "mock", again: !!again }) });
  const host = trackerHost(test);
  if (data.already && host) {
    host.innerHTML = `<div class="czneed" data-keep>This failure was already raised as <b>${esc(data.already)}</b>.
      <div class="btnrow" style="margin-top:7px"><button class="sm" data-tragain>Send it again anyway</button></div></div>`;
    host.querySelector("[data-tragain]").onclick = () => trackerSend(test, true);
    if (button) { button.disabled = false; button.textContent = `Send to ${TRACKER ? TRACKER.name : "…"}`; }
    return;
  }
  if (!data.ok) {
    banner("err", data.error || "could not send it");
    if (host) host.innerHTML = `<div class="czneed" data-keep>${esc(data.error || "It could not be sent.")}
      <div class="btnrow" style="margin-top:7px"><button class="sm" data-trchange>Connect somewhere else</button></div></div>`;
    const change = host && host.querySelector("[data-trchange]");
    if (change) change.onclick = () => trackerForm(test);
    if (button) { button.disabled = false; button.textContent = `Send to ${TRACKER ? TRACKER.name : "…"}`; }
    return;
  }
  banner("ok", data.message);
  await loadTests();
  const fresh = trackerHost(test);
  if (fresh) {
    fresh.innerHTML = `<div class="czneed" data-trsent>${esc(data.message)}
      ${data.url ? ` <a href="${esc(data.url)}" target="_blank" rel="noopener">Open it</a>` : ""}
      ${data.linked ? ' <span class="hint">· kept on this test under Linked</span>' : ""}
      <div class="btnrow" style="margin-top:7px"><button class="sm" data-trchange>Send somewhere else next time</button></div></div>`;
    fresh.querySelector("[data-trchange]").onclick = async () => {
      await api("/api/tracker/forget", { method: "POST" }); TRACKER = null;
      const now = trackerHost(test); if (now) now.innerHTML = "";
      libRender();
    };
  }
}

/* ------------------------------------------------------------- watching
   The document says what the API should do; only traffic says what it does.
   Point an app at the address given and use it as normal: everything is
   passed through to the real server, and where the server and the document
   disagree is worked out here, without anybody reading a log. */
let WATCH_TIMER = null;

async function watchLoad() {
  if (!$("watchCard")) return;
  let d;
  try { ({ data: d } = await api("/api/record")); } catch { return; }
  const servers = d.servers || [];
  $("watchIdle").innerHTML = d.watching ? `
    <div class="watchon">
      <span>Watching <b>${esc(d.server)}</b>. Point your app at <code>${esc(d.address)}</code>
        instead of the server and use it as normal.</span>
      <span class="grow"></span><b id="watchCount">${d.calls} call${d.calls === 1 ? "" : "s"} so far</b>
      <button class="sm" id="watchStop">Stop</button>
    </div>` : `
    <p class="hint" style="margin-top:0">Let mockd watch your app talk to the real API. It passes
      everything through, and tells you where the API and the document disagree, which endpoints
      the app really uses — and can turn what you clicked through into a test.</p>
    <div class="watchline">
      ${servers.length ? `<select id="watchServer">${servers.map((s) =>
          `<option value="${esc(s.name)}">${esc(s.name)}${s.read_only ? " — read only" : ""}</option>`).join("")}</select>
        <button class="primary sm" id="watchStart">Start watching</button>
        <span class="hint">or</span>` : '<span class="hint">Add a server above to watch it live, or</span>'}
      <button class="sm" id="watchLoadHar">Load a browser recording…</button>
    </div>`;
  if ($("watchStart")) $("watchStart").onclick = async () => {
    $("watchStart").disabled = true;
    const { data } = await api("/api/record/start", { method: "POST",
      body: JSON.stringify({ server: $("watchServer").value }) });
    if (!data.ok) { banner("err", data.error || "could not start"); $("watchStart").disabled = false; return; }
    banner("ok", `Watching ${data.server}. Point your app at ${data.address}.`);
    watchLoad();
  };
  if ($("watchStop")) $("watchStop").onclick = async () => {
    await api("/api/record/stop", { method: "POST" });
    banner("ok", "Stopped watching.");
    watchLoad();
  };
  if ($("watchLoadHar")) $("watchLoadHar").onclick = () => $("watchHar").click();

  const f = d.found;
  const KIND = { missing: "missing", type: "wrong type", differs: "differs", extra: "not documented" };
  const lines = !f ? [] : [
    ...(f.undocumented_endpoints || []).map((x) =>
      `<div class="line"><span class="k">not in the document</span><code>${esc(x.method)} ${esc(x.path)}</code>
         <span class="hint">· called ${x.calls} time${x.calls === 1 ? "" : "s"}</span></div>`),
    ...(f.undocumented_statuses || []).map((x) =>
      `<div class="line"><span class="k">status not mentioned</span><code>${esc(x.operation)}</code> answered <b>${esc(x.status)}</b></div>`),
    ...(f.fields || []).map((x) =>
      `<div class="line"><span class="k ${esc(x.kind)}">${esc(KIND[x.kind] || x.kind)}</span><code>${esc(x.operation)}</code>
         → <b>${esc(x.field)}</b> <span class="hint">· ${esc(x.detail)}</span></div>`)];
  $("watchFound").innerHTML = !f ? "" : `
    <div class="watchfound">
      <div><b>${esc((f.meta || {}).server || "What was recorded")}:</b> ${esc(f.sentence)}</div>
      ${lines.length ? `<details style="margin-top:6px"${lines.length <= 6 ? " open" : ""}>
          <summary style="cursor:pointer">Where they disagree</summary>${lines.slice(0, 30).join("")}
          ${lines.length > 30 ? `<div class="hint">…and ${lines.length - 30} more.</div>` : ""}</details>` : ""}
      ${(f.unused || []).length ? `<details style="margin-top:4px">
          <summary style="cursor:pointer">${f.unused.length} documented endpoint${f.unused.length === 1 ? " was" : "s were"} never called</summary>
          ${f.unused.slice(0, 40).map((o) => `<div class="line"><code>${esc(o)}</code></div>`).join("")}</details>` : ""}
      <div class="btnrow" style="margin-top:10px">
        <button class="sm primary" id="watchTest">Make a test from this</button>
        <button class="sm" id="watchClear">Clear</button>
      </div>
    </div>`;
  if ($("watchTest")) $("watchTest").onclick = async () => {
    $("watchTest").disabled = true;
    const { data } = await api("/api/record/test", { method: "POST" });
    if (!data.ok) { banner("err", data.error || "could not make a test"); $("watchTest").disabled = false; return; }
    banner("ok", `Saved a ${data.steps}-step test from what was recorded.`);
    await loadTests();
    LIB.only = { keys: new Set([data.key]), label: "made from the recording" };
    LIB.q = ""; LIB.module = ""; LIB.type = ""; LIB.priority = ""; LIB.result = ""; LIB.page = 0;
    $("libSearch").value = ""; LIB.open = data.key;
    showView("tests");
  };
  if ($("watchClear")) $("watchClear").onclick = async () => {
    await api("/api/record/clear", { method: "POST" });
    watchLoad();
  };

  clearTimeout(WATCH_TIMER);
  if (d.watching && document.querySelector('.view[data-view="environments"]').classList.contains("on")) {
    WATCH_TIMER = setTimeout(watchLoad, 3000);
  }
}

$("watchHar").addEventListener("change", async (ev) => {
  const file = ev.target.files[0];
  if (!file) return;
  banner("ok", `Reading ${file.name}…`);
  const { data } = await api("/api/record/har", { method: "POST",
    body: JSON.stringify({ content: await file.text(), name: file.name }) });
  ev.target.value = "";
  if (!data.ok) { banner("err", data.error || "could not read it"); return; }
  banner("ok", `${data.calls} call${data.calls === 1 ? "" : "s"} to ${data.host} read from that recording.`);
  watchLoad();
});

/* ------------------------------------------------------------------ map
   The whole API on one screen. Every endpoint is a dot, grouped by resource
   and coloured by what is known about it on one server: proven, failing, not
   run there yet, or not tested at all — with a ring where it is open to anyone
   or differs from the document, and a bolt where it is slow. When the tests
   run, each call goes out from the centre and its endpoint settles to the
   colour it earned. It is drawn to be watched, and every part of it is a
   fact: hover for what, click for the tests. */
const MAP = { env: "mock", data: null, dots: {}, busy: false, core: { x: 112, y: 150 } };
const MAP_CALM = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const SVGNS = "http://www.w3.org/2000/svg";
const svgEl = (tag, attrs, parent) => {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
  if (parent) parent.appendChild(el);
  return el;
};
const MAP_WORDS = (env) => {
  const where = env === "mock" ? "the mock" : env;
  return { pass: `proven on ${where}`, fail: `failing on ${where}`,
           idle: `has tests, but they have not run on ${where}`, none: "no test calls this yet" };
};

async function mapLoad(animate) {
  if (!$("mapCard") || MAP.busy) return;
  let d;
  try { ({ data: d } = await api("/api/map?env=" + encodeURIComponent(MAP.env))); } catch { return; }
  if (!d || !d.ok || !d.total) { $("mapCard").hidden = true; return; }
  MAP.env = d.env;
  mapRender(d, animate !== false);
}

function mapLayout(resources) {
  // clusters packed into rows to the right of the centre; each as wide as its ring
  // ...and never narrower than its label, so two names cannot run together
  const left = 250, right = 985, gapX = 14;
  const rows = [[]];
  let x = left;
  for (const r of resources) {
    const n = r.endpoints.length;
    const radius = Math.max(19, Math.min(64, n * 2.75));
    const wide = Math.max(radius * 2, 112);
    if (x + wide > right && rows[rows.length - 1].length) { rows.push([]); x = left; }
    rows[rows.length - 1].push({ resource: r, radius, wide, cx: x + wide / 2 });
    x += wide + gapX;
  }
  let y = 26;
  const placed = [];
  for (const row of rows) {
    const tall = Math.max(...row.map((c) => c.radius));
    const used = row[row.length - 1].cx + row[row.length - 1].wide / 2 - left;
    const shift = (right - left - used) / 2;                 // centre the row
    for (const c of row) placed.push({ ...c, cx: c.cx + shift, cy: y + tall });
    y += tall * 2 + 46;
  }
  return { placed, height: Math.max(300, y + 4) };
}

function mapRender(d, animate) {
  MAP.data = d;
  $("mapCard").hidden = false;
  $("mapName").textContent = d.title ? `· ${d.title}` : "";
  $("mapSentence").textContent = d.sentence;
  $("mapServers").innerHTML = (d.servers || []).length > 1 ? d.servers.map((n) =>
    `<button class="${n === d.env ? "on" : ""}" data-mapenv="${esc(n)}">${esc(n)}</button>`).join("") : "";
  $("mapServers").querySelectorAll("[data-mapenv]").forEach((b) =>
    b.addEventListener("click", () => { if (MAP.busy) return; MAP.env = b.dataset.mapenv; mapLoad(true); }));
  if (!MAP.busy) {
    $("mapRun").disabled = false;
    $("mapRun").textContent = `▶ Run every test on ${d.env}`;
  }

  const svg = $("mapSvg");
  const { placed, height } = mapLayout(d.resources || []);
  MAP.core = { x: 112, y: height / 2 };
  svg.setAttribute("viewBox", `0 0 1000 ${height}`);
  svg.innerHTML = `<defs>
      <linearGradient id="mapScan" x1="0" x2="1"><stop offset="0" stop-color="#60a5fa" stop-opacity="0"/>
        <stop offset="1" stop-color="#60a5fa" stop-opacity=".22"/></linearGradient>
      <pattern id="mapGrid" width="40" height="40" patternUnits="userSpaceOnUse">
        <path d="M40 0H0V40" fill="none" class="grid"/></pattern></defs>
    <rect width="1000" height="${height}" fill="url(#mapGrid)"/>
    <rect class="scan" x="0" y="0" width="140" height="${height}"/>`;
  const links = svgEl("g", {}, svg), clusters = svgEl("g", {}, svg), packets = svgEl("g", { id: "mapPackets" }, svg);
  void packets;

  // the centre: one number for the whole API on this server
  const core = MAP.core, R = 56, around = 2 * Math.PI * R;
  svgEl("circle", { class: "core", cx: core.x, cy: core.y, r: R + 18 }, svg);
  svgEl("circle", { class: "ringbg", cx: core.x, cy: core.y, r: R }, svg);
  const ring = svgEl("circle", { class: "ring", id: "mapRing", cx: core.x, cy: core.y, r: R,
    "stroke-dasharray": around, "stroke-dashoffset": around,
    transform: `rotate(-90 ${core.x} ${core.y})` }, svg);
  const pct = svgEl("text", { class: "pct", id: "mapPct", x: core.x, y: core.y + 8 }, svg);
  svgEl("text", { class: "pctlabel", x: core.x, y: core.y + 26 }, svg).textContent = "PROVEN";
  const settle = () => {
    ring.setAttribute("stroke-dashoffset", around * (1 - d.health / 100));
    ring.style.stroke = d.counts.fail ? (d.health >= 60 ? "#fbbf24" : "#f87171") : "#34d399";
  };
  if (animate && !MAP_CALM) {
    const from = Number(pct.dataset.at || 0), start = performance.now();
    const tick = (now) => {
      const t = Math.min(1, (now - start) / 900);
      pct.textContent = Math.round(from + (d.health - from) * t) + "%";
      if (t < 1) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick); requestAnimationFrame(() => requestAnimationFrame(settle));
  } else { pct.textContent = d.health + "%"; settle(); }

  MAP.dots = {};
  const words = MAP_WORDS(d.env);
  placed.forEach((c, ci) => {
    const midX = (core.x + R + 18 + c.cx - c.radius) / 2;
    svgEl("path", { class: "link", d: `M${core.x + R + 18} ${core.y} C${midX} ${core.y} ${midX} ${c.cy} ${c.cx - c.radius} ${c.cy}` }, links);
    const g = svgEl("g", { class: "cluster", "data-mapres": c.resource.name }, clusters);
    svgEl("circle", { class: "orbit", cx: c.cx, cy: c.cy, r: c.radius }, g);
    const n = c.resource.endpoints.length, size = n > 12 ? 5 : 6.5;
    c.resource.endpoints.forEach((e, i) => {
      const angle = -Math.PI / 2 + (2 * Math.PI * i) / n;
      const x = c.cx + (n === 1 ? 0 : c.radius * Math.cos(angle)), y = c.cy + (n === 1 ? 0 : c.radius * Math.sin(angle));
      if (e.differs) svgEl("circle", { class: "halo differs", cx: x, cy: y, r: size + 3.5 }, g);
      if (e.open) svgEl("circle", { class: "halo open", cx: x, cy: y, r: size + 2 }, g);
      const dot = svgEl("circle", { class: `dot ${e.state}`, cx: x, cy: y, r: size, "data-mapkey": e.key, tabindex: "0" }, g);
      if (e.slow) svgEl("text", { class: "bolt", x: x + size - 1, y: y - size + 2 }, g).textContent = "⚡";
      if (animate && !MAP_CALM) { dot.style.opacity = "0"; setTimeout(() => { dot.style.opacity = ""; dot.classList.add("hit"); }, 120 + ci * 60 + i * 14); }
      MAP.dots[e.key] = { el: dot, x, y, e };
      const tip = () => {
        const flags = (e.open ? '<span class="flag bad">answered a caller who had not signed in</span>' : "")
          + (e.differs ? '<span class="flag">real traffic differed from the document here</span>' : "")
          + (e.slow ? '<span class="flag">slow, or slower than it used to be</span>' : "");
        $("mapTip").innerHTML = `<b>${esc(e.key)}</b>${e.summary ? esc(e.summary) + " · " : ""}<span class="st">${esc(words[e.state])}`
          + (e.tests.length ? ` · ${e.tests.length} test${e.tests.length === 1 ? "" : "s"}` : "") + `</span>${flags}`;
        const box = $("mapWrap").getBoundingClientRect(), at = dot.getBoundingClientRect();
        $("mapTip").hidden = false;
        const w = $("mapTip").offsetWidth;
        $("mapTip").style.left = Math.max(8, Math.min(box.width - w - 8, at.left - box.left + at.width / 2 - w / 2)) + "px";
        $("mapTip").style.top = (at.bottom - box.top + 9) + "px";
      };
      dot.addEventListener("mouseenter", tip); dot.addEventListener("focus", tip);
      dot.addEventListener("mouseleave", () => { $("mapTip").hidden = true; });
      dot.addEventListener("blur", () => { $("mapTip").hidden = true; });
      dot.addEventListener("click", (ev) => { ev.stopPropagation(); mapOpen([e], e.key); });
    });
    svgEl("text", { class: "label", x: c.cx, y: c.cy + c.radius + 19 }, g).textContent =
      c.resource.name.length > 18 ? c.resource.name.slice(0, 17) + "…" : c.resource.name;
    const bad = c.resource.endpoints.filter((e) => e.state === "fail").length;
    svgEl("text", { class: "sub", x: c.cx, y: c.cy + c.radius + 32 }, g).textContent =
      `${n} endpoint${n === 1 ? "" : "s"}${bad ? ` · ${bad} failing` : ""}`;
    g.addEventListener("click", () => mapOpen(c.resource.endpoints, c.resource.name));
  });
}

/* From the map to the tests behind it — or, where there are none, to making one. */
function mapOpen(endpoints, what) {
  if (MAP.busy) return;
  const keys = new Set(endpoints.flatMap((e) => e.tests));
  if (!keys.size) {
    const e = endpoints[0];
    $("czStory").value = `Check ${e.summary ? e.summary.toLowerCase() : e.key} (${e.key}).`;
    showView("create");
    banner("ok", `No test calls ${endpoints.length === 1 ? e.key : what} yet — describe what it should do.`);
    return;
  }
  LIB.only = { keys, label: endpoints.length === 1 ? `that call ${what}` : `for ${what}` };
  LIB.q = ""; LIB.module = ""; LIB.type = ""; LIB.priority = ""; LIB.result = ""; LIB.page = 0;
  $("libSearch").value = ""; LIB.wantServer = MAP.env;
  showView("tests");
}

function mapPacket(to, done, ms) {
  const layer = document.getElementById("mapPackets");
  if (!layer || MAP_CALM) { if (done) done(); return; }
  const from = MAP.core, dot = svgEl("circle", { class: "packet", r: 2.6, cx: from.x, cy: from.y }, layer);
  const start = performance.now(), took = ms || 420, bend = (to.y - from.y) * 0.35;
  const step = (now) => {
    const t = Math.min(1, (now - start) / took), k = 1 - t;
    dot.setAttribute("cx", k * k * from.x + 2 * k * t * ((from.x + to.x) / 2) + t * t * to.x);
    dot.setAttribute("cy", k * k * from.y + 2 * k * t * (from.y + bend) + t * t * to.y);
    if (t < 1) requestAnimationFrame(step); else { dot.remove(); if (done) done(); }
  };
  requestAnimationFrame(step);
}

/* Run everything on the server shown, and watch it happen. While the run is
   going the calls are shown going out; when it ends each endpoint settles to
   what the run actually found — nothing on screen is decided by the animation. */
async function mapRun() {
  if (MAP.busy || !MAP.data) return;
  const env = MAP.env;
  const { data } = await api("/api/tests/run", { method: "POST",
    body: JSON.stringify({ env, drafts: true, kinds: [], verbose: false }) });
  if (data.error) { banner("err", data.error); return; }
  MAP.busy = true;
  LAST_RUN = data.job;
  $("mapRun").disabled = true;
  $("mapSvg").classList.add("running");
  const all = Object.values(MAP.dots);
  const probing = setInterval(() => {
    const target = all[Math.floor(Math.random() * all.length)];
    if (target) mapPacket(target, null, 380);
  }, 110);
  const began = Date.now();
  let job = {};
  for (;;) {
    await new Promise((r) => setTimeout(r, 700));
    try { job = (await api("/api/job/" + data.job)).data || {}; } catch { job = { done: true }; }
    $("mapRun").textContent = `Running on ${env}… ${Math.round((Date.now() - began) / 1000)}s`;
    if (job.done || job.error) break;
  }
  clearInterval(probing);
  $("mapSvg").classList.remove("running");
  let fresh = null;
  try { fresh = (await api("/api/map?env=" + encodeURIComponent(env))).data; } catch { /* keep what is shown */ }
  if (fresh && fresh.ok) {
    const order = fresh.resources.flatMap((r) => r.endpoints).filter((e) => MAP.dots[e.key]);
    const gap = Math.min(45, 3200 / Math.max(order.length, 1));
    await new Promise((finish) => {
      if (!order.length || MAP_CALM) { finish(); return; }
      let left = order.length;
      order.forEach((e, i) => setTimeout(() => mapPacket(MAP.dots[e.key], () => {
        const el = MAP.dots[e.key].el;
        el.setAttribute("class", `dot ${e.state} hit`);
        if (--left === 0) finish();
      }, 360), i * gap));
    });
    MAP.busy = false;
    mapRender(fresh, true);
    const c = fresh.counts;
    banner(c.fail ? "err" : "ok", `${env}: ${fresh.sentence}`);
  } else MAP.busy = false;
  $("mapRun").disabled = false;
  $("mapRun").textContent = `▶ Run every test on ${env}`;
  loadTests(); homeNoticed(); loadBaseline && loadBaseline();
}
$("mapRun").addEventListener("click", mapRun);

/* ------------------------------------------------------------- noticed
   Things mockd found without being asked: a contract that moved under the
   tests, a test that cannot pass, a mock that no longer matches. Shown only
   when there is something to say, each with the one thing to do about it. */
async function homeNoticed() {
  let d;
  try { ({ data: d } = await api("/api/noticed")); } catch { return; }
  const list = (d && d.noticed) || [];
  $("homeNoticedCard").hidden = !list.length;
  $("homeNoticed").innerHTML = list.map((f, i) => `
    <div class="noticed ${esc(f.level)}" data-noticed="${esc(f.key)}">
      <span class="mark"></span>
      <span class="what"><b>${esc(f.title)}</b><div>${esc(f.detail)}</div>
        ${(f.more || []).length ? `<ul>${f.more.map((m) => `<li>${esc(m)}</li>`).join("")}</ul>` : ""}</span>
      <span class="btns">
        ${f.action ? `<button class="sm" data-noticedgo="${i}">${esc(f.action.label)}</button>` : ""}
        ${f.dismiss ? `<button class="sm" data-noticedoff="${esc(f.key)}" title="stop showing this">Dismiss</button>` : ""}
      </span>
    </div>`).join("");
  $("homeNoticed").querySelectorAll("[data-noticedgo]").forEach((b) =>
    b.addEventListener("click", () => {
      const action = list[Number(b.dataset.noticedgo)].action;
      if (action.go === "tests") {
        LIB.only = { keys: new Set(action.only || []), label: action.label_for_filter || "picked out for you" };
        LIB.q = ""; LIB.module = ""; LIB.type = ""; LIB.priority = ""; LIB.result = ""; LIB.page = 0;
        $("libSearch").value = "";
        LIB.wantServer = action.server || "";
        showView("tests");
      } else showView(action.go);
    }));
  $("homeNoticed").querySelectorAll("[data-noticedoff]").forEach((b) =>
    b.addEventListener("click", async () => {
      await api("/api/noticed/dismiss", { method: "POST", body: JSON.stringify({ key: b.dataset.noticedoff }) });
      homeNoticed();
    }));
}

/* ----------------------------------------------------------------- home
   The first screen answers two questions and nothing else: is everything in
   place, and what should I do now. Each line is a fact with one action beside
   it; the first thing that is not done yet is lifted to the top as the next
   step, so nobody has to work out an order. */
function showMore(on) {
  const open = on === undefined ? $("navMoreItems").hidden : !!on;
  $("navMoreItems").hidden = !open;
  $("navMore").textContent = open ? "More ▾" : "More ▸";
  $("navMore").setAttribute("aria-expanded", String(open));
  try { localStorage.setItem("mockd.more", open ? "1" : ""); } catch { /* private mode */ }
}
window.showMore = showMore;
$("navMore").addEventListener("click", () => showMore());
try { if (localStorage.getItem("mockd.more") === "1") showMore(true); } catch { /* fine */ }

async function homeLoad() {
  const get = async (url) => { try { return (await api(url)).data || {}; } catch { return {}; } };
  const [state, proj, check, base, tax, envs] = await Promise.all([
    get("/api/state"), get("/api/project"), get("/api/mock/selfcheck"),
    get("/api/tests/baseline"), get("/api/tests/taxonomy"), get("/api/environments")]);

  const spec = proj.spec || "";
  const specThere = !!spec && (proj.specs || []).includes(spec)
    || ((proj.modules || []).find((m) => m.module === "mock") || {}).exists;
  const running = !!state.running;
  // the document the mock should be on, and the one it is actually on
  const wanted = ((proj.modules || []).find((m) => m.module === "mock") || {}).spec || spec;
  const serving = (state.options || {}).spec || "";
  const stale = running && !!serving && !!wanted && serving !== wanted;
  const sample = proj.from === "the built-in default";
  const ops = stale ? null : (state.drift || {}).spec_operations;
  const verified = ((check.summary || {}).ok) || 0;
  const broken = check.failed_count || 0;
  const mine = (tax.modules || []).filter((m) => m.name !== "baseline")
    .reduce((n, m) => n + m.tests, 0);
  const mockRun = (base.by_env || {}).mock;
  const others = (envs.environments || []).filter((e) => !/^mock/.test(e.name));
  const ready = others.filter((e) => e.ready);

  const rows = [
    { key: "spec", ok: specThere && !sample,
      title: sample ? "You are on the built-in sample"
        : specThere ? "API document loaded" : "No API document yet",
      note: sample ? "Add your own API document — a file or a link — to work on your API. "
                     + "Or start the mock below to look around with the sample."
        : specThere ? `${esc(spec)}${ops ? ` — ${ops} endpoints` : ""}`
                      : "Everything starts from your API document (OpenAPI / Swagger). "
                        + "Give a file or a link.",
      action: ["Change", "source"], todo: ["Add your API document", "source"] },
    { key: "mock", ok: running && !stale,
      title: stale ? "The mock is running a different document"
        : running ? "Mock server is running" : "Mock server is stopped",
      todo: stale ? ["Restart the mock on it", "restart"] : ["Start the mock", "start"],
      note: stale ? `It is answering from ${esc(serving)}; your API document is ${esc(wanted)}.`
        : running
        ? `${esc(state.base_url || "")}`
          + (check.state === "done"
              ? (broken ? ` — ${broken} operation(s) do not match the document`
                        : ` — ${verified} operations checked against the document`)
              : check.state === "running" ? " — checking itself against the document…" : "")
        : "The mock answers like your API would, so the app and the tests have "
          + "something to talk to today.",
      action: ["Settings", "server"] },
    { key: "baseline", ok: !!base.exists && !!mockRun && mockRun.other === 0,
      idle: !!base.exists && !mockRun,
      title: base.exists ? `${base.tests} baseline tests ready` : "Baseline tests not made yet",
      note: base.exists
        ? (mockRun ? `${mockRun.pass} of ${mockRun.pass + mockRun.other} passing on the mock.`
                   : "Made for you from the document. They have not been run yet.")
        : stale ? "They are made once the mock is on your document."
        : running ? "Being made now — this takes a few seconds."
        : "They are made automatically once the mock is running.",
      action: ["Run them", "baseline"],
      todo: base.exists ? ["Run the baseline", "baseline"]
        : stale ? ["Restart the mock on it", "restart"]
        : running ? ["Check again", "refresh"] : ["Start the mock", "start"] },
    { key: "tests", ok: mine > 0, idle: mine === 0,
      title: mine ? `${mine} test${mine === 1 ? "" : "s"} of your own` : "No tests of your own yet",
      note: mine ? "Find, run and manage them under Tests."
                 : "Describe what you want proven and they are written and tried for you.",
      action: ["Create more", "create"], todo: ["Create tests", "create"] },
    { key: "servers", ok: ready.length > 0, idle: ready.length === 0,
      title: ready.length ? `${ready.length} other server${ready.length === 1 ? "" : "s"} ready: `
                            + ready.map((e) => esc(e.name)).join(", ")
                          : "No other server set up yet",
      note: ready.length ? "The same tests run there — pick the server on the Tests screen."
                         : "Add your dev or staging address to run the same tests against it.",
      action: ["Manage", "environments"], todo: ["Set up a server", "environments"] },
  ];

  const next = rows.find((r) => !r.ok && !r.idle) || rows.find((r) => !r.ok) || null;
  $("homeNext").innerHTML = next
    ? `<div class="say"><b>Next: ${esc(next.todo[0])}</b><span>${next.note}</span></div>
       <button class="primary" data-homego="${esc(next.todo[1])}">${esc(next.todo[0])}</button>`
    : `<div class="say"><b>Everything is in place.</b>
         <span>Create tests for what you want proven, or run what you have.</span></div>
       <button class="primary" data-homego="create">Create tests</button>
       <button data-homego="tests">Open Tests</button>`;

  homeNoticed();
  mapLoad(!MAP.data);                 // animate the first time it appears, not on every refresh

  $("homeList").innerHTML = rows.map((r) => `
    <div class="homerow" data-home="${r.key}">
      <span class="dot ${r.ok ? "ok" : r.idle ? "idle" : "todo"}">${r.ok ? "✓" : r.idle ? "·" : "!"}</span>
      <span class="what"><b>${r.title}</b><div>${r.note}</div></span>
      <button class="sm" data-homego="${esc((r.ok ? r.action : r.todo)[1])}">${
        esc((r.ok ? r.action : r.todo)[0])}</button>
    </div>`).join("");

  document.querySelectorAll('.view[data-view="home"] [data-homego]').forEach((b) =>
    b.addEventListener("click", async () => {
      const go = b.dataset.homego;
      if (go === "refresh") { await homeLoad(); return; }
      if (go === "start" || go === "restart") {
        b.disabled = true; b.textContent = go === "restart" ? "Restarting…" : "Starting…";
        if (go === "restart") { await stop(); }
        if (wanted) $("spec").value = wanted;   // the project's document, whatever was typed before
        // Start it directly. Pressing the top-bar button from here did nothing
        // whenever the page still believed the mock was running — it had been
        // stopped somewhere else — because that button was disabled.
        await refreshState();
        $("stateful").checked = true;     // tests read back what they create
        await start();
        for (let i = 0; i < 40; i++) {            // then say what happened, here
          const now = await get("/api/state");
          if (now.running) break;
          await new Promise((r) => setTimeout(r, 1000));
        }
        await homeLoad();
        // The self-check and the baseline follow the start; how long depends on
        // the size of the document, so watch for them instead of guessing.
        (async () => {
          for (let i = 0; i < 60; i++) {
            await new Promise((r) => setTimeout(r, 2000));
            const made = await get("/api/tests/baseline");
            if (made.exists || (made.last && made.last.ok === false)) break;
          }
          if (document.querySelector('.view[data-view="home"]').classList.contains("on")) homeLoad();
        })();
        return;
      }
      if (go === "baseline") { showView("tests"); setTimeout(() => $("baseRun").click(), 600); return; }
      showView(go);
    }));
}

/* ------------------------------------------------------------- the list
   One list of every test, with a search box and four filters. This is the whole
   Tests screen for most people: find what you care about, pick a server, press
   Run. Building flows by hand, id bindings, pipelines and import/export are
   still here, under Advanced tools, which stays shut unless asked for. */
const LIB = { q: "", module: "", type: "", priority: "", result: "", page: 0, open: null };
const LIB_PAGE = 25;

function showAdvanced(on) {
  const open = on === undefined ? $("advWrap").hidden : !!on;
  $("advWrap").hidden = !open;
  $("advToggle").textContent = open ? "Advanced tools ▾" : "Advanced tools ▸";
  $("advToggle").setAttribute("aria-expanded", String(open));
  try { localStorage.setItem("mockd.advanced", open ? "1" : ""); } catch { /* private mode */ }
}
window.showAdvanced = showAdvanced;
$("advToggle").addEventListener("click", () => showAdvanced());
try { if (localStorage.getItem("mockd.advanced") === "1") showAdvanced(true); } catch { /* fine */ }

function libAll() {
  return (TESTS_CACHE || []).flatMap((s) =>
    [...(s.cases || []).map((t) => ({ ...t, flow: false })),
     ...(s.scenarios || []).map((t) => ({ ...t, flow: true }))]
      .map((t) => ({ ...t, suite: s.name, stage: s.stage })));
}

function libOutcome(t, env) {
  const seen = ((t.history || {}).by_env || {})[env];
  if (!seen || !seen.last_outcome) return "never";
  return seen.last_outcome === "pass" ? "pass" : "fail";
}

function libFiltered() {
  const env = $("libEnv").value || "mock";
  const q = LIB.q.toLowerCase();
  return libAll().filter((t) =>
    (!LIB.only || LIB.only.keys.has(`${t.suite}|${t.stage}|${t.id}`))
    && (!q || `${t.name || ""} ${t.id} ${t.description || ""}`.toLowerCase().includes(q))
    && (!LIB.module || t.suite === LIB.module)
    && (!LIB.type || (t.levels || []).includes(LIB.type))
    && (!LIB.priority || t.priority === LIB.priority)
    && (!LIB.result || libOutcome(t, env) === LIB.result));
}

function libFillPickers() {
  const all = libAll();
  const keep = (id, options, current) => {
    const html = options.map(([v, label]) =>
      `<option value="${esc(v)}"${v === current ? " selected" : ""}>${esc(label)}</option>`).join("");
    if ($(id).innerHTML !== html) $(id).innerHTML = html;
  };
  const modules = [...new Set(all.map((t) => t.suite))].sort();
  keep("libModule", [["", "All modules"], ...modules.map((m) => [m, m])], LIB.module);
  keep("libType", [["", "All types"], ...["smoke", "sanity", "regression", "negative",
       "performance"].map((l) => [l, l])], LIB.type);
  keep("libPriority", [["", "Any priority"], ["P0", "P0 — must never break"],
       ["P1", "P1 — main paths"], ["P2", "P2 — ordinary"], ["P3", "P3 — edges"]], LIB.priority);
  keep("libResult", [["", "Any result"], ["pass", "Passing"], ["fail", "Not passing"],
       ["never", "Never run"]], LIB.result);
  // The servers a person set up, the mock first; the variants that exist to
  // exercise sign-in handling stay in the advanced run panel.
  const servers = (ENVS || []).filter((e) => !e.technical)
    .sort((a, b) => (b.builtin - a.builtin) || (b.ready - a.ready) || a.name.localeCompare(b.name));
  const html = servers.map((e) => `<option value="${esc(e.name)}"${e.ready ? "" : " disabled"}>`
    + `${esc(e.name)} — ${esc(e.ready ? (e.base_url || "") : "not set up yet")}</option>`).join("");
  if (servers.length && $("libEnv").dataset.html !== html) {
    const was = $("libEnv").value;
    $("libEnv").innerHTML = html;
    $("libEnv").dataset.html = html;
    $("libEnv").value = servers.some((e) => e.name === was && e.ready) ? was
      : servers.some((e) => e.name === $("testEnv").value && e.ready) ? $("testEnv").value : "mock";
  }
}

function libRender() {
  if (!$("libList")) return;
  libFillPickers();
  if (LIB.wantServer && [...$("libEnv").options].some((o) => o.value === LIB.wantServer && !o.disabled)) {
    $("libEnv").value = LIB.wantServer; $("testEnv").value = LIB.wantServer;
  }
  LIB.wantServer = "";
  const env = $("libEnv").value || "mock";
  const all = libAll(), shown = libFiltered();
  $("libOnly").hidden = !LIB.only;
  if (LIB.only) {
    $("libOnly").innerHTML = `<span>Showing only the <b>${shown.length}</b> test${shown.length === 1 ? "" : "s"} ${
      esc(LIB.only.label)}.</span><span class="grow"></span><button class="sm" id="libOnlyOff">Show all tests</button>`;
    $("libOnlyOff").onclick = () => { LIB.only = null; libRender(); };
  }
  const pages = Math.max(1, Math.ceil(shown.length / LIB_PAGE));
  LIB.page = Math.min(LIB.page, pages - 1);
  const slice = shown.slice(LIB.page * LIB_PAGE, (LIB.page + 1) * LIB_PAGE);
  $("libCount").textContent = shown.length === all.length
    ? `${all.length} test${all.length === 1 ? "" : "s"}`
    : `${shown.length} of ${all.length}`;
  $("libRun").textContent = shown.length === all.length ? "Run all"
    : `Run these ${shown.length}`;
  $("libRun").disabled = !shown.length;

  // The list is redrawn whenever a run finishes or a test is saved, which can
  // be a second after somebody opened a form in a row. A form that vanishes
  // while it is being typed into is worse than a stale row, so anything marked
  // as in progress is lifted out and put back.
  const inProgress = {};
  $("libList").querySelectorAll("[data-libhost]").forEach((h) => {
    if (!h.querySelector(":scope > [data-keep]")) return;
    const held = document.createDocumentFragment();
    while (h.firstChild) held.appendChild(h.firstChild);
    inProgress[h.dataset.libhost] = held;
  });
  const WORD = { pass: "passes", fail: "does not pass", never: "not run yet" };
  $("libList").innerHTML = slice.length ? slice.map((t) => {
    const key = `${t.suite}|${t.stage}|${t.id}`;
    const out = libOutcome(t, env);
    const open = LIB.open === key;
    const servers = Object.entries(((t.history || {}).by_env) || {})
      .filter(([name]) => !/^https?:/i.test(name)).map(([name, r]) =>
      `<span class="libres ${r.last_outcome === "pass" ? "pass" : "fail"}"
             title="${esc(r.last_run || "")}">${esc(name)}: ${
        r.last_outcome === "pass" ? "passes" : esc(r.last_outcome || "?")}</span>`).join(" ");
    return `<div class="librow" data-lib="${esc(key)}">
      <div class="top" data-libopen="${esc(key)}">
        <span class="prio ${esc(t.priority)}">${esc(t.priority)}</span>
        <span style="flex:1;min-width:0">
          <div class="nm">${esc(t.name || t.id)}</div>
          <div class="sub">${esc(t.suite)} · ${(t.levels || []).map(esc).join(", ")}
            ${t.flow ? ` · ${(t.steps || []).length} steps` : ""}
            ${t.stage === "shared" ? " · shared with the team" : ""}
            ${t.status && t.status !== "ready" ? ` · <b>${esc(t.status)}</b>` : ""}
            ${t.owner ? ` · ${esc(t.owner)}` : ""}</div>
        </span>
        ${(t.needs || []).length
          ? '<span class="libres wait" title="it holds a placeholder — a pass with one in place proves nothing">needs a value</span>'
          : `<span class="libres ${out}">${WORD[out]}</span>`}
        <button class="sm" data-librun="${esc(key)}">Run</button>
      </div>
      ${open ? `<div class="more">
        ${(t.needs || []).map((name) => `
          <div class="czneed" data-libneed="${esc(name)}">
            <b>One thing only you know:</b> a real value for <code>${esc(name)}</code>.
            Nothing in the API document says where it comes from, so this test cannot pass
            until it has one.
            <div class="row" style="margin-top:7px;align-items:end">
              <div style="flex:1"><input type="text" data-needvalue="${esc(name)}"
                   placeholder="paste the value" autocomplete="off"></div>
              <div><button class="sm primary" data-needset="${esc(name)}"
                   data-key="${esc(key)}">Save</button></div>
            </div>
          </div>`).join("")}
        ${t.description ? `<div style="font-size:13px;margin-bottom:8px">${esc(t.description)}</div>` : ""}
        ${t.flow ? `<div class="sub" style="margin-bottom:8px">${(t.steps || []).map((st, i) =>
            `${i + 1}. ${esc(st.name || `${st.method} ${st.path}`)}`).join("<br>")}</div>`
          : `<div class="sub" style="margin-bottom:8px">${esc(t.method || "")} ${esc(t.path || "")}</div>`}
        ${servers ? `<div style="margin-bottom:8px">${servers}</div>` : ""}
        ${(t.links || []).length ? `<div class="sub" style="margin-bottom:8px">Linked: ${t.links.map(esc).join(", ")}</div>` : ""}
        <div class="btnrow">
          <button class="sm" data-libact="record" data-key="${esc(key)}">Edit details</button>
          <button class="sm" data-libact="open" data-key="${esc(key)}">Edit steps</button>
          ${t.stage === "draft" ? `<button class="sm" data-libact="promote" data-key="${esc(key)}"
              ${(t.history || {}).last_pass ? "" : "disabled"}
              title="${(t.history || {}).last_pass ? "put it in the shared suite the whole team runs"
                       : "it has to pass once before it can be shared"}">Share with team</button>` : ""}
          ${out === "fail" ? `<button class="sm" data-libact="bug" data-key="${esc(key)}"
              title="everything a developer needs to reproduce it, ready to paste into a ticket"
            >Copy bug report</button>
            <button class="sm" data-libact="send" data-key="${esc(key)}"
              title="post the same report to where your team tracks work"
            >${TRACKER ? `Send to ${esc(TRACKER.name)}` : "Send it somewhere…"}</button>` : ""}
          <button class="sm danger" data-libact="delete" data-key="${esc(key)}">Remove</button>
        </div>
        <div data-libhost="${esc(key)}"></div>
      </div>` : ""}
    </div>`;
  }).join("") : `<div class="empty">${all.length
      ? "Nothing matches those filters."
      : "No tests yet. Start the mock for a baseline, or use Create tests."}</div>`;

  Object.entries(inProgress).forEach(([key, held]) => {
    const h = $("libList").querySelector(`[data-libhost="${CSS.escape(key)}"]`);
    if (h) h.appendChild(held);
  });

  $("libPager").innerHTML = pages > 1 ? `
    <button class="sm" data-libpage="${LIB.page - 1}" ${LIB.page ? "" : "disabled"}>‹ Previous</button>
    <span class="hint">${LIB.page * LIB_PAGE + 1}–${Math.min((LIB.page + 1) * LIB_PAGE, shown.length)}
      of ${shown.length}</span>
    <button class="sm" data-libpage="${LIB.page + 1}" ${LIB.page < pages - 1 ? "" : "disabled"}>Next ›</button>` : "";

  const parts = (key) => { const [suite, stage, ...rest] = key.split("|");
                           return { suite, stage, id: rest.join("|") }; };
  $("libList").querySelectorAll("[data-needset]").forEach((b) =>
    b.addEventListener("click", async () => {
      const name = b.dataset.needset;
      const box = b.closest(".czneed").querySelector("[data-needvalue]");
      b.disabled = true;
      try {
        const { data } = await api("/api/tests/value", { method: "POST",
          body: JSON.stringify({ ...parts(b.dataset.key), name, value: box.value.trim() }) });
        if (!data.ok) { banner("err", data.error || "could not save it"); return; }
        banner("ok", `${name} saved. Run the test to see how it does.`);
        await loadTests();
      } finally { if (b.isConnected) b.disabled = false; }
    }));
  $("libList").querySelectorAll("[data-libopen]").forEach((el) =>
    el.addEventListener("click", (ev) => {
      if (ev.target.closest("button")) return;
      LIB.open = LIB.open === el.dataset.libopen ? null : el.dataset.libopen;
      libRender();
    }));
  $("libList").querySelectorAll("[data-librun]").forEach((b) =>
    b.addEventListener("click", async () => {
      const { suite, stage, id } = parts(b.dataset.librun);
      b.disabled = true; b.textContent = "running…";
      await runScoped({ suite, only: `^${escapeRegex(id)}( |$)`,
                        drafts: true, what: id });
      $("testOutCard").scrollIntoView({ behavior: "smooth", block: "nearest" });
    }));
  $("libList").querySelectorAll("[data-libact]").forEach((b) =>
    b.addEventListener("click", async () => {
      const { suite, stage, id } = parts(b.dataset.key);
      if (b.dataset.libact === "record") {
        openRecordEditor(suite, stage, id,
          $("libList").querySelector(`[data-libhost="${CSS.escape(b.dataset.key)}"]`));
        return;
      }
      if (b.dataset.libact === "open") { await testAction("open", suite, id, stage); return; }
      if (b.dataset.libact === "bug") {
        // A failure written up for somebody else: what was asked, what came
        // back, whose problem it looks like, and a curl to reproduce it.
        const q = new URLSearchParams({ suite, id, env: $("libEnv").value || "mock" });
        const { data } = await api("/api/tests/bug?" + q);
        if (!data.ok) { banner("err", data.error); return; }
        copyText(data.markdown, b);
        const host = $("libList").querySelector(`[data-libhost="${CSS.escape(b.dataset.key)}"]`);
        if (host) {
          host.innerHTML = `<p class="hint" style="margin:10px 0 4px 0">Copied — paste it into
            your tracker. This is what was copied:</p>
            <pre class="bugtext" style="max-height:260px;overflow:auto"></pre>`;
          host.querySelector(".bugtext").textContent = data.markdown;
        }
        banner("ok", "Bug report copied — paste it into your tracker.");
        return;
      }
      if (b.dataset.libact === "send") {
        if (!TRACKER) { trackerForm({ suite, stage, id }); return; }
        trackerSend({ suite, stage, id }, false, b);
        return;
      }
      if (b.dataset.libact === "delete" && !b.dataset.sure) {
        // one stray click should not cost somebody a test
        b.dataset.sure = "1"; b.textContent = "Really remove?";
        setTimeout(() => { if (b.isConnected) { delete b.dataset.sure; b.textContent = "Remove"; } }, 4000);
        return;
      }
      await testAction(b.dataset.libact, suite, id, stage);
    }));
  $("libPager").querySelectorAll("[data-libpage]").forEach((b) =>
    b.addEventListener("click", () => { LIB.page = Number(b.dataset.libpage); libRender(); }));
}

$("libSearch").addEventListener("input", () => { LIB.q = $("libSearch").value.trim(); LIB.page = 0; libRender(); });
for (const [id, field] of [["libModule", "module"], ["libType", "type"],
                           ["libPriority", "priority"], ["libResult", "result"]]) {
  $(id).addEventListener("change", () => { LIB[field] = $(id).value; LIB.page = 0; libRender(); });
}
$("libEnv").addEventListener("change", () => { $("testEnv").value = $("libEnv").value; libRender(); });
$("libRun").addEventListener("click", async () => {
  const shown = libFiltered(), all = libAll();
  if (!shown.length) return;
  const env = $("libEnv").value || "mock";
  $("testEnv").value = env;
  $("libRun").disabled = true;
  const label = $("libRun").textContent;
  $("libRun").textContent = "Running…";
  try {
    const everything = shown.length === all.length;
    await runScoped({
      only: everything ? undefined
        : "^(" + shown.map((t) => escapeRegex(t.id)).join("|") + ")( |$)",
      drafts: true,
      what: everything ? "all tests" : `${shown.length} test${shown.length === 1 ? "" : "s"}` });
    $("testOutCard").scrollIntoView({ behavior: "smooth", block: "nearest" });
  } finally { $("libRun").textContent = label; $("libRun").disabled = false; libRender(); }
});

/* ------------------------------------------------------------ create tests
   One path from a sentence to tests that have been tried. Everything a person
   used to do by hand afterwards — check the JSON, fix the ids, run it once to
   see — happens before they are shown anything, so what they read is a list of
   tests and how each one did, not machinery. */
let CZ = { suite: null, ids: [], job: null, brief: "", stop: false };

function czShow(stage) {
  for (const id of ["czAsk", "czWait", "czPaste", "czDone", "czMcp"]) {
    $(id).hidden = id !== ({ ask: "czAsk", wait: "czWait", paste: "czPaste",
                             done: "czDone", mcp: "czMcp" })[stage];
  }
  const step = { ask: "ask", wait: "write", paste: "write", done: "done", mcp: "ask" }[stage];
  $("czSteps").querySelectorAll("span").forEach((el) =>
    el.classList.toggle("on", el.dataset.s === step));
}

async function czLoad() {
  let d;
  try { ({ data: d } = await api("/api/create/context")); } catch { return; }
  $("czBaseline").innerHTML = d.baseline
    ? `<b>${d.baseline} baseline tests</b> were already made for you from the API `
      + `document — you do not need to write checks that each endpoint answers. `
      + `Use this for what <i>you</i> want proven.`
    : "Start the mock and a baseline of tests is made for you. Use this for what "
      + "<i>you</i> want proven.";
  $("czExamples").innerHTML = (d.examples || []).map((e) =>
    `<button type="button" class="chip" data-eg="${esc(e)}">${esc(e)}</button>`).join("");
  $("czExamples").querySelectorAll("[data-eg]").forEach((b) =>
    b.addEventListener("click", () => { $("czStory").value = b.dataset.eg; $("czStory").focus(); }));
  $("czModules").innerHTML = (d.modules || []).map((m) =>
    `<option value="${esc(m)}">`).join("");

  const gens = d.generators || {};
  const options = [];
  if (gens.claude) options.push(["claude", "Claude writes them for me",
    "Runs on this computer. Takes two to five minutes."]);
  if (gens.codex) options.push(["codex", "Codex writes them for me",
    "Runs on this computer. Usually faster, sometimes less thorough."]);
  options.push(["", "I will use my own AI tool",
    "You get a prompt to copy into ChatGPT or similar, then paste its answer back."]);
  const keep = (document.querySelector('input[name="czVia"]:checked') || {}).value;
  $("czWriters").innerHTML = options.map(([value, title, note], i) => `
    <label class="${(keep === undefined ? i === 0 : keep === value) ? "on" : ""}">
      <input type="radio" name="czVia" value="${esc(value)}"
             ${(keep === undefined ? i === 0 : keep === value) ? "checked" : ""}>
      <span>${esc(title)}<small>${esc(note)}</small></span></label>`).join("");
  $("czWriters").querySelectorAll("input").forEach((r) =>
    r.addEventListener("change", () => $("czWriters").querySelectorAll("label")
      .forEach((l) => l.classList.toggle("on", l.querySelector("input").checked))));
  $("czGoNote").textContent = d.mock_running ? ""
    : "The mock is not running, so new tests will be saved but not tried yet.";
}

function czRender(d) {
  CZ.suite = d.suite; CZ.ids = (d.tests || []).map((t) => t.id);
  const all = d.total && d.passed === d.total;
  const where = !d.env || d.env === "mock" ? "the mock" : d.env;
  CZ.env = d.env || "mock";
  $("czDoneHead").textContent = !d.total ? "No tests came back."
    : all ? `${d.total} test${d.total === 1 ? "" : "s"} created, and all pass on ${where}.`
    : d.tried ? `${d.total} test${d.total === 1 ? "" : "s"} created — ${d.passed} pass on ${where}, `
                + `${d.total - d.passed} need a look.`
    : `${d.total} test${d.total === 1 ? "" : "s"} created.`;
  $("czDoneNote").textContent = (d.tried ? "" : (d.untried_because || "") + " ")
    + `Saved under “${d.suite}”. They are yours to edit, run on any server, or remove.`;

  $("czNeeds").innerHTML = (d.needs || []).map((n) => `
    <div class="czneed">
      <b>One thing only you know:</b> a real value for <code>${esc(n.name)}</code>.
      Nothing in the API document says where it comes from.
      <div class="row" style="margin-top:7px;align-items:end">
        <div style="flex:1"><input type="text" data-need="${esc(n.name)}"
             placeholder="paste the value"></div>
        <div><button class="sm" data-needsave="${esc(n.name)}">Use it and try again</button></div>
      </div>
    </div>`).join("");
  $("czNeeds").querySelectorAll("[data-needsave]").forEach((b) =>
    b.addEventListener("click", async () => {
      const name = b.dataset.needsave;
      const value = $("czNeeds").querySelector(`[data-need="${CSS.escape(name)}"]`).value.trim();
      b.disabled = true;
      try {
        const { data } = await api("/api/create/value", { method: "POST",
          body: JSON.stringify({ suite: CZ.suite, ids: CZ.ids, name, value }) });
        if (!data.ok) { banner("err", data.error); return; }
        czRender(data);
      } finally { b.disabled = false; }
    }));

  // A test about the API's own rules cannot pass on the mock, and saying only
  // "does not pass" leaves somebody rewriting a test that was right.
  const others = (d.servers || []).filter((n) => n !== CZ.env);
  const stuckOnMock = d.tried && CZ.env === "mock" && !all && !(d.needs || []).length;
  $("czWhere").innerHTML = (stuckOnMock || CZ.env !== "mock") && d.total ? `
    <div class="czneed">
      ${stuckOnMock ? `<b>This may not be the tests' fault.</b> The mock answers in the right
        shape, but it does not do your API's own rules — what a total comes to, what stock is
        left, what is refused the second time. Tests about those are settled on a real server.`
        : `Tried on <b>${esc(CZ.env)}</b>.`}
      <div class="btnrow" style="margin-top:8px">
        ${others.map((n) => `<button class="sm${stuckOnMock ? " primary" : ""}" data-cztry="${esc(n)}">Try them on ${esc(n)}</button>`).join("")}
        ${CZ.env !== "mock" ? '<button class="sm" data-cztry="mock">Try them on the mock</button>' : ""}
        ${stuckOnMock && !others.length
          ? '<button class="sm" data-czservers>Set up a server</button>' : ""}
      </div>
    </div>` : "";
  $("czWhere").querySelectorAll("[data-cztry]").forEach((b) =>
    b.addEventListener("click", () => czTry(b.dataset.cztry, b)));
  const setup = $("czWhere").querySelector("[data-czservers]");
  if (setup) setup.addEventListener("click", () => showView("environments"));

  $("czList").innerHTML = (d.tests || []).map((t) => {
    const cls = t.outcome === "pass" ? "pass" : (t.needs || []).length ? "wait" : "bad";
    const mark = t.outcome === "pass" ? "passes" : (t.needs || []).length ? "waiting"
      : t.outcome === "untried" ? "not tried" : "does not pass";
    return `<div class="cztest ${cls}" data-cz="${esc(t.id)}">
      <div class="top"><span class="prio ${esc(t.priority)}">${esc(t.priority)}</span>
        <span class="nm">${esc(t.name)}</span><span class="grow"></span>
        <span class="tag">${esc(mark)}</span></div>
      ${t.description ? `<div class="recmeta">${esc(t.description)}</div>` : ""}
      <div class="recmeta">${t.steps} step${t.steps === 1 ? "" : "s"} · ${(t.levels || []).map(esc).join(", ")}</div>
      <div class="say">${esc(t.words)}</div></div>`;
  }).join("");
  czShow("done");
}

/* Connecting an AI tool directly is set up once, outside this page; all this
   screen can usefully do is hand over exactly what to paste, and say what the
   tool will and will not be given. */
$("czMcpOpen").addEventListener("click", async () => {
  const { data } = await api("/api/mcp/setup");
  $("czMcpWays").innerHTML = (data.ways || []).map((w, i) => `
    <div class="mcpway" data-mcp="${esc(w.tool)}">
      <div class="hd"><b>${esc(w.tool)}</b><span class="hint">${esc(w.how)}</span>
        <span class="grow"></span><button class="sm" data-mcpcopy="${i}">Copy</button></div>
      <pre>${esc(w.shown)}</pre>
    </div>`).join("");
  $("czMcpWays").querySelectorAll("[data-mcpcopy]").forEach((b) =>
    b.addEventListener("click", async () => {
      const way = data.ways[Number(b.dataset.mcpcopy)];
      try {
        await navigator.clipboard.writeText(way.copy);
        b.textContent = "Copied";
        banner("ok", `Copied, with the full path for this computer. ${way.how}`);
      } catch {
        banner("err", "Could not copy — select the text and copy it yourself.");
      }
      setTimeout(() => { b.textContent = "Copy"; }, 2500);
    }));
  czShow("mcp");
});
$("czMcpBack").addEventListener("click", () => czShow("ask"));

async function czTry(env, button) {
  if (button) { button.disabled = true; button.textContent = "Trying…"; }
  try {
    const { data } = await api("/api/create/try", { method: "POST",
      body: JSON.stringify({ suite: CZ.suite, ids: CZ.ids, env }) });
    if (!data.ok) { banner("err", data.error || "could not try them"); return; }
    czRender(data);
    const where = env === "mock" ? "the mock" : env;
    banner(data.passed === data.total ? "ok" : "err",
           `${data.passed} of ${data.total} pass on ${where}.`);
  } finally { if (button && button.isConnected) { button.disabled = false; } }
}

async function czFinish(payload, noteEl) {
  if (noteEl) noteEl.textContent = "checking them and trying each one on the mock…";
  const { data } = await api("/api/create/finish", { method: "POST",
    body: JSON.stringify({ ...payload, story: $("czStory").value.trim(),
                           module: $("czModule").value.trim() }) });
  if (noteEl) noteEl.textContent = "";
  if (!data.ok) {
    const extra = (data.errors || []).slice(0, 4).join("  ·  ");
    banner("err", data.error + (extra ? "  " + extra : ""));
    if (noteEl) noteEl.textContent = extra || data.detail || "";
    return false;
  }
  czRender(data);
  // Creating them worked, whatever the first try said — the page says the rest.
  banner("ok", `${data.total} test${data.total === 1 ? "" : "s"} created in ${data.suite}.`);
  loadTests();                       // so the sidebar count includes them now
  return true;
}

$("czGo").addEventListener("click", async () => {
  const story = $("czStory").value.trim();
  if (story.length < 12) { banner("err", "Say what you want to test, in a sentence or two."); return; }
  const via = (document.querySelector('input[name="czVia"]:checked') || {}).value || "";
  $("czGo").disabled = true;
  $("czGoNote").textContent = "reading your API document…";
  try {
    const { data } = await api("/api/tests/generate", { method: "POST",
      body: JSON.stringify({ story, via, module: $("czModule").value.trim(),
                             refine: via === "claude" }) });
    if (!data.ok) { banner("err", data.error); $("czGoNote").textContent = data.error; return; }
    $("czGoNote").textContent = "";
    CZ.brief = data.brief || "";
    if (!via) {
      $("czPrompt").textContent = CZ.brief; $("czReply").value = "";
      $("czCopyNote").textContent = ""; $("czCheckNote").textContent = "";
      czShow("paste");
      return;
    }
    CZ.job = data.job; CZ.stop = false;
    $("czWaitHead").textContent = `${via === "claude" ? "Claude" : "Codex"} is writing your tests…`;
    czShow("wait");
    const started = Date.now();
    let finished = false;
    for (let i = 0; i < 1200 && !CZ.stop; i++) {
      let status;
      try { ({ data: status } = await api(`/api/job/${data.job}`)); } catch { break; }
      if (status && status.done) { finished = true; break; }
      const secs = Math.round((Date.now() - started) / 1000);
      $("czWaitNote").textContent = `${secs}s so far. Two to five minutes is normal — `
        + `it is reading your whole API document. You can leave this open.`;
      await new Promise((r) => setTimeout(r, 1500));
    }
    if (CZ.stop) { czShow("ask"); return; }
    if (!finished) {
      banner("err", "That took too long. Use your own AI tool instead — the prompt is ready.");
      $("czPrompt").textContent = CZ.brief; czShow("paste");
      return;
    }
    $("czWaitHead").textContent = "Checking the tests and trying each one…";
    const ok = await czFinish({ job: data.job }, $("czWaitNote"));
    if (!ok) { $("czPrompt").textContent = CZ.brief; czShow("paste"); }
  } finally { $("czGo").disabled = false; }
});

$("czCancel").addEventListener("click", async () => {
  CZ.stop = true;
  if (CZ.job) { try { await api(`/api/job/${CZ.job}/cancel`, { method: "POST" }); } catch { /* gone */ } }
  czShow("ask");
});
$("czCopy").addEventListener("click", () => {
  copyText($("czPrompt").textContent, $("czCopy"));
  $("czCopyNote").textContent = "Copied. Paste it into your AI tool and send it.";
});
$("czBack").addEventListener("click", () => czShow("ask"));
$("czCheck").addEventListener("click", async () => {
  const reply = $("czReply").value.trim();
  if (!reply) { banner("err", "Paste the answer first."); return; }
  $("czCheck").disabled = true;
  try { await czFinish({ reply }, $("czCheckNote")); }
  finally { $("czCheck").disabled = false; }
});
$("czMore").addEventListener("click", () => { $("czStory").value = ""; czShow("ask"); czLoad(); });
$("czOpen").addEventListener("click", () => showView("tests"));
$("czRetry").addEventListener("click", async () => {
  $("czRetry").disabled = true;
  try {
    await czTry(CZ.env || "mock");
  } finally { $("czRetry").disabled = false; }
});
$("czDiscard").addEventListener("click", async () => {
  const { data } = await api("/api/create/discard", { method: "POST",
    body: JSON.stringify({ suite: CZ.suite, ids: CZ.ids }) });
  banner("ok", `Removed ${data.removed || 0} test${data.removed === 1 ? "" : "s"}.`);
  czShow("ask"); czLoad();
});

/* The tests nobody had to write. They exist as soon as a spec is loaded, so the
   first thing a new person can do is press one button and see every
   integration point answer. */
async function loadBaseline() {
  let d;
  try { ({ data: d } = await api("/api/tests/baseline")); } catch { return; }
  if (!d.exists) {
    $("baseCount").textContent = "not made yet";
    $("baseCount").className = "tag warn";
    $("baseState").textContent = "Start the mock on your spec and they are made for you.";
    $("baseRun").disabled = true;
    return;
  }
  $("baseRun").disabled = false;
  $("baseCount").className = "tag ok";
  $("baseCount").textContent = `${d.tests} ready`;
  // One plain sentence per server it has run on. Runs made from a terminal are
  // recorded under a raw address rather than a server's name; those are the
  // same server seen twice, so only named ones are shown.
  const ran = Object.entries(d.by_env || {}).filter(([env]) => !/^https?:/i.test(env))
    .map(([env, r]) => `${r.pass} of ${r.pass + r.other} passing on ${env}`).join("  ·  ");
  const needs = (d.needs_values || []).length;
  $("baseState").textContent = (ran || "Not run yet.")
    + (needs ? `  ·  ${needs} need${needs === 1 ? "s" : ""} a value only you know — open it in the list.` : "");
}

/* The result card lives far down the page. Pressing this and seeing nothing
   change nearby reads as "broken", so say what is happening right here, and
   take the reader to the result when it arrives. */
$("baseRun").addEventListener("click", async () => {
  const env = $("testEnv").value || "mock";
  $("baseRun").disabled = true;
  $("baseCount").className = "tag";
  $("baseCount").textContent = "running…";
  $("baseState").textContent = `Running the baseline against ${env} — this takes a few seconds.`;
  try {
    await runScoped({ suite: "baseline", drafts: true, what: "the baseline" });
    const rep = LAST_RUN ? ((await api(`/api/job/${LAST_RUN}/report`)).data || {}) : {};
    const s = rep.summary || {};
    const total = Object.values(s).reduce((a, n) => a + n, 0);
    const good = s.pass || 0;
    await loadBaseline();
    if (total) {
      const all = good === total;
      $("baseCount").className = "tag " + (all ? "ok" : "err");
      $("baseCount").textContent = `${good} of ${total} passed on ${rep.env || env}`;
      banner(all ? "ok" : "err",
             all ? `Baseline: all ${total} passed on ${rep.env || env}.`
                 : `Baseline: ${total - good} of ${total} did not pass on ${rep.env || env} `
                   + `— the details are in Result, below.`);
    }
    $("testOutCard").scrollIntoView({ behavior: "smooth", block: "start" });
  } finally { $("baseRun").disabled = false; }
});

$("baseRefresh").addEventListener("click", async () => {
  $("baseRefresh").disabled = true;
  try {
    const { data } = await api("/api/tests/baseline", { method: "POST", body: "{}" });
    if (!data.ok) {
      banner("err", data.error || data.skipped || (data.errors || []).join("  ·  "));
      return;
    }
    const bits = [];
    if (data.added) bits.push(`${data.added} new`);
    if (data.updated) bits.push(`${data.updated} updated`);
    if (data.removed) bits.push(`${data.removed} removed`);
    if (data.kept_edited) bits.push(`${data.kept_edited} of yours left as they are`);
    banner("ok", `Baseline is up to date with ${data.spec}`
                 + (bits.length ? ` — ${bits.join(", ")}.` : " — nothing changed."));
    await loadTests(); await loadBaseline();
  } finally { $("baseRefresh").disabled = false; }
});

async function fillBindEnvs(current) {
  const names = (ENVS || []).map((e) => e.name);
  $("bindEnv").innerHTML = (names.length ? names : ["mock"])
    .map((n) => `<option value="${esc(n)}"${n === current ? " selected" : ""}>`
                + `${esc(n)}</option>`).join("");
}

$("bindLearn").addEventListener("click", async () => {
  const env = $("bindEnv").value || "mock";
  $("bindLearn").disabled = true;
  $("bindLearned").className = "tag";
  $("bindLearned").textContent = `sweeping ${env}…`;
  try {
    const { data } = await api("/api/bindings/index",
      { method: "POST", body: JSON.stringify({ env }) });
    if (!data.ok) { banner("err", data.error); $("bindLearned").textContent = ""; return; }
    for (let i = 0; i < 900; i++) {
      let status;
      try { ({ data: status } = await api(`/api/job/${data.job}`)); } catch { break; }
      if (status && status.done) break;
      $("bindLearned").textContent = `sweeping ${env} — ${status.seconds || 0}s`;
      await new Promise((r) => setTimeout(r, 2000));
    }
    const { data: used } = await api("/api/bindings/index/use",
      { method: "POST", body: JSON.stringify({ env }) });
    if (!used.ok) { banner("err", used.error); return; }
    banner("ok", `Field names now come from ${env} — ${used.fields} field(s).`);
    await loadBindings();
  } finally { $("bindLearn").disabled = false; }
});

async function fillBindSuites() {
  try {
    const { data } = await api("/api/tests");
    const names = (data.suites || []).map((s) => s.name);
    $("bindSuite").innerHTML = [...new Set(names)]
      .map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
  } catch { /* the picker is a convenience */ }
}

$("bindFix").addEventListener("click", async () => {
  const suite = $("bindSuite").value;
  if (!suite) { banner("err", "Pick a suite."); return; }
  $("bindFix").disabled = true;
  try {
    const { data } = await api("/api/tests/rebind", {
      method: "POST", body: JSON.stringify({ suite, stage: "draft" }) });
    if (!data.ok) {
      banner("err", (data.errors || [data.error]).join("  ·  "));
      $("bindFixNote").textContent = (data.notes || []).join("\n")
        || data.error || "refused";
      return;
    }
    $("bindFixNote").textContent = data.changed
      ? (data.notes || []).join("\n")
      : `Nothing to fix in ${suite} — no placeholder ids left.`;
    banner(data.changed ? "ok" : "ok",
           data.changed ? `Rewrote ${data.changed} test(s) in ${suite}.`
                        : `${suite} has no placeholder ids.`);
    await loadTests();
  } finally { $("bindFix").disabled = false; }
});

/* Derived flows read back what they create, so a stateless mock cannot pass
   them — saying that here is cheaper than letting someone debug it. */
function renderLifecycles(d) {
  const flows = d.flows || [];
  if (!flows.length) {
    $("genLifeOut").innerHTML =
      '<div class="empty">No lifecycle could be derived from this spec — a resource '
      + 'needs a create plus a way to read, change or remove what it creates.</div>';
    return;
  }
  $("genLifeOut").innerHTML = flows.map((f) => `
    <div class="runrow ${f.guessed_capture ? "warn" : "pass"}">
      <span class="v">${f.steps.length}</span>
      <span class="what" title="${esc(f.name)}"><code>${esc(f.id)}</code></span>
      <span class="why2">${esc(f.steps.join("  \u2192  "))}</span>
    </div>`).join("")
    + (d.skipped || []).map((s) => `
    <div class="runrow"><span class="v">\u2014</span>
      <span class="what"><code>${esc(s.resource)}</code></span>
      <span class="why2">${esc(s.why)}</span></div>`).join("");
  const guessed = flows.filter((f) => f.guessed_capture).length;
  const parts = [`${flows.length} flow${flows.length === 1 ? "" : "s"} from `
                 + `${d.spec || "the project spec"}`];
  if (guessed) parts.push(`${guessed} take the id by house convention — the document `
                          + `declares none`);
  if (d.running && !d.stateful) parts.push("the mock is running stateless, so these "
                                           + "cannot pass against it — restart it "
                                           + "stateful under Source");
  $("genLifeNote").textContent = parts.join("  \u00b7  ");
}

$("genLifeGo").addEventListener("click", async () => {
  $("genLifeGo").disabled = true;
  $("genLifeNote").textContent = "reading the spec…";
  try {
    const { data } = await api("/api/tests/blueprint", {
      method: "POST", body: JSON.stringify({}) });
    if (!data.ok) { banner("err", data.error || "could not derive"); return; }
    renderLifecycles(data);
    $("genLifeImport").disabled = !(data.flows || []).length;
  } finally { $("genLifeGo").disabled = false; }
});

$("genLifeImport").addEventListener("click", async () => {
  $("genLifeImport").disabled = true;
  try {
    const { data } = await api("/api/tests/blueprint", {
      method: "POST", body: JSON.stringify({ import: true }) });
    if (!data.ok) {
      banner("err", (data.errors || [data.error || "import failed"]).join("  ·  "));
      return;
    }
    banner("ok", `added to drafts as ${data.suite} — run them, then promote what passes`);
    await loadTests();
  } finally { $("genLifeImport").disabled = false; }
});
$("genClose").addEventListener("click", () => { $("genCard").hidden = true; });
$("genCopy").addEventListener("click", () => copyText($("genOut").textContent, $("genCopy")));

$("genPipeGo").addEventListener("click", async () => {
  const kind = $("testKind").value;
  const { data } = await api("/api/tests/pipeline", {
    method: "POST",
    body: JSON.stringify({
      env: $("testEnv").value || "mock",
      levels: pickedValues("testLevels"),
      priorities: pickedValues("testPriorities"),
      modules: pickedValues("testModules"),
      only: $("testOnly").value.trim() || undefined,
      kinds: kind ? [kind] : [],
      drafts: $("testDrafts").checked,
      format: $("genFormat").value,
    }),
  });
  if (!data.ok) { banner("err", data.error || "could not build a pipeline"); return; }
  $("genOut").textContent = data.yaml || data.command;
  $("genMeta").textContent = data.selects !== undefined
    ? `${data.selects} test(s) — ${data.describes}` : data.describes;
  $("genNote").textContent = data.filename
    ? `Save this as ${data.filename}. It runs: ${data.command}`
    : "Run this wherever you like.";
});

/* Export is the other half of import: a suite has to be able to leave, or the
   tool is somewhere tests go to be trapped. The file it writes is the shape
   import accepts, so it also doubles as the thing you hand an assistant. */
$("btnExport").addEventListener("click", async () => {
  const modules = pickedValues("testModules");
  const one = modules.length === 1 ? modules[0] : null;
  const q = new URLSearchParams(one ? { suite: one, download: "1" } : { download: "1" });
  window.location.href = "/api/tests/export?" + q;
  banner("ok", one
    ? `Exporting ${one}. The same file imports straight back.`
    : "Exporting every suite. Select modules above to export just those.");
});

/* "More like these" is a different question from "here is a story": the house
   style is already settled and what is missing is coverage, so the brief shows
   real examples and names the operations nothing touches. */
$("genMoreGo").addEventListener("click", async () => {
  const modules = pickedValues("testModules");
  $("genMoreGo").disabled = true;
  try {
    const { data } = await api("/api/tests/more-like", {
      method: "POST",
      body: JSON.stringify({ module: modules.length === 1 ? modules[0] : null }),
    });
    if (!data.ok) { banner("err", data.error || "could not build the brief"); return; }
    $("genOut").textContent = data.brief;
    $("genMeta").textContent = `more like ${data.module}`;
    $("genNote").textContent =
      "Paste this wherever your team works. Bring the JSON back through Import — "
      + "it lands in drafts and still has to pass before it can be promoted.";
  } finally {
    $("genMoreGo").disabled = false;
  }
});

$("genStoryGo").addEventListener("click", async () => {
  const story = $("genStory").value.trim();
  if (story.length < 12) { banner("err", "Give a sentence or two of story."); return; }
  const via = ($("genVia") && $("genVia").value) || "";
  if (via) { await generateWith(via); return; }

  $("genStoryGo").disabled = true;
  try {
    const { data } = await api("/api/tests/story",
      { method: "POST", body: JSON.stringify({ story }) });
    if (!data.ok) { banner("err", data.error); return; }
    $("genOut").textContent = data.brief;
    $("genMeta").textContent = data.matched ? "operations matched" : "no operation matched";
    if (!data.matched) {
      banner("err", "No operation in the spec matched that story — the brief asks for the "
                  + "endpoints to be named rather than invented.");
    }
  } finally {
    $("genStoryGo").disabled = false;
  }
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
  // the sidebar counts the servers a person set up, not the technical variants
  $("navEnvs").textContent = list.filter((e) => !e.technical).length || "";
  srvRender(list);
  $("envList").innerHTML = list.length ? `<table>${list.map((e) => `
    <tr>
      <td><b>${esc(e.name)}</b></td>
      <td><code>${esc(e.base_url || "—")}</code></td>
      <td><span class="tag">auth: ${esc(e.auth_mode)}</span></td>
      <td>${e.ready
        ? '<span class="outcome pass">ready</span>'
        : `<span class="outcome fail">needs ${esc(e.unresolved.join(", "))}</span>`}</td>
      <td><button class="sm env-config" data-env="${esc(e.name)}">Configure</button>
          <button class="sm env-data" data-env="${esc(e.name)}"
                  title="what a value is on this server — ids, expected counts"
            >Test data</button></td>
    </tr>
    ${e.description ? `<tr><td></td><td colspan="3" class="hint"
       style="margin:0">${esc(e.description)}</td></tr>` : ""}`).join("")}</table>
    <p class="hint">Use <b>Configure</b> to fill these in from here. Anything shown as
      <code>${"${VAR}"}</code> is resolved from your shell,
      <code>.env</code>, or <code>environments.local.json</code> — the last two are
      gitignored. Per-environment test data goes in that environment's
      <code>data</code> block.</p>`
    : '<div class="empty">No environments defined.</div>';
  $("envList").querySelectorAll(".env-data").forEach((b) =>
    b.addEventListener("click", () => openEnvData(b.dataset.env)));
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
    : `<p class="hint" style="color:var(--err)">${esc(data.error)}${
      (data.unresolved || []).length
        ? " Fill in the field(s) above, then press Save." : ""}</p>`;
});

$("envDlgCancel").addEventListener("click", () => $("envDlg").close());

$("btnEnvLogin").addEventListener("click", async () => {
  const name = $("liveEnv").value;
  if (!name) { banner("err", "Pick an environment first."); return; }
  $("btnEnvLogin").disabled = true;
  const { data } = await api("/api/env-login",
    { method: "POST", body: JSON.stringify({ name }) });
  $("btnEnvLogin").disabled = false;
  if (!data.ok) {
    banner("err", data.error);
    // A message telling you to edit a file is a dead end when the button that
    // writes that file is on this very page — so open it.
    if ((data.unresolved || []).length) configureEnv(name);
    return;
  }
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
  // open where the address says, or on Home, which is where anyone new starts
  const want = location.hash.replace("#", "");
  if (want && document.querySelector(`.view[data-view="${want}"]`)) showView(want);
  else homeLoad();
  setInterval(() => { if (RUNNING) refreshStdout(); }, 5000);
})();
