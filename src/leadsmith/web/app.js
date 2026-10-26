/* Leadsmith dashboard. Plain JS, no build step. */
"use strict";

// ── helpers ────────────────────────────────────────────────────────────
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const main = $("#main");
const topbar = $("#topbar");
const state = { overview: null, page: null, jobsSeen: {}, selected: new Set(), reviewTab: "draft", reviewContact: null, reviewStep: {}, activeJob: null };
const isMac = /Mac|iPhone|iPad/.test(navigator.platform);

const ICONS = {
  home: '<path d="M3.5 10.5 12 3.5l8.5 7"/><path d="M5.5 9v11h13V9"/><path d="M10 20v-5h4v5"/>',
  building: '<rect x="4.5" y="3.5" width="15" height="17" rx="1.5"/><path d="M9 7.5h1M14 7.5h1M9 11h1M14 11h1M9 14.5h1M14 14.5h1M10.5 20.5v-3h3v3"/>',
  inbox: '<path d="M3.5 13h5l1.5 2.5h4l1.5-2.5h5"/><path d="M6 5h12l2.5 8v6a1 1 0 0 1-1 1h-15a1 1 0 0 1-1-1v-6z"/>',
  send: '<path d="M20.5 3.5 10 14"/><path d="m20.5 3.5-6.5 17-4-6.5-6.5-4z"/>',
  activity: '<path d="M3 12h4l2.5-6.5 5 13L17 12h4"/>',
  card: '<rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="9" cy="11" r="2"/><path d="M5.5 16c.7-1.6 2-2.4 3.5-2.4s2.8.8 3.5 2.4M15 10h3.5M15 13.5h3.5"/>',
  settings: '<path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="m20 20-4.2-4.2"/>',
  mail: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3.5 6.5 8.5 6.5 8.5-6.5"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  upload: '<path d="M12 15V4.5M7.5 9 12 4.5 16.5 9"/><path d="M4.5 15v3.5a1.5 1.5 0 0 0 1.5 1.5h12a1.5 1.5 0 0 0 1.5-1.5V15"/>',
  download: '<path d="M12 4.5V15M7.5 10.5 12 15l4.5-4.5"/><path d="M4.5 15v3.5a1.5 1.5 0 0 0 1.5 1.5h12a1.5 1.5 0 0 0 1.5-1.5V15"/>',
  sparkles: '<path d="m12 3.5 1.7 4.8 4.8 1.7-4.8 1.7L12 16.5l-1.7-4.8-4.8-1.7 4.8-1.7z"/><path d="m18.5 15 .8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8z"/>',
  zap: '<path d="M13 3 5 13.5h6.5L10.5 21 19 10.5h-6.5z"/>',
  target: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="4"/><circle cx="12" cy="12" r=".5"/>',
  users: '<circle cx="9" cy="8.5" r="3.5"/><path d="M3 20c.7-3.4 3-5.3 6-5.3s5.3 1.9 6 5.3"/><path d="M16 5.2a3.5 3.5 0 0 1 0 6.6M17.5 14.9c1.8.7 3 2.5 3.5 5.1"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4.5 20.5c1-4 4-6 7.5-6s6.5 2 7.5 6"/>',
  globe: '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.4 2.6 3.5 5.4 3.5 8.5s-1.1 5.9-3.5 8.5c-2.4-2.6-3.5-5.4-3.5-8.5s1.1-5.9 3.5-8.5z"/>',
  pin: '<path d="M12 21s6.5-6 6.5-11a6.5 6.5 0 0 0-13 0c0 5 6.5 11 6.5 11z"/><circle cx="12" cy="10" r="2.3"/>',
  tag: '<path d="M3.5 12.5V4.5h8l9 9-8 8z"/><circle cx="8" cy="9" r="1.3"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  checkCircle: '<circle cx="12" cy="12" r="8.5"/><path d="m8.5 12.2 2.4 2.4 4.6-4.8"/>',
  x: '<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>',
  alert: '<path d="M12 4.5 3 19.5h18z"/><path d="M12 10v4M12 16.8v.2"/>',
  info: '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5M12 8v.2"/>',
  file: '<path d="M14 3.5H7.5a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2V8z"/><path d="M14 3.5V8h4.5"/>',
  refresh: '<path d="M19.5 11.5a7.5 7.5 0 1 0-2.2 5.3"/><path d="M19.5 4.5v7h-7"/>',
  external: '<path d="M14 4.5h5.5V10M19.5 4.5 11 13"/><path d="M17 14v4.5a1 1 0 0 1-1 1H5.5a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1H10"/>',
  ban: '<circle cx="12" cy="12" r="8.5"/><path d="m6 6 12 12"/>',
  filter: '<path d="M4 6h16M7 12h10M10 18h4"/>',
  chevron: '<path d="m9 6 6 6-6 6"/>',
  play: '<path d="M7 5v14l11-7z"/>',
  stop: '<rect x="6.5" y="6.5" width="11" height="11" rx="1.5"/>',
  reply: '<path d="M10 8 5 12.5 10 17"/><path d="M5 12.5h9a5 5 0 0 1 5 5V19"/>',
  calendar: '<rect x="3.5" y="5" width="17" height="15" rx="2"/><path d="M3.5 10h17M8 3v4M16 3v4"/>',
  hash: '<path d="M5 9h14M5 15h14M10 4 8 20M16 4l-2 16"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M4.6 4.6 6 6M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4 6 18M18 6l1.4-1.4"/>',
  moon: '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>',
  monitor: '<rect x="3" y="4.5" width="18" height="12" rx="2"/><path d="M8.5 20h7M12 16.5V20"/>',
  copy: '<rect x="8.5" y="8.5" width="11" height="11" rx="2"/><path d="M15.5 8.5V6a1.5 1.5 0 0 0-1.5-1.5H6A1.5 1.5 0 0 0 4.5 6v8A1.5 1.5 0 0 0 6 15.5h2.5"/>',
};
const icon = (name, cls = "") => `<svg class="i ${cls}" viewBox="0 0 24 24" aria-hidden="true">${ICONS[name] || ""}</svg>`;
$$("[data-icon]").forEach((el) => (el.outerHTML = el.className ? `<span class="${el.className}">${icon(el.dataset.icon)}</span>` : icon(el.dataset.icon)));

const ENGINE_LABELS = { claude: "Claude Code", codex: "Codex", opencode: "OpenCode", gemini: "Gemini CLI" };

async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method, headers: { "Content-Type": "application/json", "X-Leadsmith-Client": "1" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = {};
  try { data = await res.json(); } catch { /* non-JSON */ }
  if (!res.ok || data.error) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

function toast(message, type = "ok", ms = 4500) {
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.innerHTML = `${icon(type === "error" ? "alert" : "checkCircle")}<div>${message}</div>`;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), ms);
}
const fail = (e) => { if (!e?.silent) toast(esc(e.message || e), "error", 7000); };

function ago(iso) {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}
const day = (iso) => (iso ? new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "");
const plural = (n, one, many) => `${n} ${n === 1 ? one : many || one + "s"}`;
const fullName = (c) => [c.first_name, c.last_name].filter(Boolean).join(" ") || c.email || "Unknown";
const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : "");

function md(text) {
  const lines = esc(text || "").split(/\r?\n/);
  let html = "", inList = false;
  const inline = (s) => s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/(^|\s)(https?:\/\/[^\s<]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
  for (const line of lines) {
    const m = line.match(/^\s*[-*]\s+(.*)/);
    if (m) { if (!inList) { html += "<ul>"; inList = true; } html += `<li>${inline(m[1])}</li>`; continue; }
    if (inList) { html += "</ul>"; inList = false; }
    const h = line.match(/^(#{1,4})\s+(.*)/);
    if (h) html += `<h${h[1].length + 1}>${inline(h[2])}</h${h[1].length + 1}>`;
    else if (line.trim()) html += `<p>${inline(line)}</p>`;
  }
  if (inList) html += "</ul>";
  return `<div class="md">${html}</div>`;
}

const AVATAR_COLORS =["#f97066", "#f79009", "#12b76a", "#2e90fa", "#7a5af8", "#ee46bc", "#06aed4", "#66c61c", "#6172f3", "#fb6514"];
function hash(s) { let h = 0; for (const ch of String(s)) h = (h * 31 + ch.charCodeAt(0)) >>> 0; return h; }
function avatar(name, cls = "") {
  const parts = String(name || "?").trim().split(/\s+/);
  const initials = ((parts[0] || "?")[0] + (parts.length > 1 ? parts[parts.length - 1][0] : "")).toUpperCase();
  return `<span class="avatar ${cls}" style="background:${AVATAR_COLORS[hash(name) % AVATAR_COLORS.length]}" aria-hidden="true">${esc(initials)}</span>`;
}
const hasSite = (domain) => Boolean(domain) && !String(domain).endsWith(".no-website");
const siteLabel = (domain) => (hasSite(domain) ? esc(domain) : "No website");
function logo(domain, name, cls = "") {
  const letter = esc((name || domain || "?")[0].toUpperCase());
  if (!hasSite(domain)) domain = "";
  return `<span class="logo ${cls}" aria-hidden="true">${domain ? `<img src="https://icons.duckduckgo.com/ip3/${encodeURIComponent(domain)}.ico" alt="" loading="lazy" onerror="this.replaceWith(document.createTextNode('${letter}'))">` : letter}</span>`;
}

const sourceUrl = (notes) => (String(notes || "").match(/https?:\/\/[^\s)]+/) || [])[0];
const sourceLink = (notes) => {
  const url = sourceUrl(notes);
  if (!url) return "";
  let host = url; try { host = new URL(url).hostname.replace(/^www\./, ""); } catch { /* keep raw */ }
  return `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(host)}</a>`;
};

const TONE = {
  new: "", researched: "blue", ready: "violet", contacted: "blue", replied: "green", meeting: "green", won: "green", lost: "red", disqualified: "red",
  draft: "", approved: "green", sent: "blue", rejected: "red", cancelled: "",
  drafted: "violet", bounced: "red", unsubscribed: "red",
  queued: "", running: "blue", done: "green", failed: "red",
  guessed: "amber", manual: "blue", public: "green", verified: "green", valid: "green", imported: "blue", invalid: "red",
  hiring: "blue", funding: "green", leadership_change: "violet", launch: "violet", expansion: "violet", news: "", tech_change: "amber", competitor: "red",
};
const badge = (status, label, dot = true) => `<span class="badge ${TONE[status] || ""}">${dot ? '<span class="dot"></span>' : ""}${esc(label || cap(String(status || "").replace(/_/g, " ")))}</span>`;
const score = (n) => (n === null || n === undefined ? '<span class="score">–</span>' : `<span class="score ${n >= 7 ? "hi" : n >= 4 ? "mid" : ""}">${n}</span>`);

function setTop(iconName, title, sub = "", actions = "") {
  topbar.innerHTML = `<div class="crumb">${icon(iconName)}${esc(title)}${sub ? `<span class="muted">· ${sub}</span>` : ""}</div><div class="actions">${actions}</div>`;
}

// ── modal ──────────────────────────────────────────────────────────────
function modal({ title, intro = "", art = "", body = "", confirm = "OK", cancel = "Cancel", wide = false, danger = false, onConfirm }) {
  const root = $("#modal-root");
  root.innerHTML = `
    <div class="backdrop">
      <div class="modal ${wide ? "wide" : ""}" role="dialog" aria-modal="true" aria-label="${esc(title)}">
        <div class="modal-head">${art ? `<div class="art">${icon(art)}</div>` : ""}<div><h3>${esc(title)}</h3>${intro ? `<p>${intro}</p>` : ""}</div></div>
        <div class="modal-body">${body}</div>
        <div class="modal-foot">
          ${cancel ? `<button class="btn" data-close>${esc(cancel)}</button>` : ""}
          ${confirm ? `<button class="btn ${danger ? "danger" : "primary"}" data-ok>${esc(confirm)}</button>` : ""}
        </div>
      </div>
    </div>`;
  const close = () => (root.innerHTML = "");
  $$("[data-close]", root).forEach((b) => (b.onclick = close));
  $(".backdrop", root).onclick = (e) => { if (e.target.classList.contains("backdrop")) close(); };
  const ok = $("[data-ok]", root);
  if (ok) ok.onclick = async () => {
    ok.disabled = true;
    try { const keep = await onConfirm?.(root); if (keep !== true) close(); }
    catch (e) { fail(e); ok.disabled = false; }
  };
  const first = $("input, textarea, select", root) || ok;
  if (first) first.focus();
}

function engineCards(list, current) {
  return `<div class="engines">${list.map((e) => {
    const on = current.connected && current.key === e.key;
    const last = !current.connected && current.key === e.key;
    return `<div class="engine ${on ? "on" : ""} ${e.installed ? "" : "missing"}">
      <span class="engine-main"><span class="engine-name"><b>${esc(e.label)}</b>${on ? '<span class="engine-tag">Connected</span>' : last ? '<span class="engine-tag last">Last used</span>' : ""}</span>
        <span class="engine-state"><span class="dot ${on ? "green" : e.installed ? "grey" : "red"}"></span>${on ? `Running on ${esc(e.plan)}` : e.installed ? "Installed · not connected" : "Not installed"}</span>
        ${e.installed ? "" : `<span class="engine-help">${esc(e.install)}</span>`}</span>
      <span class="engine-act">${on ? `<button class="btn sm" data-disconnect>Disconnect</button>`
        : e.installed ? `<button class="btn sm ${last ? "primary" : ""}" data-connect="${e.key}">Connect</button>` : ""}</span>
    </div>`;
  }).join("")}</div>`;
}

function wireEngineCards(root, after) {
  $$("[data-connect]", root).forEach((b) => (b.onclick = async () => {
    const label = b.closest(".engine").querySelector("b").textContent;
    b.disabled = true; b.textContent = "Checking…";
    try {
      await api("/api/engines/connect", { method: "POST", body: { engine: b.dataset.connect } });
      toast(`${esc(label)} connected`);
      after(true);
    } catch (e) { fail(e); b.disabled = false; b.textContent = "Connect"; }
  }));
  $$("[data-disconnect]", root).forEach((b) => (b.onclick = async () => {
    await api("/api/engines/disconnect", { method: "POST", body: {} });
    toast("Agent disconnected. Nothing runs until you connect one again.");
    after(false);
  }));
}

// Runs need a connected agent. If none is, ask which one to connect, then carry on.
function ensureEngine() {
  if (state.overview?.engine?.connected) return Promise.resolve(true);
  return new Promise(async (resolve) => {
    let data;
    try { data = await api("/api/engines"); } catch (e) { fail(e); return resolve(false); }
    let done = false;
    modal({
      title: "Connect an agent to run this", art: "zap", wide: true, confirm: null, cancel: "Cancel",
      intro: "Leadsmith only uses an agent you connect, one at a time. It stays connected until you disconnect it or restart the dashboard.",
      body: engineCards(data.engines, data.engine),
    });
    const root = $("#modal-root");
    wireEngineCards(root, async (connected) => {
      if (!connected) return;
      done = true;
      await refreshOverview();
      root.innerHTML = "";
      resolve(true);
    });
    // Closing the prompt without connecting cancels the run.
    const watch = new MutationObserver(() => { if (!root.innerHTML && !done) { watch.disconnect(); resolve(false); } });
    watch.observe(root, { childList: true });
  });
}

const cancelled = Object.assign(new Error("cancelled"), { silent: true });

async function startJob(kind, params, { quiet = false } = {}) {
  if (!(await ensureEngine())) throw cancelled;
  const job = await api("/api/jobs", { method: "POST", body: { kind, ...params } });
  state.jobsSeen[job.id] = "running";
  if (!quiet) toast(`Started “${esc(job.title)}”. <a href="#/activity">View run</a>`);
  refreshOverview();
  return job;
}

// ── shell ──────────────────────────────────────────────────────────────
// When the local server stops (terminal closed, PC restarted), say so instead of failing silently.
function setOffline(off) {
  let bar = $("#offline");
  if (off && !bar) {
    bar = document.createElement("div");
    bar.id = "offline";
    bar.className = "callout red";
    bar.style.cssText = "position:fixed;top:12px;left:50%;transform:translateX(-50%);z-index:70;box-shadow:var(--shadow-lg);max-width:560px";
    bar.innerHTML = `${icon("alert")}<div><b>The dashboard server isn't running.</b> Open a terminal in the leadsmith folder and run <code>uv run leadsmith</code>, then keep that window open. This page reconnects by itself.</div>`;
    document.body.appendChild(bar);
  } else if (!off && bar) {
    bar.remove();
    render();
  }
  state.offline = off;
}

async function refreshOverview() {
  try {
    const o = await api("/api/overview");
    setOffline(false);
    state.overview = o;
    $("#count-leads").textContent = o.counts.accounts || "";
    $("#count-review").textContent = o.counts.drafts_by_status?.draft || "";
    $("#count-outreach").textContent = o.approved_queue || "";
    $("#count-jobs").innerHTML = o.jobs_running ? '<span class="spin" style="color:var(--primary)" aria-label="running"></span>' : "";
    const c = o.engine || {};
    const brainOk = Object.values(o.brain).every((b) => b.complete);
    $("#health").innerHTML = [
      `<a href="#/settings"><span class="dot ${c.connected ? "green" : "amber"}"></span>${c.connected ? `${esc(c.label)} connected` : "No agent connected"}</a>`,
      `<a href="#/settings"><span class="dot ${o.mail.app_password_set ? (o.mail.send_enabled ? "green" : "amber") : "amber"}"></span>${o.mail.app_password_set ? (o.mail.send_enabled ? "Gmail connected · sending on" : "Gmail connected · sending paused") : "Gmail not connected"}</a>`,
      `<a href="#/business"><span class="dot ${brainOk ? "green" : "amber"}"></span>Profile ${brainOk ? "complete" : "incomplete"}</a>`,
      o.mail.test_recipient ? `<a href="#/settings" title="Every email goes to ${esc(o.mail.test_recipient)}"><span class="dot blue"></span>Test mode on</a>` : "",
    ].join("");
  } catch (e) {
    if (e instanceof TypeError) setOffline(true); // fetch couldn't reach the server at all
  }
}

async function pollJobs() {
  if (state.offline) return; // only the overview poll checks for the server coming back
  try {
    const { jobs } = await api("/api/jobs");
    for (const job of jobs) {
      const prev = state.jobsSeen[job.id];
      if (prev && prev !== job.status && ["done", "failed", "cancelled"].includes(job.status)) {
        const verb = { done: "Finished", failed: "Failed", cancelled: "Stopped" }[job.status];
        toast(`${verb}: “${esc(job.title)}”`, job.status === "failed" ? "error" : "ok", 7000);
        if (["home", "leads", "review", "business", "outreach"].includes(state.page)) render();
      }
      state.jobsSeen[job.id] = job.status;
    }
  } catch { /* ignore */ }
}

const PAGES = {};
const route = () => { const p = location.hash.replace(/^#\/?/, "").split("?")[0]; return PAGES[p] ? p : null; };
async function render() {
  const page = route() || "home";
  if (!route()) history.replaceState(null, "", `#/${page}`);
  state.page = page;
  $$("#nav a").forEach((a) => { a.classList.toggle("active", a.dataset.page === page); a.toggleAttribute("aria-current", a.dataset.page === page); });
  try { await PAGES[page](); } catch (e) { main.innerHTML = `<div class="page"><div class="callout red">${icon("alert")}${esc(e.message)}</div></div>`; }
}
window.addEventListener("hashchange", () => { closeDrawer(); render(); });
$("#quick-kbd").textContent = isMac ? "⌘K" : "Ctrl K";

// Appearance: light, dark, or follow the system. Remembered per browser.
function applyTheme(choice) {
  if (choice === "light" || choice === "dark") document.documentElement.dataset.theme = choice;
  else delete document.documentElement.dataset.theme;
  try { localStorage.setItem("leadsmith-theme", choice); } catch { /* storage blocked: still works for this visit */ }
  $$("[data-theme-set]").forEach((b) => { const on = b.dataset.themeSet === choice; b.classList.toggle("active", on); b.setAttribute("aria-checked", on); });
}
let savedTheme = "system";
try { savedTheme = localStorage.getItem("leadsmith-theme") || "system"; } catch { /* ignore */ }
applyTheme(savedTheme);
$$("[data-theme-set]").forEach((b) => (b.onclick = () => applyTheme(b.dataset.themeSet)));
$("#quick").onclick = () => { location.hash = "#/home"; setTimeout(() => $("#ask")?.focus(), 50); };
document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); $("#quick").click(); }
  if (e.key === "Escape") { closeDrawer(); $("#modal-root").innerHTML = ""; }
});

// ── Home ───────────────────────────────────────────────────────────────
PAGES.home = async () => {
  const [{ accounts }, { drafts }, { jobs }, homeSug] = await Promise.all([api("/api/accounts"), api("/api/drafts?status=draft"), api("/api/jobs"), getSuggestions()]);
  const o = state.overview || {};
  const hour = new Date().getHours();
  const greet = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
  const toReview = new Set(drafts.map((d) => d.contact_id)).size;
  const sentLimit = o.mail?.daily_limit || 25;
  const setup = [];
  if (o.engine && !o.engine.connected) setup.push(["zap", "Connect an agent", "Claude Code, Codex, OpenCode or Gemini CLI. Nothing runs until you connect one.", "#/settings"]);
  if (o.brain && !Object.values(o.brain).every((b) => b.complete)) setup.push(["card", "Finish your business profile", "Agents use it to pick targets and write emails.", "#/business"]);
  if (!o.mail?.app_password_set) setup.push(["mail", "Connect Gmail", "Send approved emails and track replies.", "#/settings"]);
  setTop("home", "Home");
  main.innerHTML = `<div class="page" style="max-width:1080px">
    <div class="hello"><h1>${greet}</h1><p>${toReview ? `${plural(toReview, "sequence")} ${toReview === 1 ? "is" : "are"} waiting for your review.` : "Tell the agents who you want to reach."}</p></div>
    <div class="ask">
      <textarea id="ask" rows="2" placeholder="Find 10 seed-stage fintechs in India hiring their first sales rep and draft emails to the founders" aria-label="Ask the agents"></textarea>
      <div class="ask-foot">
        <span class="hint">${o.engine?.connected ? `Runs on ${esc(o.engine.label)} · ${esc(o.engine.plan)}` : "No agent connected yet: you'll pick one when you run"} · nothing is sent without you</span>
        ${(homeSug.suggestions.length ? homeSug.suggestions.slice(0, 3).map((x) => `<button class="suggest" data-suggest="Find 10 buyers: ${esc(x.query)}" title="${esc(x.query)}">${icon("sparkles", "sm")}${esc(x.label)}</button>`).join("")
          : `<button class="suggest" data-suggest="Find 10 B2B SaaS companies in the US that raised a Series A in the last 60 days and are hiring sales reps">${icon("zap", "sm")}Just raised + hiring</button>
        <button class="suggest" data-suggest="Find 10 YC companies hiring their first growth marketer">${icon("users", "sm")}YC, first marketer</button>`)}
        <button class="btn primary sm" id="ask-go">${icon("sparkles")}Run</button>
      </div>
    </div>
    <div class="stats">
      <div class="card stat"><div class="label">${icon("building", "sm")}Leads</div><div class="value">${o.counts?.accounts ?? 0}</div><div class="foot">${accounts.filter((a) => (a.intent_score || 0) >= 7).length} with strong intent</div></div>
      <div class="card stat"><div class="label">${icon("inbox", "sm")}To review</div><div class="value">${o.counts?.drafts_by_status?.draft || 0}</div><div class="foot">${plural(toReview, "person", "people")}</div></div>
      <div class="card stat"><div class="label">${icon("send", "sm")}Sent today</div><div class="value">${o.mail?.sent_today ?? 0}<small> / ${sentLimit}</small></div><div class="meter"><i style="width:${Math.min(100, ((o.mail?.sent_today || 0) / sentLimit) * 100)}%"></i></div></div>
      <div class="card stat"><div class="label">${icon("reply", "sm")}Replies</div><div class="value">${o.replied ?? 0}</div><div class="foot">${o.approved_queue || 0} approved, waiting to send</div></div>
    </div>
    <div class="two">
      <div class="card">
        <div class="card-head">${icon("target")}Top leads<div class="actions"><a class="btn ghost sm" href="#/leads">View all</a></div></div>
        <div class="list">${accounts.slice(0, 6).map((a) => `
          <a href="#/leads" data-open="${esc(a.domain)}">${logo(a.domain, a.name)}<div class="grow"><div style="font-weight:550">${esc(a.name || a.domain)}</div><div class="s" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(a.why_now) || siteLabel(a.domain)}</div></div>${score(a.fit_score)}${score(a.intent_score)}</a>`).join("") || `<div class="s">No leads yet. Ask above or <a href="#/leads">add companies</a>.</div>`}</div>
      </div>
      <div class="stack" style="display:grid;gap:16px;align-content:start">
        ${setup.length ? `<div class="card"><div class="card-head">${icon("checkCircle")}Get set up</div><div class="list">${setup.map(([ic, t, s, href]) => `<a href="${href}"><span class="logo">${icon(ic, "sm")}</span><div class="grow"><div style="font-weight:550">${t}</div><div class="s">${s}</div></div>${icon("chevron", "sm")}</a>`).join("")}</div></div>` : ""}
        <div class="card"><div class="card-head">${icon("activity")}Recent agent runs<div class="actions"><a class="btn ghost sm" href="#/activity">View all</a></div></div>
          <div class="list">${jobs.slice(0, 5).map((j) => `<a href="#/activity" data-job="${j.id}"><div class="grow"><div style="font-weight:550;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${esc(j.title)}</div><div class="s">${ago(j.created_at)}${j.cost_usd ? ` · $${Number(j.cost_usd).toFixed(2)}` : ""}</div></div>${j.status === "running" ? '<span class="spin" style="color:var(--primary)"></span>' : badge(j.status)}</a>`).join("") || '<div class="s">No runs yet.</div>'}</div></div>
      </div>
    </div></div>`;
  const run = async () => {
    const prompt = $("#ask").value.trim();
    if (!prompt) return $("#ask").focus();
    try { const job = await startJob("custom", { prompt }, { quiet: true }); state.activeJob = job.id; location.hash = "#/activity"; } catch (e) { fail(e); }
  };
  $("#ask-go").onclick = run;
  $("#ask").onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); run(); } };
  $$("[data-suggest]").forEach((b) => (b.onclick = () => { $("#ask").value = b.dataset.suggest; $("#ask").focus(); }));
  $$("[data-open]").forEach((a) => (a.onclick = (e) => { e.preventDefault(); openAccount(a.dataset.open); }));
  $$("[data-job]").forEach((a) => (a.onclick = () => { state.activeJob = Number(a.dataset.job); }));
};

// ── Business profile ───────────────────────────────────────────────────
const BRAIN_TABS = [["company", "Company & offer", "building"], ["icp", "Ideal customer", "target"], ["personas", "Personas", "users"], ["voice", "Voice & rules", "mail"]];
PAGES.business = async () => {
  const tab = state.brainTab || "company";
  if (tab === "steps") return stepsPage();
  const brain = await api("/api/brain");
  setTop("card", "Business profile", "what every agent reads first", `
    <input type="text" id="site" placeholder="yourcompany.com" style="width:190px;height:32px" aria-label="Your website">
    <button class="btn" id="autofill">${icon("sparkles")}Draft from website</button>
    <button class="btn primary" id="save-brain">Save</button>`);
  main.innerHTML = `
    ${brainTabs(tab, brain)}
    <div class="page" style="max-width:980px">
      <div class="callout blue" style="margin-bottom:14px">${icon("info")}<div>Replace every <b>TODO</b> with real details. Agents quote your proof exactly and never invent their own.</div></div>
      <div class="card"><textarea id="brain-text" spellcheck="true" aria-label="${esc(tab)}" style="min-height:520px;border:0;box-shadow:none;font:13px/1.7 var(--mono);padding:18px 20px;border-radius:10px">${esc(brain[tab].content)}</textarea></div>
    </div>`;
  $$("[data-tab]").forEach((b) => (b.onclick = () => { state.brainTab = b.dataset.tab; render(); }));
  $("#save-brain").onclick = async () => {
    try {
      const r = await api(`/api/brain/${tab}`, { method: "PUT", body: { content: $("#brain-text").value } });
      toast(r.complete ? "Saved" : "Saved. Some TODOs are still open.");
      refreshOverview(); render();
    } catch (e) { fail(e); }
  };
  $("#autofill").onclick = async () => {
    const website = $("#site").value.trim();
    if (!website) { $("#site").focus(); return toast("Enter your website first.", "error"); }
    try { await startJob("autofill", { website }); } catch (e) { fail(e); }
  };
};

function brainTabs(tab, brain) {
  return `<div class="tabs" role="tablist">${BRAIN_TABS.map(([k, label, ic]) => `<button role="tab" data-tab="${k}" class="${k === tab ? "active" : ""}" aria-selected="${k === tab}">${icon(ic, "sm")}${esc(label)}${!brain ? "" : brain[k].complete ? badge("done", "Done", false) : badge("guessed", `${brain[k].todos} to fill`, false)}</button>`).join("")}
    <button role="tab" data-tab="steps" class="${tab === "steps" ? "active" : ""}" aria-selected="${tab === "steps"}">${icon("file", "sm")}Email steps</button></div>`;
}

// Fill {placeholders} the same way the server does, for the live preview.
function fillTemplate(text, values) {
  return (text || "").replace(/\{([a-z_][a-z0-9_]*)\}/g, (m, key) => (key in values ? values[key] || `[${key}]` : `⟨${key}: the AI writes this⟩`));
}

async function stepsPage() {
  const { steps, placeholders, sample } = await api("/api/templates");
  const values = sample ? sample.values : Object.fromEntries(Object.keys(placeholders).map((k) => [k, `[${k}]`]));
  setTop("card", "Business profile", "what every agent reads first", `<button class="btn primary" id="save-steps">Save</button>`);
  main.innerHTML = `
    ${brainTabs("steps", null)}
    <div class="page" style="max-width:980px">
      <div class="callout blue" style="margin-bottom:14px">${icon("info")}<div>For each step, let the AI write a fresh email (with an extra instruction if you like) or use <b>your own template</b>. Templates fill in the lead's details; any <code>{name}</code> that isn't listed below, like <code>{personal_line}</code>, is written by the AI as one sourced sentence. Signature and opt-out line are added at send time.</div></div>
      ${steps.map((t) => `
      <div class="card step-card" data-step="${t.step}" style="margin-bottom:14px">
        <div class="card-head">${icon("mail", "sm")}Step ${t.step} · ${esc(t.name)}
          <div class="actions"><div class="seg" role="radiogroup" aria-label="Who writes step ${t.step}">
            <button type="button" data-mode="ai" class="${t.mode === "ai" ? "active" : ""}" aria-pressed="${t.mode === "ai"}">AI writes it</button>
            <button type="button" data-mode="fixed" class="${t.mode === "fixed" ? "active" : ""}" aria-pressed="${t.mode === "fixed"}">My template</button>
          </div></div></div>
        <div class="card-body">
          <div class="mode-ai" ${t.mode === "fixed" ? "hidden" : ""}>
            <label class="field">Extra instruction <span class="help">Optional. Added on top of your Voice & rules for this step only.</span>
              <textarea data-k="instruction" rows="2" placeholder="${t.step === 1 ? "e.g. Mention we've made 40+ launch videos for SaaS teams" : t.step === 2 ? "e.g. Include a one-line case study from company.md" : "e.g. Keep it to two sentences"}">${esc(t.instruction || "")}</textarea></label>
          </div>
          <div class="mode-fixed" ${t.mode === "fixed" ? "" : "hidden"}>
            ${t.step === 1 ? `<label class="field">Subject<input type="text" data-k="subject" value="${esc(t.subject || "")}" placeholder="ideas for {company}"></label>` : `<p class="help" style="margin:0 0 10px">Sent as a reply in the same thread, so it keeps step 1's subject.</p>`}
            <label class="field">Body<textarea data-k="body" rows="8" style="font:13px/1.6 var(--mono)" placeholder="${t.step === 1 ? "Hi {first_name},\n\n{personal_line}\n\nThree things I could make for {company}:\n{ideas}\n\nWhich one should I start with?" : "Hi {first_name}, ..."}">${esc(t.body || "")}</textarea></label>
            <div class="ph-chips">${Object.entries(placeholders).map(([k, d]) => `<button type="button" class="ph" data-ph="${k}" title="${esc(d)}">{${k}}</button>`).join("")}<button type="button" class="ph custom" data-ph="personal_line" title="Not a lead field: the AI writes one sourced sentence here">{personal_line}</button></div>
            <div class="step-preview"><div class="label">${icon("mail", "sm")}Preview${sample ? ` with ${esc(sample.who)}` : " (no leads yet, so showing field names)"}</div><div class="pv-subject"></div><pre class="pv-body"></pre></div>
          </div>
        </div>
      </div>`).join("")}
    </div>`;
  $$("[data-tab]").forEach((b) => (b.onclick = () => { state.brainTab = b.dataset.tab; render(); }));
  const preview = (card) => {
    const subj = $('[data-k="subject"]', card);
    $(".pv-subject", card).textContent = subj ? fillTemplate(subj.value, values) : "";
    $(".pv-body", card).textContent = fillTemplate($('[data-k="body"]', card).value, values) || "Write your template above.";
  };
  $$(".step-card").forEach((card) => {
    $$("[data-mode]", card).forEach((b) => (b.onclick = () => {
      $$("[data-mode]", card).forEach((x) => { x.classList.toggle("active", x === b); x.setAttribute("aria-pressed", x === b); });
      $(".mode-ai", card).hidden = b.dataset.mode !== "ai";
      $(".mode-fixed", card).hidden = b.dataset.mode !== "fixed";
    }));
    let last = $('[data-k="body"]', card);
    $$("input[data-k], textarea[data-k]", card).forEach((el) => {
      el.addEventListener("focus", () => { if (el.dataset.k !== "instruction") last = el; });
      el.addEventListener("input", () => preview(card));
    });
    $$("[data-ph]", card).forEach((b) => (b.onclick = () => {
      const el = last, tag = `{${b.dataset.ph}}`, at = el.selectionStart ?? el.value.length;
      el.value = el.value.slice(0, at) + tag + el.value.slice(el.selectionEnd ?? at);
      el.focus(); el.selectionStart = el.selectionEnd = at + tag.length; preview(card);
    }));
    preview(card);
  });
  $("#save-steps").onclick = async () => {
    try {
      for (const card of $$(".step-card")) {
        const val = (k) => ($(`[data-k="${k}"]`, card) || {}).value || "";
        await api(`/api/templates/${card.dataset.step}`, { method: "PUT", body: {
          mode: $("[data-mode].active", card).dataset.mode, subject: val("subject"), body: val("body"), instruction: val("instruction") } });
      }
      toast("Saved. The writer uses these for every new draft.");
    } catch (e) { fail(e); }
  };
}

function regionChip(region) {
  return region ? ` <span class="region-chip" title="${esc(region.label)}: ${esc(region.rule)}">${esc(region.code)}</span>` : "";
}

// One paid BetterContact lookup per click, with the cost stated up front.
function wireVerify(root, after) {
  $$("[data-verify]", root).forEach((b) => (b.onclick = () => modal({
    title: "Verify this email with BetterContact?", art: "checkCircle",
    intro: "BetterContact checks many email providers for this person. It charges your BetterContact credits only if it finds a usable address; not-found costs nothing. This can take up to two minutes.",
    confirm: "Verify",
    onConfirm: async () => {
      toast("Checking with BetterContact…");
      try {
        const r = await api(`/api/contacts/${b.dataset.verify}/verify`, { method: "POST" });
        const left = r.credits_left != null ? ` ${r.credits_left} credits left.` : "";
        if (!r.done) toast(r.note);
        else if (r.verified) toast(`Verified: ${r.email}.${left}`);
        else toast(`No verified email found (${String(r.status).replace(/_/g, " ")}). Nothing was charged for that.${left}`, "error");
        after();
      } catch (e) { fail(e); }
    },
  })));
}

// ── Leads ──────────────────────────────────────────────────────────────
PAGES.leads = async () => {
  const status = state.leadStatus || "";
  const q = state.leadQuery || "";
  const { accounts } = await api(`/api/accounts?status=${encodeURIComponent(status)}&q=${encodeURIComponent(q)}`);
  state.selected = new Set([...state.selected].filter((d) => accounts.some((a) => a.domain === d)));
  const statuses = ["", "new", "researched", "ready", "contacted", "replied", "meeting", "won", "lost", "disqualified"];
  setTop("building", "Leads", "", `
    <button class="btn" id="import">${icon("upload")}Import CSV</button>
    <button class="btn" id="add">${icon("plus")}Add companies</button>
    <button class="btn primary" id="find">${icon("sparkles")}Find buyers</button>`);
  const filtered = Boolean(status || q);
  main.innerHTML = `
    <div class="toolbar">
      <select id="status" aria-label="Stage">${statuses.map((s) => `<option value="${s}" ${s === status ? "selected" : ""}>${s ? cap(s) : "All companies"}</option>`).join("")}</select>
      <div class="input-icon">${icon("search")}<input type="search" id="q" placeholder="Search companies or signals" value="${esc(q)}" aria-label="Search"></div>
      <div class="right">${plural(accounts.length, "company", "companies")}<button class="btn sm" id="sources">${icon("activity", "sm")}Sources</button></div>
    </div>
    <div id="bulk"></div>
    ${accounts.length ? `<div class="table-wrap"><table class="grid">
      <thead><tr>
        <th><input type="checkbox" id="all" aria-label="Select all"></th>
        <th><span class="h">${icon("building", "sm")}Company</span></th>
        <th title="Match with your ideal customer, out of 10"><span class="h">${icon("target", "sm")}Fit</span></th>
        <th title="Strength and recency of buying signals, out of 10"><span class="h">${icon("zap", "sm")}Intent</span></th>
        <th><span class="h">${icon("sparkles", "sm")}Why now</span></th>
        <th><span class="h">${icon("users", "sm")}People</span></th>
        <th><span class="h">${icon("tag", "sm")}Stage</span></th>
        <th class="col-upd"><span class="h">${icon("clock", "sm")}Updated</span></th>
      </tr></thead>
      <tbody>${accounts.map((a) => `
        <tr class="click ${state.selected.has(a.domain) ? "selected" : ""}" data-domain="${esc(a.domain)}">
          <td data-stop><input type="checkbox" data-sel="${esc(a.domain)}" ${state.selected.has(a.domain) ? "checked" : ""} aria-label="Select ${esc(a.name || a.domain)}"></td>
          <td><div class="company">${logo(a.domain, a.name)}<div style="min-width:0"><div class="name">${esc(a.name || a.domain)}${a.feedback === "good" ? ` <span class="fb-mark good" title="You marked this a good fit">${icon("check", "sm")}</span>` : a.feedback === "bad" ? ` <span class="fb-mark bad" title="You marked this not a fit">${icon("x", "sm")}</span>` : ""}${regionChip(a.region)}</div>${a.name ? `<div class="domain">${siteLabel(a.domain)}</div>` : ""}</div></div></td>
          <td>${score(a.fit_score)}</td>
          <td>${score(a.intent_score)}</td>
          <td class="wrap">${a.why_now ? esc(a.why_now) : '<span style="color:var(--text-3)">Not researched yet</span>'}</td>
          <td>${a.contacts ? `<span class="row" style="display:inline-flex;gap:6px;align-items:center">${plural(a.contacts, "person", "people")}${a.drafts_to_review ? ` ${badge("drafted", `${a.drafts_to_review} to review`, false)}` : ""}</span>` : '<span style="color:var(--text-3)">–</span>'}</td>
          <td>${badge(a.status)}</td>
          <td class="col-upd" style="color:var(--text-2)">${ago(a.updated_at)}</td>
        </tr>`).join("")}</tbody>
    </table></div>` : `
    <div class="empty"><div class="art">${icon(filtered ? "search" : "building")}</div>
      <h3>${filtered ? "No matches" : "No leads yet"}</h3>
      <p>${filtered ? "Try a different search or stage." : "Describe who you're after and the agents find companies with a reason to buy now. Or bring a list you already have."}</p>
      ${filtered ? "" : `<div class="actions" style="justify-content:center"><button class="btn primary" data-act="find">${icon("sparkles")}Find buyers</button><button class="btn" data-act="add">${icon("plus")}Add companies</button><button class="btn" data-act="import">${icon("upload")}Import CSV</button></div>`}
    </div>`}`;
  const renderBulk = () => {
    const n = state.selected.size;
    $("#bulk").innerHTML = n ? `
      <div class="selbar"><b>${n} selected</b>
        <button class="btn sm" data-bulk="research">${icon("sparkles", "sm")}Research & write</button>
        <button class="btn sm" data-bulk="write">${icon("mail", "sm")}Rewrite emails</button>
        <button class="btn sm" data-bulk="emails">${icon("search", "sm")}Find published emails</button>
        <button class="btn sm" data-bulk="deck">${icon("file", "sm")}Build decks</button>
        <button class="btn sm danger" data-bulk="disqualify">${icon("ban", "sm")}Disqualify</button>
        <button class="btn sm ghost" data-bulk="clear">Clear</button></div>` : "";
    $$("[data-bulk]").forEach((b) => (b.onclick = () => bulkLeads(b.dataset.bulk)));
  };
  renderBulk();
  $("#q").oninput = debounce((e) => { state.leadQuery = e.target.value; render(); }, 350);
  $("#status").onchange = (e) => { state.leadStatus = e.target.value; render(); };
  $("#find").onclick = findBuyersModal; $("#add").onclick = addCompaniesModal; $("#import").onclick = importModal;
  $("#sources").onclick = sourcesModal;
  $$("[data-act]").forEach((b) => (b.onclick = { find: findBuyersModal, add: addCompaniesModal, import: importModal }[b.dataset.act]));
  $$("[data-sel]").forEach((c) => (c.onchange = () => { c.checked ? state.selected.add(c.dataset.sel) : state.selected.delete(c.dataset.sel); c.closest("tr").classList.toggle("selected", c.checked); renderBulk(); }));
  const all = $("#all");
  if (all) all.onchange = () => { $$("[data-sel]").forEach((c) => { c.checked = all.checked; c.closest("tr").classList.toggle("selected", all.checked); all.checked ? state.selected.add(c.dataset.sel) : state.selected.delete(c.dataset.sel); }); renderBulk(); };
  $$("tr.click").forEach((tr) => (tr.onclick = (e) => { if (!e.target.closest("[data-stop]")) openAccount(tr.dataset.domain); }));
  if (q) { const input = $("#q"); input.focus(); input.setSelectionRange(q.length, q.length); }
};

async function bulkLeads(action) {
  const domains = [...state.selected];
  try {
    if (action === "clear") { state.selected.clear(); return render(); }
    if (action === "disqualify") {
      for (const d of domains) await api(`/api/accounts/${encodeURIComponent(d)}/status`, { method: "POST", body: { status: "disqualified" } });
      toast(`Disqualified ${plural(domains.length, "company", "companies")}`);
    } else {
      await startJob(action, { domains });
    }
    state.selected.clear(); render();
  } catch (e) { fail(e); }
}

async function getSuggestions() {
  try { return await api("/api/suggestions"); } catch { return { suggestions: [], missing: null }; }
}

function suggestionBlock(sug) {
  if (sug.suggestions.length) {
    return `<div class="field" style="gap:8px"><span style="display:flex;align-items:center;gap:6px;font-weight:500">${icon("sparkles", "sm")}Suggested from your business profile</span>
      <div style="display:flex;flex-wrap:wrap;gap:6px">${sug.suggestions.map((x, i) => `<button type="button" class="suggest" data-sugg="${i}" title="${esc(x.query)}">${esc(x.label)}</button>`).join("")}</div></div>`;
  }
  return sug.missing ? `<div class="callout blue">${icon("info")}<div>${esc(sug.missing)} <a href="#/business" data-close-modal>Open Business profile</a></div></div>` : "";
}

async function sourcesModal() {
  const { sources } = await api("/api/sources");
  const pct = (v) => (v === null || v === undefined ? "–" : `${Math.round(v * 100)}%`);
  modal({
    title: "Where your leads come from", art: "activity", wide: true, confirm: null, cancel: "Close",
    intro: "Which sources produced leads you liked and that replied. The scout reads this and leans on what works.",
    body: sources.length ? `<div class="table-wrap"><table class="grid">
      <thead><tr><th style="padding-left:12px;width:auto;border-right:1px solid var(--border)">Source</th><th>Leads</th><th>Good / not a fit</th><th>Contacted</th><th>Replied</th><th>Reply rate</th><th>Meetings</th></tr></thead>
      <tbody>${sources.map((r) => `<tr><td style="padding-left:12px;width:auto;border-right:1px solid var(--border)"><b>${esc(r.source)}</b></td>
        <td>${r.leads}</td><td>${r.good ? `<span style="color:var(--green)">${r.good}</span>` : 0} / ${r.bad ? `<span style="color:var(--red)">${r.bad}</span>` : 0}</td>
        <td>${r.contacted}</td><td>${r.replied}</td><td>${pct(r.reply_rate)}</td><td>${r.meetings}</td></tr>`).join("")}</tbody></table></div>`
      : `<p style="color:var(--text-2)">No leads yet.</p>`,
  });
}

async function findBuyersModal() {
  const sug = await getSuggestions();
  const first = sug.suggestions[0]?.query || "";
  modal({
    title: "Find buyers", art: "sparkles",
    intro: "Agents find companies with a reason to buy now, research them, find the right people and draft emails for your review.",
    confirm: "Start search",
    body: `
      ${suggestionBlock(sug)}
      <label class="field">Who are you looking for?<span class="help">${sug.suggestions.length ? "Pick a suggestion above, edit it, or write your own." : "Leave empty to use your business profile."}</span>
        <textarea id="f-focus" rows="3" placeholder="${esc(first || "Seed to Series A fintechs in India hiring their first sales rep")}">${esc(first)}</textarea></label>
      <label class="field">Number of companies<input type="number" id="f-count" min="1" max="50" value="10" style="width:100px"></label>
      <div class="callout blue">${icon("info")}<div>Uses company websites, job boards, Google News, the YC directory, Hacker News hiring threads and web search. Nothing is sent.</div></div>`,
    onConfirm: async (root) => { await startJob("find_buyers", { count: Number($("#f-count", root).value) || 10, focus: $("#f-focus", root).value }); },
  });
  const root = $("#modal-root");
  $$("[data-sugg]", root).forEach((b) => (b.onclick = () => {
    const box = $("#f-focus", root);
    box.value = sug.suggestions[Number(b.dataset.sugg)].query;
    $$("[data-sugg]", root).forEach((x) => x.classList.toggle("active", x === b));
    box.focus();
  }));
  if (sug.suggestions.length) $("[data-sugg='0']", root)?.classList.add("active");
  $$("[data-close-modal]", root).forEach((a) => (a.onclick = () => (root.innerHTML = "")));
}

function addCompaniesModal() {
  modal({
    title: "Add companies", art: "plus", intro: "One per line: a website, a business name (for businesses without a website) or an email.", confirm: "Add companies",
    body: `<textarea id="a-domains" rows="7" placeholder="acme.com&#10;Sharma Sweets, Pune&#10;owner.bakery@gmail.com" aria-label="Companies"></textarea>
      <label class="check"><input type="checkbox" id="a-research" checked> Research them and draft emails now</label>`,
    onConfirm: async (root) => {
      const research = $("#a-research", root).checked;
      const domains = $("#a-domains", root).value;
      if (research && !(await ensureEngine())) throw cancelled;
      const r = await api("/api/accounts", { method: "POST", body: { domains, research } });
      toast(`Added ${plural(r.added.length, "company", "companies")}${r.skipped_suppressed.length ? `, skipped ${r.skipped_suppressed.length} on your do-not-contact list` : ""}`);
      render();
    },
  });
}

function importModal() {
  modal({
    title: "Import from CSV", art: "upload", intro: "From Apollo, Sales Navigator, Clay, your CRM or a spreadsheet.", confirm: "Import",
    body: `<input type="file" id="i-file" accept=".csv,text/csv" aria-label="CSV file">
      <div class="callout blue">${icon("info")}<div>Columns are matched by name: company, website or domain, first and last name (or name), title, email, linkedin. Businesses without a website and Gmail addresses are fine.</div></div>
      <label class="check"><input type="checkbox" id="i-research"> Research the imported companies and draft emails now</label>`,
    onConfirm: async (root) => {
      const file = $("#i-file", root).files[0];
      if (!file) throw new Error("Choose a CSV file.");
      const r = await api("/api/import", { method: "POST", body: { csv: await file.text() } });
      toast(`Imported ${plural(r.accounts, "company", "companies")} and ${plural(r.contacts, "person", "people")}${r.skipped ? `. Skipped ${plural(r.skipped, "row")} without a company domain` : ""}`);
      if ($("#i-research", root).checked && r.domains.length) await startJob("research", { domains: r.domains });
      render();
    },
  });
}

// ── record drawer ──────────────────────────────────────────────────────
function closeDrawer() { const d = $("#drawer"); d.hidden = true; d.innerHTML = ""; }

async function openAccount(domain) {
  const d = $("#drawer");
  d.hidden = false;
  d.innerHTML = `<div class="drawer-top"><span style="color:var(--text-2)">Loading…</span></div>`;
  try {
    const a = await api(`/api/accounts/${encodeURIComponent(domain)}`);
    const statuses = ["new", "researched", "ready", "contacted", "replied", "meeting", "won", "lost", "disqualified"];
    const prop = (ic, k, v) => `<div class="k">${icon(ic, "sm")}${k}</div><div>${v || '<span style="color:var(--text-3)">Empty</span>'}</div>`;
    d.innerHTML = `
      <div class="drawer-top">${icon("building")}<span style="font-weight:600">Company</span>
        <span style="margin-left:auto"></span>
        ${a.deck && a.deck.pdf_path ? `<a class="btn sm" href="/api/decks/${encodeURIComponent(a.domain)}" target="_blank">${icon("file", "sm")}Deck</a>` : ""}
        <button class="btn sm" data-job="deck">${icon("file", "sm")}${a.deck ? "Rebuild deck" : "Build deck"}</button>
        <button class="btn sm primary" data-job="research">${icon("sparkles", "sm")}Research & write</button>
        <button class="btn ghost icon" data-x aria-label="Close">${icon("x")}</button></div>
      <div class="drawer-body">
        <div class="rec-head">${logo(a.domain, a.name, "lg")}<div><h2>${esc(a.name || a.domain)}</h2>${hasSite(a.domain) ? `<a href="https://${esc(a.domain)}" target="_blank" rel="noopener" style="display:inline-flex;gap:4px;align-items:center">${esc(a.domain)}${icon("external", "sm")}</a>` : '<span style="color:var(--text-2)">No website · researched from listings and social pages</span>'}</div></div>
        <div class="feedback-row">
          <span>Is this a good lead?</span>
          <button class="btn sm ${a.feedback === "good" ? "fb-on-good" : ""}" data-fb="good">${icon("check", "sm")}Good fit</button>
          <button class="btn sm ${a.feedback === "bad" ? "fb-on-bad" : ""}" data-fb="bad">${icon("x", "sm")}Not a fit</button>
          ${a.feedback_reason ? `<span class="fb-reason">“${esc(a.feedback_reason)}”</span>` : ""}
        </div>
        ${a.region ? `<div class="callout region-note" style="margin-top:14px">${icon("info")}<div><b>${esc(a.region.label)}.</b> ${esc(a.region.rule)}</div></div>` : ""}
        ${a.why_now ? `<div class="context">${icon("sparkles")}<div><b>Why now.</b> ${esc(a.why_now)}${a.angle ? `<br><b>Angle.</b> ${esc(a.angle)}` : ""}</div></div>` : ""}
        <div class="props">
          ${prop("tag", "Stage", `<select id="d-status" style="width:auto;height:28px;padding-top:0;padding-bottom:0" aria-label="Stage">${statuses.map((s) => `<option value="${s}" ${s === a.status ? "selected" : ""}>${cap(s)}</option>`).join("")}</select>`)}
          ${prop("target", "Fit", a.fit_score != null ? score(a.fit_score) : "")}
          ${prop("zap", "Intent", a.intent_score != null ? score(a.intent_score) : "")}
          ${prop("hash", "Industry", esc(a.industry || ""))}
          ${prop("users", "Employees", esc(a.employees || ""))}
          ${prop("pin", "Location", esc(a.location || ""))}
          ${prop("globe", "Source", esc(a.source || ""))}
        </div>
        ${a.deck && a.deck.error ? `<div class="callout" style="margin-top:14px">${icon("alert")}${esc(a.deck.error)}</div>` : ""}
        <div class="sect">${icon("sparkles", "sm")}Ideas offered <span class="n">${(a.ideas || []).length}</span><button class="btn sm" data-job="ideas" style="margin-left:auto">${icon("refresh", "sm")}${(a.ideas || []).length ? "New ideas" : "Write ideas"}</button></div>
        ${(a.ideas || []).length ? ideaCards(a.ideas, { showPick: true }) : '<div style="color:var(--text-2)">No ideas yet. The writer creates three when it drafts emails.</div>'}
        <div class="sect">${icon("file", "sm")}Research</div>
        ${a.research ? md(a.research) : `<div style="color:var(--text-2)">Not researched yet. Click Research & write.</div>`}
        <div class="sect">${icon("zap", "sm")}Signals <span class="n">${a.signals.length}</span></div>
        ${a.signals.length ? `<div class="feed">${a.signals.map((s) => `<div>${badge(s.type, null, false)}<div style="min-width:0"><div class="t">${s.url ? `<a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title)}</a>` : esc(s.title)}</div><div class="s">${[s.detail, s.source, s.observed_at && day(s.observed_at)].filter(Boolean).map(esc).join(" · ")}</div></div></div>`).join("")}</div>` : '<div style="color:var(--text-2)">None saved yet.</div>'}
        <div class="sect">${icon("users", "sm")}People <span class="n">${a.contacts.length}</span><button class="btn sm" data-add-person style="margin-left:auto">${icon("plus", "sm")}Add person</button></div>
        ${a.contacts.length ? `<div class="feed">${a.contacts.map((c) => `<div>${avatar(fullName(c), "lg")}<div style="min-width:0;flex:1"><div class="t">${esc(fullName(c))} <span class="s">${esc(c.title || "")}</span></div>
          <div class="s">${c.email ? esc(c.email) : "No email"}${c.email && c.email_status === "public" && sourceUrl(c.notes) ? ` · found on ${sourceLink(c.notes)}` : c.email_status === "guessed" ? " · guessed from the company's email pattern" : ""}${c.linkedin_url ? ` · <a href="${esc(c.linkedin_url)}" target="_blank" rel="noopener">LinkedIn</a>` : ""}</div></div>
          <div style="display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end">${c.email_status ? badge(c.email_status, `${cap(c.email_status)} email`, false) : ""}${badge(c.status)}${state.overview?.bettercontact && c.email_status !== "verified" && c.email_status !== "public" && c.last_name ? `<button class="btn sm" data-verify="${c.id}" title="Paid: BetterContact charges only if it finds a usable email">${icon("checkCircle", "sm")}Verify</button>` : ""}</div></div>`).join("")}</div>` : '<div style="color:var(--text-2)">Nobody yet.</div>'}
        ${a.contacts.some((c) => c.drafts.some((x) => x.status === "draft")) ? `<div style="margin-top:12px"><a class="btn primary sm" href="#/review" data-review="${a.contacts.find((c) => c.drafts.some((x) => x.status === "draft")).id}">${icon("inbox", "sm")}Review drafts</a></div>` : ""}
        ${a.events.length ? `<div class="sect">${icon("clock", "sm")}History</div><div class="feed">${a.events.map((e) => `<div><div class="s" style="width:56px;flex:none">${day(e.at)}</div><div>${esc(e.type)} <span class="s">${esc(e.detail || "")}</span></div></div>`).join("")}</div>` : ""}
        <div style="margin-top:28px"><button class="btn sm danger" data-suppress>${icon("ban", "sm")}Never contact ${esc(a.name || a.domain)}</button></div>
      </div>`;
    $("[data-x]", d).onclick = closeDrawer;
    wirePicks(d, () => openAccount(a.domain));
    wireVerify(d, () => openAccount(a.domain));
    $$("[data-fb]", d).forEach((b) => (b.onclick = () => {
      const verdict = b.dataset.fb;
      if (a.feedback === verdict) {  // clicking the active one clears it
        return api(`/api/accounts/${encodeURIComponent(a.domain)}/feedback`, { method: "POST", body: { verdict: null } }).then(() => { openAccount(a.domain); if (state.page === "leads") render(); });
      }
      modal({
        title: verdict === "good" ? "Good fit: why?" : "Not a fit: why?", art: verdict === "good" ? "check" : "x",
        intro: "Optional, but a reason teaches the agents the most. They read every verdict before looking for new leads.",
        body: `<input type="text" id="fb-reason" placeholder="${verdict === "good" ? "e.g. exactly the size and stage we sell to" : "e.g. too big, already has an in-house video team"}">`,
        confirm: verdict === "good" ? "Save: good fit" : "Save: not a fit",
        onConfirm: async (root) => {
          await api(`/api/accounts/${encodeURIComponent(a.domain)}/feedback`, { method: "POST", body: { verdict, reason: $("#fb-reason", root).value } });
          toast(verdict === "good" ? "Noted: the agents will look for more like this." : "Noted: removed from your active leads, and the agents will avoid this pattern.");
          openAccount(a.domain); if (state.page === "leads") render();
        },
      });
    }));
    $("#d-status", d).onchange = async (e) => {
      try { await api(`/api/accounts/${encodeURIComponent(a.domain)}/status`, { method: "POST", body: { status: e.target.value } }); toast("Stage updated"); if (state.page === "leads") render(); } catch (err) { fail(err); }
    };
    $$("[data-job]", d).forEach((b) => (b.onclick = async () => { try { await startJob(b.dataset.job, { domains: [a.domain] }); } catch (e) { fail(e); } }));
    $("[data-add-person]", d).onclick = () => addPersonModal(a);
    const rv = $("[data-review]", d);
    if (rv) rv.onclick = () => { state.reviewTab = "draft"; state.reviewContact = Number(rv.dataset.review); };
    $("[data-suppress]", d).onclick = () => modal({
      title: `Never contact ${a.name || a.domain}?`, art: "ban", intro: "Adds the whole domain to your do-not-contact list and cancels its pending emails.", confirm: "Never contact", danger: true,
      onConfirm: async () => { await api(`/api/accounts/${encodeURIComponent(a.domain)}/suppress`, { method: "POST", body: {} }); toast("Added to your do-not-contact list"); closeDrawer(); render(); },
    });
  } catch (e) { d.innerHTML = `<div class="drawer-body"><div class="callout red">${icon("alert")}${esc(e.message)}</div></div>`; }
}

function addPersonModal(a) {
  modal({
    title: `Add a person at ${a.name || a.domain}`, art: "user", confirm: "Add person",
    intro: "Any email works, including a @gmail.com address you own for testing.",
    body: `
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
        <label class="field">First name<input type="text" id="p-first" autocomplete="off"></label>
        <label class="field">Last name<input type="text" id="p-last" autocomplete="off"></label>
      </div>
      <label class="field">Title<input type="text" id="p-title" placeholder="Head of Growth" autocomplete="off"></label>
      <label class="field">Email<input type="email" id="p-email" placeholder="name@gmail.com" autocomplete="off"></label>
      <label class="check"><input type="checkbox" id="p-write" checked> Draft an email sequence for them now</label>`,
    onConfirm: async (root) => {
      await api(`/api/accounts/${encodeURIComponent(a.domain)}/contacts`, { method: "POST", body: {
        first_name: $("#p-first", root).value, last_name: $("#p-last", root).value,
        title: $("#p-title", root).value, email: $("#p-email", root).value,
      } });
      toast("Person added");
      if ($("#p-write", root).checked) await startJob("write", { domains: [a.domain] });
      openAccount(a.domain);
      if (state.page === "leads") render();
    },
  });
}

// ── ideas (the menu a prospect picks from) ──────────────────────────────
// "…static images (https://www.producthunt.com/products/x)" -> "…static images · producthunt.com" (a link)
function sourcesAsLinks(text) {
  return esc(text).replace(/\s*\(?(https?:\/\/[^\s)]+)\)?/g, (_, url) => {
    let host = url;
    try { host = new URL(url.replace(/&amp;/g, "&")).hostname.replace(/^www\./, ""); } catch { /* keep raw */ }
    return ` · <a href="${url}" target="_blank" rel="noopener">${host}</a>`;
  });
}

function ideaCards(ideas, { pickFor = null, showPick = false } = {}) {
  return `<div class="ideas-grid">${ideas.map((i) => `
    <div class="idea ${i.status === "picked" ? "picked" : ""}">
      <div class="idea-top"><span class="idea-letter">${esc(i.letter)}</span><b>${esc(i.title)}</b>${i.status === "picked" ? badge("approved", "Picked", false) : ""}</div>
      ${i.pitch ? `<div class="idea-pitch">${esc(i.pitch)}</div>` : ""}
      ${i.why ? `<div class="idea-why">${icon("zap", "sm")}<span>${sourcesAsLinks(i.why)}</span></div>` : ""}
      <div class="idea-foot">${i.format ? `<span class="badge">${esc(i.format)}</span>` : ""}
        ${showPick && i.status !== "picked" ? `<button class="btn sm" data-pick-idea="${i.id}" ${pickFor ? `data-pick-contact="${pickFor}"` : ""}>${icon("check", "sm")}Mark as picked</button>` : ""}</div>
    </div>`).join("")}</div>`;
}

function wirePicks(root, after) {
  $$("[data-pick-idea]", root).forEach((b) => (b.onclick = async () => {
    try {
      const r = await api(`/api/ideas/${b.dataset.pickIdea}/pick`, { method: "POST", body: { contact_id: b.dataset.pickContact || null } });
      toast(`Marked idea ${esc(r.picked.letter)} as picked: “${esc(r.picked.title)}”`);
      after?.();
    } catch (e) { fail(e); }
  }));
}

// ── Review: inbox + composer ───────────────────────────────────────────
PAGES.review = async () => {
  const tab = state.reviewTab;
  const { drafts, ideas } = await api(`/api/drafts?status=${tab}`);
  state.reviewIdeas = ideas || {};
  const groups = new Map();
  for (const d of drafts) {
    if (!groups.has(d.contact_id)) groups.set(d.contact_id, []);
    groups.get(d.contact_id).push(d);
  }
  if (!groups.has(state.reviewContact)) state.reviewContact = groups.keys().next().value ?? null;
  const passing = drafts.filter((d) => d.status === "draft" && (!d.lint || d.lint.ok) && d.email);
  setTop("inbox", "Review", "", tab === "draft" && passing.length ? `<button class="btn" id="approve-all">${icon("check")}Approve ${plural(passing.length, "clean email")}</button>` : "");
  const tabs = [["draft", "To review"], ["approved", "Approved"], ["rejected", "Rejected"], ["sent", "Sent"]];
  main.innerHTML = `
    <div class="split">
      <div class="queue">
        <div class="queue-tabs"><div class="seg" role="tablist">${tabs.map(([k, l]) => `<button role="tab" data-rtab="${k}" class="${k === tab ? "active" : ""}" aria-selected="${k === tab}">${l}</button>`).join("")}</div></div>
        ${[...groups.values()].map((steps) => {
          const c = steps[0];
          const issues = steps.reduce((n, s) => n + (s.lint?.fails || 0), 0);
          const warns = steps.reduce((n, s) => n + (s.lint?.warnings || 0), 0);
          return `<div class="qitem ${c.contact_id === state.reviewContact ? "active" : ""}" data-contact="${c.contact_id}" tabindex="0" role="button">
            ${avatar(fullName(c), "lg")}
            <div class="who">${esc(fullName(c))}</div>
            <div>${issues ? badge("rejected", `${issues} to fix`, false) : warns ? badge("guessed", plural(warns, "tip"), false) : badge("approved", plural(steps.length, "email"), false)}</div>
            <div class="co">${logo(c.domain, c.company)}${esc(c.company || c.domain)}</div>
            <div class="subj">${esc((steps.find((s) => s.step === 1) || steps[0]).subject || (steps[0].body || "").slice(0, 60))}</div>
          </div>`;
        }).join("") || `<div class="empty" style="padding:48px 16px"><div class="art">${icon("inbox")}</div><h3>${tab === "draft" ? "All caught up" : "Nothing here"}</h3><p>${tab === "draft" ? "New sequences appear here when the agents finish writing." : ""}</p></div>`}
      </div>
      <div class="composer" id="composer"></div>
    </div>`;
  $$("[data-rtab]").forEach((b) => (b.onclick = () => { state.reviewTab = b.dataset.rtab; state.reviewContact = null; render(); }));
  $$(".qitem").forEach((el) => {
    const open = () => { state.reviewContact = Number(el.dataset.contact); $$(".qitem").forEach((q) => q.classList.toggle("active", q === el)); renderComposer(groups.get(state.reviewContact)); };
    el.onclick = open;
    el.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } };
  });
  const all = $("#approve-all");
  if (all) all.onclick = () => modal({
    title: `Approve ${plural(passing.length, "email")}?`, art: "check", intro: "Only emails without issues to fix are included. They wait on the Outreach page until you send them.", confirm: "Approve",
    onConfirm: async () => { const r = await api("/api/drafts/bulk", { method: "POST", body: { ids: passing.map((d) => d.id), action: "approve" } }); toast(`Approved ${plural(r.done.length, "email")}`); refreshOverview(); render(); },
  });
  renderComposer(groups.get(state.reviewContact));
};

function checksHtml(lint) {
  if (!lint) return "";
  const issues = lint.issues || [];
  const rows = issues.map((i) => `<div class="row ${i.level}">${icon(i.level === "FAIL" ? "x" : "alert", "sm")}<span>${esc(i.message)}</span></div>`).join("");
  return `${rows || `<div class="row OK">${icon("check", "sm")}<span>Reads clean: no spam triggers, AI phrases or formatting issues.</span></div>`}<div class="meta">${lint.words} words</div>`;
}

function renderComposer(steps) {
  const box = $("#composer");
  if (!box) return;
  if (!steps) { box.innerHTML = `<div class="empty"><div class="art">${icon("mail")}</div><h3>Select a person</h3><p>Their email sequence opens here.</p></div>`; return; }
  const c = steps[0];
  const stepKey = state.reviewStep[c.contact_id];
  const s = steps.find((x) => x.id === stepKey) || steps[0];
  const subject1 = (steps.find((x) => x.step === 1) || {}).subject || c.first_subject || "";
  const editable = s.status !== "sent";
  const pending = steps.filter((x) => x.status === "draft");
  box.innerHTML = `<div class="composer-inner">
    <div class="comp-head">${avatar(fullName(c), "lg")}
      <div><h2>${esc(fullName(c))}</h2><div class="meta">${esc(c.title || "")}${c.title ? " at" : ""} ${logo(c.domain, c.company)} ${esc(c.company || c.domain)}</div></div>
      <div class="actions"><button class="btn sm" data-open="${esc(c.domain)}">${icon("building", "sm")}Company</button>
        ${c.has_deck ? `<a class="btn sm" href="/api/decks/${encodeURIComponent(c.domain)}" target="_blank">${icon("file", "sm")}Deck</a>` : ""}
        ${pending.length > 1 ? `<button class="btn sm" data-approve-all>${icon("check", "sm")}Approve all ${pending.length}</button>` : ""}</div>
    </div>
    ${c.why_now ? `<div class="context">${icon("sparkles")}<div><b>Why now.</b> ${esc(c.why_now)}</div></div>` : '<div style="height:14px"></div>'}
    ${(() => {
      const ideas = (state.reviewIdeas || {})[c.domain] || [];
      return `<div class="ideas-block">
        <div class="ideas-head">${icon("sparkles", "sm")}<b>Ideas offered</b><span>${ideas.length ? "they reply with a letter, then you make it" : "no ideas yet for this company"}</span>
          <button class="btn sm" data-new-ideas style="margin-left:auto">${icon("refresh", "sm")}${ideas.length ? "New ideas" : "Write ideas"}</button></div>
        ${ideas.length ? ideaCards(ideas) : ""}
      </div>`;
    })()}
    <div class="steps" role="tablist">${steps.map((x) => `<button role="tab" data-step="${x.id}" class="${x.id === s.id ? "active" : ""}" aria-selected="${x.id === s.id}">${x.kind === "reply" ? "Your reply" : x.channel === "linkedin" ? "LinkedIn note" : `Email ${x.step}`}<span class="day">${x.kind === "reply" ? "answer" : x.channel === "linkedin" ? "manual" : x.step === 1 ? "Day 0" : `Day ${x.send_after_days}`}</span>${x.status !== "draft" ? badge(x.status, null, false) : ""}</button>`).join("")}</div>
    <div class="mail" data-draft="${s.id}">
      ${s.channel === "email" ? `
        <div class="mail-row"><span class="k">To</span><span class="chip-person" style="white-space:nowrap">${avatar(fullName(c))}${esc(fullName(c))}</span><span style="color:var(--text-2)">${esc(c.email || "no email")}</span>${c.email_status ? badge(c.email_status, `${cap(c.email_status)} email`, false) : ""}</div>
        ${c.region ? `<div class="mail-row mail-note">${icon("info", "sm")}<span><b>${esc(c.region.label)}:</b> ${esc(c.region.rule)}</span></div>` : ""}
        ${c.email && c.email_status === "public" && sourceUrl(c.contact_notes) ? `<div class="mail-row mail-note">${icon("checkCircle", "sm")}<span>Published address, found on ${sourceLink(c.contact_notes)}</span></div>`
          : c.email_status === "guessed" ? `<div class="mail-row mail-note warn">${icon("alert", "sm")}<span>Guessed from the company's email pattern, so it may bounce.</span><button class="btn sm" data-find-email style="margin-left:auto">${icon("search", "sm")}Look for a published email</button>${state.overview?.bettercontact ? `<button class="btn sm" data-verify="${c.contact_id}" title="Paid: BetterContact charges only if it finds a usable email">${icon("checkCircle", "sm")}Verify (BetterContact)</button>` : ""}</div>` : ""}
        <div class="mail-row"><span class="k">Subject</span>${s.step === 1 ? `<input type="text" data-subject value="${esc(s.subject || "")}" placeholder="Subject" aria-label="Subject" ${editable ? "" : "disabled"}>` : `<span style="font-weight:600">Re: ${esc(subject1)}</span><span style="color:var(--text-2);margin-left:auto;font-size:12px">same thread</span>`}</div>`
        : `<div class="mail-row"><span class="k">LinkedIn</span><span>Connection note to ${esc(fullName(c))}. Send it yourself.</span></div>`}
      <textarea data-body aria-label="Email body" ${editable ? "" : "disabled"}>${esc(s.body)}</textarea>
      <div class="mail-foot">
        ${c.has_deck && s.channel === "email" ? `<label class="check" style="font-size:12.5px"><input type="checkbox" data-attach ${s.attach_deck ? "checked" : ""} ${editable ? "" : "disabled"}>${icon("file", "sm")}Attach deck</label>` : ""}
        ${s.channel === "linkedin" ? `<button class="btn sm" data-copy>${icon("copy", "sm")}Copy</button>` : ""}
        <span class="grow"></span>
        ${s.channel === "email" && editable ? `<button class="btn sm ghost" data-test>${icon("send", "sm")}Send test to me</button>` : ""}
        ${s.status === "draft" ? `<button class="btn sm" data-reject>${icon("x", "sm")}Reject</button><button class="btn sm primary" data-approve>${icon("check", "sm")}Approve</button>` : ""}
        ${["approved", "rejected"].includes(s.status) ? `<button class="btn sm" data-reopen>${icon("refresh", "sm")}Back to draft</button>` : ""}
      </div>
    </div>
    <div class="checks" data-lint>${checksHtml(s.lint)}</div>
  </div>`;

  const el = $(".mail", box);
  const ta = $("[data-body]", el);
  const fit = () => { ta.style.height = "auto"; ta.style.height = `${ta.scrollHeight + 2}px`; };
  fit(); ta.addEventListener("input", fit);
  const save = async (extra = {}) => {
    const body = { body: ta.value, ...extra };
    const subj = $("[data-subject]", el);
    if (subj) body.subject = subj.value;
    const r = await api(`/api/drafts/${s.id}`, { method: "PUT", body });
    s.body = body.body; if (subj) s.subject = body.subject;
    s.lint = r.lint;
    $("[data-lint]", box).innerHTML = checksHtml(r.lint);
    return r;
  };
  $$("[data-body], [data-subject]", el).forEach((input) => {
    input.onchange = async () => {
      try { const r = await save(); if (r.status === "draft" && s.status !== "draft") { toast("Edited, so it needs your approval again."); render(); } } catch (e) { fail(e); }
    };
  });
  const act = async (action) => {
    try {
      await save();
      const r = await api(`/api/drafts/${s.id}/${action}`, { method: "POST", body: {} });
      if (r.refused?.length) return toast(esc(r.refused[0].reason), "error");
      const next = steps.find((x) => x.status === "draft" && x.id !== s.id);
      if (next) state.reviewStep[c.contact_id] = next.id;
      refreshOverview(); render();
    } catch (e) { fail(e); }
  };
  const on = (sel, fn) => { const b = $(sel, box); if (b) b.onclick = fn; };
  on("[data-approve]", () => act("approve"));
  on("[data-reject]", () => act("reject"));
  on("[data-reopen]", () => act("reopen"));
  on("[data-copy]", () => { navigator.clipboard.writeText(ta.value); toast("Copied"); });
  on("[data-test]", async () => { try { await save(); const r = await api(`/api/drafts/${s.id}/test`, { method: "POST", body: {} }); toast(`Test sent to ${esc(r.sent_to)}`); } catch (e) { fail(e); } });
  on("[data-open]", () => openAccount(c.domain));
  on("[data-new-ideas]", async () => { try { await startJob("ideas", { domains: [c.domain] }); } catch (e) { fail(e); } });
  on("[data-find-email]", async () => { try { await startJob("emails", { domains: [c.domain] }); } catch (e) { fail(e); } });
  wireVerify(el, () => render());
  on("[data-approve-all]", async () => {
    try {
      const r = await api("/api/drafts/bulk", { method: "POST", body: { ids: pending.map((x) => x.id), action: "approve" } });
      if (r.refused.length) toast(`${plural(r.refused.length, "email")} not approved: ${esc(r.refused[0].reason)}`, "error");
      refreshOverview(); render();
    } catch (e) { fail(e); }
  });
  const attach = $("[data-attach]", el);
  if (attach) attach.onchange = async () => { try { await save({ attach_deck: attach.checked }); s.attach_deck = attach.checked ? 1 : 0; toast(attach.checked ? "Deck will be attached" : "Deck removed"); } catch (e) { fail(e); } };
  $$("[data-step]", box).forEach((b) => (b.onclick = () => { state.reviewStep[c.contact_id] = Number(b.dataset.step); renderComposer(steps); }));
}

// ── Outreach ───────────────────────────────────────────────────────────
PAGES.outreach = async () => {
  const { contacts, mail, scheduler, ideas: outreachIdeas = {} } = await api("/api/outreach");
  const o = state.overview || {};
  const queued = contacts.reduce((n, c) => n + c.steps.filter((s) => s.status === "approved").length, 0);
  const sentTotal = contacts.reduce((n, c) => n + c.steps.filter((s) => s.status === "sent").length, 0);
  const stages = ["drafted", "contacted", "replied", "meeting", "bounced", "unsubscribed", "disqualified"];
  setTop("send", "Outreach", "", `
    <button class="btn" id="replies" ${mail.app_password_set ? "" : "disabled"}>${icon("refresh")}Check replies</button>
    <button class="btn primary" id="send" ${queued ? "" : "disabled"}>${icon("send")}Review & send</button>`);
  main.innerHTML = `<div class="page">
    <div id="send-progress"></div>
    ${!mail.app_password_set ? `<div class="callout" style="margin-bottom:16px">${icon("alert")}<div>Connect Gmail in <a href="#/settings">Settings</a> to send from here, or <a href="/api/export/sequences?status=approved">download approved sequences</a> for Instantly, Smartlead or Lemlist.</div></div>`
      : !mail.send_enabled ? `<div class="callout" style="margin-bottom:16px">${icon("alert")}<div>Sending is off. Turn it on in <a href="#/settings">Settings</a> when you're ready.</div></div>` : ""}
    ${mail.test_recipient ? `<div class="callout blue" style="margin-bottom:16px">${icon("info")}<div><b>Test mode is on.</b> Every email and follow-up goes to ${esc(mail.test_recipient)} instead of the prospect. Clear it in <a href="#/settings">Settings</a> to go live.</div></div>` : ""}
    <div class="stats">
      <div class="card stat"><div class="label">${icon("clock", "sm")}Waiting to send</div><div class="value">${queued}</div><div class="foot">approved emails and follow-ups</div></div>
      <div class="card stat"><div class="label">${icon("send", "sm")}Sent today</div><div class="value">${mail.sent_today}<small> / ${mail.daily_limit}</small></div>${mail.warmup && !mail.warmup.done ? `<div class="foot">warm-up day ${mail.warmup.day}: limit grows to ${mail.daily_limit_max}</div>` : ""}<div class="meter"><i style="width:${Math.min(100, (mail.sent_today / (mail.daily_limit || 1)) * 100)}%"></i></div></div>
      <div class="card stat"><div class="label">${icon("reply", "sm")}Replies</div><div class="value">${o.replied ?? 0}</div><div class="foot">${sentTotal ? `${Math.round(((o.replied || 0) / sentTotal) * 100)}% reply rate` : "no emails sent yet"}</div></div>
      <div class="card stat"><div class="label">${icon("refresh", "sm")}Auto follow-ups</div><div class="value" style="font-size:16px;margin-top:10px">${scheduler.last_run ? `Checked ${ago(scheduler.last_run)}` : mail.send_enabled ? "Waiting" : "Off"}</div><div class="foot">${scheduler.last_result?.error ? esc(scheduler.last_result.error) : scheduler.last_result?.overdue_waiting ? `${scheduler.last_result.overdue_waiting} late follow-up(s) wait for you: use Review & send` : "every 30 min while open, never late ones"}</div></div>
    </div>
    <div class="card" style="margin-top:16px;overflow:hidden">
      <div class="card-head">${icon("users")}People in sequence<div class="actions"><a class="btn ghost sm" href="/api/export/sequences?status=approved">${icon("download", "sm")}Export</a></div></div>
      ${contacts.length ? `<div class="table-wrap"><table class="grid">
        <thead><tr><th style="padding-left:16px;width:auto;border-right:1px solid var(--border)"><span class="h">${icon("user", "sm")}Person</span></th><th><span class="h">${icon("mail", "sm")}Sequence</span></th><th><span class="h">${icon("clock", "sm")}Last sent</span></th><th><span class="h">${icon("tag", "sm")}Stage</span></th></tr></thead>
        <tbody>${contacts.map((c) => `
          <tr>
            <td style="padding-left:16px;width:auto;border-right:1px solid var(--border);white-space:normal;max-width:460px"><div class="company">${avatar(fullName(c))}<div style="min-width:0"><div class="name">${esc(fullName(c))}</div><div class="domain">${esc(c.company || c.domain)} · ${esc(c.email || "")}</div></div></div>
              ${replyBlock(c, outreachIdeas[c.domain] || [])}</td>
            <td><div style="display:flex;gap:6px;flex-wrap:wrap">${c.steps.map((s) => { const n = s.kind === "reply" ? "Reply" : s.step; return s.status === "sent" ? badge("sent", `${n} · Sent ${day(s.sent_at)}`) : s.due ? badge("ready", `${n} · Due ${day(s.due)}`) : badge(s.status, `${n} · ${cap(s.status)}`); }).join("")}</div></td>
            <td style="color:var(--text-2)">${c.last_sent ? ago(c.last_sent) : "–"}</td>
            <td><select data-stage="${c.id}" style="width:auto;height:28px;padding-top:0;padding-bottom:0" aria-label="Stage for ${esc(fullName(c))}">${[c.status, ...stages.filter((s) => s !== c.status)].map((s) => `<option value="${s}">${cap(s)}</option>`).join("")}</select></td>
          </tr>`).join("")}</tbody></table></div>`
        : `<div class="empty"><div class="art">${icon("send")}</div><h3>Nothing queued</h3><p>Approve emails on the Review page and they wait here until you send them.</p><a class="btn" href="#/review">${icon("inbox")}Go to Review</a></div>`}
    </div></div>`;
  wirePicks(main, () => render());
  $$("[data-draft-reply]").forEach((b) => (b.onclick = async () => {
    try { await startJob("reply", { contact_id: Number(b.dataset.draftReply) }); b.disabled = true; b.textContent = "Drafting…"; } catch (e) { fail(e); }
  }));
  $$("[data-review-contact]").forEach((a) => (a.onclick = () => { state.reviewTab = "draft"; state.reviewContact = Number(a.dataset.reviewContact); }));
  $$("[data-stage]").forEach((s) => (s.onchange = async () => {
    try { await api(`/api/contacts/${s.dataset.stage}/status`, { method: "POST", body: { status: s.value } }); toast("Stage updated"); render(); } catch (e) { fail(e); }
  }));
  $("#replies").onclick = async (e) => {
    e.currentTarget.disabled = true;
    try {
      const r = await api("/api/replies", { method: "POST", body: {} });
      toast(r.replies.length ? `${plural(r.replies.length, "new reply", "new replies")}: ${r.replies.map((x) => esc(x.name || x.email)).join(", ")}` : `No new replies${r.bounces?.length ? `. ${plural(r.bounces.length, "bounce")}` : ""}`);
      refreshOverview(); render();
    } catch (err) { fail(err); }
  };
  $("#send").onclick = sendFlow;
  watchSending();
};

async function watchSending() {
  const box = $("#send-progress");
  if (!box || state.page !== "outreach") return;
  let p;
  try { p = await api("/api/send/status"); } catch { return; }
  if (p.running) {
    const done = (p.sent || 0) + (p.failed || 0);
    const secs = p.next_at ? Math.max(0, Math.round((new Date(p.next_at) - Date.now()) / 1000)) : null;
    box.innerHTML = `<div class="card send-progress">
      <div class="spread"><div style="display:flex;gap:8px;align-items:center"><span class="spin" style="color:var(--primary)"></span>
        <b>Sending ${p.total ? `${Math.min(done + 1, p.total)} of ${p.total}` : "…"}</b>
        <span style="color:var(--text-2)">${secs !== null ? `next email in ${secs}s` : "sending now"} · keep the dashboard window open</span></div>
        <button class="btn sm danger" id="stop-send">${icon("stop", "sm")}Stop</button></div>
      <div class="meter"><i style="width:${p.total ? Math.round((done / p.total) * 100) : 0}%"></i></div>
      ${p.last_error ? `<div style="color:var(--red);font-size:12.5px;margin-top:6px">${esc(p.last_error)}</div>` : ""}
    </div>`;
    $("#stop-send").onclick = async () => { await api("/api/send/stop", { method: "POST", body: {} }); toast("Stopping. The email in progress finishes; the rest stay approved."); };
    state.wasSending = true;
    setTimeout(watchSending, 1000);
  } else {
    box.innerHTML = p.finished_at && state.wasSending ? `<div class="callout ${p.last_error ? "red" : "blue"}" style="margin-bottom:16px">${icon(p.last_error ? "alert" : "checkCircle")}<div>${p.stopped ? "Stopped. " : "Done. "}Sent ${plural(p.sent || 0, "email")}${p.failed ? `, ${p.failed} failed` : ""}.${p.last_error ? ` ${esc(p.last_error)}` : ""}</div></div>` : "";
    if (state.wasSending) { state.wasSending = false; refreshOverview(); setTimeout(() => state.page === "outreach" && render(), 1500); }
  }
}

function replyBlock(c, ideas) {
  const picked = ideas.find((i) => i.status === "picked");
  if (!c.last_reply && !picked) return "";
  const guess = !picked && c.reply_pick ? ideas.find((i) => i.letter === c.reply_pick) : null;
  if (c.reply_kind === "optout") return `<div class="reply">${c.last_reply ? `<div class="reply-text">${icon("reply", "sm")}<span>${esc(c.last_reply.slice(0, 200))}</span></div>` : ""}<div class="reply-pick" style="color:var(--red)">${icon("ban", "sm")}Asked to stop: added to your do-not-contact list automatically.</div></div>`;
  if (c.reply_kind === "not_interested") return `<div class="reply">${c.last_reply ? `<div class="reply-text">${icon("reply", "sm")}<span>${esc(c.last_reply.slice(0, 200))}</span></div>` : ""}<div class="reply-pick">${icon("x", "sm")}Not interested: follow-ups stopped.</div></div>`;
  const replyDraft = (c.steps || []).find((s) => s.kind === "reply" && s.status !== "sent" && s.status !== "cancelled");
  const draftBtn = c.last_reply && c.reply_kind !== "optout"
    ? (replyDraft ? `<a class="btn sm" href="#/review" data-review-contact="${c.id}">${icon("inbox", "sm")}Review your reply</a>`
                  : `<button class="btn sm primary" data-draft-reply="${c.id}">${icon("sparkles", "sm")}Draft reply</button>`) : "";
  return `<div class="reply">
    ${c.last_reply ? `<div class="reply-text">${icon("reply", "sm")}<span>${esc(c.last_reply.slice(0, 280))}${c.last_reply.length > 280 ? "…" : ""}</span></div>` : ""}
    ${draftBtn ? `<div class="reply-pick">${draftBtn}</div>` : ""}
    ${picked ? `<div class="reply-pick ok">${icon("checkCircle", "sm")}<span>Picked <b>${esc(picked.letter)}) ${esc(picked.title)}</b>: this is the one to make</span></div>`
      : guess ? `<div class="reply-pick">${icon("sparkles", "sm")}Looks like they picked <b>${esc(guess.letter)}) ${esc(guess.title)}</b><button class="btn sm primary" data-pick-idea="${guess.id}" data-pick-contact="${c.id}">Confirm</button></div>`
      : ideas.length && c.last_reply ? `<div class="reply-pick">Which idea did they pick? ${ideas.map((i) => `<button class="btn sm" data-pick-idea="${i.id}" data-pick-contact="${c.id}" title="${esc(i.title)}">${esc(i.letter)}</button>`).join("")}</div>` : ""}
  </div>`;
}

async function sendFlow() {
  try {
    const preview = await api("/api/send", { method: "POST", body: { dry_run: true, limit: 50 } });
    const n = preview.sent.length;
    const mail = state.overview?.mail || {};
    const canSend = n && mail.send_enabled;
    modal({
      title: n ? `Send ${plural(n, "email")}` : "Nothing is due yet", art: "send", wide: true,
      intro: n ? `From ${esc(mail.gmail_address || "your Gmail")}. ${preview.daily_budget_left} left in today's limit. ${n > 1 ? `They go out one at a time, about ${Math.round((n - 1) * (mail.gap_seconds ?? 60) / 60)} min in total.` : ""} At most one email per person per send; follow-ups stay spaced out.${preview.sent.some((e) => e.days_late >= 1) ? " <b>Some follow-ups are late</b> because the dashboard was closed. Check they still make sense before sending." : ""}${mail.test_recipient ? ` <b>Test mode:</b> all of these go to ${esc(mail.test_recipient)}.` : ""}` : "Follow-ups wait for their day. Details below.",
      confirm: canSend ? `Send ${plural(n, "email")}` : null, cancel: canSend ? "Cancel" : "Close",
      body: `${preview.sent.map((e) => `<div class="preview"><div class="mail-row"><span class="k">To</span><span>${esc(e.to)}</span>${e.test_mode ? `<span style="color:var(--text-2);font-size:12px">instead of ${esc(e.prospect)}</span>` : ""}${badge(e.test_mode ? "manual" : "", e.test_mode ? `Test · email ${e.step}` : `Email ${e.step}`, false)}${e.days_late >= 1 ? badge("guessed", `${Math.round(e.days_late)} days late`, false) : ""}</div><div class="mail-row"><span class="k">Subject</span><b>${esc(e.subject)}</b></div><pre>${esc(e.preview)}</pre></div>`).join("")}
        ${preview.skipped.length ? `<div><div class="sect" style="margin-top:0">${icon("clock", "sm")}Not now</div>${preview.skipped.map((s) => `<div style="color:var(--text-2);font-size:12.5px">${esc(s.to || "#" + s.draft_id)}: ${esc(s.reason || s.error)}</div>`).join("")}</div>` : ""}
        ${n && !mail.send_enabled ? `<div class="callout">${icon("alert")}<div>Sending is off. Turn it on in <a href="#/settings">Settings</a>, then come back.</div></div>` : ""}`,
      onConfirm: async () => {
        const r = await api("/api/send", { method: "POST", body: { dry_run: false, limit: 50 } });
        if (!r.started) throw new Error(r.error || "Couldn't start sending.");
        const gap = Math.round(preview.gap_seconds ?? state.overview?.mail?.gap_seconds ?? 60);
        toast(`Sending ${plural(n, "email")}${gap ? `, one every ${gap} seconds` : ""}. Keep the dashboard window open until it finishes.`);
        location.hash = "#/outreach"; render();
      },
    });
  } catch (e) { fail(e); }
}

// ── Agent runs ─────────────────────────────────────────────────────────
PAGES.activity = async () => {
  const { jobs } = await api("/api/jobs");
  if (!jobs.some((j) => j.id === state.activeJob)) state.activeJob = jobs[0]?.id ?? null;
  setTop("activity", "Agent runs", "", `<button class="btn primary" id="new-run">${icon("sparkles")}New run</button>`);
  main.innerHTML = jobs.length ? `
    <div class="runs">
      <div class="run-list">${jobs.map((j) => `
        <div class="run ${j.id === state.activeJob ? "active" : ""}" data-job-id="${j.id}" tabindex="0" role="button">
          <div class="t">${esc(j.title)}</div>
          <div class="s">${j.status === "running" ? `<span class="spin" style="color:var(--primary)"></span>Running` : badge(j.status)}<span>${ago(j.created_at)}</span>${j.cost_usd ? `<span>· $${Number(j.cost_usd).toFixed(2)}</span>` : ""}</div>
        </div>`).join("")}</div>
      <div class="run-detail" id="job-detail"></div>
    </div>` : `<div class="empty"><div class="art">${icon("activity")}</div><h3>No runs yet</h3><p>Ask the agents something from Home, or use Find buyers on Leads.</p><a class="btn primary" href="#/home">${icon("sparkles")}Ask the agents</a></div>`;
  $("#new-run").onclick = () => { location.hash = "#/home"; setTimeout(() => $("#ask")?.focus(), 50); };
  $$("[data-job-id]").forEach((el) => {
    const open = () => { state.activeJob = Number(el.dataset.jobId); render(); };
    el.onclick = open;
    el.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } };
  });
  if (state.activeJob) loadJob(state.activeJob);
};

// The final summary is shown in its own card above, so don't repeat it at the end of the steps.
function stepsOnly(j) {
  const log = (j.log || "").trimEnd();
  const result = (j.result || "").trim();
  if (result && log.endsWith(result)) return log.slice(0, log.length - result.length).trimEnd();
  return log;
}

async function loadJob(id) {
  const el = $("#job-detail");
  if (!el || state.page !== "activity" || state.activeJob !== id) return;
  try {
    const j = await api(`/api/jobs/${id}`);
    const running = ["queued", "running"].includes(j.status);
    const old = $(".log", el);
    const atBottom = !old || old.scrollHeight - old.scrollTop - old.clientHeight < 40;
    el.innerHTML = `
      <div class="comp-head"><div><h2>${esc(j.title)}</h2><div class="meta">${running ? `<span class="spin" style="color:var(--primary)"></span>Running` : badge(j.status)}<span>${j.started_at ? `Started ${ago(j.started_at)}` : "Waiting to start"}${j.finished_at ? ` · finished ${ago(j.finished_at)}` : ""}${j.cost_usd ? ` · $${Number(j.cost_usd).toFixed(2)} of usage` : ""}${j.engine ? ` · ${esc(ENGINE_LABELS[j.engine] || j.engine)}` : ""}</span></div></div>
        <div class="actions">${running ? `<button class="btn sm danger" data-cancel>${icon("stop", "sm")}Stop</button>` : ""}</div></div>
      ${j.result ? `<div class="${j.status === "failed" ? "callout red" : "context"}" style="margin:16px 0">${icon(j.status === "failed" ? "alert" : "sparkles")}<div>${md(j.result)}</div></div>` : '<div style="height:16px"></div>'}
      <div class="sect">${icon("activity", "sm")}Steps</div>
      <div class="log">${esc(stepsOnly(j) || (running ? `Starting ${ENGINE_LABELS[j.engine] || "the agent"}…` : "No output."))}</div>
      <details style="margin-top:14px"><summary style="color:var(--text-2);cursor:pointer">Instructions sent to the agent</summary><div class="log" style="margin-top:8px">${esc(j.prompt)}</div></details>`;
    const log = $(".log", el);
    if (atBottom) log.scrollTop = log.scrollHeight;
    const cancel = $("[data-cancel]", el);
    if (cancel) cancel.onclick = async () => { await api(`/api/jobs/${id}/cancel`, { method: "POST", body: {} }); toast("Stopping…"); };
    if (running) setTimeout(() => loadJob(id), 2000);
  } catch (e) { el.innerHTML = `<div class="callout red">${icon("alert")}${esc(e.message)}</div>`; }
}

// ── Settings ───────────────────────────────────────────────────────────
PAGES.settings = async () => {
  const s = await api("/api/settings");
  const f = s.settings;
  const field = (key, extra = "") => {
    const x = f[key];
    if (x.kind === "bool") return `<div class="toggle-row"><div><div style="font-weight:500">${esc(x.label)}</div><div style="color:var(--text-2);font-size:12.5px">${esc(x.help)}</div></div>
      <label class="switch"><input type="checkbox" data-key="${key}" ${x.value === "true" ? "checked" : ""} aria-label="${esc(x.label)}"><span></span></label></div>`;
    if (x.kind === "textarea") return `<label class="field">${esc(x.label)}<span class="help">${esc(x.help)}</span><textarea rows="3" data-key="${key}">${esc(x.value)}</textarea></label>`;
    const type = x.kind === "secret" ? "password" : x.kind === "int" ? "number" : "text";
    const ph = x.kind === "secret" ? (x.is_set ? "Saved. Type to replace" : "xxxx xxxx xxxx xxxx") : "";
    return `<label class="field">${esc(x.label)}<span class="help">${esc(x.help)}</span><input type="${type}" data-key="${key}" value="${esc(x.value)}" ${ph ? `placeholder="${esc(ph)}"` : ""} ${type === "number" ? 'style="width:120px"' : ""} ${extra}></label>`;
  };
  const set = (title, blurb, body) => `<div class="set"><div><h3>${title}</h3><p>${blurb}</p></div><div class="fields">${body}</div></div>`;
  setTop("settings", "Settings", "", `<button class="btn primary" id="save-settings">Save changes</button>`);
  main.innerHTML = `<div class="page settings">
    ${set("Agent engine", "The coding agent that runs the four agents, on your own plan. No API key needed in Leadsmith.",
      `<p class="help" style="margin:0 0 10px">One agent at a time, and only when you connect it. Nothing starts on its own: after you restart the dashboard, connect again.</p>
       ${engineCards(s.engines, s.engine)}
       ${field("AGENT_MODEL", 'id="agent-model"')}`)}
    ${set("Gmail", "Emails go out from your own inbox, so replies land there too.",
      `${field("GMAIL_ADDRESS")}${field("GMAIL_APP_PASSWORD", 'autocomplete="new-password"')}
       <div class="actions"><button class="btn sm" id="test-gmail">${icon("refresh", "sm")}Test connection</button><span id="gmail-result" style="color:var(--text-2);font-size:12.5px"></span></div>`)}
    ${set("Sending", "Nothing leaves without your approval and this switch.", `${field("TEST_RECIPIENT", 'placeholder="you+test@gmail.com"')}${field("SEND_ENABLED")}${field("AUTO_FOLLOWUPS")}${field("DAILY_SEND_LIMIT")}${field("WARMUP")}${field("WARMUP_START")}${field("WARMUP_STEP")}${field("SEND_GAP_SECONDS")}${field("SEND_HOURS")}${field("SEND_WEEKDAYS_ONLY")}${field("PROSPECT_TIMEZONE")}`)}
    ${set("Signature", "Added to the bottom of every email.", `${field("SENDER_NAME")}${field("SENDER_SIGNATURE")}${field("POSTAL_ADDRESS")}${field("OPT_OUT_LINE")}`)}
    ${set("Verified emails", "Optional and paid. Off unless you add a key.",
      `${field("BETTERCONTACT_API_KEY", 'autocomplete="off"')}<div class="help" style="font-size:12.5px;color:var(--text-2)">Get a key at <a href="https://bettercontact.rocks" target="_blank" rel="noopener">bettercontact.rocks</a>. The free sources still run first; this only adds a Verify button on guessed emails.</div>`)}
    ${set("Sending responsibly", "You are the sender. This is a summary, not legal advice.",
      `<div class="legal-list">
        <div><b>US (CAN-SPAM)</b><span>Honest From and subject, your postal address, a working opt-out. Fill in Postal address above.</span></div>
        <div><b>EU / EEA (GDPR)</b><span>Legitimate interest at minimum; some countries expect consent even for B2B. Stop at the first objection.</span></div>
        <div><b>UK (PECR)</b><span>Company addresses are fine with an opt-out. Sole traders need consent.</span></div>
        <div><b>Switzerland, Canada</b><span>Consent rules are stricter. Keep each email one-to-one and relevant to their role.</span></div>
      </div>
      <div class="help" style="font-size:12.5px;color:var(--text-2)">Leads in these regions get a flag in Leads and Review. Opt-out replies and bounces are added to your do-not-contact list automatically. The full notice is in <code>COMPLIANCE.md</code> in the Leadsmith folder.</div>`)}
    ${set("Decks", s.browser ? `PDFs are rendered with ${esc(s.browser.split(/[\\/]/).pop())}.` : "No Chrome or Edge found, so decks are saved as HTML.", field("DECK_COLOR"))}
    ${set("Data", `Everything lives on this computer in <code>${esc(s.workspace)}</code>`,
      `<div class="actions"><a class="btn sm" href="/api/export/sequences">${icon("download", "sm")}Sequences</a><a class="btn sm" href="/api/export/contacts">${icon("download", "sm")}People</a><a class="btn sm" href="/api/export/accounts">${icon("download", "sm")}Companies</a></div>
       <label class="field">Do-not-contact list<span class="help">An email address or a whole domain. Pending emails to them are cancelled.</span>
         <div style="display:flex;gap:8px"><input type="text" id="dnc" placeholder="person@company.com or company.com"><button class="btn" id="dnc-add">Add</button></div></label>`)}
  </div>`;
  $("#save-settings").onclick = async () => {
    // Send only what changed on this page, so a stale tab can't overwrite other settings.
    const body = {};
    $$("[data-key]").forEach((el) => {
      const value = el.type === "checkbox" ? el.checked : el.value;
      if (String(value) !== el.dataset.orig) body[el.dataset.key] = value;
    });
    if (!Object.keys(body).length) return toast("Nothing changed");
    try { await api("/api/settings", { method: "PUT", body }); toast("Settings saved"); refreshOverview(); render(); } catch (e) { fail(e); }
  };
  $$("[data-key]").forEach((el) => (el.dataset.orig = String(el.type === "checkbox" ? el.checked : el.value)));
  const hintFor = s.engines.find((x) => x.key === s.engine.key) || {};
  $("#agent-model").placeholder = hintFor.model_hint ? `Default model · ${hintFor.model_hint}` : "Default model";
  wireEngineCards(main, () => { refreshOverview(); render(); });
  $("#test-gmail").onclick = async () => {
    // Save a changed address or app password first: the test reads the saved settings.
    const body = {};
    ["GMAIL_ADDRESS", "GMAIL_APP_PASSWORD"].forEach((key) => {
      const el = $(`[data-key="${key}"]`);
      if (el && el.value !== el.dataset.orig) body[key] = el.value;
    });
    $("#gmail-result").textContent = Object.keys(body).length ? "Saving and testing…" : "Testing…";
    try {
      if (Object.keys(body).length) {
        await api("/api/settings", { method: "PUT", body });
        Object.keys(body).forEach((key) => { const el = $(`[data-key="${key}"]`); el.dataset.orig = el.value; });
        refreshOverview();
      }
      const r = await api("/api/settings/test-gmail", { method: "POST", body: {} });
      const msg = r.result === "ok" ? "Connected" : r.result === "not configured" ? "Enter your Gmail address and app password first."
        : /535|Username and Password not accepted|BadCredentials/i.test(r.result) ? "Google rejected the login. Use a 16-letter app password, not your normal Gmail password."
        : r.result;
      $("#gmail-result").textContent = msg;
      $("#gmail-result").style.color = r.result === "ok" ? "var(--green)" : "var(--red)";
    } catch (e) { fail(e); }
  };
  $("#dnc-add").onclick = async () => {
    const value = $("#dnc").value.trim();
    if (!value) return;
    try { const r = await api("/api/suppress", { method: "POST", body: { value } }); toast(`${esc(r.suppressed)} will never be contacted`); $("#dnc").value = ""; } catch (e) { fail(e); }
  };
};

function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

// ── boot ───────────────────────────────────────────────────────────────
(async () => {
  await refreshOverview();
  try { const { jobs } = await api("/api/jobs"); jobs.forEach((j) => (state.jobsSeen[j.id] = j.status)); } catch { /* ignore */ }
  render();
  setInterval(refreshOverview, 8000);
  setInterval(pollJobs, 4000);
})();
