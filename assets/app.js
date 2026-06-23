"use strict";

const DATA_URL = "./data/conferences.json";
const SOON_MS = 7 * 24 * 3600 * 1000;   // < 7 days  -> amber
const URGENT_MS = 48 * 3600 * 1000;     // < 48 hrs  -> red

const state = {
  all: [],
  view: "upcoming",   // upcoming | past | all
  tag: "All",
  q: "",
  visible: [],
};

/* ---------- safe localStorage (degrades if unavailable) ---------- */
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* ignore */ } },
};

/* ---------- helpers ---------- */
const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function datedDeadlines(card) {
  return card.deadlines.filter((d) => d.utc).map((d) => ({ ...d, t: new Date(d.utc).getTime() }));
}
function nextDeadline(card, now) {
  const future = datedDeadlines(card).filter((d) => d.t > now).sort((a, b) => a.t - b.t);
  return future[0] || null;
}
function lastDeadline(card) {
  const d = datedDeadlines(card).sort((a, b) => a.t - b.t);
  return d[d.length - 1] || null;
}
function hasAnyDate(card) { return datedDeadlines(card).length > 0; }

// Upcoming = has a future deadline, or not announced yet (TBD).
function isUpcoming(card, now) {
  return !hasAnyDate(card) || datedDeadlines(card).some((d) => d.t > now);
}

function fmtCountdown(ms) {
  if (ms <= 0) return "Passed";
  const s = Math.floor(ms / 1000);
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (d >= 1) return `${d}d ${h}h ${m}m`;
  return `${String(h).padStart(2, "0")}h ${String(m).padStart(2, "0")}m ${String(sec).padStart(2, "0")}s`;
}
function localTimeString(utc) {
  try {
    return new Date(utc).toLocaleString(undefined, {
      year: "numeric", month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit", timeZoneName: "short",
    });
  } catch { return ""; }
}

/* ---------- filtering ---------- */
function computeVisible(now) {
  let cards = state.all.filter((c) => {
    const up = isUpcoming(c, now);
    if (state.view === "upcoming" && !up) return false;
    if (state.view === "past" && up) return false;
    if (state.tag !== "All" && !(c.tags || []).includes(state.tag)) return false;
    if (state.q) {
      const hay = `${c.conf} ${c.edition} ${c.full_name} ${c.place} ${(c.tags || []).join(" ")}`.toLowerCase();
      if (!hay.includes(state.q)) return false;
    }
    return true;
  });
  cards.sort((a, b) => {
    const an = nextDeadline(a, now), bn = nextDeadline(b, now);
    // In "past" view, show most recently passed first.
    if (state.view === "past") {
      return (lastDeadline(b)?.t || 0) - (lastDeadline(a)?.t || 0);
    }
    // Upcoming/all: soonest future deadline first; TBD (no next) last.
    if (an && bn) return an.t - bn.t;
    if (an) return -1;
    if (bn) return 1;
    return (a.primary_utc || "") < (b.primary_utc || "") ? 1 : -1;
  });
  return cards;
}

/* ---------- rendering ---------- */
function cardHTML(card, now) {
  const tags = (card.tags || []).map((t) => `<span class="tag tag-${esc(t)}">${esc(t)}</span>`).join("");
  const nd = nextDeadline(card, now);
  const announced = hasAnyDate(card);

  let cdClass = "live", cdText, cdLabel, localLine = "";
  if (!announced) {
    cdClass = ""; cdText = "TBA"; cdLabel = "Deadline";
  } else if (nd) {
    const ms = nd.t - now;
    cdText = fmtCountdown(ms);
    cdLabel = `${esc(nd.label)} deadline`;
    localLine = `<div class="localtime">${esc(nd.display)} &nbsp;·&nbsp; your time: ${esc(localTimeString(nd.utc))}</div>`;
  } else {
    cdClass = "passed"; cdText = "Passed"; cdLabel = "Closed";
  }

  const dls = card.deadlines.map((d) => {
    const done = d.utc && new Date(d.utc).getTime() <= now;
    const when = d.utc ? esc(d.display) : "TBD";
    return `<li class="${done ? "done" : ""}"><span class="dl-label">${esc(d.label)}</span><span class="dl-when">${done ? "✓ " : ""}${when}</span></li>`;
  }).join("");

  return `<article class="card" id="card-${esc(card.id)}" data-id="${esc(card.id)}">
    <div class="card-head">
      <span class="conf">${esc(card.conf)}</span>
      <span class="edition">${esc(card.edition)}</span>
    </div>
    <div class="tags">${tags}</div>
    <div class="full-name">${esc(card.full_name)}</div>
    <div class="meta">
      <span class="loc">${esc(card.place)}</span>
      <span class="when">${esc(card.date)}</span>
    </div>
    <div class="countdown-wrap">
      <div class="cd-label" id="cl-${esc(card.id)}">${cdLabel}</div>
      <div class="countdown ${cdClass}" id="cd-${esc(card.id)}">${cdText}</div>
      <div id="lt-${esc(card.id)}">${localLine}</div>
    </div>
    <ul class="deadlines">${dls}</ul>
    <div class="card-foot"><a class="cfp-link" href="${esc(card.link)}" target="_blank" rel="noopener">Call for Papers</a></div>
  </article>`;
}

function urgencyClass(card, now) {
  const nd = nextDeadline(card, now);
  if (!hasAnyDate(card)) return "live";
  if (!nd) return "past";
  const ms = nd.t - now;
  if (ms < URGENT_MS) return "urgent";
  if (ms < SOON_MS) return "soon";
  return "live";
}

function render() {
  const now = Date.now();
  state.visible = computeVisible(now);
  const grid = $("#grid");
  grid.innerHTML = state.visible.map((c) => cardHTML(c, now)).join("");
  state.visible.forEach((c) => {
    const el = document.getElementById(`card-${c.id}`);
    if (el) el.classList.add(urgencyClass(c, now), ...(isUpcoming(c, now) ? [] : ["past"]));
  });
  $("#empty").hidden = state.visible.length > 0;
}

// Per-second update without rebuilding the DOM (keeps it smooth).
function tick() {
  const now = Date.now();
  let boundaryCrossed = false;
  for (const c of state.visible) {
    const cd = document.getElementById(`cd-${c.id}`);
    if (!cd) continue;
    if (!hasAnyDate(c)) continue; // TBA, nothing to tick
    const nd = nextDeadline(c, now);
    const card = document.getElementById(`card-${c.id}`);
    if (!nd) {
      if (cd.textContent !== "Passed") { boundaryCrossed = true; }
      continue;
    }
    cd.textContent = fmtCountdown(nd.t - now);
    const cl = document.getElementById(`cl-${c.id}`);
    if (cl) cl.textContent = `${nd.label} deadline`;
    const lt = document.getElementById(`lt-${c.id}`);
    if (lt) lt.innerHTML = `<div class="localtime">${esc(nd.display)} &nbsp;·&nbsp; your time: ${esc(localTimeString(nd.utc))}</div>`;
    // refresh urgency colour
    if (card) {
      card.classList.remove("urgent", "soon", "live");
      card.classList.add(urgencyClass(c, now));
    }
  }
  if (boundaryCrossed) render(); // a deadline just passed -> re-filter/re-sort
}

/* ---------- chrome: tags, toggles, theme ---------- */
function renderTags(tags) {
  const bar = $("#tag-bar");
  const all = ["All", ...tags];
  bar.innerHTML = all.map((t) =>
    `<button class="chip ${t === state.tag ? "active" : ""}" data-tag="${esc(t)}">${esc(t)}</button>`).join("");
  bar.querySelectorAll(".chip").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.tag = btn.dataset.tag;
      bar.querySelectorAll(".chip").forEach((b) => b.classList.toggle("active", b === btn));
      render();
    });
  });
}

function setupControls() {
  $("#search").addEventListener("input", (e) => { state.q = e.target.value.trim().toLowerCase(); render(); });
  document.querySelectorAll(".seg").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.view = btn.dataset.view;
      document.querySelectorAll(".seg").forEach((b) => b.classList.toggle("active", b === btn));
      render();
    });
  });
  const toggle = $("#theme-toggle");
  toggle.addEventListener("click", () => {
    const cur = document.documentElement.getAttribute("data-theme");
    const next = cur === "dark" ? "light" : "dark";
    applyTheme(next);
    store.set("theme", next);
  });
}

function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  const t = $("#theme-toggle");
  if (t) t.textContent = theme === "dark" ? "☀️" : "🌙";
}

function initTheme() {
  const saved = store.get("theme", null);
  const prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  applyTheme(saved || (prefersDark ? "dark" : "light"));
}

/* ---------- boot ---------- */
async function main() {
  initTheme();
  setupControls();
  try {
    const res = await fetch(DATA_URL, { cache: "no-cache" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    state.all = data.conferences || [];
    renderTags(data.tags || []);
    const gen = $("#generated");
    if (gen && data.generated) gen.textContent = new Date(data.generated).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
    render();
    setInterval(tick, 1000);
  } catch (err) {
    $("#grid").innerHTML = `<p class="empty">Could not load deadline data (${esc(err.message)}).<br/>If you just cloned this, run <code>python scripts/update_deadlines.py --offline</code>.</p>`;
  }
}

document.addEventListener("DOMContentLoaded", main);
