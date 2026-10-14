// Play view: tab reflowed to the window, plus a metronome. Loaded after app.js.
const play = {
  song: null,
  perLine: 2,          // null once the size slider has been dragged by hand
  scale: 1,
  theme: "paper",
  from: "home",
};
try {
  const saved = JSON.parse(localStorage.getItem("tabz.play") || "{}");
  if (saved.theme) play.theme = saved.theme;
  if (saved.perLine !== undefined) play.perLine = saved.perLine;
  if (saved.scale) play.scale = saved.scale;
} catch {}
function savePlayPrefs() {
  try { localStorage.setItem("tabz.play", JSON.stringify({ theme: play.theme, perLine: play.perLine, scale: play.scale })); } catch {}
}

async function openSong(id, from) {
  const m = await api().load_song(id);
  if (!m) return;
  play.song = m;
  play.from = from;
  $("#play-title").textContent = m.title;
  $("#play-notes").textContent = [m.notes, m.channel].filter(Boolean).join("  ·  ");
  $("#bpm").value = m.bpm || Number((m.notes.match(/(\d+) bpm/) || [])[1]) || 80;
  $("#beats").value = String(m.beats || 4);
  metro.setBeats(Number($("#beats").value));
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

function sheetWidth() {
  const v = $("#sheet"), cs = getComputedStyle(v);
  return v.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
}

// the size slider is logarithmic, 0.12x to 3x of the source pixels
const Z_MIN = Math.log(0.12), Z_MAX = Math.log(3);
const sliderToScale = (v) => Math.exp(Z_MIN + (v / 1000) * (Z_MAX - Z_MIN));
const scaleToSlider = (s) => Math.round(((Math.log(s) - Z_MIN) / (Z_MAX - Z_MIN)) * 1000);

function layoutSheet() {
  const m = play.song;
  if (!m) return;
  const W = sheetWidth();
  const pageGap = m.unit * 0.03;
  if (play.perLine) play.scale = W / (m.unit * play.perLine + pageGap * (play.perLine - 1));
  $("#zoom").value = scaleToSlider(play.scale);
  $$("#per-line button").forEach((b) => b.classList.toggle("on", Number(b.dataset.n) === play.perLine));
  $$("#theme button").forEach((b) => b.classList.toggle("on", b.dataset.theme === play.theme));

  const s = play.scale, view = $("#sheet");
  view.classList.toggle("dark", play.theme === "dark");
  const keep = view.scrollTop / Math.max(1, view.scrollHeight);
  view.innerHTML = "";
  const head = document.createElement("div");
  head.className = "shead";
  head.innerHTML = "<h1></h1><p></p>";
  head.firstChild.textContent = m.title;
  head.lastChild.textContent = m.notes || "";
  head.lastChild.hidden = !m.notes;
  view.appendChild(head);

  // Bars from one scrolled strip sit flush, since they were cut on barlines.
  // Whole pages from swap-style videos get a small gap between them.
  const rows = [];
  let row = [], x = 0;
  m.segments.forEach((sg, i) => {
    if (!sg.w) return;
    const prev = m.segments[i - 1];
    const joined = prev && sg.kind === "bar" && prev.kind === "bar" && prev.group === sg.group;
    const w = sg.w * s;
    let gap = row.length && !joined ? pageGap * s : 0;
    if (row.length && x + gap + w > W + 0.5) { rows.push(row); row = []; x = 0; gap = 0; }
    row.push({ sg, x: x + gap, w });
    x += gap + w;
  });
  if (row.length) rows.push(row);

  const rowGap = m.sp * s * 1.6;
  for (const r of rows) {
    const above = Math.max(...r.map((p) => p.sg.staff_y * s));
    const below = Math.max(...r.map((p) => (p.sg.h - p.sg.staff_y) * s));
    const div = document.createElement("div");
    div.className = "srow";
    div.style.height = `${above + below}px`;
    div.style.marginBottom = `${rowGap}px`;
    for (const p of r) {
      const img = document.createElement("img");
      img.src = p.sg.src;
      img.alt = "";
      Object.assign(img.style, {
        left: `${p.x}px`, top: `${above - p.sg.staff_y * s}px`,
        width: `${p.w}px`, height: `${p.sg.h * s}px`,
      });
      div.appendChild(img);
    }
    view.appendChild(div);
  }
  view.scrollTop = keep * view.scrollHeight;
}

new ResizeObserver(() => state.screen === "play" && layoutSheet()).observe($("#sheet"));

function setPerLine(n) { play.perLine = n; savePlayPrefs(); layoutSheet(); }
function setScale(s) { play.perLine = null; play.scale = Math.min(3, Math.max(0.12, s)); savePlayPrefs(); layoutSheet(); }

$("#per-line").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (b) setPerLine(Number(b.dataset.n));
});
$("#zoom").addEventListener("input", (e) => setScale(sliderToScale(Number(e.target.value))));
$("#theme").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  play.theme = b.dataset.theme;
  savePlayPrefs();
  layoutSheet();
});

function leavePlay() {
  metro.stop();
  if (document.body.classList.contains("full")) toggleFull();
  document.body.classList.remove("playing");
  if (play.from === "review" && state.review) show("review");
  else { show("home"); refreshLibrary(); }
}

function toggleFull() {
  document.body.classList.toggle("full");
  api().toggle_fullscreen();
}

// ---- metronome --------------------------------------------------------------
// Web Audio look-ahead scheduling: clicks are queued a little ahead on the audio
// clock, so the timing holds steady even while the page is busy re-laying out.
const metro = {
  ctx: null, on: false, beats: 4, next: 0, beat: 0, timer: null, queue: [],
  setBeats(n) {
    this.beats = n;
    $("#beat-dots").innerHTML = Array.from({ length: n }, (_, i) => `<i class="${i ? "" : "one"}"></i>`).join("");
  },
  bpm() { return Math.min(300, Math.max(30, Math.round(Number($("#bpm").value)) || 80)); },
  start() {
    this.ctx ||= new AudioContext();
    this.ctx.resume();
    this.on = true;
    this.beat = 0;
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
      this.click(this.next, this.beat === 0);
      this.queue.push({ t: this.next, beat: this.beat });
      this.beat = (this.beat + 1) % this.beats;
      this.next += 60 / this.bpm();
    }
  },
  click(t, accent) {
    const o = this.ctx.createOscillator(), g = this.ctx.createGain();
    o.frequency.value = accent ? 1760 : 1175;
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(accent ? 0.7 : 0.45, t + 0.002);
    g.gain.exponentialRampToValueAtTime(0.0001, t + 0.05);
    o.connect(g).connect(this.ctx.destination);
    o.start(t);
    o.stop(t + 0.06);
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
metro.setBeats(4);

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
  if (state.screen !== "play" || e.target.matches("input[type=number], select")) return;
  const k = e.key, v = $("#sheet");
  if (k === " ") { e.preventDefault(); metro.on ? metro.stop() : metro.start(); }
  else if (k === "Escape") document.body.classList.contains("full") ? toggleFull() : leavePlay();
  else if (k === "f" || k === "F") toggleFull();
  else if (k === "t" || k === "T") tap();
  else if (k === "ArrowUp" || k === "ArrowDown") {
    e.preventDefault();
    $("#bpm").value = metro.bpm() + (k === "ArrowUp" ? 1 : -1) * (e.shiftKey ? 5 : 1);
    bpmChanged();
  } else if (k >= "1" && k <= "4") setPerLine(Number(k));
  else if (k === "+" || k === "=") setScale(play.scale * 1.1);
  else if (k === "-") setScale(play.scale / 1.1);
  else if (k === "PageDown" || k === "PageUp") {
    e.preventDefault();
    v.scrollBy({ top: (k === "PageDown" ? 1 : -1) * v.clientHeight * 0.85, behavior: "smooth" });
  }
});
