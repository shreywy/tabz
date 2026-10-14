// tabz UI. Python pushes events through tabz.on(); we call window.pywebview.api.*
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const api = () => window.pywebview.api;
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

const state = {
  screen: "home",
  prev: "home",           // where to go back to from the area check / review
  settings: {},
  library: [],
  coll: "__all",
  sort: "recent",
  view: "cards",
  jobs: [],
  watch: null,            // job the user just added from New: open its area check when ready
  told: new Set(),        // jobs we've already shown an area toast for
  area: null,             // {job, box, deadline, held}
  review: null,
  removed: new Set(),
};
try { state.view = localStorage.getItem("tabz.libview") || "cards"; } catch {}

// ---- helpers -------------------------------------------------------------------
const clock = (s) => { s = Math.max(0, Math.round(s || 0)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
const today = () => new Date().toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
const isLink = (s) => /^(https?:\/\/|www\.|youtu\.?be|m\.youtube|youtube\.com|music\.youtube)/i.test(s.trim());
const hasList = (s) => /[?&]list=/.test(s);
const ytId = (url) => (String(url || "").match(/[?&]v=([\w-]{6,})/) || [])[1];
function when(ts) {
  const d = new Date(ts * 1000), now = new Date();
  const days = Math.floor((new Date(now.toDateString()) - new Date(d.toDateString())) / 864e5);
  if (days === 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 7) return `${days} days ago`;
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: d.getFullYear() === now.getFullYear() ? undefined : "numeric" });
}
function views(n) {
  if (!n) return "";
  if (n >= 1e6) return `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)}M views`;
  if (n >= 1e3) return `${Math.round(n / 1e3)}K views`;
  return `${n} views`;
}
function infoChips(info, { max = 9, chords = true } = {}) {
  const c = [];
  if (info.capo != null) c.push(info.capo === 0 ? "No capo" : `Capo ${info.capo}`);
  if (info.tuning && info.tuning !== "Standard") c.push(info.tuning);
  if (info.key) c.push(`<span class="chip key">${esc(info.key)}</span>`);
  if (info.tempo) c.push(`${info.tempo} bpm`);
  if (chords) for (const ch of info.chords || []) c.push(ch);
  return c.slice(0, max).map((x) => (x.startsWith("<span") ? x : `<span class="chip">${esc(x)}</span>`)).join("");
}
function toast(text, actions = [], ms = 6000) {
  const t = document.createElement("div");
  t.className = "toast";
  t.innerHTML = `<span>${esc(text)}</span>`;
  for (const [label, fn] of actions) {
    const b = document.createElement("button");
    b.textContent = label;
    b.onclick = () => { t.remove(); fn(); };
    t.appendChild(b);
  }
  $("#toasts").appendChild(t);
  setTimeout(() => t.remove(), ms);
}
function ask(text, { input = null, ok = "OK", danger = false } = {}) {
  return new Promise((done) => {
    $("#dialog-text").textContent = text;
    const inp = $("#dialog-input");
    inp.hidden = input === null;
    inp.value = input ?? "";
    $("#dialog-yes").textContent = ok;
    $("#dialog-yes").className = `btn ${danger ? "danger" : "primary"}`;
    $("#dialog").hidden = false;
    (input === null ? $("#dialog-no") : inp).focus();
    const close = (v) => { $("#dialog").hidden = true; done(v); };
    $("#dialog-yes").onclick = () => close(input === null ? true : inp.value.trim());
    $("#dialog-no").onclick = () => close(null);
    inp.onkeydown = (e) => { if (e.key === "Enter") close(inp.value.trim()); };
  });
}

// ---- screens -----------------------------------------------------------------------
function show(screen) {
  if (state.screen !== screen && !["area", "review", "play"].includes(state.screen)) state.prev = state.screen;
  state.screen = screen;
  $$(".screen").forEach((s) => s.classList.toggle("on", s.dataset.screen === screen));
  $$(".nav button").forEach((b) => b.classList.toggle("on", b.dataset.go === screen));
}

async function go(where) {
  if (state.screen === "review") await api().close_review();
  if (state.screen === "play") leavePlay(true);
  if (where === "library") { await refreshLibrary(); show("library"); }
  else { show("home"); refreshLibrary(); $("#url").focus(); }
}

// ---- theme ---------------------------------------------------------------------------
function currentTheme() {
  const t = document.documentElement.dataset.theme;
  return t || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
}
function toggleTheme() {
  const next = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("tabz.theme", next); } catch {}
}

// ---- events from Python ------------------------------------------------------------------
window.tabz = { on(event, data) { handlers[event]?.(data); } };

const STATUS = {
  queued: "Waiting", download: "Downloading", downloaded: "Downloaded", locate: "Reading", area: "Check area",
  ready: "Ready to scan", scan: "Scanning",
  read: "Chords", save: "Saving", done: "Done", failed: "Failed", skipped: "Skipped", cancelled: "Cancelled",
};
const ACTIVE = ["queued", "download", "downloaded", "locate", "area", "ready", "scan", "read", "save"];

const handlers = {
  jobs(list) {
    const before = new Map(state.jobs.map((j) => [j.id, j.status]));
    state.jobs = list;
    renderQueue();
    for (const j of list) {
      if (j.status === "area" && before.get(j.id) !== "area") onAreaReady(j);
      if (state.area?.job.id === j.id) syncArea(j);
    }
  },
  job_done(j) {
    refreshLibrary();
    if (state.area?.job.id === j.id) leaveArea();
    toast(`Ready: ${j.title}`, [["Open", () => openSong(j.song, state.screen)], ["Review", () => openReview(j.song)]], 9000);
  },
};

// ---- queue -----------------------------------------------------------------------------
function renderQueue() {
  const active = state.jobs.filter((j) => ACTIVE.includes(j.status));
  const waiting = state.jobs.filter((j) => j.status === "area");
  const btn = $("#queue-btn");
  btn.hidden = state.jobs.length === 0;
  btn.classList.toggle("busy", active.length > 0);
  btn.classList.toggle("attn", waiting.length > 0);
  $("#queue-badge").textContent = active.length || "";
  const working = state.jobs.filter((j) => ["download", "locate", "scan", "read", "save"].includes(j.status));
  const scanning = working.find((j) => j.status === "scan");
  $("#queue-label").textContent = waiting.length ? "Check area"
    : working.length > 1 ? `${working.length} working`
    : working.length ? `${STATUS[working[0].status]} ${Math.round(working[0].progress * 100)}%`
    : scanning ? "Scanning" : "Queue";

  const ul = $("#queue-list");
  let html = "", group;
  for (const j of state.jobs) {
    if (j.collection !== group) {
      group = j.collection;
      if (group) html += `<li class="q-group">${esc(group)}</li>`;
    }
    const pct = Math.round(j.progress * 100);
    const showBar = ["download", "scan"].includes(j.status);
    let sub = j.error || j.detail || "";
    if (j.status === "scan" && j.found) sub = `${j.found} ${j.found === 1 ? "strip" : "strips"} so far`;
    if (j.status === "area") sub = j.held ? "Waiting for you" : `Going ahead with the guess in ${j.left}s`;
    if (j.status === "done" && j.notes) sub = j.notes;
    const acts = [];
    if (j.status === "area") acts.push(`<button class="link-btn" data-q="area" data-id="${j.id}">Check area</button>`);
    if (j.status === "done" && j.song) acts.push(`<button class="link-btn" data-q="open" data-id="${j.id}">Open</button>`, `<button class="link-btn" data-q="review" data-id="${j.id}">Review</button>`);
    if (ACTIVE.includes(j.status)) acts.push(`<button class="link-btn" data-q="cancel" data-id="${j.id}">Cancel</button>`);
    html += `<li class="qitem ${j.status}">
      <div class="q-top"><span class="q-title" title="${esc(j.title)}">${esc(j.title)}</span><span class="q-state">${STATUS[j.status]}${showBar ? ` ${pct}%` : ""}</span></div>
      ${sub ? `<span class="q-sub">${esc(sub)}</span>` : ""}
      ${showBar ? `<div class="q-bar"><i style="width:${pct}%"></i></div>` : ""}
      ${acts.length ? `<div class="q-acts">${acts.join("")}</div>` : ""}
    </li>`;
  }
  ul.innerHTML = html;
  $("#queue-empty").hidden = state.jobs.length > 0;
}

function toggleQueue(open) {
  const q = $("#queue");
  q.hidden = open === undefined ? !q.hidden : !open;
  $("#queue-btn").setAttribute("aria-expanded", String(!q.hidden));
}
$("#queue-btn").onclick = () => toggleQueue();
$("#queue-close").onclick = () => toggleQueue(false);
$("#queue-clear").onclick = () => api().jobs_clear();
$("#queue-list").addEventListener("click", (e) => {
  const b = e.target.closest("[data-q]");
  if (!b) return;
  const j = state.jobs.find((x) => x.id === Number(b.dataset.id));
  if (!j) return;
  if (b.dataset.q === "area") { toggleQueue(false); openArea(j); }
  if (b.dataset.q === "open") { toggleQueue(false); openSong(j.song, state.screen); }
  if (b.dataset.q === "review") { toggleQueue(false); openReview(j.song); }
  if (b.dataset.q === "cancel") api().job_cancel(j.id);
});

// ---- new: link, playlist or search ------------------------------------------------------------
function syncForm() {
  const v = $("#url").value;
  const link = isLink(v);
  const list = link && hasList(v);
  $("#go").textContent = link ? (list ? "Just this video" : "Get tabs") : "Search";
  $("#go-list").hidden = !list;
  $("#form-hint").hidden = !list;
  $("#form-hint").textContent = "This link is part of a playlist. Get just this video, or every video in it, filed together.";
}
$("#url").addEventListener("input", syncForm);

$("#link-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = $("#url").value.trim();
  if (!q) return;
  $("#home-error").hidden = true;
  if (isLink(q)) return addLink(q, false);
  search(q);
});
$("#go-list").onclick = () => addLink($("#url").value.trim(), true);

async function addLink(url, whole) {
  const r = await api().add(url, whole);
  if (r.error) {
    $("#home-error").textContent = r.error;
    $("#home-error").hidden = false;
    if (r.single_ok) toast("Get just the video from that link instead?", [["Get the video", () => addLink(url, false)]], 12000);
    return;
  }
  $("#url").value = "";
  syncForm();
  if (r.playlist) {
    toast(`Queued ${r.added} from “${r.playlist}”${r.skipped ? `. Skipped ${r.skipped} (private or deleted)` : ""}.`, [["Show queue", () => toggleQueue(true)]]);
  } else {
    state.watch = r.job;
    toast("Added to the queue. Keep browsing; tabz asks you to check the tab area when it's ready.", [["Show queue", () => toggleQueue(true)]]);
  }
}

let searchId = 0;
async function search(q) {
  const id = ++searchId;
  $(".home").classList.add("searched");
  $("#results-wrap").hidden = false;
  $("#recent-wrap").hidden = true;
  $("#results-title").textContent = `Results for “${q}”`;
  $("#results").innerHTML = Array.from({ length: 8 }, () => `<li class="result skeleton"><div class="thumb"></div><b></b></li>`).join("");
  const res = await api().search(q);
  if (id !== searchId) return;
  if (res.error) {
    $("#results").innerHTML = "";
    $("#home-error").textContent = res.error;
    $("#home-error").hidden = false;
    return;
  }
  const have = new Set(state.library.map((s) => s.song));
  $("#results").innerHTML = res.results.map((v) => `
    <li class="result" data-url="${esc(v.url)}" title="${esc(v.title)}">
      <div class="thumb"><img src="${esc(v.thumb)}" alt="" loading="lazy">
        ${v.duration ? `<span class="dur">${clock(v.duration)}</span>` : ""}
        <span class="go">${have.has(v.id) ? "In your library" : "Add to queue"}</span></div>
      <b>${esc(v.title)}</b>
      <span class="by">${esc(v.channel)}${v.views ? ` · ${views(v.views)}` : ""}</span>
    </li>`).join("") || `<p class="empty">Nothing found.</p>`;
}
$("#results").addEventListener("click", async (e) => {
  const li = e.target.closest(".result[data-url]");
  if (!li) return;
  const r = await api().add(li.dataset.url, false);
  if (r.job) {
    li.querySelector(".go").textContent = "Queued";
    li.querySelector(".go").style.opacity = 1;
    toast("Added to the queue.", [["Show queue", () => toggleQueue(true)]], 3500);
  }
});

// ---- area check ------------------------------------------------------------------------------
function onAreaReady(j) {
  if (state.told.has(j.id)) return;
  state.told.add(j.id);
  // the user just added this one and isn't busy elsewhere: show it straight away
  const idle = ["home", "library"].includes(state.screen) && $("#dialog").hidden;
  if (state.watch === j.id && idle) return openArea(j);
  if (!j.collection) toast(`Check the tab area for “${j.title}”`, [["Check", () => openArea(j)]], 14000);
}

async function openArea(j) {
  const job = state.jobs.find((x) => x.id === j.id) || j;
  if (job.status !== "area") return;
  state.area = { job, box: [...job.area.region], deadline: Date.now() + (job.left ?? 15) * 1000, held: job.held };
  show("area");
  $("#area-title").textContent = job.title;
  $("#area-sub").textContent = [job.notes, clock(job.area.duration)].filter(Boolean).join("  ·  ");
  $("#area-msg").textContent = job.area.found
    ? "The box is a guess. Drag it or its edges so it covers the string lines and the chord names above them. Scrub through to check it fits the whole way."
    : "tabz couldn't find tab lines by itself, so the box starts on the bottom third. Drag it over the tab, then scan.";
  $("#scrub-dur").textContent = clock(job.area.duration);
  $("#scrub").value = Math.round((job.area.time / job.area.duration) * 1000);
  tickArea();
  loadFrame(job.area.time);
}

function syncArea(j) {
  state.area.job = j;
  if (j.status !== "area" && state.screen === "area") {
    // went ahead without us (timer ran out) or got cancelled
    leaveArea();
    if (j.status === "scan") toast(`Scanning “${j.title}” with the guessed box.`);
  }
}

function hold() {
  const a = state.area;
  if (!a || a.held) return;
  a.held = true;
  api().job_hold(a.job.id);
  tickArea();
}

function tickArea() {
  const a = state.area;
  if (!a || state.screen !== "area") return;
  const total = 15, left = Math.max(0, (a.deadline - Date.now()) / 1000);
  $("#countdown").classList.toggle("held", a.held);
  $("#cd-fill").style.width = a.held ? "100%" : `${(left / total) * 100}%`;
  $("#cd-text").textContent = a.held
    ? "Timer stopped. Scan when the box looks right."
    : `Scanning with this box in ${Math.ceil(left)}s. Touch the box to take your time.`;
  if (!a.held) setTimeout(tickArea, 250);
}

function leaveArea() {
  state.area = null;
  if (state.screen === "area") show(["area", "review", "play"].includes(state.prev) ? "library" : state.prev);
}

let frameReq = 0;
async function loadFrame(t) {
  const id = ++frameReq;
  $("#scrub-time").textContent = clock(t);
  const src = await api().job_frame(state.area.job.id, t);
  if (id !== frameReq || !src) return;
  const img = $("#frame");
  img.onload = placeBox;
  img.src = src;
}

$("#scrub").addEventListener("input", (e) => {
  hold();
  const t = (e.target.value / 1000) * state.area.job.area.duration;
  $("#scrub-time").textContent = clock(t);
  clearTimeout(loadFrame.t);
  loadFrame.t = setTimeout(() => loadFrame(t), 60);
});

const frameScale = () => $("#frame").clientWidth / state.area.job.area.width;

function placeBox() {
  const img = $("#frame"), crop = $("#crop");
  if (!img.naturalWidth || !state.area) return;
  const s = frameScale();
  const [x, y, w, h] = state.area.box;
  Object.assign(crop.style, { left: `${x * s}px`, top: `${y * s}px`, width: `${w * s}px`, height: `${h * s}px` });
}
window.addEventListener("resize", placeBox);

(function dragBox() {
  const crop = $("#crop");
  let start = null;
  crop.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    hold();
    crop.setPointerCapture(e.pointerId);
    start = { x: e.clientX, y: e.clientY, box: [...state.area.box], h: e.target.dataset.h || "move" };
  });
  crop.addEventListener("pointermove", (e) => {
    if (!start) return;
    const s = frameScale(), W = state.area.job.area.width, H = state.area.job.area.height, min = 40;
    const dx = (e.clientX - start.x) / s, dy = (e.clientY - start.y) / s;
    let [x, y, w, h] = start.box;
    const hd = start.h;
    if (hd === "move") {
      x = Math.min(Math.max(0, x + dx), W - w);
      y = Math.min(Math.max(0, y + dy), H - h);
    } else {
      if (hd.includes("w")) { const nx = Math.min(Math.max(0, x + dx), x + w - min); w += x - nx; x = nx; }
      if (hd.includes("e")) w = Math.min(Math.max(min, w + dx), W - x);
      if (hd.includes("n")) { const ny = Math.min(Math.max(0, y + dy), y + h - min); h += y - ny; y = ny; }
      if (hd.includes("s")) h = Math.min(Math.max(min, h + dy), H - y);
    }
    state.area.box = [x, y, w, h].map(Math.round);
    placeBox();
  });
  const end = () => (start = null);
  crop.addEventListener("pointerup", end);
  crop.addEventListener("pointercancel", end);
})();

$("#reset-box").onclick = () => { hold(); state.area.box = [...state.area.job.area.region]; placeBox(); };
$("#scan-btn").onclick = async () => {
  const a = state.area;
  await api().job_confirm(a.job.id, a.box);
  toast(`Scanning “${a.job.title}”. It'll be in your library when it's done.`, [["Show queue", () => toggleQueue(true)]]);
  leaveArea();
};
$("#skip-job").onclick = async () => {
  await api().job_cancel(state.area.job.id);
  leaveArea();
};

// ---- library ---------------------------------------------------------------------------------
async function refreshLibrary() {
  state.library = await api().library();
  $("#lib-count").textContent = state.library.length || "";
  renderRecent();
  renderRail();
  renderLibrary();
}

function cardHtml(it, compact) {
  const i = it.info || {};
  const song = i.song || it.name;
  const thumb = it.thumb
    ? `<div class="tab-thumb"><img src="${it.thumb}" alt="" loading="lazy"></div>`
    : `<div class="tab-thumb pdf">PDF only</div>`;
  return `
    <li class="card" data-key="${esc(it.song || it.path)}">
      ${thumb}
      <div class="body">
        <div class="title-row">
          ${it.art ? `<img class="art" src="${it.art}" alt="" loading="lazy">` : ""}
          <div><span class="song">${esc(song)}</span>
          ${i.artist ? `<span class="artist">${esc([i.artist, i.year].filter(Boolean).join(" · "))}</span>` : ""}
          ${!compact && it.collection && state.coll === "__all" ? `<span class="coll">${esc(it.collection)}</span>` : ""}</div>
        </div>
        <div class="chips">${infoChips(i, { max: 8 })}</div>
        <div class="foot">
          <span>${esc([it.channel, when(it.created || it.when)].filter(Boolean).join(" · "))}</span>
          <span class="acts"><button class="icon-btn" data-card="menu" title="More" aria-label="More">
            <svg viewBox="0 0 20 20" width="16" height="16"><circle cx="5" cy="10" r="1.5" fill="currentColor"/><circle cx="10" cy="10" r="1.5" fill="currentColor"/><circle cx="15" cy="10" r="1.5" fill="currentColor"/></svg>
          </button></span>
        </div>
      </div>
    </li>`;
}

function renderRecent() {
  const items = state.library.slice(0, 4);
  $("#recent").innerHTML = items.map((it) => cardHtml(it, true)).join("");
  $("#recent-empty").hidden = items.length > 0;
}

function collections() {
  const m = new Map();
  for (const it of state.library) if (it.collection) m.set(it.collection, (m.get(it.collection) || 0) + 1);
  return [...m.entries()].sort((a, b) => a[0].localeCompare(b[0]));
}

function renderRail() {
  $("#c-all").textContent = state.library.length || "";
  $("#rail-colls").innerHTML = collections()
    .map(([name, n]) => `<button class="rail-item" data-coll="${esc(name)}" title="${esc(name)}"><span>${esc(name)}</span><span class="count">${n}</span></button>`)
    .join("");
  $("#coll-names").innerHTML = collections().map(([n]) => `<option value="${esc(n)}">`).join("");
  $$(".rail-item").forEach((b) => b.classList.toggle("on", b.dataset.coll === state.coll));
}

function libraryItems() {
  const q = $("#lib-filter").value.trim().toLowerCase();
  let items = state.library;
  if (state.coll === "__recent") items = items.filter((it) => it.played).sort((a, b) => b.played - a.played).slice(0, 30);
  else if (state.coll === "__loose") items = items.filter((it) => !it.collection);
  else if (state.coll !== "__all") items = items.filter((it) => it.collection === state.coll);
  if (q) {
    items = items.filter((it) => {
      const i = it.info || {};
      const hay = [it.name, i.song, i.artist, i.album, i.year, i.genre, it.channel, it.collection, i.key, i.tuning, ...(i.chords || []),
        i.capo != null ? `capo ${i.capo}` : "", i.tempo ? `${i.tempo} bpm` : ""].join(" ").toLowerCase();
      return q.split(/\s+/).every((w) => hay.includes(w));
    });
  }
  const key = (it) => (state.sort === "artist" ? it.info?.artist : it.info?.song || it.name) || "~";
  if (state.sort !== "recent" && state.coll !== "__recent") items = [...items].sort((a, b) => key(a).localeCompare(key(b), undefined, { sensitivity: "base" }));
  return items;
}

function renderLibrary() {
  const items = libraryItems();
  const titles = { __all: "All songs", __recent: "Played recently", __loose: "Not in a collection" };
  $("#lib-title").textContent = titles[state.coll] || state.coll;
  const ul = $("#library");
  ul.className = `cards ${state.view === "list" ? "list" : ""}`;
  ul.innerHTML = items.map((it) => cardHtml(it, false)).join("");
  $("#library-empty").hidden = state.library.length > 0;
  $("#library-nomatch").hidden = !(state.library.length && !items.length);
  $$("#lib-sort button").forEach((b) => b.classList.toggle("on", b.dataset.sort === state.sort));
  $$("#lib-view button").forEach((b) => b.classList.toggle("on", b.dataset.view === state.view));
}

$("#lib-filter").addEventListener("input", renderLibrary);
$("#lib-sort").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (b) { state.sort = b.dataset.sort; renderLibrary(); }
});
$("#lib-view").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  state.view = b.dataset.view;
  try { localStorage.setItem("tabz.libview", state.view); } catch {}
  renderLibrary();
});
$(".lib-rail").addEventListener("click", (e) => {
  const b = e.target.closest(".rail-item");
  if (!b) return;
  state.coll = b.dataset.coll;
  renderRail();
  renderLibrary();
});

const findItem = (key) => state.library.find((x) => (x.song || x.path) === key);

function onCardClick(e) {
  const card = e.target.closest(".card");
  if (!card) return;
  const it = findItem(card.dataset.key);
  if (!it) return;
  const menuBtn = e.target.closest("[data-card=menu]");
  if (menuBtn) return openMenu(it, menuBtn);
  if (it.song) openSong(it.song, state.screen);
  else api().open_path(it.path);
}
$("#library").addEventListener("click", onCardClick);
$("#recent").addEventListener("click", onCardClick);

function openMenu(it, anchor) {
  const m = $("#card-menu");
  const items = [];
  if (it.song) items.push(["play", "Open in reader"]);
  if (it.song && it.editable) items.push(["review", "Review and edit"]);
  if (it.path) items.push(["pdf", "Open PDF"], ["folder", "Show PDF in folder"]);
  if (it.url) items.push(["video", "Watch on YouTube"]);
  if (it.song) items.push(["-"], ["move", it.collection ? "Move to another collection" : "Add to a collection"]);
  if (it.song && it.collection) items.push(["unfile", `Take out of “${it.collection}”`]);
  if (it.song) items.push(["-"], ["delete", "Remove from library"]);
  m.innerHTML = items.map(([k, label]) => (k === "-" ? "<hr>" : `<button data-m="${k}" class="${k === "delete" ? "danger" : ""}">${esc(label)}</button>`)).join("");
  m.hidden = false;
  const r = anchor.getBoundingClientRect();
  const mw = m.offsetWidth, mh = m.offsetHeight;
  m.style.left = `${Math.max(8, Math.min(r.right - mw, innerWidth - mw - 8))}px`;
  m.style.top = `${r.bottom + mh + 6 > innerHeight ? r.top - mh - 6 : r.bottom + 6}px`;
  m.onclick = async (e) => {
    const k = e.target.closest("[data-m]")?.dataset.m;
    if (!k) return;
    m.hidden = true;
    const name = it.info?.song || it.name;
    if (k === "play") openSong(it.song, state.screen);
    if (k === "review") openReview(it.song);
    if (k === "pdf") api().open_path(it.path);
    if (k === "folder") api().show_in_folder(it.path);
    if (k === "video") api().open_url(it.url);
    if (k === "move") {
      const v = await ask(`Collection for “${name}”`, { input: it.collection || "", ok: "Move" });
      if (v) { await api().set_song(it.song, { collection: v }); refreshLibrary(); }
    }
    if (k === "unfile") { await api().set_song(it.song, { collection: null }); refreshLibrary(); }
    if (k === "delete") {
      if (await ask(`Remove “${name}” from your library? Its PDF stays in the folder.`, { ok: "Remove", danger: true })) {
        await api().delete_song(it.song);
        refreshLibrary();
      }
    }
  };
}
document.addEventListener("pointerdown", (e) => {
  if (!e.target.closest("#card-menu, [data-card=menu]")) $("#card-menu").hidden = true;
});

// ---- review --------------------------------------------------------------------------------
async function openReview(sid) {
  const r = await api().open_review(sid);
  if (!r || r.error) return toast(r?.error || "Couldn't open that song.");
  state.removed = new Set(r.strips.filter((s) => s.removed).map((s) => s.i));
  $("#video-box").open = false;
  $("#video-frame").innerHTML = "";
  show("review");
  applyReview(r);
}

function applyReview(r) {
  state.review = r;
  state.settings = { ...state.settings, ...r.settings };
  $("#pages").classList.remove("busy");
  renderReview();
}

$("#video-box").addEventListener("toggle", () => {
  const id = ytId(state.review?.meta?.url);
  $("#video-frame").innerHTML = $("#video-box").open && id
    ? `<iframe src="https://www.youtube-nocookie.com/embed/${id}?rel=0" allow="autoplay; encrypted-media; picture-in-picture; fullscreen" allowfullscreen referrerpolicy="strict-origin-when-cross-origin"></iframe>`
    : "";
});

let measureCtx;
function fitText(text, font, maxW) {
  measureCtx ||= document.createElement("canvas").getContext("2d");
  measureCtx.font = font;
  return measureCtx.measureText(text).width <= maxW;
}

function renderReview() {
  const r = state.review, box = $("#pages");
  $("#saved-name").textContent = r.name || "Nothing to save. Every strip is left out.";
  $$('[data-action="open-pdf"], [data-action="show-pdf"]').forEach((b) => (b.disabled = !r.pdf));
  fillDetails(r.info || {});
  syncSegs();

  $("#strip-count").textContent = `· ${r.strips.length}`;
  $("#strips").innerHTML = r.strips.map((s) => `
    <li class="${state.removed.has(s.i) ? "out" : ""}" data-i="${s.i}" title="${state.removed.has(s.i) ? "Put this strip back" : "Leave this strip out"}">
      <img alt="" src="${s.src}" loading="lazy">
      <span class="when"><span>${clock(s.t0)}–${clock(s.t1)}</span><span>${s.screens > 1.3 ? `${s.screens} screens, scrolled` : ""}</span></span>
    </li>`).join("");

  // pages, laid out the way write_pdf lays them out (units are points)
  box.innerHTML = "";
  const P = r.paper;
  if (!P || !r.rows.length) return;
  const pageW = Math.min(720, box.clientWidth - 56);
  const k = pageW / P.w, M = P.margin, cw = P.w - 2 * M;
  const meta = r.meta || {}, info = r.info || {};
  let page, y, n = 1;
  const px = (v) => `${v * k}px`;
  const newPage = () => {
    page = document.createElement("div");
    page.className = "page";
    page.style.width = px(P.w);
    page.style.height = px(P.h);
    box.appendChild(page);
    y = M;
  };
  const el = (cls, html, top, size, extra = {}) => {
    const d = document.createElement("div");
    d.className = cls;
    d.innerHTML = html;
    Object.assign(d.style, { left: px(M), top: px(top), fontSize: px(size), maxWidth: px(cw), ...extra });
    page.appendChild(d);
    return d;
  };
  newPage();

  // header (draw_header in layout.py)
  const song = info.song || meta.title || "";
  const art = r.header?.art, ART = 62, textW = cw - (art ? ART + 16 : 0);
  if (art) {
    const im = document.createElement("img");
    im.src = art;
    Object.assign(im.style, { left: px(M + cw - ART), top: px(M), width: px(ART), height: px(ART), borderRadius: "2px" });
    page.appendChild(im);
  }
  const songLines = fitText(song, "600 21px Plex", textW) ? 1 : 2;
  el("p-song", esc(song), y + 3, 21, { whiteSpace: songLines > 1 ? "normal" : "nowrap", lineHeight: px(24), maxWidth: px(textW) });
  y += 24 * songLines;
  const sub = r.header?.sub || info.artist;
  if (sub) { el("p-artist", esc(sub).replace(/ {3}/g, "&ensp;"), y + 4, 12, { whiteSpace: "nowrap" }); y += 17; }
  if (art) y = Math.max(y, M + ART - 4);
  const cells = r.header?.cells || [];
  if (cells.length) {
    el("p-cells", cells.map(([l, v]) => `<div class="p-cell" style="margin-right:${24 * k}px"><small style="font-size:${6.8 * k}px">${esc(l)}</small><span style="font-size:${10.5 * k}px">${esc(v)}</span></div>`).join(""), y + 6, 10.5);
    y += 26;
  }
  if ((info.chords || []).length) {
    el("p-cells", `<div class="p-cell"><small style="font-size:${6.8 * k}px">Chords</small><span style="font-size:${10.5 * k}px;word-spacing:${8 * k}px">${esc(info.chords.join(" "))}</span></div>`, y + 6, 10.5);
    y += 24;
  }
  const url = (meta.url || "").replace("https://", "").replace("www.", "");
  el("p-src", `${esc([meta.channel ? `Tab from ${meta.channel}` : "", clock(meta.duration), today()].filter(Boolean).join("   ·   "))}   ·   <i>${esc(url)}</i>`, y + 14, 8.5);
  y = M + (r.header?.h || y - M);
  el("p-rule", "", y - 16, 1, { width: px(cw), height: "1px" });

  const foot = () => {
    el("p-foot", esc([info.song, info.artist].filter(Boolean).join(" — ") || meta.title), P.h - 30, 7.5, { maxWidth: px(cw * 0.85), overflow: "hidden", textOverflow: "ellipsis" });
    const num = el("p-foot", String(n), P.h - 30, 7.5);
    num.style.left = "auto";
    num.style.right = px(M);
  };
  for (const row of r.rows) {
    if (y + row.h > P.h - M - 10) { foot(); newPage(); n++; }
    const img = document.createElement("img");
    img.className = "row";
    img.src = row.src;
    Object.assign(img.style, { left: px(M), top: px(y), width: px(row.w), height: px(row.h) });
    page.appendChild(img);
    y += row.h + P.gap;
  }
  foot();
}

function fillDetails(i) {
  const f = $("#details");
  if (f.contains(document.activeElement)) return;     // don't stomp on typing
  for (const k of ["song", "artist", "album", "tuning", "key"]) f[k].value = i[k] || "";
  f.capo.value = i.capo ?? "";
  f.tempo.value = i.tempo || "";
  f.chords.value = (i.chords || []).join(" ");
  $("#key-guess").hidden = !i.key_guessed;
}

$("#details").addEventListener("change", (e) => {
  if (e.target.name) push({ info: { [e.target.name]: e.target.value } });
});
$("#details").addEventListener("submit", (e) => { e.preventDefault(); document.activeElement.blur(); });

$("#strips").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-i]");
  if (!li) return;
  const i = Number(li.dataset.i);
  state.removed.has(i) ? state.removed.delete(i) : state.removed.add(i);
  push({ removed: [...state.removed] });
});

function syncSegs() {
  for (const seg of $$(".review .seg")) {
    const v = String(state.settings[seg.dataset.setting]);
    $$("button", seg).forEach((b) => b.classList.toggle("on", b.dataset.value === v));
  }
}

let pushing = Promise.resolve();
function push(changes) {
  $("#pages").classList.add("busy");
  pushing = pushing.then(() => api().update(changes)).then((r) => r && state.screen === "review" && applyReview(r));
}

$$(".review .seg").forEach((seg) =>
  seg.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    const key = seg.dataset.setting;
    let v = b.dataset.value;
    v = key === "per_row" && v !== "auto" ? Number(v) : key === "keep_color" ? v === "true" : v;
    if (state.settings[key] === v) return;
    state.settings[key] = v;
    syncSegs();
    push({ [key]: v });
  })
);
window.addEventListener("resize", () => state.screen === "review" && renderReview());

// ---- actions -----------------------------------------------------------------------------------
document.addEventListener("click", async (e) => {
  const a = e.target.closest("[data-action], [data-go]");
  if (!a) return;
  e.preventDefault();
  if (a.dataset.go) return go(a.dataset.go);
  const act = a.dataset.action;
  const r = state.review;
  if (act === "open-pdf" && r) api().open_path(r.pdf);
  if (act === "show-pdf" && r) api().show_in_folder(r.pdf);
  if (act === "play-current" && r) openSong(r.song, "review");
  if (act === "review-done") {
    await api().close_review();
    state.review = null;
    await refreshLibrary();
    show(["area", "review", "play"].includes(state.prev) ? "library" : state.prev);
  }
  if (act === "theme") toggleTheme();
});

$("#open-settings").onclick = () => {
  $("#out-dir").textContent = state.settings.out_dir;
  $("#out-dir").title = state.settings.out_dir;
  $("#gemini-key").value = "";
  $("#gemini-key").placeholder = state.settings.gemini_set ? "A key is saved. Paste a new one to replace it, or save empty to remove it." : "Paste a key from aistudio.google.com";
  $("#settings").hidden = false;
};
$("#save-key").onclick = async () => {
  state.settings = await api().set_settings({ gemini_key: $("#gemini-key").value.trim() });
  $("#gemini-key").value = "";
  toast(state.settings.gemini_set ? "Gemini key saved. New songs will use it." : "Gemini key removed.");
  $("#open-settings").click();
};
$("#close-settings").onclick = () => { $("#settings").hidden = true; refreshLibrary(); };
$("#settings").addEventListener("click", (e) => { if (e.target.id === "settings") e.target.hidden = true; });
$("#pick-folder").onclick = async () => {
  state.settings = await api().pick_folder();
  $("#out-dir").textContent = state.settings.out_dir;
};

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { $("#settings").hidden = true; $("#card-menu").hidden = true; toggleQueue(false); }
  const typing = e.target.matches?.("input, select, textarea");
  if (!typing && (e.key === "d" || e.key === "D") && !e.ctrlKey && !e.metaKey) toggleTheme();
});

window.addEventListener("pywebviewready", async () => {
  state.settings = await api().get_settings();
  await document.fonts.ready;
  await refreshLibrary();
  handlers.jobs(await api().jobs());
  show("home");
  $("#url").focus();
});
