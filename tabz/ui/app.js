// tabz UI. Python pushes events through tabz.on(); we call window.pywebview.api.*
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const api = () => window.pywebview.api;
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

const state = {
  screen: "home",
  stage: null,
  busy: false,
  meta: null,
  area: null,      // {width, height, duration, region, found, time}
  box: null,       // [x, y, w, h] in source pixels
  review: null,
  removed: new Set(),
  settings: {},
  library: [],
  sort: "recent",
};

// ---- helpers ----------------------------------------------------------------
const clock = (s) => { s = Math.max(0, Math.round(s || 0)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
const today = () => new Date().toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
const isLink = (s) => /^(https?:\/\/|www\.|youtu\.?be|m\.youtube|youtube\.com)/i.test(s.trim());
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

// ---- screens and navigation -----------------------------------------------------
function show(screen) {
  state.screen = screen;
  $$(".screen").forEach((s) => s.classList.toggle("on", s.dataset.screen === screen));
  const flow = ["working", "area", "review"].includes(screen);
  $("#steps").hidden = !flow;
  $$(".nav button").forEach((b) => b.classList.toggle("on", !flow && b.dataset.go === screen));
  const step = screen === "working" ? (["scan", "read"].includes(state.stage) ? "scan" : "download") : screen;
  const order = ["download", "area", "scan", "review"];
  $$(".steps li").forEach((li) => {
    const i = order.indexOf(li.dataset.step), cur = order.indexOf(step);
    li.classList.toggle("now", i === cur);
    li.classList.toggle("done", i < cur);
  });
}

async function go(where) {
  if (state.screen === "working") return;          // let a running job finish
  if (["area", "review"].includes(state.screen)) { await api().finish(); state.review = null; }
  if (where === "library") { await refreshLibrary(); show("library"); $("#lib-filter").focus(); }
  else { show("home"); refreshLibrary(); $("#url").focus(); }
}

function meter(p) {
  const m = $(".meter");
  m.classList.toggle("wait", p == null);
  if (p != null) {
    $("#work-fill").style.width = `${(p * 100).toFixed(1)}%`;
    $("#work-pct").textContent = `${Math.round(p * 100)}%`;
  } else {
    $("#work-pct").textContent = "";
  }
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

// ---- events from Python ----------------------------------------------------------------
window.tabz = { on(event, data) { handlers[event]?.(data); } };

const handlers = {
  stage({ stage }) {
    state.stage = stage;
    state.busy = true;
    show("working");
    $("#work-stage").textContent = { download: "Downloading", locate: "Looking for the tab", scan: "Scanning", read: "Reading chords" }[stage];
    $("#work-detail").textContent = {
      download: "Video only, no audio. It's deleted when you're done.",
      locate: "Finding the string lines and reading capo, tuning and tempo notes.",
      scan: "Watching the box for new bars.",
      read: "Reading chord names and tempo marks above the staff.",
    }[stage];
    meter(["locate", "read"].includes(stage) ? null : 0);
  },
  progress(p) { meter(p); },
  meta(m) {
    state.meta = m;
    const i = m.info || {};
    $("#work-title").textContent = i.song ? `${i.song}${i.artist ? ` · ${i.artist}` : ""}` : m.title;
    $("#work-notes").textContent = m.notes || "";
    $("#work-notes").hidden = !m.notes;
  },
  found(n) { $("#work-detail").textContent = `${n} ${n === 1 ? "strip" : "strips"} found so far`; },
  area(a) {
    state.busy = false;
    state.area = a;
    state.box = [...a.region];
    $("#scrub-dur").textContent = clock(a.duration);
    $("#scrub").value = Math.round((a.time / a.duration) * 1000);
    $("#area-msg").classList.remove("error");
    $("#area-msg").textContent = a.found
      ? "The box is a guess. Drag it or its edges so it covers the string lines and the chord names above them. Scrub through the video to check it fits the whole way."
      : "tabz couldn't find tab lines by itself, so the box starts on the bottom third. Drag it over the tab, then scan.";
    const i = state.meta?.info || {};
    const rows = [["Song", i.song], ["Artist", i.artist], ["Length", clock(a.duration)], ["Found", state.meta?.notes]];
    $("#area-facts").innerHTML = rows.filter(([, v]) => v).map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("");
    show("area");
    loadFrame(a.time);
  },
  review(r) {
    state.busy = false;
    state.review = r;
    if (r.info) state.meta = { ...(state.meta || {}), info: r.info, notes: r.notes };
    state.removed = new Set(r.strips.filter((s) => s.removed).map((s) => s.i));
    $("#pages").classList.remove("busy");
    show("review");
    renderReview();
  },
  error(msg) {
    state.busy = false;
    if (["scan", "read"].includes(state.stage) && state.area) {
      show("area");
      $("#area-msg").textContent = msg;
      $("#area-msg").classList.add("error");
    } else {
      show("home");
      $("#home-error").textContent = msg;
      $("#home-error").hidden = false;
      refreshLibrary();
    }
  },
};

// ---- new: link or search -------------------------------------------------------------------
$("#url").addEventListener("input", (e) => {
  $("#go").textContent = isLink(e.target.value) ? "Get tabs" : "Search";
});

$("#link-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = $("#url").value.trim();
  if (!q) return;
  $("#home-error").hidden = true;
  if (isLink(q)) return startJob(q);
  search(q);
});

async function startJob(url) {
  state.meta = null;
  $("#work-title").textContent = "Fetching video…";
  $("#work-notes").hidden = true;
  await api().start(url);
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
        <span class="go">${have.has(v.id) ? "In your library" : "Get tabs"}</span></div>
      <b>${esc(v.title)}</b>
      <span class="by">${esc(v.channel)}${v.views ? ` · ${views(v.views)}` : ""}</span>
    </li>`).join("") || `<p class="empty">Nothing found.</p>`;
}
$("#results").addEventListener("click", (e) => {
  const li = e.target.closest(".result[data-url]");
  if (li) startJob(li.dataset.url);
});

// ---- library --------------------------------------------------------------------------------
async function refreshLibrary() {
  state.library = await api().library();
  $("#lib-count").textContent = state.library.length || "";
  renderRecent();
  renderLibrary();
}

function cardHtml(it) {
  const i = it.info || {};
  const song = i.song || it.name;
  const thumb = it.thumb
    ? `<div class="tab-thumb"><img src="${it.thumb}" alt=""></div>`
    : `<div class="tab-thumb pdf">PDF only</div>`;
  return `
    <li class="card" data-song="${esc(it.song || "")}" data-path="${esc(it.path || "")}">
      ${thumb}
      <div class="body">
        <div class="title-row">
          ${it.art ? `<img class="art" src="${it.art}" alt="">` : ""}
          <div><span class="song">${esc(song)}</span>
          ${i.artist ? `<span class="artist">${esc([i.artist, i.year].filter(Boolean).join(" · "))}</span>` : ""}</div>
        </div>
        <div class="chips">${infoChips(i, { max: 8 })}</div>
        <div class="foot">
          <span>${esc([it.channel, when(it.when)].filter(Boolean).join(" · "))}</span>
          <span class="acts">
            ${it.path ? `<button class="icon-btn" data-card="pdf" title="Open PDF" aria-label="Open PDF"><svg viewBox="0 0 20 20" width="15" height="15"><path d="M5 2.8h6.5L15.2 6.5v10.7H5Z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/><path d="M11.3 2.8v3.9h3.9" fill="none" stroke="currentColor" stroke-width="1.5"/></svg></button>` : ""}
            ${it.url ? `<button class="icon-btn" data-card="video" title="Watch on YouTube" aria-label="Watch on YouTube"><svg viewBox="0 0 20 20" width="15" height="15"><rect x="2.8" y="4.8" width="14.4" height="10.4" rx="2.4" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="m8.6 7.8 3.8 2.2-3.8 2.2Z" fill="currentColor"/></svg></button>` : ""}
            ${it.song ? `<button class="icon-btn" data-card="delete" title="Remove from library" aria-label="Remove from library"><svg viewBox="0 0 20 20" width="15" height="15"><path d="M4.5 6h11M8 6V4.2h4V6M6 6l.7 10.2h6.6L14 6" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg></button>` : ""}
          </span>
        </div>
      </div>
    </li>`;
}

function renderRecent() {
  const items = state.library.slice(0, 4);
  $("#recent").innerHTML = items.map(cardHtml).join("");
  $("#recent-empty").hidden = items.length > 0;
}

function renderLibrary() {
  const q = $("#lib-filter").value.trim().toLowerCase();
  let items = state.library.filter((it) => {
    if (!q) return true;
    const i = it.info || {};
    const hay = [it.name, i.song, i.artist, i.album, i.year, i.genre, it.channel, i.key, i.tuning, ...(i.chords || []),
      i.capo != null ? `capo ${i.capo}` : "", i.tempo ? `${i.tempo} bpm` : ""].join(" ").toLowerCase();
    return q.split(/\s+/).every((w) => hay.includes(w));
  });
  const key = (it) => (state.sort === "artist" ? it.info?.artist : it.info?.song || it.name) || "~";
  if (state.sort !== "recent") items = [...items].sort((a, b) => key(a).localeCompare(key(b), undefined, { sensitivity: "base" }));
  $("#library").innerHTML = items.map(cardHtml).join("");
  $("#library-empty").hidden = state.library.length > 0;
  $("#library-nomatch").hidden = !(state.library.length && !items.length);
  $$("#lib-sort button").forEach((b) => b.classList.toggle("on", b.dataset.sort === state.sort));
}

$("#lib-filter").addEventListener("input", renderLibrary);
$("#lib-sort").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  state.sort = b.dataset.sort;
  renderLibrary();
});

function onCardClick(e) {
  const card = e.target.closest(".card");
  if (!card) return;
  const it = state.library.find((x) => (x.song || "") === card.dataset.song && (x.path || "") === card.dataset.path);
  if (!it) return;
  const act = e.target.closest("[data-card]")?.dataset.card;
  if (act === "pdf") return api().open_path(it.path);
  if (act === "video") return api().open_url(it.url);
  if (act === "delete") {
    return confirmBox(`Remove “${it.info?.song || it.name}” from your library? The PDF stays in its folder.`, async () => {
      await api().delete_song(it.song);
      refreshLibrary();
    });
  }
  if (it.song) openSong(it.song, state.screen);
  else api().open_path(it.path);
}
$("#library").addEventListener("click", onCardClick);
$("#recent").addEventListener("click", onCardClick);

function confirmBox(text, yes) {
  $("#confirm-text").textContent = text;
  $("#confirm").hidden = false;
  $("#confirm-no").focus();
  $("#confirm-yes").onclick = () => { $("#confirm").hidden = true; yes(); };
  $("#confirm-no").onclick = () => { $("#confirm").hidden = true; };
}

// ---- area: frame + crop box ------------------------------------------------------------------
let frameReq = 0;
async function loadFrame(t) {
  const id = ++frameReq;
  $("#scrub-time").textContent = clock(t);
  const src = await api().frame(t);
  if (id !== frameReq || !src) return;
  const img = $("#frame");
  img.onload = placeBox;
  img.src = src;
}

$("#scrub").addEventListener("input", (e) => {
  const t = (e.target.value / 1000) * state.area.duration;
  $("#scrub-time").textContent = clock(t);
  clearTimeout(loadFrame.t);
  loadFrame.t = setTimeout(() => loadFrame(t), 60);
});

const frameScale = () => $("#frame").clientWidth / state.area.width;

function placeBox() {
  const img = $("#frame"), stage = $("#stage"), crop = $("#crop");
  if (!img.naturalWidth || !state.box) return;
  const s = frameScale();
  const ir = img.getBoundingClientRect(), sr = stage.getBoundingClientRect();
  const [x, y, w, h] = state.box;
  Object.assign(crop.style, {
    left: `${ir.left - sr.left + x * s}px`, top: `${ir.top - sr.top + y * s}px`,
    width: `${w * s}px`, height: `${h * s}px`,
  });
}
window.addEventListener("resize", placeBox);

(function dragBox() {
  const crop = $("#crop");
  let start = null;
  crop.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    crop.setPointerCapture(e.pointerId);
    start = { x: e.clientX, y: e.clientY, box: [...state.box], h: e.target.dataset.h || "move" };
  });
  crop.addEventListener("pointermove", (e) => {
    if (!start) return;
    const s = frameScale(), W = state.area.width, H = state.area.height, min = 40;
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
    state.box = [x, y, w, h].map(Math.round);
    placeBox();
  });
  const end = () => (start = null);
  crop.addEventListener("pointerup", end);
  crop.addEventListener("pointercancel", end);
})();

$("#reset-box").onclick = () => { state.box = [...state.area.region]; placeBox(); };
$("#scan-btn").onclick = async () => {
  $("#work-detail").textContent = "";
  await api().scan(state.box);
};

// ---- review -------------------------------------------------------------------------------------
let measureCtx;
function fitText(text, font, maxW) {
  measureCtx ||= document.createElement("canvas").getContext("2d");
  measureCtx.font = font;
  return measureCtx.measureText(text).width <= maxW;
}

function renderReview() {
  const r = state.review, box = $("#pages");
  $("#saved-name").textContent = r.name || "Nothing to save. Every strip is left out.";
  $$(".saved .btn").forEach((b) => (b.disabled = !r.pdf));
  fillDetails(r.info || {});
  syncSegs();

  const ul = $("#strips");
  $("#strip-count").textContent = `· ${r.strips.length}`;
  ul.innerHTML = r.strips.map((s) => `
    <li class="${state.removed.has(s.i) ? "out" : ""}" data-i="${s.i}" title="${state.removed.has(s.i) ? "Put this strip back" : "Leave this strip out"}">
      <img alt="" src="${s.src}">
      <span class="when"><span>${clock(s.t0)}–${clock(s.t1)}</span><span>${s.screens > 1.3 ? `${s.screens} screens, scrolled` : ""}</span></span>
    </li>`).join("");

  // pages, laid out the way write_pdf lays them out (units are points)
  box.innerHTML = "";
  const P = r.paper;
  if (!P || !r.rows.length) return;
  const pageW = Math.min(720, box.clientWidth - 56);
  const k = pageW / P.w, M = P.margin, cw = P.w - 2 * M;
  const meta = state.meta || {}, info = r.info || {};
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
  f.song.value = i.song || "";
  f.artist.value = i.artist || "";
  f.capo.value = i.capo ?? "";
  f.tuning.value = i.tuning || "";
  f.key.value = i.key || "";
  f.tempo.value = i.tempo || "";
  f.chords.value = (i.chords || []).join(" ");
  $("#key-guess").hidden = !i.key_guessed;
}

$("#details").addEventListener("change", (e) => {
  const name = e.target.name;
  if (name) push({ info: { [name]: e.target.value } });
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
  pushing = pushing.then(() => api().update(changes));
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

// ---- actions ---------------------------------------------------------------------------------------
document.addEventListener("click", async (e) => {
  const a = e.target.closest("[data-action], [data-go]");
  if (!a) return;
  e.preventDefault();
  if (a.dataset.go) return go(a.dataset.go);
  const act = a.dataset.action;
  if (act === "open-pdf") api().open_path(state.review.pdf);
  if (act === "show-pdf") api().show_in_folder(state.review.pdf);
  if (act === "open-folder") api().show_in_folder(null);
  if (act === "play-current" && state.review?.song) openSong(state.review.song, "review");
  if (act === "theme") toggleTheme();
  if (act === "new") go("home");
});

$("#open-settings").onclick = () => {
  $("#out-dir").textContent = state.settings.out_dir;
  $("#out-dir").title = state.settings.out_dir;
  $("#settings").hidden = false;
};
$("#close-settings").onclick = () => { $("#settings").hidden = true; refreshLibrary(); };
$("#settings").addEventListener("click", (e) => { if (e.target.id === "settings") e.target.hidden = true; });
$("#pick-folder").onclick = async () => {
  state.settings = await api().pick_folder();
  $("#out-dir").textContent = state.settings.out_dir;
};

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { $("#settings").hidden = true; $("#confirm").hidden = true; }
  const typing = e.target.matches?.("input, select, textarea");
  if (!typing && (e.key === "d" || e.key === "D") && !e.ctrlKey && !e.metaKey) toggleTheme();
});

window.addEventListener("pywebviewready", async () => {
  state.settings = await api().get_settings();
  await document.fonts.ready;
  await refreshLibrary();
  show("home");
  $("#url").focus();
});
