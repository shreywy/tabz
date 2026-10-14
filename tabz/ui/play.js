// Reader: tab reflowed to the window, scroll or page-turn, song details, metronome.
// Loaded after app.js.
const play = {
  song: null,
  perLine: 2,          // null once the size slider has been dragged by hand
  scale: 1,
  mode: "scroll",      // or "pages"
  page: 0,
  pages: [],           // [[row elements]] when paged
  from: "home",
};
try {
  const saved = JSON.parse(localStorage.getItem("tabz.reader") || "{}");
  Object.assign(play, { perLine: saved.perLine ?? 2, scale: saved.scale || 1, mode: saved.mode || "scroll" });
} catch {}
function saveReaderPrefs() {
  try { localStorage.setItem("tabz.reader", JSON.stringify({ perLine: play.perLine, scale: play.scale, mode: play.mode })); } catch {}
}

async function openSong(id, from) {
  const m = await api().load_song(id);
  if (!m) return;
  const i = m.info || {};
  setFollow(false);
  play.song = m;
  play.from = from;
  play.page = 0;
  $("#play-title").textContent = i.song || m.title;
  $("#play-artist").textContent = [i.artist, m.notes].filter(Boolean).join("  ·  ");
  $("#bpm").value = m.bpm || i.tempo || 80;
  play.baseBpm = i.tempo || Number($("#bpm").value);
  $("#beats").value = String(m.beats || 4);
  metro.setBeats(Number($("#beats").value));
  renderInfo();
  document.body.classList.add("playing");
  show("play");
  $("#sheet").scrollTop = 0;
  // decode every bar first so widths are known before the first layout
  await Promise.all(m.segments.map((s) => new Promise((ok) => {
    const img = new Image();
    img.onload = () => { s.w = img.naturalWidth; s.h = img.naturalHeight; ok(); };
    img.onerror = ok;
    img.src = s.src;
  })));
  layoutSheet();
}

function renderInfo() {
  const m = play.song, i = m.info || {};
  const rows = [];
  if (i.album) rows.push(["Album", i.album]);
  if (i.year) rows.push(["Year", i.year]);
  if (i.genre) rows.push(["Genre", i.genre]);
  if (i.capo != null) rows.push(["Capo", i.capo === 0 ? "None" : `Fret ${i.capo}`]);
  if (i.tuning) rows.push(["Tuning", i.tuning]);
  if (i.key) rows.push([i.key_guessed ? "Key (guess)" : "Key", i.key]);
  if (i.tempo) rows.push(["Tempo", `${i.tempo} bpm`]);
  rows.push(["Length", clock(m.duration)]);
  if (m.channel) rows.push(["Tab from", m.channel]);
  $("#info-body").innerHTML = `
    ${m.art ? `<img class="cover" src="${m.art}" alt="">` : ""}
    <div><h2>${esc(i.song || m.title)}</h2>${i.artist ? `<p class="by">${esc(i.artist)}</p>` : ""}</div>
    <dl>${rows.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("")}</dl>
    ${(i.chords || []).length ? `<div class="group"><span class="group-label">Chords</span><div class="chips">${i.chords.map((c) => `<span class="chip">${esc(c)}</span>`).join("")}</div></div>` : ""}
    <div class="side-actions">
      ${m.url ? `<button class="btn ghost small" data-reader="video">Watch on YouTube</button>` : ""}
      ${m.pdf ? `<button class="btn ghost small" data-reader="pdf">Open PDF</button>` : ""}
    </div>`;
}
$("#info-body").addEventListener("click", (e) => {
  const a = e.target.closest("[data-reader]")?.dataset.reader;
  if (a === "video") api().open_url(play.song.url);
  if (a === "pdf") api().open_path(play.song.pdf);
});

function sheetBox() {
  const v = $("#sheet"), cs = getComputedStyle(v);
  return {
    w: v.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight),
    h: v.clientHeight - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom),
  };
}

// the size slider is logarithmic, 0.12x to 3x of the source pixels
const Z_MIN = Math.log(0.12), Z_MAX = Math.log(3);
const sliderToScale = (v) => Math.exp(Z_MIN + (v / 1000) * (Z_MAX - Z_MIN));
const scaleToSlider = (s) => Math.round(((Math.log(s) - Z_MIN) / (Z_MAX - Z_MIN)) * 1000);

function layoutSheet() {
  const m = play.song;
  if (!m) return;
  const view = $("#sheet");
  view.classList.toggle("paged", play.mode === "pages");
  const { w: W, h: H } = sheetBox();
  const pageGap = m.unit * 0.03;
  if (play.perLine) play.scale = W / (m.unit * play.perLine + pageGap * (play.perLine - 1));
  $("#zoom").value = scaleToSlider(play.scale);
  $$("#per-line button").forEach((b) => b.classList.toggle("on", Number(b.dataset.n) === play.perLine));
  $$("#view-mode button").forEach((b) => b.classList.toggle("on", b.dataset.mode === play.mode));

  const s = play.scale;
  const keep = view.scrollTop / Math.max(1, view.scrollHeight);
  view.innerHTML = "";
  const i = m.info || {};
  const head = document.createElement("div");
  head.className = "shead";
  head.innerHTML = `<h1>${esc(i.song || m.title)}</h1>${i.artist ? `<p class="by">${esc(i.artist)}</p>` : ""}
    <div class="chips">${infoChips(i, { max: 20 })}</div>`;
  view.appendChild(head);

  // Bars from one scrolled strip sit flush, since they were cut on barlines.
  // Whole pages from swap-style videos get a small gap between them.
  const rows = [];
  let row = [], x = 0;
  m.segments.forEach((sg, idx) => {
    if (!sg.w) return;
    const prev = m.segments[idx - 1];
    const joined = prev && sg.kind === "bar" && prev.kind === "bar" && prev.group === sg.group;
    const w = sg.w * s;
    let gap = row.length && !joined ? pageGap * s : 0;
    if (row.length && x + gap + w > W + 0.5) { rows.push(row); row = []; x = 0; gap = 0; }
    row.push({ sg, x: x + gap, w });
    x += gap + w;
  });
  if (row.length) rows.push(row);

  const rowGap = m.sp * s * 1.6;
  const els = rows.map((r) => {
    const above = Math.max(...r.map((p) => p.sg.staff_y * s));
    const below = Math.max(...r.map((p) => (p.sg.h - p.sg.staff_y) * s));
    const div = document.createElement("div");
    div.className = "srow";
    div.style.height = `${above + below}px`;
    div.style.marginBottom = `${rowGap}px`;
    div.innerHTML = r.map((p) => `<img alt="" src="${p.sg.src}" style="left:${p.x}px;top:${above - p.sg.staff_y * s}px;width:${p.w}px;height:${p.sg.h * s}px">`).join("");
    div._h = above + below + rowGap;
    return div;
  });

  if (play.mode === "pages") {
    // group rows into screen-high pages; the header only sits on the first
    play.pages = [];
    let pg = [], used = head.offsetHeight + 24;
    for (const d of els) {
      if (pg.length && used + d._h > H) { play.pages.push(pg); pg = []; used = 0; }
      pg.push(d);
      used += d._h;
    }
    if (pg.length) play.pages.push(pg);
    play.page = Math.min(play.page, play.pages.length - 1);
    showPage();
  } else {
    els.forEach((d) => view.appendChild(d));
    view.scrollTop = keep * view.scrollHeight;
    $("#page-nav").hidden = true;
  }
}

function showPage() {
  const view = $("#sheet");
  view.querySelectorAll(".srow").forEach((d) => d.remove());
  $(".shead", view).hidden = play.page > 0;
  play.pages[play.page].forEach((d) => view.appendChild(d));
  $("#page-nav").hidden = false;
  $("#page-no").textContent = `${play.page + 1} / ${play.pages.length}`;
}
function turn(d) {
  if (play.mode !== "pages") {
    const v = $("#sheet");
    v.scrollBy({ top: d * v.clientHeight * 0.85, behavior: "smooth" });
    return;
  }
  const n = Math.min(play.pages.length - 1, Math.max(0, play.page + d));
  if (n !== play.page) { play.page = n; showPage(); }
}

new ResizeObserver(() => state.screen === "play" && layoutSheet()).observe($("#sheet"));

function setPerLine(n) { play.perLine = n; saveReaderPrefs(); layoutSheet(); }
function setScale(s) { play.perLine = null; play.scale = Math.min(3, Math.max(0.12, s)); saveReaderPrefs(); layoutSheet(); }

$("#per-line").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (b) setPerLine(Number(b.dataset.n));
});
$("#zoom").addEventListener("input", (e) => setScale(sliderToScale(Number(e.target.value))));
$("#view-mode").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  play.mode = b.dataset.mode;
  play.page = 0;
  saveReaderPrefs();
  layoutSheet();
});

function leavePlay(silent) {
  metro.stop();
  setFollow(false);
  if (document.body.classList.contains("full")) toggleFull();
  document.body.classList.remove("playing");
  $("#metro-pop").hidden = true;
  if (silent) return;                    // navigating somewhere else already
  if (play.from === "review" && state.review) show("review");
  else if (play.from === "home") { show("home"); refreshLibrary(); }
  else { show("library"); refreshLibrary(); }
}

// ---- follow: move through the song by itself ------------------------------------------
// Paced by how long the tab was on screen in the video, scaled by the tempo you set
// against the song's own tempo. Scroll mode glides; Pages mode turns pages.
function setFollow(on) {
  play.follow = on;
  $("#follow").classList.toggle("on", on);
  cancelAnimationFrame(play.followRaf);
  if (!on || !play.song) return;
  play.followLast = performance.now();
  play.pageClock = 0;
  const step = (now) => {
    if (!play.follow) return;
    const dt = (now - play.followLast) / 1000;
    play.followLast = now;
    const m = play.song;
    const base = m.info?.tempo || play.baseBpm || metro.bpm();
    const span = (m.span || m.duration || 180) * (base / metro.bpm());   // seconds for the whole song
    const v = $("#sheet");
    if (play.mode === "pages") {
      play.pageClock += dt;
      if (play.pageClock >= span / Math.max(1, play.pages.length)) {
        play.pageClock = 0;
        if (play.page < play.pages.length - 1) turn(1);
        else return setFollow(false);
      }
    } else {
      const total = v.scrollHeight - v.clientHeight;
      play.followPos = (play.followPos ?? v.scrollTop) + (total / span) * dt;
      if (Math.abs(play.followPos - v.scrollTop) > 40) play.followPos = v.scrollTop;   // the user scrolled
      v.scrollTop = play.followPos;
      if (v.scrollTop >= total - 1) return setFollow(false);
    }
    play.followRaf = requestAnimationFrame(step);
  };
  play.followPos = null;
  play.followRaf = requestAnimationFrame(step);
}
$("#follow").onclick = () => setFollow(!play.follow);

function toggleFull() {
  document.body.classList.toggle("full");
  api().toggle_fullscreen();
}

function toggleInfo() {
  const d = $("#info-drawer");
  d.hidden = !d.hidden;
  $('[data-action="info"]').classList.toggle("on", !d.hidden);
  layoutSheet();
}

document.addEventListener("click", (e) => {
  const act = e.target.closest("[data-action]")?.dataset.action;
  if (act === "play-back") leavePlay();
  if (act === "fullscreen") toggleFull();
  if (act === "info") toggleInfo();
  if (act === "page-prev") turn(-1);
  if (act === "page-next") turn(1);
  const pop = $("#metro-pop");
  if (!pop.hidden && !e.target.closest("#metro-pop, #metro-more")) {
    pop.hidden = true;
    $("#metro-more").setAttribute("aria-expanded", "false");
  }
});

// ---- metronome ---------------------------------------------------------------------
// Web Audio look-ahead scheduling: clicks are queued a little ahead on the audio clock,
// so the timing holds steady even while the page is busy re-laying out.
const metro = {
  ctx: null, out: null, noise: null, on: false,
  beats: 4, sub: 1, accent: true, sound: "beep",
  next: 0, tick: 0, timer: null, queue: [],
  setBeats(n) {
    this.beats = n;
    $("#beat-dots").innerHTML = Array.from({ length: n }, (_, i) => `<i class="${i ? "" : "one"}"></i>`).join("");
  },
  bpm() { return Math.min(300, Math.max(30, Math.round(Number($("#bpm").value)) || 80)); },
  setup() {
    if (this.ctx) return;
    this.ctx = new AudioContext();
    this.out = this.ctx.createGain();
    this.out.connect(this.ctx.destination);
    this.volume();
    // one second of white noise, reused for hats and rims
    const len = this.ctx.sampleRate;
    this.noise = this.ctx.createBuffer(1, len, len);
    const d = this.noise.getChannelData(0);
    for (let i = 0; i < len; i++) d[i] = Math.random() * 2 - 1;
  },
  volume() { if (this.out) this.out.gain.value = (Number($("#m-vol").value) / 100) ** 2 * 1.4; },
  start() {
    this.setup();
    this.ctx.resume();
    this.on = true;
    this.tick = 0;
    this.queue = [];
    this.next = this.ctx.currentTime + 0.06;
    this.timer = setInterval(() => this.schedule(), 25);
    $("#metro-go").textContent = "Stop";
    requestAnimationFrame(() => this.draw());
  },
  stop() {
    this.on = false;
    clearInterval(this.timer);
    this.queue = [];
    $("#metro-go").textContent = "Start";
    $$("#beat-dots i").forEach((d) => d.classList.remove("hit"));
  },
  schedule() {
    while (this.next < this.ctx.currentTime + 0.12) {
      const perBeat = this.sub, beat = Math.floor(this.tick / perBeat) % this.beats, part = this.tick % perBeat;
      const level = part ? "sub" : beat === 0 && this.accent ? "one" : "beat";
      this.play(this.next, level);
      if (!part) this.queue.push({ t: this.next, beat });
      this.tick++;
      this.next += 60 / this.bpm() / perBeat;
    }
  },
  env(t, peak, decay) {
    const g = this.ctx.createGain();
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(peak, t + 0.002);
    g.gain.exponentialRampToValueAtTime(0.0001, t + decay);
    g.connect(this.out);
    return g;
  },
  tone(t, freq, peak, decay, type = "sine", endFreq) {
    const o = this.ctx.createOscillator();
    o.type = type;
    o.frequency.setValueAtTime(freq, t);
    if (endFreq) o.frequency.exponentialRampToValueAtTime(endFreq, t + decay);
    o.connect(this.env(t, peak, decay));
    o.start(t);
    o.stop(t + decay + 0.02);
  },
  hiss(t, hp, peak, decay, bp) {
    const src = this.ctx.createBufferSource();
    src.buffer = this.noise;
    const f = this.ctx.createBiquadFilter();
    f.type = bp ? "bandpass" : "highpass";
    f.frequency.value = hp;
    if (bp) f.Q.value = bp;
    src.connect(f).connect(this.env(t, peak, decay));
    src.start(t, Math.random() * 0.5);
    src.stop(t + decay + 0.02);
  },
  play(t, level) {
    const v = { one: 1, beat: 0.7, sub: 0.32 }[level];
    switch (this.sound) {
      case "hat":
        this.hiss(t, level === "one" ? 6500 : 8000, 0.55 * v, level === "one" ? 0.09 : 0.045);
        break;
      case "wood":
        this.tone(t, level === "one" ? 1250 : 950, 0.8 * v, 0.06, "triangle");
        this.tone(t, level === "one" ? 2500 : 1900, 0.2 * v, 0.025, "sine");
        break;
      case "kit":
        if (level === "one") this.tone(t, 150, 1.0, 0.22, "sine", 45);
        this.hiss(t, 7500, 0.42 * v, level === "sub" ? 0.03 : 0.05);
        break;
      case "rim":
        this.hiss(t, 1800, 0.9 * v, 0.03, 4);
        this.tone(t, level === "one" ? 820 : 640, 0.35 * v, 0.03, "square");
        break;
      default:
        this.tone(t, level === "one" ? 1760 : level === "beat" ? 1175 : 880, 0.6 * v, 0.05);
    }
  },
  draw() {
    if (!this.on) return;
    const now = this.ctx.currentTime;
    while (this.queue.length > 1 && this.queue[1].t <= now) this.queue.shift();
    const cur = this.queue[0];
    if (cur && cur.t <= now) {
      $$("#beat-dots i").forEach((d, i) => d.classList.toggle("hit", i === cur.beat && now - cur.t < 0.12));
    }
    requestAnimationFrame(() => this.draw());
  },
};

// remember click settings across songs
try {
  const m = JSON.parse(localStorage.getItem("tabz.metro") || "{}");
  if (m.sound) $("#m-sound").value = m.sound;
  if (m.sub) $("#m-sub").value = String(m.sub);
  if (m.vol != null) $("#m-vol").value = m.vol;
  if (m.accent != null) $("#m-accent").value = m.accent ? "1" : "0";
} catch {}
function readMetroPrefs() {
  metro.sound = $("#m-sound").value;
  metro.sub = Number($("#m-sub").value);
  metro.accent = $("#m-accent").value === "1";
  metro.volume();
  try { localStorage.setItem("tabz.metro", JSON.stringify({ sound: metro.sound, sub: metro.sub, vol: Number($("#m-vol").value), accent: metro.accent })); } catch {}
}
readMetroPrefs();
metro.setBeats(4);
["#m-sound", "#m-sub", "#m-accent", "#m-vol"].forEach((s) => $(s).addEventListener("input", readMetroPrefs));
$("#m-sound").addEventListener("change", () => {
  if (metro.on) return;
  metro.setup();
  metro.ctx.resume();
  metro.play(metro.ctx.currentTime + 0.02, "one");       // preview the sound
});
$("#metro-more").onclick = () => {
  const p = $("#metro-pop");
  p.hidden = !p.hidden;
  $("#metro-more").setAttribute("aria-expanded", String(!p.hidden));
};

let bpmSave;
function bpmChanged() {
  $("#bpm").value = metro.bpm();
  clearTimeout(bpmSave);
  bpmSave = setTimeout(() => play.song && api().set_song(play.song.id, { bpm: metro.bpm(), beats: metro.beats }), 600);
}
$$(".step-btn").forEach((b) => (b.onclick = () => { $("#bpm").value = metro.bpm() + Number(b.dataset.bpm); bpmChanged(); }));
$("#bpm").addEventListener("change", bpmChanged);
$("#beats").addEventListener("change", (e) => { metro.setBeats(Number(e.target.value)); bpmChanged(); });
$("#metro-go").onclick = () => (metro.on ? metro.stop() : metro.start());

const taps = [];
function tap() {
  const now = performance.now();
  if (taps.length && now - taps[taps.length - 1] > 2000) taps.length = 0;
  taps.push(now);
  if (taps.length > 5) taps.shift();
  if (taps.length >= 2) {
    $("#bpm").value = Math.round(60000 / ((taps[taps.length - 1] - taps[0]) / (taps.length - 1)));
    bpmChanged();
  }
}
$("#tap").onclick = tap;

document.addEventListener("keydown", (e) => {
  if (state.screen !== "play" || e.target.matches?.("input[type=number], input[type=text], select")) return;
  const k = e.key;
  if (k === " ") { e.preventDefault(); metro.on ? metro.stop() : metro.start(); }
  else if (k === "Escape") document.body.classList.contains("full") ? toggleFull() : leavePlay();
  else if (k === "f" || k === "F") toggleFull();
  else if (k === "t" || k === "T") tap();
  else if (k === "i" || k === "I") toggleInfo();
  else if (k === "g" || k === "G") setFollow(!play.follow);
  else if (k === "ArrowUp" || k === "ArrowDown") {
    e.preventDefault();
    $("#bpm").value = metro.bpm() + (k === "ArrowUp" ? 1 : -1) * (e.shiftKey ? 5 : 1);
    bpmChanged();
  } else if (k >= "1" && k <= "4") setPerLine(Number(k));
  else if (k === "+" || k === "=") setScale(play.scale * 1.1);
  else if (k === "-") setScale(play.scale / 1.1);
  else if (["PageDown", "ArrowRight"].includes(k)) { e.preventDefault(); turn(1); }
  else if (["PageUp", "ArrowLeft"].includes(k)) { e.preventDefault(); turn(-1); }
});
