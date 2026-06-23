"use strict";

const DATA_URL = "./data/conferences.json";
const SOON_MS = 14 * 24 * 3600 * 1000;   // < 14 days -> amber
const URGENT_MS = 48 * 3600 * 1000;      // < 48 hrs  -> red
const YEAR_MS = 365 * 24 * 3600 * 1000;  // "All" shows the past year of deadlines

const state = { all: [], view: "upcoming", tag: "All", q: "", dateFmt: "iso", visible: [] };

const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* ignore */ } },
};

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const FULL_MONTHS = ["January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December"];

/* Normalize any edition string to the unified  'YY  form. */
function shortYear(ed) {
  const m4 = String(ed).match(/(\d{4})/);
  if (m4) return `'${m4[1].slice(2)}`;
  const m2 = String(ed).match(/'?(\d{2})\b/);
  if (m2) return `'${m2[1]}`;
  return String(ed);
}
/* Abbreviate full month names so conference dates read "Feb 23-25, 2027". */
function abbrevMonths(s) {
  let r = String(s);
  FULL_MONTHS.forEach((m) => { r = r.replace(new RegExp(m, "g"), m.slice(0, 3)); });
  return r.replace(/\bSept\b/g, "Sep");
}

const MON = { Jan: 1, Feb: 2, Mar: 3, Apr: 4, May: 5, Jun: 6, Jul: 7, Aug: 8, Sep: 9, Oct: 10, Nov: 11, Dec: 12 };
function season(m) { return (m >= 3 && m <= 5) ? "Spring" : (m >= 6 && m <= 8) ? "Summer" : (m >= 9 && m <= 11) ? "Fall" : "Winter"; }
function cycleLabel(group) {
  const main = group.filter((d) => d.label.toLowerCase() !== "abstract").slice(-1)[0] || group[group.length - 1];
  const mon = MON[String(main.display).slice(0, 3)] || (new Date(main.t).getUTCMonth() + 1);
  return season(mon);
}
/* Split a venue that has several submission cycles into one entry per cycle.
   Deadlines within ~45 days are one cycle (abstract + paper); larger gaps split. */
function expandCard(card) {
  const dated = card.deadlines.filter((d) => d.utc)
    .map((d) => ({ ...d, t: new Date(d.utc).getTime() })).sort((a, b) => a.t - b.t);
  const make = (deadlines, cycle, idx) =>
    ({ ...card, id: idx == null ? card.id : `${card.id}-${idx}`, deadlines, cycle: cycle || "" });
  if (dated.length <= 1) return [make(card.deadlines, "", null)];
  const GAP = 45 * 864e5;
  const groups = []; let cur = [dated[0]];
  for (let i = 1; i < dated.length; i++) {
    if (dated[i].t - cur[cur.length - 1].t > GAP) { groups.push(cur); cur = [dated[i]]; }
    else cur.push(dated[i]);
  }
  groups.push(cur);
  if (groups.length <= 1) return [make(card.deadlines, "", null)];
  return groups.map((g, i) => make(g.map(({ t, ...d }) => d), cycleLabel(g), i));
}

/* ---------- deadline selection (paper only; abstract hidden) ---------- */
function activeDeadline(card, now) {
  const dated = card.deadlines.filter((d) => d.utc).map((d) => ({ ...d, t: new Date(d.utc).getTime() }));
  if (!dated.length) return card.deadlines.length ? { tbd: true } : null;
  let pool = dated.filter((d) => d.label.toLowerCase() !== "abstract");
  if (!pool.length) pool = dated;
  pool.sort((a, b) => a.t - b.t);
  return pool.filter((d) => d.t > now)[0] || pool[pool.length - 1];
}
function isUpcoming(card, now) {
  const a = activeDeadline(card, now);
  if (!a) return false;
  return a.tbd ? true : a.t > now;
}
function splitDisplay(display) {
  const parts = String(display).split(", ");
  if (parts.length >= 3) return { date: `${parts[0]}, ${parts[1]}`, time: parts.slice(2).join(", ") };
  return { date: display, time: "" };
}
function isoDate(display) {
  const sd = splitDisplay(display);
  const p = sd.date.split(", ");          // ["Sep 24", "2026"]
  if (p.length < 2) return sd.date;        // e.g. "TBD"
  const [mon, day] = p[0].split(" ");
  const m = MON[mon];
  if (!m || !day) return sd.date;
  return `${p[1]}-${String(m).padStart(2, "0")}-${String(parseInt(day, 10)).padStart(2, "0")}`;
}
function relText(ms) {
  if (ms <= 0) return "passed";
  const s = Math.floor(ms / 1000);
  const d = Math.floor(s / 86400);
  if (d >= 2) return `${d} days left`;
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (d === 1) return `1 day ${h}h left`;
  return `${String(h).padStart(2, "0")}h ${String(m).padStart(2, "0")}m ${String(sec).padStart(2, "0")}s`;
}

/* ---------- filtering / sorting ---------- */
function computeVisible(now) {
  let cards = state.all.filter((c) => {
    const a = activeDeadline(c, now);
    const up = isUpcoming(c, now);
    if (state.view === "upcoming") {
      if (!up) return false;
    } else {                                   // "all": upcoming + anything within the last year
      if (!up && a && !a.tbd && (now - a.t) > YEAR_MS) return false;
    }
    if (state.tag !== "All" && !(c.tags || []).includes(state.tag)) return false;
    if (state.q) {
      const hay = `${c.conf} ${c.edition} ${c.cycle || ""} ${c.full_name} ${c.place} ${(c.tags || []).join(" ")}`.toLowerCase();
      if (!hay.includes(state.q)) return false;
    }
    return true;
  });
  cards.sort((a, b) => {                         // chronological by deadline (oldest first)
    const aa = activeDeadline(a, now), ba = activeDeadline(b, now);
    const at = aa && !aa.tbd ? aa.t : null;
    const bt = ba && !ba.tbd ? ba.t : null;
    if (at && bt) return at - bt;
    return at ? -1 : (bt ? 1 : 0);               // TBA goes last
  });
  return cards;
}

/* ---------- rendering ---------- */
function rowHTML(card, now) {
  const a = activeDeadline(card, now);
  let dlHTML, cd, tbd = false;
  if (a && a.tbd) {
    tbd = true; dlHTML = `<span class="dl-d">TBA</span>`; cd = "—";
  } else if (a) {
    const sd = splitDisplay(a.display);
    const dstr = state.dateFmt === "iso" ? isoDate(a.display) : sd.date;
    dlHTML = `<span class="dl-d">${esc(dstr)}</span>${sd.time ? `<span class="dl-t">${esc(sd.time)}</span>` : ""}`;
    const ms = a.t - now;
    cd = ms > 0 ? relText(ms) : "Passed";
  } else {
    dlHTML = `<span class="dl-d">—</span>`; cd = "";
  }
  return `<div class="row" id="row-${esc(card.id)}" data-id="${esc(card.id)}">
    <div class="c-dl${tbd ? " tbd" : ""}">${dlHTML}</div>
    <div class="c-conf"><a class="conf-link" href="${esc(card.homepage || card.link)}" target="_blank" rel="noopener" title="${esc(card.conf)} home page"><span class="conf">${esc(card.conf)}</span><span class="edition">${esc(shortYear(card.edition))}</span></a>${card.cycle ? `<span class="conf-cycle">${esc(card.cycle)}</span>` : ""}</div>
    <div class="c-loc">${esc(card.place)}</div>
    <div class="c-dates">${esc(abbrevMonths(card.date))}</div>
    <div class="c-cd" id="cd-${esc(card.id)}">${esc(cd)}</div>
    <div class="c-link"><a href="${esc(card.link)}" target="_blank" rel="noopener">CFP</a></div>
  </div>`;
}
function urgencyClass(card, now) {
  const a = activeDeadline(card, now);
  if (!a || a.tbd) return "live";
  const ms = a.t - now;
  if (ms <= 0) return "past";
  if (ms < URGENT_MS) return "urgent";
  if (ms < SOON_MS) return "soon";
  return "live";
}
function render() {
  const now = Date.now();
  state.visible = computeVisible(now);
  $("#list").innerHTML = state.visible.map((c) => rowHTML(c, now)).join("");
  state.visible.forEach((c) => {
    const el = document.getElementById(`row-${c.id}`);
    if (el) el.classList.add(urgencyClass(c, now));
  });
  $("#empty").hidden = state.visible.length > 0;
}
function tick() {
  const now = Date.now();
  let rerender = false;
  for (const c of state.visible) {
    const a = activeDeadline(c, now);
    if (!a || a.tbd) continue;
    const cd = document.getElementById(`cd-${c.id}`);
    if (cd) cd.textContent = a.t > now ? relText(a.t - now) : "Passed";
    if (state.view === "upcoming" && a.t <= now) rerender = true;
    const row = document.getElementById(`row-${c.id}`);
    if (row) { row.classList.remove("urgent", "soon", "live", "past"); row.classList.add(urgencyClass(c, now)); }
  }
  if (rerender) render();
}

/* ---------- tz note (once, above the table) ---------- */
function setTzNote() {
  let line = "Times shown in each conference's deadline timezone (mostly <b>AoE</b>).";
  try {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    const off = -new Date().getTimezoneOffset() / 60;
    line += ` Your timezone: <b>${esc(tz)}</b> (UTC${off >= 0 ? "+" : "-"}${Math.abs(off).toString().replace(".5", ":30")}).`;
  } catch { /* keep base */ }
  $("#tzinfo").innerHTML = line;
}

/* ---------- chrome ---------- */
function renderTags(tags) {
  const bar = $("#tag-bar");
  bar.innerHTML = ["All", ...tags].map((t) =>
    `<button class="chip ${t === state.tag ? "active" : ""}" data-tag="${esc(t)}">${esc(t)}</button>`).join("");
  bar.querySelectorAll(".chip").forEach((btn) => btn.addEventListener("click", () => {
    state.tag = btn.dataset.tag;
    bar.querySelectorAll(".chip").forEach((b) => b.classList.toggle("active", b === btn));
    render();
  }));
}
function setupControls() {
  $("#search").addEventListener("input", (e) => { state.q = e.target.value.trim().toLowerCase(); render(); });
  document.querySelectorAll(".view-toggle .seg").forEach((btn) => btn.addEventListener("click", () => {
    state.view = btn.dataset.view;
    document.querySelectorAll(".view-toggle .seg").forEach((b) => b.classList.toggle("active", b === btn));
    render();
  }));
  document.querySelectorAll(".fmt-toggle .seg").forEach((btn) => btn.addEventListener("click", () => {
    state.dateFmt = btn.dataset.fmt; store.set("dateFmt", state.dateFmt);
    document.querySelectorAll(".fmt-toggle .seg").forEach((b) => b.classList.toggle("active", b === btn));
    render();
  }));
  $("#theme-toggle").addEventListener("click", () => {
    const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    applyTheme(next); store.set("theme", next);
  });
}
function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  const t = $("#theme-toggle"); if (t) t.textContent = theme === "dark" ? "☀️" : "🌙";
}
function initTheme() {
  const saved = store.get("theme", null);
  const prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  applyTheme(saved || (prefersDark ? "dark" : "light"));
}

/* ---------- boot ---------- */
async function main() {
  state.dateFmt = store.get("dateFmt", "iso");
  initTheme(); setupControls(); setTzNote();
  document.querySelectorAll(".fmt-toggle .seg").forEach((b) => b.classList.toggle("active", b.dataset.fmt === state.dateFmt));
  try {
    const res = await fetch(DATA_URL, { cache: "no-cache" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    state.all = (data.conferences || []).flatMap(expandCard);
    renderTags(data.tags || []);
    const gen = $("#generated");
    if (gen && data.generated) gen.textContent = new Date(data.generated).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
    render();
    setInterval(tick, 1000);
  } catch (err) {
    $("#list").innerHTML = `<p class="empty">Could not load deadline data (${esc(err.message)}).<br/>Run <code>python scripts/update_deadlines.py --offline</code> then serve over http://.</p>`;
  }
}
document.addEventListener("DOMContentLoaded", main);
