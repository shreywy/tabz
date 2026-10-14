// UI for tabz. Python pushes events through tabz.on(); we call window.pywebview.api.*
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const api = () => window.pywebview.api;

const state = {
  screen: "home",
  stage: null,
  meta: null,
  area: null,      // {width, height, duration, region, found, time}
  box: null,       // [x, y, w, h] in source pixels
  review: null,
  removed: new Set(),
  settings: {},
};

// ---- helpers ------------------------------------------------------------
const clock = (s) => { s = Math.max(0, Math.round(s || 0)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
const today = () => new Date().toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
function when(ts) {
  const d = new Date(ts * 1000), now = new Date();
  const days = Math.floor((new Date(now.toDateString()) - new Date(d.toDateString())) / 864e5);
  if (days === 0) return "Today";
  if (days === 1) return "Yesterday";
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: d.getFullYear() === now.getFullYear() ? undefined : "numeric" });
}

function show(screen) {
  state.screen = screen;
  $$(".screen").forEach((s) => s.classList.toggle("on", s.dataset.screen === screen));
  const step = { home: "home", working: state.stage === "scan" ? "scan" : "home", area: "area", review: "review" }[screen];
  const order = ["home", "area", "scan", "review"];
  $$(".steps li").forEach((li) => {
    const i = order.indexOf(li.dataset.step), cur = order.indexOf(step);
    li.classList.toggle("now", i === cur);
    li.classList.toggle("done", i < cur);
  });
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

// ---- events from Python -------------------------------------------------------
window.tabz = {
  on(event, data) {
    const h = handlers[event];
    if (h) h(data);
  },
};

const handlers = {
  stage({ stage }) {
    state.stage = stage;
    show("working");
    const label = { download: "Downloading", locate: "Looking for the tab", scan: "Scanning" }[stage];
    $("#work-stage").textContent = label;
    $("#work-detail").textContent = {
      download: "Video only, no audio. It's deleted once you're done.",
      locate: "Reading a few frames for string lines and capo or tuning notes.",
      scan: "Watching the box for new bars.",
    }[stage];
    meter(stage === "locate" ? null : 0);
  },
  progress(p) { meter(p); },
  meta(m) {
    state.meta = m;
    $("#work-title").textContent = m.title;
    $("#work-notes").textContent = m.notes || "";
    $("#work-notes").hidden = !m.notes;
  },
  found(n) { $("#work-detail").textContent = `${n} ${n === 1 ? "strip" : "strips"} found so far`; },
  area(a) {
    state.area = a;
    state.box = [...a.region];
    $("#scrub-dur").textContent = clock(a.duration);
    $("#scrub").value = Math.round((a.time / a.duration) * 1000);
    $("#area-msg").classList.remove("error");
    $("#area-msg").textContent = a.found
      ? "The box is a guess. Drag it or its edges so it covers the string lines and the chord names above them. Scrub through the video to check it fits the whole way."
      : "tabz couldn't find tab lines by itself, so the box starts on the bottom third. Drag it over the tab, then scan.";
    const facts = $("#area-facts");
    facts.innerHTML = "";
    const rows = [["Video", clock(a.duration)], ...(state.meta?.notes ? [["Found", state.meta.notes]] : [])];
    for (const [k, v] of rows) facts.insertAdjacentHTML("beforeend", `<dt>${k}</dt><dd></dd>`), (facts.lastElementChild.textContent = v);
    show("area");
    loadFrame(a.time);
  },
  review(r) {
    state.review = r;
    state.removed = new Set(r.strips.filter((s) => s.removed).map((s) => s.i));
    $("#pages").classList.remove("busy");
    show("review");
    renderReview();
  },
  error(msg) {
    if (state.stage === "scan" && state.area) {
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

// ---- home -------------------------------------------------------------------
async function refreshLibrary() {
  const items = await api().library();
  const ul = $("#library");
  ul.innerHTML = "";
  for (const it of items) {
    const li = document.createElement("li");
    li.innerHTML = `<span class="lib-name"></span><span class="lib-when mono"></span>`;
    li.firstChild.textContent = it.name;
    li.lastChild.textContent = when(it.when);
    li.title = "Open PDF";
    li.onclick = () => api().open_path(it.path);
    ul.appendChild(li);
  }
  $("#library-empty").hidden = items.length > 0;
}

$("#link-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = $("#url").value.trim();
  if (!url) return;
  $("#home-error").hidden = true;
  state.meta = null;
  $("#work-title").textContent = "Fetching video…";
  $("#work-notes").hidden = true;
  await api().start(url);
});

// ---- area: frame + crop box ---------------------------------------------------
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

function scale() {
  const img = $("#frame");
  return img.clientWidth / state.area.width;
}

function placeBox() {
  const img = $("#frame"), stage = $("#stage"), crop = $("#crop");
  if (!img.naturalWidth || !state.box) return;
  const s = scale();
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
    const s = scale(), W = state.area.width, H = state.area.height, min = 40;
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

// ---- review -----------------------------------------------------------------------
let measureCtx;
function textLines(text, font, maxW) {
  measureCtx ||= document.createElement("canvas").getContext("2d");
  measureCtx.font = font;
  const words = text.split(/\s+/), lines = [];
  let cur = "";
  for (const w of words) {
    const t = cur ? `${cur} ${w}` : w;
    if (measureCtx.measureText(t).width <= maxW || !cur) cur = t;
    else { lines.push(cur); cur = w; }
  }
  if (cur) lines.push(cur);
  return lines;
}

function renderReview() {
  const r = state.review, box = $("#pages");
  $("#saved-name").textContent = r.name || "Nothing to save. Every strip is left out.";
  $$(".saved .btn").forEach((b) => (b.disabled = !r.pdf));
  if (document.activeElement !== $("#notes")) $("#notes").value = r.notes || "";
  syncSegs();

  // strips list
  const ul = $("#strips");
  ul.innerHTML = "";
  $("#strip-count").textContent = `· ${r.strips.length}`;
  for (const s of r.strips) {
    const li = document.createElement("li");
    li.classList.toggle("out", state.removed.has(s.i));
    li.innerHTML = `<img alt=""><span class="when"><span></span><span></span></span>`;
    li.querySelector("img").src = s.src;
    const [a, b] = li.querySelectorAll(".when span");
    a.textContent = `${clock(s.t0)}–${clock(s.t1)}`;
    b.textContent = s.screens > 1.3 ? `${s.screens} screens, scrolled` : "";
    li.title = state.removed.has(s.i) ? "Put this strip back" : "Leave this strip out";
    li.onclick = () => {
      state.removed.has(s.i) ? state.removed.delete(s.i) : state.removed.add(s.i);
      push({ removed: [...state.removed] });
    };
    ul.appendChild(li);
  }

  // pages, laid out the same way as the PDF (units are points, scaled to fit)
  box.innerHTML = "";
  const P = r.paper;
  if (!P || !r.rows.length) return;
  const pageW = Math.min(720, box.clientWidth - 56);
  const k = pageW / P.w, M = P.margin, cw = P.w - 2 * M;
  const meta = state.meta || {};
  let page, y;
  const newPage = () => {
    page = document.createElement("div");
    page.className = "page";
    page.style.width = `${pageW}px`;
    page.style.height = `${P.h * k}px`;
    box.appendChild(page);
    y = M;
  };
  const add = (cls, text, top, size, extra = {}) => {
    const el = document.createElement("div");
    el.className = cls;
    el.textContent = text;
    Object.assign(el.style, { left: `${M * k}px`, top: `${top * k}px`, fontSize: `${size * k}px`, ...extra });
    page.appendChild(el);
    return el;
  };
  newPage();
  const titleLines = textLines(meta.title || "", `600 17px Plex`, cw).slice(0, 2);
  add("p-title", titleLines.join("\n"), y + 3, 17, { whiteSpace: "pre", lineHeight: `${20 * k}px` });
  y += 20 * titleLines.length;
  const notes = r.notes || "";
  if (notes) { y += 17; add("p-notes", notes, y - 11, 10.5); }
  y += 15;
  add("p-meta", [meta.channel, clock(meta.duration), today()].filter(Boolean).join("   ·   "), y - 9, 9);
  y += 14;
  add("p-link", meta.url || "", y - 8.5, 8.5);
  y += 12;
  add("p-rule", "", y, 1, { width: `${cw * k}px`, height: "1px" });
  y += 16;

  let n = 1;
  const foot = () => {
    add("p-foot", meta.title || "", P.h - 30, 7.5, { width: `${cw * 0.85 * k}px`, overflow: "hidden", whiteSpace: "nowrap", textOverflow: "ellipsis" });
    const num = add("p-foot", String(n), P.h - 30, 7.5);
    num.style.left = "auto"; num.style.right = `${M * k}px`;
  };
  for (const row of r.rows) {
    if (y + row.h > P.h - M - 10) { foot(); newPage(); n++; }
    const img = document.createElement("img");
    img.className = "row";
    img.src = row.src;
    Object.assign(img.style, { left: `${M * k}px`, top: `${y * k}px`, width: `${row.w * k}px`, height: `${row.h * k}px` });
    page.appendChild(img);
    y += row.h + P.gap;
  }
  foot();
}

function syncSegs() {
  for (const seg of $$(".seg")) {
    const v = String(state.settings[seg.dataset.setting]);
    $$("button", seg).forEach((b) => b.classList.toggle("on", b.dataset.value === v));
  }
}

let pushing = Promise.resolve();
function push(changes) {
  $("#pages").classList.add("busy");
  pushing = pushing.then(() => api().update(changes));
}

$$(".seg").forEach((seg) =>
  seg.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    const key = seg.dataset.setting;
    let v = b.dataset.value;
    v = key === "per_row" ? Number(v) : key === "keep_color" ? v === "true" : v;
    if (state.settings[key] === v) return;
    state.settings[key] = v;
    syncSegs();
    push({ [key]: v });
  })
);

$("#notes").addEventListener("change", (e) => push({ notes: e.target.value }));
$("#notes").addEventListener("keydown", (e) => { if (e.key === "Enter") e.target.blur(); });
window.addEventListener("resize", () => state.screen === "review" && renderReview());

// ---- actions ----------------------------------------------------------------------
document.addEventListener("click", async (e) => {
  const a = e.target.closest("[data-action], [data-go]");
  if (!a) return;
  e.preventDefault();
  const act = a.dataset.action || a.dataset.go;
  if (act === "open-pdf") api().open_path(state.review.pdf);
  if (act === "show-pdf") api().show_in_folder(state.review.pdf);
  if (act === "open-folder") api().show_in_folder(null);
  if (act === "new" || act === "home") {
    if (state.screen === "working") return;   // don't abandon a running job
    await api().finish();
    state.review = null;
    $("#url").value = "";
    show("home");
    refreshLibrary();
    $("#url").focus();
  }
});

$("#open-settings").onclick = async () => {
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
document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("#settings").hidden = true; });

window.addEventListener("pywebviewready", async () => {
  state.settings = await api().get_settings();
  await document.fonts.ready;
  refreshLibrary();
  show("home");
  $("#url").focus();
});
