// Reader: tab reflowed to the window (or the original pages), a cursor that moves through
// it, the transport bar, backing tracks and the metronome. Guitar Pro scores are drawn by
// gp.js, which plugs into the same transport. Loaded after app.js.
const play = {
  song: null,
  perLine: 2,          // null once the size slider has been dragged by hand
  scale: 1,
  mode: "scroll",      // or "pages": the PDF or picture as it came
  from: "library",
  engine: null,        // sheet (below) or gp (gp.js)
  speed: 1,
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
  play.engine?.stop();
  play.song = m;
  play.from = from;
  $("#play-title").textContent = i.song || m.title;
  $("#play-artist").textContent = [i.artist, m.notes].filter(Boolean).join("  ·  ");
  $("#bpm").value = m.bpm || i.tempo || 80;
  $("#beats").value = String(m.beats || 4);
  metro.setBeats(Number($("#beats").value));
  const vm = $$("#view-mode button");
  vm[0].textContent = m.kind === "gp" ? "Score" : "Tab";
  vm[2].textContent = m.kind === "gp" ? "PDF" : "Pages";
  vm[1].hidden = m.kind === "gp" || !m.tex_src;
  vm[2].hidden = !(m.pdf_src || m.original_src);
  $("#view-mode").hidden = vm.filter((b) => !b.hidden).length < 2;
  renderInfo();
  // other versions of this song (another arrangement, the PDF, the video), to switch between
  const lead = state.library.find((x) => x.song === id)?.lead;
  const vs = lead?.versions || [];
  $("#versions").hidden = vs.length < 2;
  $("#versions").innerHTML = vs.map((v) => `<option value="${esc(v.song)}" ${v.song === id ? "selected" : ""}>${esc([KIND[v.kind] || "Video", v.info?.song !== (lead.info?.song) ? v.name : "", v.folder || v.channel].filter(Boolean).join(" · "))}</option>`).join("");
  document.body.classList.add("playing");
  show("play");
  setSpeed(1);
  backing.load(m);
  if (m.kind === "gp") {
    $(".play").dataset.kind = "gp";
    $("#sheet").hidden = true;
    play.engine = gp;
    sound.load(m);
    return gp.open(m);
  }
  sheet.reset();
  $("#sheet").scrollTop = 0;
  // decode every bar first so widths are known before the first layout
  await Promise.all(m.segments.map((s) => new Promise((ok) => {
    const img = new Image();
    img.onload = () => { s.w = img.naturalWidth; s.h = img.naturalHeight; ok(); };
    img.onerror = ok;
    img.src = s.src;
  })));
  const mode = play.mode === "playable" && !m.tex_src ? "scroll" : play.mode;
  await setView(mode, true);
  sound.load(m);
}

// Tab (the pictures, reflowed), Playable (the notes read from them, as a score) or Pages
// (the PDF or picture as it came). Scores have Score and PDF.
async function setView(mode, first) {
  const m = play.song;
  if (!first) play.engine?.stop();
  if (m.kind === "gp") return gp.showPdf(mode === "pages");
  play.mode = mode;
  if (!first) saveReaderPrefs();
  $$("#view-mode button").forEach((b) => b.classList.toggle("on", b.dataset.mode === mode));
  if (mode === "playable") {
    $(".play").dataset.kind = "gp";
    $("#sheet").hidden = true;
    play.engine = gp;
    if (!first) sound.fit();
    await gp.open(m, { embedded: true });
    return;
  }
  gp.close();
  $(".play").dataset.kind = m.kind;
  $("#sheet").hidden = false;
  play.engine = sheet;
  if (!first) sound.fit();
  layoutSheet();
}

function renderInfo() {
  const m = play.song, i = m.info || {};
  const rows = [];
  if (i.album) rows.push(["Album", i.album]);
  if (i.year) rows.push(["Year", i.year]);
  if (i.genre) rows.push(["Genre", i.genre]);
  if (i.guitar) rows.push(["Guitar", i.guitar]);
  if (i.capo != null) rows.push(["Capo", i.capo === 0 ? "None" : `Fret ${i.capo}`]);
  if (i.tuning) rows.push(["Tuning", i.tuning]);
  if (i.key) rows.push([i.key_guessed ? "Key (guess)" : "Key", i.key]);
  if (i.tempo) rows.push(["Tempo", `${i.tempo} bpm`]);
  if (m.duration) rows.push(["Length", clock(m.duration)]);
  if (m.channel) rows.push(["Tab from", m.channel]);
  if (m.folder) rows.push(["Folder", m.folder]);
  $("#info-body").innerHTML = `
    ${m.art ? `<img class="cover" src="${m.art}" alt="">` : ""}
    <div><h2>${esc(i.song || m.title)}</h2>${i.artist ? `<p class="by">${esc(i.artist)}</p>` : ""}</div>
    <dl>${rows.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("")}</dl>
    ${(i.chords || []).length ? `<div class="group"><span class="group-label">Chords</span><div class="chips">${i.chords.map((c) => `<span class="chip">${esc(c)}</span>`).join("")}</div></div>` : ""}
    <div class="side-actions">
      <button class="btn ghost small" data-reader="details">Edit details</button>
      ${m.url ? `<button class="btn ghost small" data-reader="video">Watch on YouTube</button>` : ""}
      ${m.pdf ? `<button class="btn ghost small" data-reader="pdf">Open PDF</button>` : ""}
    </div>
    ${(m.files || []).length ? `<div class="group"><span class="group-label">Imported from</span>${m.files.map((f) => `<p class="hint" style="word-break:break-all">${esc(f)}</p>`).join("")}</div>` : ""}`;
}
$("#info-body").addEventListener("click", (e) => {
  const a = e.target.closest("[data-reader]")?.dataset.reader;
  if (a === "video") api().open_url(play.song.url);
  if (a === "pdf") api().open_path(play.song.pdf);
  if (a === "details") readerDetails();
});
function readerDetails() {
  const m = play.song;
  editDetails(m.id, m.info || {}, null, (info) => {
    m.info = info;
    $("#play-title").textContent = info.song || m.title;
    $("#play-artist").textContent = [info.artist, m.notes].filter(Boolean).join("  ·  ");
    renderInfo();
  });
}

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
  if (!m || m.kind === "gp" || play.mode === "playable") return;
  const view = $("#sheet");
  const hasTab = m.segments.length > 0;
  if (!hasTab) play.mode = "pages";                  // nothing to reflow: show it as it came
  const paged = play.mode === "pages";
  view.classList.toggle("paged", paged);
  view.classList.toggle("pic", paged && !!m.original_src && !m.pdf_src);
  $$("#view-mode button").forEach((b) => b.classList.toggle("on", b.dataset.mode === play.mode));
  $("#per-line").parentElement.hidden = paged;
  if (paged) {
    sheet.stop();
    const src = m.pdf_src || m.original_src || "";
    if (view.dataset.pdf === src) return;      // a resize: keep the viewer's place
    view.dataset.pdf = src;
    view.innerHTML = m.pdf_src ? `<iframe class="pdf" title="PDF" src="${m.pdf_src}#view=FitH"></iframe>`
      : m.original_src ? `<img class="original" alt="" src="${m.original_src}">`
      : `<p class="pdf-none">This song has no PDF yet. Open it in Review to make one.</p>`;
    return;
  }
  delete view.dataset.pdf;
  const { w: W } = sheetBox();
  const pageGap = m.unit * 0.03;
  if (play.perLine) play.scale = W / (m.unit * play.perLine + pageGap * (play.perLine - 1));
  $("#zoom").value = scaleToSlider(play.scale);
  $$("#per-line button").forEach((b) => b.classList.toggle("on", Number(b.dataset.n) === play.perLine));

  const s = play.scale;
  const keep = view.scrollTop / Math.max(1, view.scrollHeight);
  view.innerHTML = "";
  const i = m.info || {};
  const head = document.createElement("div");
  head.className = "shead";
  head.innerHTML = `<h1>${esc(i.song || m.title)}</h1>${i.artist ? `<p class="by">${esc(i.artist)}</p>` : ""}
    <div class="chips">${infoChips(i, { max: 20 })}</div>`;
  view.appendChild(head);

  // Bars from one scrolled strip (or one line of a PDF) sit flush, since they were cut on
  // barlines. Whole pages from swap-style videos get a small gap between them.
  const rows = [];
  let row = [], x = 0;
  m.segments.forEach((sg, idx) => {
    if (!sg.w) return;
    const prev = m.segments[idx - 1];
    const joined = prev && sg.kind === "bar" && prev.kind === "bar" && prev.group === sg.group;
    const w = sg.w * s;
    let gap = row.length && !joined ? pageGap * s : 0;
    if (row.length && x + gap + w > W + 0.5) { rows.push(row); row = []; x = 0; gap = 0; }
    row.push({ sg, idx, x: x + gap, w });
    x += gap + w;
  });
  if (row.length) rows.push(row);

  const rowGap = m.sp * s * 1.6;
  sheet.places = [];
  rows.forEach((r) => {
    const above = Math.max(...r.map((p) => p.sg.staff_y * s));
    const below = Math.max(...r.map((p) => (p.sg.h - p.sg.staff_y) * s));
    const div = document.createElement("div");
    div.className = "srow";
    div.style.height = `${above + below}px`;
    div.style.marginBottom = `${rowGap}px`;
    div.innerHTML = r.map((p) => `<img alt="" data-i="${p.idx}" src="${p.sg.src}" style="left:${p.x}px;top:${above - p.sg.staff_y * s}px;width:${p.w}px;height:${p.sg.h * s}px">`).join("");
    view.appendChild(div);
    for (const p of r) {
      sheet.places[p.idx] = { row: div, x: p.x, w: p.w, top: above - m.sp * s * 1.2, h: (p.sg.staff_h + m.sp * 2.4) * s };
    }
  });
  view.scrollTop = keep * view.scrollHeight;
  sheet.timeline();
  sheet.draw(false);
}

function turn(d) {
  if (play.mode === "pages" || play.song?.kind === "gp") return;
  const v = $("#sheet");
  v.scrollBy({ top: d * v.clientHeight * 0.85, behavior: "smooth" });
}

new ResizeObserver(() => state.screen === "play" && layoutSheet()).observe($("#sheet"));

function setPerLine(n) { play.perLine = n; saveReaderPrefs(); layoutSheet(); }
function setScale(s) { play.perLine = null; play.scale = Math.min(3, Math.max(0.12, s)); saveReaderPrefs(); layoutSheet(); }

$("#versions").addEventListener("change", (e) => { play.engine?.stop(); openSong(e.target.value, play.from); });
$("#per-line").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (b) setPerLine(Number(b.dataset.n));
});
$("#zoom").addEventListener("input", (e) => setScale(sliderToScale(Number(e.target.value))));
$("#view-mode").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  setView(b.dataset.mode);
});

function leavePlay(silent) {
  metro.stop();
  countIn.cancel();
  play.engine?.stop();
  backing.unload();
  gp.close();
  if (document.body.classList.contains("full")) toggleFull();
  document.body.classList.remove("playing");
  $("#metro-pop").hidden = true;
  $("#backing-pop").hidden = true;
  if (silent) return;                    // navigating somewhere else already
  if (play.from === "review" && state.review) show("review");
  else if (play.from === "add") { show("add"); refreshLibrary(); }
  else if (play.from === "explore") go("explore");
  else { show("library"); refreshLibrary(); }
}

// ---- transport -------------------------------------------------------------------------
function setSpeed(v) {
  play.speed = Math.min(1.5, Math.max(0.25, Math.round(v * 20) / 20));
  $("#t-speed").value = Math.round(play.speed * 100);
  $("#t-speed-val").textContent = `${Math.round(play.speed * 100)}%`;
  play.engine?.setSpeed?.(play.speed);
}
$("#t-speed").addEventListener("input", (e) => setSpeed(Number(e.target.value) / 100));
$("#t-play").onclick = () => transportToggle();
$("#t-start").onclick = () => play.engine?.toStart();
function transportToggle() {
  const e = play.engine;
  if (!e) return;
  if (e.on || countIn.timer) {
    countIn.cancel();
    e.stop();
    return;
  }
  metro.stop();                 // a free-running click gives way to the song's own beats
  if (sound.countIn) countIn.run(() => e.start());
  else e.start();
}
function transportState(on) {
  $("#t-play").classList.toggle("on", on);
  $("#t-play").setAttribute("aria-label", on ? "Pause" : "Play");
  if (on) clickTrack.start();
  else if (!countIn.timer) clickTrack.stop();
}
function transportTime(t, total) {
  $("#t-time").textContent = total ? `${clock(t)} / ${clock(total)}` : clock(t);
}

// ---- the click, following the song ------------------------------------------------------
// Clicks are placed on the song's own beats (play.engine.beats(), in song seconds) and
// scheduled a little ahead on the audio clock, whatever drives the song: the backing track,
// the score's player or the plain clock. So a click can sit on top of a backing track.
const clickTrack = {
  timer: 0, next: null, lastNow: 0,
  start() {
    clearInterval(this.timer);
    this.next = null;
    this.timer = setInterval(() => this.tick(), 25);
  },
  stop() {
    clearInterval(this.timer);
    this.timer = 0;
  },
  tick() {
    const e = play.engine;
    if (!e?.on || !sound.clicking()) { this.next = null; return; }
    metro.setup();
    if (metro.ctx.state !== "running") metro.ctx.resume();
    const now = e.now(), beats = e.beats(), rate = play.speed;
    if (this.next == null || now < this.lastNow - 0.05 || now > this.lastNow + 0.6) {
      this.next = beats.findIndex((b) => b.t >= now - 0.005);       // started or jumped
    }
    this.lastNow = now;
    while (this.next >= 0 && this.next < beats.length && beats[this.next].t <= now + 0.12 * rate) {
      const b = beats[this.next++];
      const when = metro.ctx.currentTime + Math.max(0, (b.t - now) / rate);
      metro.play(when, b.one ? "one" : "beat");
      metro.queue.push({ t: when, beat: b.beat });
      metro.kick();
    }
  },
};

// one bar of clicks at the current tempo before the song starts
const countIn = {
  timer: 0,
  run(then) {
    metro.setup();
    metro.ctx.resume();
    const e = play.engine, n = e.beatsPerBar(), step = 60 / (e.tempoNow() * play.speed);
    const t0 = metro.ctx.currentTime + 0.05;
    for (let k = 0; k < n; k++) {
      metro.play(t0 + k * step, k ? "beat" : "one");
      metro.queue.push({ t: t0 + k * step, beat: k });
    }
    metro.kick();
    $("#t-play").classList.add("on");
    this.timer = setTimeout(() => { this.timer = 0; then(); }, (0.05 + n * step) * 1000 - 15);
  },
  cancel() {
    clearTimeout(this.timer);
    this.timer = 0;
    $("#t-play").classList.remove("on");
  },
};

// ---- what you hear --------------------------------------------------------------------
// Backing track, the tab's own sound (scores, and tab read into a playable score), the
// click by itself, or nothing but the moving line. The click can go on top of any of them.
const SOUND = {
  backing: ["Backing track", "The song itself, from the backing track. The line follows the recording."],
  tab: ["Tab sound", "The tab played by the computer. Untick the parts you play yourself."],
  click: ["Click only", "Just the metronome, on the song's beats."],
  silent: ["Silent", "Nothing plays. The line moves at the tempo while you play."],
};
const sound = {
  mode: "silent", click: false, countIn: false, muted: [],
  scored() { return play.engine === gp; },
  options() { return ["backing", ...(this.scored() ? ["tab"] : []), "click", "silent"]; },
  load(m) {
    const s = m.sound || {};
    this.click = !!s.click;
    this.countIn = !!s.countIn;
    this.muted = s.muted || [];
    this.mode = s.mode || (m.backing_src ? "backing" : m.kind === "gp" ? "tab" : "silent");
    this.fit();
  },
  fit() {      // a choice the current view can't do falls back to the nearest one
    if (this.mode === "backing" && !backing.src) this.mode = this.scored() ? "tab" : "silent";
    if (this.mode === "tab" && !this.scored()) this.mode = "silent";
    this.render();
  },
  save() {
    api().set_song(play.song.id, { sound: { mode: this.mode, click: this.click, countIn: this.countIn, muted: this.muted } });
  },
  set(mode) {
    if (mode === "backing" && !backing.src) {
      return backingDialog(play.song.id, play.song.info?.song || play.song.title, (r) => { onNewBacking(r); sound.set("backing"); });
    }
    const was = this.mode;
    play.engine?.stop();
    this.mode = mode;
    this.save();
    this.render();
    play.engine?.soundChanged?.(was);
  },
  clicking() { return this.click || this.mode === "click"; },
  toggleClick() {
    this.click = !this.click;
    this.save();
    this.render();
    // stopped: a plain metronome at the tempo, like before; playing: on the song's beats
    if (!play.engine?.on) this.click ? metro.start(play.engine?.tempoNow?.()) : metro.stop();
  },
  render() {
    $("#t-backing-label").textContent = SOUND[this.mode][0];
    $("#t-backing").classList.toggle("on", this.mode !== "silent");
    $("#t-metro").classList.toggle("on", this.click);
    $("#t-countin").classList.toggle("on", this.countIn);
    if (!$("#backing-pop").hidden) this.renderPop();
  },
  renderPop() {
    const pop = $("#backing-pop"), opts = this.options(), gpSong = this.scored();
    const off = backing.offset(), anchors = (backing.info?.sync || []).length;
    const tracks = gpSong ? gp.api?.score?.tracks || [] : [];
    pop.innerHTML = `
      <div><p class="t-label">Hear while playing</p>
        <div class="seg sound-seg">${Object.keys(SOUND).map((k) => `<button data-sound="${k}" class="${this.mode === k ? "on" : ""}" ${opts.includes(k) ? "" : "disabled title=\"Switch to the Playable view to hear the tab\""}>${SOUND[k][0]}</button>`).join("")}</div>
        <p class="hint">${SOUND[this.mode][1]}</p></div>
      ${this.mode === "tab" && tracks.length > 1 ? `<div class="group"><span class="group-label">Parts you hear</span>
        ${tracks.map((t) => `<label class="check"><input type="checkbox" data-hear="${t.index}" ${this.muted.includes(t.index) ? "" : "checked"}> ${esc(t.name || `Part ${t.index + 1}`)}</label>`).join("")}</div>` : ""}
      ${backing.src ? `<div class="group">
        <span class="group-label">Backing track</span>
        <p class="b-name">${esc(backing.info.name || "Backing track")}</p>
        ${this.mode === "backing" ? `
        <label class="fld"><span>Volume</span><input type="range" id="b-vol" min="0" max="100" value="${Math.round((backing.info.volume ?? 1) * 100)}"></label>
        <span class="t-label">Sync</span>
        <div class="b-row">
          <button class="btn ghost small" data-nudge="-0.5">−0.5s</button>
          <button class="btn ghost small" data-nudge="-0.05">−50ms</button>
          <span class="mono" title="Where the tab starts in the audio">${off >= 0 ? "+" : ""}${off.toFixed(2)}s</span>
          <button class="btn ghost small" data-nudge="0.05">+50ms</button>
          <button class="btn ghost small" data-nudge="0.5">+0.5s</button>
        </div>
        <p class="hint">Line behind the music: press −. Ahead of it: press +.</p>
        ${backing.marking ? `<button class="btn primary small" id="b-now">${gpSong ? `Bar ${backing.marking.label} starts now` : "That bar starts now"}</button>
          <p class="hint">The audio is playing. Press the button the moment you hear the bar you picked begin.</p>`
        : `<button class="btn ghost small" id="b-mark">Set by ear</button>
          <p class="hint">Click the bar to line up (the first one is easiest), press Set by ear, and tap when you hear it.${gpSong ? " Do it again at a later bar if the song drifts." : ""}</p>`}
        ${off || anchors ? `<button class="link-btn" id="b-reset">Reset sync</button>` : ""}` : ""}
        <div class="side-actions"><button class="btn ghost small" id="b-change">Change</button></div>
      </div>` : `<button class="btn ghost small" id="b-change">Add a backing track</button>`}`;
  },
};
$("#t-metro").onclick = () => sound.toggleClick();
$("#t-countin").onclick = () => { sound.countIn = !sound.countIn; sound.save(); sound.render(); };
$("#t-backing").onclick = () => {
  const pop = $("#backing-pop");
  pop.hidden = !pop.hidden;
  $("#t-backing").setAttribute("aria-expanded", String(!pop.hidden));
  if (!pop.hidden) sound.renderPop();
};

// ---- tab without a score: a cursor driven by the clock or the backing track --------------
// Song time is the video's own clock for tab from videos (each bar knows when it was on
// screen). PDFs and pictures have no clock, so each bar lasts one bar of the metronome tempo.
const sheet = {
  places: [], spans: [], total: 0, t: 0, on: false, raf: 0, last: 0, cursor: null, _beats: null,
  reset() { this.stop(); this.t = 0; this.places = []; },
  timeline() {
    const m = play.song;
    this.spans = [];
    this._beats = null;
    if (m.segments.some((sg) => sg.t1 != null)) {
      m.segments.forEach((sg, i) => (this.spans[i] = [sg.t0, sg.t1]));
    } else {
      // one bar per bar image; a whole page without barlines counts by its width
      const bars = m.segments.filter((sg) => sg.kind === "bar").map((sg) => sg.w).sort((a, b) => a - b);
      const typical = bars.length ? bars[Math.floor(bars.length / 2)] : m.unit / 4;
      const perBar = (metro.beats * 60) / metro.bpm();
      let t = 0;
      m.segments.forEach((sg, i) => {
        const n = sg.kind === "bar" ? 1 : Math.max(1, Math.round((sg.w || typical) / typical));
        this.spans[i] = [t, t + n * perBar];
        t += n * perBar;
      });
    }
    const last = this.spans[this.spans.length - 1];
    this.total = last ? last[1] : 0;
    if (!this.on) this.t = Math.min(this.t, this.total);
    transportTime(this.t, this.total);
  },
  start0() { return this.spans[0]?.[0] || 0; },
  now() { return this.t; },
  tempoNow() { return metro.bpm(); },
  beatsPerBar() { return metro.beats; },
  beats() {
    if (!this._beats) {
      const step = 60 / metro.bpm(), out = [];
      for (let k = 0, t = this.start0(); t <= this.total + 1e-6; k++, t = this.start0() + k * step) {
        out.push({ t, beat: k % metro.beats, one: k % metro.beats === 0 && metro.accent });
      }
      this._beats = out;
    }
    return this._beats;
  },
  toggle() { this.on ? this.stop() : this.start(); },
  start() {
    if (play.mode === "pages" || !this.spans.length) return toast("Switch to the Tab view to play along.");
    if (this.t >= this.total - 0.05) this.t = this.start0();
    this.on = true;
    this.last = performance.now();
    transportState(true);
    backing.play(this.t);
    clearInterval(this.raf);
    this.raf = setInterval(() => {
      const now = performance.now(), dt = (now - this.last) / 1000;
      this.last = now;
      const bt = backing.songTime();
      this.t = bt != null ? bt : this.t + dt * play.speed;
      if (this.t >= this.total) { this.t = this.total; this.draw(true); return this.stop(); }
      this.draw(true);
    }, 30);
  },
  stop() {
    this.on = false;
    clearInterval(this.raf);
    transportState(false);
    backing.pause();
  },
  toStart() {
    this.t = this.start0();
    if (this.on) backing.play(this.t);
    this.draw(true);
    $("#sheet").scrollTop = 0;
  },
  seek(t) {
    this.t = Math.max(0, Math.min(this.total, t));
    if (this.on) backing.play(this.t);
    this.draw(false);
  },
  setSpeed(v) { backing.rate(v); },
  soundChanged() {},
  where() {
    const i = this.spans.findIndex(([a, b]) => this.t >= a && this.t < b);
    return i < 0 ? (this.t >= this.total ? this.spans.length - 1 : 0) : i;
  },
  draw(follow) {
    transportTime(this.t, this.total);
    const i = this.where(), p = this.places[i];
    if (!p) return;
    const [a, b] = this.spans[i];
    const x = p.x + p.w * Math.min(1, Math.max(0, (this.t - a) / Math.max(1e-6, b - a)));
    if (!this.cursor || !this.cursor.isConnected) {
      this.cursor = document.createElement("div");
      this.cursor.className = "cursor";
    }
    if (this.cursor.parentElement !== p.row) p.row.appendChild(this.cursor);
    Object.assign(this.cursor.style, { left: `${x - 1}px`, top: `${p.top}px`, height: `${p.h}px` });
    if (follow) {
      // keep the line being played about a quarter of the way down the window
      const v = $("#sheet"), r = p.row.getBoundingClientRect(), vr = v.getBoundingClientRect();
      if (r.top < vr.top + 10 || r.bottom > vr.bottom - vr.height * 0.25) {
        v.scrollTo({ top: v.scrollTop + r.top - vr.top - vr.height * 0.22, behavior: "smooth" });
      }
    }
  },
};
// click a bar to put the cursor there
$("#sheet").addEventListener("click", (e) => {
  const img = e.target.closest(".srow img[data-i]");
  if (!img || !sheet.spans.length) return;
  const i = Number(img.dataset.i), r = img.getBoundingClientRect();
  const [a, b] = sheet.spans[i];
  sheet.seek(a + (b - a) * Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)));
});

// ---- backing track -------------------------------------------------------------------------
// An <audio> element playing a copy loaded into memory, so seeking works on any file.
// offset: seconds into the audio where song time 0 falls. Guitar Pro scores add sync
// points on top (gp.js); here one offset does it.
const backing = {
  audio: null, url: null, info: null, src: null, marking: null,
  load(m) {
    this.unload();
    this.info = m.backing || null;
    this.src = m.backing_src || null;
    if (!this.src) return;
    const audio = (this.audio = new Audio());
    audio.preservesPitch = true;
    audio.volume = this.info.volume ?? 1;
    fetch(this.src).then((r) => r.blob()).then((b) => {
      if (this.audio !== audio) return;
      this.url = URL.createObjectURL(b);
      audio.src = this.url;
    }).catch(() => toast("Couldn't load the backing track."));
  },
  unload() {
    this.audio?.pause();
    if (this.url) URL.revokeObjectURL(this.url);
    this.audio = this.url = null;
    this.marking = null;
  },
  active() { return !!(this.audio && sound.mode === "backing" && this.audio.src); },
  wanted() { return !!(this.src && sound.mode === "backing"); },       // chosen, even if still loading
  offset() { return Number(this.info?.offset || 0); },
  play(songT) {
    if (!this.active()) return;
    this.audio.playbackRate = play.speed;
    this.audio.currentTime = Math.max(0, songT + this.offset());
    this.audio.play().catch(() => {});
  },
  pause() { this.audio?.pause(); },
  rate(v) { if (this.audio) this.audio.playbackRate = v; },
  songTime() {
    if (!this.active() || this.audio.paused) return null;
    return this.audio.currentTime - this.offset();
  },
  save(changes) {
    Object.assign(this.info, changes);
    api().backing_set(play.song.id, changes);
    sound.renderPop();
  },
};
function onNewBacking(r) {
  play.engine?.stop();
  play.song.backing = r.backing;
  play.song.backing_src = r.src;
  backing.load(play.song);
  sound.fit();
  play.engine?.backingChanged?.();
}
$("#backing-pop").addEventListener("input", (e) => {
  if (e.target.id === "b-vol") {
    const v = Number(e.target.value) / 100;
    if (backing.audio) backing.audio.volume = v;
    clearTimeout(backing.volT);
    backing.volT = setTimeout(() => backing.save({ volume: v }), 400);
  }
});
$("#backing-pop").addEventListener("change", (e) => {
  const i = e.target.dataset.hear;
  if (i === undefined) return;
  const k = Number(i);
  sound.muted = e.target.checked ? sound.muted.filter((x) => x !== k) : [...new Set([...sound.muted, k])];
  sound.save();
  play.engine?.soundChanged?.(sound.mode);
});
$("#backing-pop").addEventListener("click", (e) => {
  const pick = e.target.closest("[data-sound]")?.dataset.sound;
  if (pick) return sound.set(pick);
  const n = e.target.closest("[data-nudge]")?.dataset.nudge;
  if (n) {
    const d = Number(n);
    if (play.engine === gp && (backing.info.sync || []).length) {
      backing.save({ sync: backing.info.sync.map((a) => ({ ...a, ms: a.ms + d * 1000 })) });
    } else backing.save({ offset: Math.round((backing.offset() + d) * 1000) / 1000 });
    play.engine?.syncChanged?.();
  }
  if (e.target.id === "b-reset") { backing.save({ offset: 0, sync: [] }); play.engine?.syncChanged?.(); }
  if (e.target.id === "b-change") { $("#backing-pop").hidden = true; backingDialog(play.song.id, play.song.info?.song || play.song.title, onNewBacking); }
  if (e.target.id === "b-mark") {
    if (!backing.audio?.src) return toast("The backing track is still loading.");
    play.engine?.stop();
    backing.marking = play.engine.markPoint();
    // play the audio by itself from a little before where that bar should be
    backing.audio.playbackRate = 1;
    backing.audio.currentTime = Math.max(0, backing.marking.guess - 4);
    backing.audio.play().catch(() => {});
    sound.renderPop();
  }
  if (e.target.id === "b-now") {
    const at = backing.audio.currentTime;
    backing.audio.pause();
    play.engine.setMark(backing.marking, at);
    backing.marking = null;
    sound.renderPop();
    toast("Synced. Press play to check it.");
  }
});
sheet.markPoint = () => ({ t: sheet.t, guess: sheet.t + backing.offset() });
sheet.setMark = (mark, audioT) => backing.save({ offset: Math.round((audioT - mark.t) * 1000) / 1000 });

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
  if (act === "details") readerDetails();
  for (const [pop, btn] of [["#metro-pop", "#metro-more"], ["#backing-pop", "#t-backing"]]) {
    if (e.target.closest("#bt-dialog")) continue;
    if (!$(pop).hidden && !e.target.closest(`${pop}, ${btn}`) && !backing.marking) {
      $(pop).hidden = true;
      $(btn).setAttribute("aria-expanded", "false");
    }
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
  free: null,          // tempo for a free-running click on a score, which has no tempo box
  bpm() { return this.free || Math.min(300, Math.max(30, Math.round(Number($("#bpm").value)) || 80)); },
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
  start(tempo) {
    this.setup();
    this.ctx.resume();
    this.free = play.engine === gp && tempo ? Math.round(tempo * play.speed) : null;
    this.on = true;
    this.tick = 0;
    this.queue = [];
    this.next = this.ctx.currentTime + 0.06;
    this.timer = setInterval(() => this.schedule(), 25);
    this.kick();
  },
  stop() {
    this.on = false;
    clearInterval(this.timer);
    this.queue = [];
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
  // the beat dots: drawn while the free click or the song's click is going
  kick() {
    if (this.drawing) return;
    this.drawing = true;
    requestAnimationFrame(() => this.draw());
  },
  draw() {
    if (!this.on && !clickTrack.timer && !countIn.timer) {
      this.drawing = false;
      $$("#beat-dots i").forEach((d) => d.classList.remove("hit"));
      return;
    }
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
  if (play.song && play.song.kind !== "gp" && !play.song.segments.some((sg) => sg.t1 != null)) {
    const frac = sheet.total ? sheet.t / sheet.total : 0;      // PDF bars last one bar at this tempo
    sheet.timeline();
    sheet.t = frac * sheet.total;
    sheet.draw(false);
  }
  clearTimeout(bpmSave);
  bpmSave = setTimeout(() => play.song && api().set_song(play.song.id, { bpm: metro.bpm(), beats: metro.beats }), 600);
}
$$(".step-btn").forEach((b) => (b.onclick = () => { $("#bpm").value = metro.bpm() + Number(b.dataset.bpm); bpmChanged(); }));
$("#bpm").addEventListener("change", bpmChanged);

// Press and drag the number sideways to change the tempo (1 bpm every 4px, Shift for
// finer steps). A plain click still puts the cursor in the box for typing.
(function dragBpm() {
  const inp = $("#bpm");
  let start = null;
  inp.addEventListener("pointerdown", (e) => {
    if (document.activeElement === inp) return;        // already typing
    e.preventDefault();
    start = { x: e.clientX, v: metro.bpm(), moved: false };
    inp.setPointerCapture(e.pointerId);
  });
  inp.addEventListener("pointermove", (e) => {
    if (!start) return;
    const dx = e.clientX - start.x;
    if (!start.moved && Math.abs(dx) < 3) return;
    start.moved = true;
    document.body.classList.add("dragging-bpm");
    inp.value = Math.min(300, Math.max(30, start.v + Math.round(dx / (e.shiftKey ? 12 : 4))));
  });
  const end = () => {
    if (!start) return;
    document.body.classList.remove("dragging-bpm");
    if (start.moved) bpmChanged();
    else { inp.focus(); inp.select(); }
    start = null;
  };
  inp.addEventListener("pointerup", end);
  inp.addEventListener("pointercancel", end);
})();
$("#beats").addEventListener("change", (e) => { metro.setBeats(Number(e.target.value)); bpmChanged(); });

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
  if (state.screen !== "play" || e.target.matches?.("input[type=number], input[type=text], input[type=search], select, textarea")) return;
  if (!$("#details-dialog").hidden || !$("#bt-dialog").hidden) return;
  const k = e.key, gpSong = play.song?.kind === "gp";
  if (k === " ") { e.preventDefault(); transportToggle(); }
  else if (k === "Home") { e.preventDefault(); play.engine?.toStart(); }
  else if (k === "Escape") document.body.classList.contains("full") ? toggleFull() : leavePlay();
  else if (k === "f" || k === "F") toggleFull();
  else if (k === "[") setSpeed(play.speed - 0.05);
  else if (k === "]") setSpeed(play.speed + 0.05);
  else if (k === "i" || k === "I") toggleInfo();
  else if (k === "c" || k === "C" || k === "m" || k === "M") sound.toggleClick();
  else if (gpSong) gp.key(e);
  else if (k === "t" || k === "T") tap();
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
