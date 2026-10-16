// Guitar Pro scores (.gp .gpx .gp5 .gp4 .gp3), drawn and played by alphaTab.
// Plugs into the reader's transport (play.js) as play.engine. Loaded after play.js.
const AT = window.alphaTab;
const VENDOR = new URL("vendor/alphatab/", location.href).href;
const NE = AT.NotationElement;

// What the View panel can switch, and the defaults for a song opened for the first time.
// Per-song choices are saved with the song; the rest carry over as your defaults.
const VIEW_DEFAULTS = {
  notation: true, tab: true, slash: false, numbered: false,
  rhythm: "auto",               // tab rhythm stems when notation is hidden
  layout: "page", barsPerRow: -1, scale: 1, stretch: 1,
  header: true, tuning: true, chordDiagrams: true, chordNames: true, tempo: true, dynamics: true,
  lyrics: true, barNumbers: true, trackNames: true, markers: true, techniques: true,
};
const ELEMENTS = {
  header: [NE.ScoreTitle, NE.ScoreSubTitle, NE.ScoreArtist, NE.ScoreAlbum, NE.ScoreWords, NE.ScoreMusic, NE.ScoreWordsAndMusic, NE.ScoreCopyright],
  tuning: [NE.GuitarTuning], chordDiagrams: [NE.ChordDiagrams], chordNames: [NE.EffectChordNames],
  tempo: [NE.EffectTempo], dynamics: [NE.EffectDynamics, NE.EffectCrescendo], lyrics: [NE.EffectLyrics],
  barNumbers: [NE.BarNumber], trackNames: [NE.TrackNames], markers: [NE.EffectMarker, NE.EffectText, NE.EffectDirections],
  techniques: [NE.EffectPalmMute, NE.EffectLetRing, NE.EffectTap, NE.EffectLeftHandTap, NE.EffectHarmonics, NE.EffectPickStroke,
    NE.EffectPickSlide, NE.EffectWhammyBar, NE.EffectWhammyBarLine, NE.EffectFingering, NE.EffectTrill, NE.EffectSlightBeatVibrato,
    NE.EffectWideBeatVibrato, NE.EffectSlightNoteVibrato, NE.EffectWideNoteVibrato, NE.EffectBeatBarre, NE.EffectGolpe,
    NE.EffectRasgueado, NE.EffectHammerOnPullOffText, NE.EffectSlideText, NE.EffectCapo],
};
const ELEMENT_LABELS = {
  header: "Title and credits", tuning: "Tuning", chordDiagrams: "Chord diagrams", chordNames: "Chord names",
  tempo: "Tempo marks", dynamics: "Dynamics", lyrics: "Lyrics", barNumbers: "Bar numbers", trackNames: "Part names",
  markers: "Sections and text", techniques: "Playing techniques",
};
const isGuitar = (t) => !t.staves[0].isPercussion && t.playbackInfo.program >= 24 && t.playbackInfo.program <= 31;
const tuningName = (staff) => {
  if (staff.isPercussion) return "Drums";
  const t = AT.model.Tuning.findTuning(staff.tuning);
  return t ? t.name.replace(/^(guitar|bass)\s+/i, "") : `${staff.tuning.length} strings`;
};

const gp = {
  api: null, song: null, view: null, shown: [], loop: false, countIn: false, click: false,
  on: false, mark: 0, playedIndex: 0, raf: 0,

  // embedded: the Playable view of tab read from pictures (m.tex_src), inside a song that
  // has its own Tab and Pages views
  async open(m, { embedded = false } = {}) {
    this.close();
    this.song = m;
    this.embedded = embedded;
    let defaults = {};
    try { defaults = JSON.parse(localStorage.getItem("tabz.gpview") || "{}"); } catch {}
    this.view = { ...VIEW_DEFAULTS, ...defaults, ...(m.view || {}) };
    if (embedded) {
      $("#score-wrap").hidden = false;
      $$("#view-mode button").forEach((b) => b.classList.toggle("on", b.dataset.mode === "playable"));
    } else this.showPdf(false);
    $("#score-loading").hidden = false;
    $("#score-loading").textContent = "Loading the score…";
    $("#gp-panel").hidden = localStorage.getItem("tabz.gppanel") !== "1";
    $("#gp-view-btn").setAttribute("aria-expanded", String(!$("#gp-panel").hidden));
    this.syncButtons();
    this.beatCache = null;
    const ext = backing.wanted();
    const api = (this.api = new AT.AlphaTabApi($("#score"), {
      core: { fontDirectory: `${VENDOR}font/`, logLevel: "warning" },
      display: this.displaySettings(),
      notation: this.notationSettings(),
      player: {
        enablePlayer: true, soundFont: `${VENDOR}soundfont/sonivox.sf2`, scrollElement: $("#score-wrap"),
        enableCursor: true, enableAnimatedBeatCursor: true, enableElementHighlighting: true, enableUserInteraction: true,
        playerMode: ext ? AT.PlayerMode.EnabledExternalMedia : AT.PlayerMode.EnabledSynthesizer,
        scrollOffsetY: -40,
      },
    }));
    api.error.on((e) => {
      $("#score-loading").hidden = false;
      $("#score-loading").textContent = `Couldn't read this score: ${e.message || e}`;
    });
    api.scoreLoaded.on((score) => this.scoreLoaded(score));
    api.renderFinished.on(() => ($("#score-loading").hidden = true));
    this.drawn = false;
    api.renderer.partialRenderFinished.on(() => { this.drawn = true; $("#score-loading").hidden = true; });   // first lines are up
    api.playerStateChanged.on((e) => {
      this.on = e.state === 1;
      this.pos = { t: this.pos?.t || 0, at: performance.now() };
      transportState(this.on);
      if (backing.wanted()) this.pump();
    });
    // times come scaled by the speed; show where we are in the song itself
    api.playerPositionChanged.on((e) => {
      const t = (e.currentTime * api.playbackSpeed) / 1000;
      this.pos = { t, at: performance.now() };
      transportTime(t, (e.endTime * api.playbackSpeed) / 1000);
    });
    api.playerReady.on(() => { this.hookBacking(); this.applySound(); });
    api.midiLoaded.on(() => { this.beatCache = null; this.applySync(); this.applySound(); });
    api.beatMouseDown.on((beat) => { this.mark = beat.voice.bar.masterBar.index; });
    api.playbackSpeed = play.speed;
    api.metronomeVolume = 0;          // the click and count-in are tabz's own (play.js), in every mode
    api.countInVolume = 0;
    if (m.tex_src && (embedded || !m.score_src)) {
      const tex = await (await fetch(m.tex_src)).text();
      if (this.api !== api) return;
      // parsed here and handed over like a loaded file: api.tex() right after creating the
      // player can be dropped before the renderer is ready
      const imp = new AT.importer.AlphaTexImporter();
      imp.initFromString(tex, api.settings);
      api.renderScore(imp.readScore(), [0]);
      // a render asked for before the music font and worker are up is dropped: ask again
      // until the first lines are drawn
      const again = () => { if (this.api === api && !this.drawn) { api.render(); setTimeout(again, 1500); } };
      setTimeout(again, 1200);
      return;
    }
    const data = new Uint8Array(await (await fetch(m.score_src)).arrayBuffer());
    if (this.api !== api) return;
    api.load(data, this.trackChoice(null));
  },

  close() {
    clearInterval(this.raf);
    if (this.api) { try { this.api.destroy(); } catch {} }
    this.api = null;
    this.on = false;
    $("#score").innerHTML = "";
    $("#score-wrap").hidden = true;
    $("#score-loading").hidden = true;
    $("#gp-panel").hidden = true;
  },

  // ---- what to show ---------------------------------------------------------------------
  displaySettings() {
    const v = this.view, dark = currentTheme() === "dark";
    const ink = dark ? "#e6e1d6" : "#1c1b19", soft = dark ? "#8f897c" : "#57534b";
    return {
      layoutMode: v.layout === "horizontal" ? AT.LayoutMode.Horizontal : AT.LayoutMode.Page,
      barsPerRow: v.barsPerRow, scale: v.scale, stretchForce: v.stretch,
      resources: {
        mainGlyphColor: ink, secondaryGlyphColor: soft, staffLineColor: dark ? "#6d685e" : "#a5a094",
        barSeparatorColor: dark ? "#8f897c" : "#57534b", barNumberColor: dark ? "#d86b4a" : "#b83a21", scoreInfoColor: ink,
      },
    };
  },
  notationSettings() {
    const v = this.view, elements = new Map();
    for (const [k, list] of Object.entries(ELEMENTS)) for (const e of list) elements.set(e, !!v[k]);
    const rhythm = v.rhythm === "off" ? AT.TabRhythmMode.Hidden : v.rhythm === "beams" ? AT.TabRhythmMode.ShowWithBeams
      : v.rhythm === "bars" ? AT.TabRhythmMode.ShowWithBars : (v.notation ? AT.TabRhythmMode.Hidden : AT.TabRhythmMode.ShowWithBeams);
    return { elements, rhythmMode: rhythm };
  },
  trackChoice(score) {
    // first time: one part, the first guitar (or the first thing that isn't drums)
    const saved = this.view.tracks;
    if (!score) return Array.isArray(saved) && saved.length ? saved : [0];
    const ok = (Array.isArray(saved) ? saved : []).filter((i) => i < score.tracks.length);
    if (ok.length) return ok;
    const t = score.tracks.find(isGuitar) || score.tracks.find((x) => !x.staves[0].isPercussion) || score.tracks[0];
    return [t.index];
  },
  scoreLoaded(score) {
    const want = this.trackChoice(score);
    this.shown = want;
    this.applyStaves(score);
    if (JSON.stringify(want) !== JSON.stringify(this.api.tracks.map((t) => t.index))) {
      this.api.renderTracks(want.map((i) => score.tracks[i]));
    }
    this.renderPanel();
    this.applySound();
    if (this.song.kind === "gp" && this.song.need_scan !== false) gpSendMeta(this.song.id, score);
  },

  // ---- what you hear: the parts, or nothing (the click is play.js's) ------------------------
  applySound() {
    const api = this.api, score = api?.score;
    if (!score || backing.wanted()) return;
    const silent = sound.mode !== "tab";
    for (const t of score.tracks) api.changeTrackMute([t], silent || sound.muted.includes(t.index));
  },
  soundChanged(was) {
    if ((was === "backing") !== (sound.mode === "backing")) return this.backingChanged();
    this.applySound();
  },
  // song time in seconds, between the player's position reports
  now() {
    if (!this.pos) return 0;
    return this.pos.t + (this.on ? ((performance.now() - this.pos.at) / 1000) * play.speed : 0);
  },
  // every beat of the song as played (repeats included), from each bar's time signature
  beats() {
    if (this.beatCache) return this.beatCache;
    const out = [], times = this.barTimes();
    times.forEach((b, i) => {
      const n = b.num || 4, end = i + 1 < times.length ? times[i + 1].at : b.at + b.dur;
      for (let k = 0; k < n; k++) out.push({ t: (b.at + ((end - b.at) * k) / n) / 1000, beat: k, one: k === 0 && metro.accent });
    });
    return (this.beatCache = out);
  },
  here() {
    const tick = this.api?.tickPosition || 0, bars = this.api?.tickCache?.masterBars || [];
    return bars.find((mb) => tick >= mb.start && tick < mb.end) || bars[0];
  },
  tempoNow() {
    const mb = this.here();
    return mb?.tempoChanges?.[0]?.tempo || this.api?.score?.tempo || 120;
  },
  beatsPerBar() { return this.here()?.masterBar?.timeSignatureNumerator || 4; },
  start() { this.api?.play(); },
  applyStaves(score) {
    const v = this.view;
    for (const t of score.tracks) {
      for (const st of t.staves) {
        if (st.isPercussion) continue;
        // never hide everything: with notation, tab and slash all off, keep the tab
        st.showStandardNotation = v.notation;
        st.showTablature = v.tab || (!v.notation && !v.slash && !v.numbered);
        st.showSlash = v.slash;
        st.showNumbered = v.numbered;
      }
    }
  },
  rerender(full) {
    const api = this.api;
    if (!api?.score) return;
    api.settings.fillFromJson({ display: this.displaySettings(), notation: this.notationSettings() });
    this.applyStaves(api.score);
    api.updateSettings();
    if (full) api.renderTracks(this.shown.map((i) => api.score.tracks[i]));
    else api.render();
  },
  saveView() {
    const { tracks, ...rest } = this.view;
    try { localStorage.setItem("tabz.gpview", JSON.stringify(rest)); } catch {}
    clearTimeout(this.saveT);
    this.saveT = setTimeout(() => api().set_song(this.song.id, { view: this.view }), 500);
  },

  renderPanel() {
    const api = this.api, score = api?.score;
    if (!score) return;
    const v = this.view;
    const seg = (key, opts) => `<div class="seg" data-v="${key}">${opts.map(([val, label]) =>
      `<button data-val="${val}" class="${String(v[key]) === String(val) ? "on" : ""}">${label}</button>`).join("")}</div>`;
    const check = (key, label) => `<label><input type="checkbox" data-v="${key}" ${v[key] ? "checked" : ""}> ${label}</label>`;
    $("#gp-panel").innerHTML = `
      <div class="group">
        <h3>Parts</h3>
        <p class="hint">Tick the parts to show. Pick what you hear from the Sound button below.</p>
        ${score.tracks.map((t) => `
          <div class="trk ${this.shown.includes(t.index) ? "on" : ""}">
            <input type="checkbox" data-track="${t.index}" ${this.shown.includes(t.index) ? "checked" : ""} aria-label="Show ${esc(t.name)}">
            <label for=""><span>${esc(t.name || `Part ${t.index + 1}`)}</span></label>
            <small>${esc(tuningName(t.staves[0]))}${t.staves[0].capo ? ` · capo ${t.staves[0].capo}` : ""}</small>
          </div>`).join("")}
      </div>
      <div class="group">
        <h3>Show</h3>
        <div class="checks">
          ${check("notation", "Notation")}${check("tab", "Tab")}${check("slash", "Rhythm slashes")}${check("numbered", "Numbers (jianpu)")}
        </div>
        <span class="t-label">Rhythm on the tab</span>
        ${seg("rhythm", [["auto", "Auto"], ["off", "Off"], ["beams", "Beams"], ["bars", "Stems"]])}
      </div>
      <div class="group">
        <h3>Layout</h3>
        ${seg("layout", [["page", "Pages"], ["horizontal", "One long line"]])}
        <label class="fld"><span>Bars per line</span>
          <select data-v="barsPerRow">${[[-1, "Auto"], [1, 1], [2, 2], [3, 3], [4, 4], [5, 5], [6, 6], [8, 8]].map(([n, l]) =>
            `<option value="${n}" ${v.barsPerRow === n ? "selected" : ""}>${l}</option>`).join("")}</select></label>
        <label class="fld"><span>Size ${Math.round(v.scale * 100)}%</span><input type="range" data-v="scale" min="0.5" max="2" step="0.05" value="${v.scale}"></label>
        <label class="fld"><span>Spacing</span><input type="range" data-v="stretch" min="0.5" max="1.6" step="0.05" value="${v.stretch}"></label>
      </div>
      <div class="group">
        <h3>Markings</h3>
        <div class="checks">${Object.entries(ELEMENT_LABELS).map(([k, l]) => check(k, l)).join("")}</div>
      </div>
      <div class="side-actions">
        <button class="btn ghost small" data-export>Save as a Guitar Pro file</button>
        <button class="link-btn" data-reset>Back to the standard view</button>
      </div>
      ${this.embedded ? `<p class="hint">These notes were read from the pictures of the tab, so some may be off, and rhythm is a guess from the spacing. The Tab view shows the original.</p>` : ""}`;
  },

  showPdf(on) {
    this.stop();
    $$("#view-mode button").forEach((b) => b.classList.toggle("on", (b.dataset.mode === "pages") === on));
    $("#score-wrap").hidden = on;
    const v = $("#sheet");
    v.hidden = !on;
    v.classList.toggle("paged", on);
    if (on && v.dataset.pdf !== this.song.pdf_src) {
      v.dataset.pdf = this.song.pdf_src;
      v.innerHTML = `<iframe class="pdf" title="PDF" src="${this.song.pdf_src}#view=FitH"></iframe>`;
    }
  },

  // ---- transport ------------------------------------------------------------------------
  toggle() { this.on ? this.stop() : this.start(); },
  stop() { if (this.api && this.on) this.api.pause(); },
  toStart() {
    if (!this.api) return;
    this.api.stop();
    $("#score-wrap").scrollTop = 0;
  },
  setSpeed(v) { if (this.api) this.api.playbackSpeed = v; },
  syncButtons() {
    $("#t-loop").classList.toggle("on", this.loop);
  },
  setLoop(on) {
    this.loop = on;
    if (this.api) {
      this.api.isLooping = on;
      if (!on) { this.api.playbackRange = null; this.api.clearPlaybackRangeHighlight?.(); }
    }
    this.syncButtons();
  },
  key(e) {
    const k = e.key.toLowerCase();
    if (k === "l") this.setLoop(!this.loop);
    else if (k === "v") toggleGpPanel();
  },

  // ---- backing track: the audio is the clock and the cursor follows it --------------------
  backingChanged() {
    const m = this.song;
    if (m) this.open({ ...m, view: this.view }, { embedded: this.embedded });
  },
  hookBacking() {
    const out = this.api?.player?.output;
    if (!backing.wanted() || !out || !("handler" in out)) return;
    const a = () => backing.audio;
    out.handler = {
      get backingTrackDuration() { return (a()?.duration || 0) * 1000; },
      get playbackRate() { return a()?.playbackRate || 1; },
      set playbackRate(v) { if (a()) a().playbackRate = v; },
      get masterVolume() { return a()?.volume ?? 1; },
      set masterVolume(v) {},
      seekTo(ms) { if (a()) a().currentTime = ms / 1000; },
      play() { a()?.play().catch(() => {}); },
      pause() { a()?.pause(); },
    };
    backing.audio.onended = () => this.api?.pause();
  },
  // A timer, not animation frames: this drives playback (end of song, loops), which has to
  // keep going while the window is in the background.
  pump() {
    clearInterval(this.raf);
    const out = this.api?.player?.output;
    if (!this.on || !out?.updatePosition) return;
    this.raf = setInterval(() => {
      if (!this.on || !backing.audio) return clearInterval(this.raf);
      out.updatePosition(backing.audio.currentTime * 1000);
    }, 30);
  },
  // Score time (ms) at the start of every bar as played, repeats included.
  barTimes() {
    const bars = this.api?.tickCache?.masterBars || [];
    let t = 0, tempo = this.api.score.tempo;
    return bars.map((mb) => {
      const at = t, num = mb.masterBar.timeSignatureNumerator;
      let tick = mb.start;
      for (const tc of mb.tempoChanges || []) {
        if (tc.tick > tick) { t += ((tc.tick - tick) * 60000) / (tempo * 960); tick = tc.tick; }
        tempo = tc.tempo;
      }
      t += ((mb.end - tick) * 60000) / (tempo * 960);
      return { at, dur: t - at, num, index: mb.masterBar.index };
    });
  },
  // Every bar gets a sync point: offset, plus any "this bar starts now" marks, stretched
  // between marks. One point alone makes alphaTab stretch the whole song to the audio.
  applySync() {
    const api = this.api;
    if (!api?.score || !backing.wanted()) return;
    const times = this.barTimes();
    if (!times.length) return;
    const anchors = (backing.info.sync || []).filter((a) => a.i < times.length).sort((a, b) => a.i - b.i);
    const off = backing.offset() * 1000;
    const map = (i) => {
      if (!anchors.length) return times[i].at + off;
      const a = [...anchors].reverse().find((x) => x.i <= i), b = anchors.find((x) => x.i > i);
      if (a && b) return a.ms + ((times[i].at - times[a.i].at) * (b.ms - a.ms)) / Math.max(1, times[b.i].at - times[a.i].at);
      if (a) return a.ms + times[i].at - times[a.i].at;
      return b.ms - (times[b.i].at - times[i].at);
    };
    const seen = new Map();
    const points = times.map((x, i) => {
      const occ = seen.get(x.index) || 0;
      seen.set(x.index, occ + 1);
      return { barIndex: x.index, barOccurence: occ, barPosition: 0, millisecondOffset: Math.max(0, map(i)) };
    });
    api.score.applyFlatSyncPoints(points);
    api.updateSyncPoints();
  },
  syncChanged() { this.applySync(); },

  // ---- a Guitar Pro file of what's showing (any score, including tab read from pictures) --------
  async exportGp() {
    const score = this.api?.score;
    if (!score) return;
    const bytes = new AT.exporter.Gp7Exporter().export(score, this.api.settings);
    let bin = "";
    for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    const r = await api().save_score(this.song.id, btoa(bin));
    if (r?.path) toast(`Saved ${r.name}`, [["Show in folder", () => api().show_in_folder(r.path)]], 9000);
    else toast(r?.error || "Couldn't save the file.");
  },
  markPoint() {
    // the bar last clicked (or the one under the cursor), as played
    const times = this.barTimes(), tick = this.api.tickPosition;
    const bars = this.api.tickCache?.masterBars || [];
    let i = bars.findIndex((mb) => tick >= mb.start && tick < mb.end);
    if (i < 0 || bars[i].masterBar.index !== this.mark) {
      const j = bars.findIndex((mb) => mb.masterBar.index === this.mark);
      if (j >= 0) i = j;
    }
    i = Math.max(0, i);
    const anchors = backing.info.sync || [];
    const guess = (anchors.length ? null : times[i].at / 1000 + backing.offset());
    return { i, label: (bars[i]?.masterBar.index ?? 0) + 1, guess: guess ?? times[i].at / 1000 + backing.offset() };
  },
  setMark(mark, audioT) {
    const anchors = (backing.info.sync || []).filter((a) => a.i !== mark.i);
    anchors.push({ i: mark.i, ms: Math.round(audioT * 1000) });
    backing.save({ sync: anchors.sort((a, b) => a.i - b.i) });
    this.applySync();
  },
};

function toggleGpPanel() {
  const p = $("#gp-panel");
  p.hidden = !p.hidden;
  $("#gp-view-btn").setAttribute("aria-expanded", String(!p.hidden));
  try { localStorage.setItem("tabz.gppanel", p.hidden ? "0" : "1"); } catch {}
  if (!p.hidden) gp.renderPanel();
}
$("#gp-view-btn").onclick = toggleGpPanel;
$("#t-loop").onclick = () => gp.setLoop(!gp.loop);

$("#gp-panel").addEventListener("change", (e) => {
  const t = e.target, v = gp.view;
  if (t.dataset.track) {
    const i = Number(t.dataset.track);
    gp.shown = t.checked ? [...new Set([...gp.shown, i])].sort((a, b) => a - b) : gp.shown.filter((x) => x !== i);
    if (!gp.shown.length) gp.shown = [i];             // always show something
    v.tracks = gp.shown;
    gp.saveView();
    gp.rerender(true);
    gp.renderPanel();
    return;
  }
  const key = t.dataset.v;
  if (!key) return;
  if (t.type === "checkbox") v[key] = t.checked;
  else if (key === "barsPerRow") v[key] = Number(t.value);
  else v[key] = Number(t.value);
  gp.saveView();
  gp.rerender(false);
  if (key === "scale") gp.renderPanel();
});
$("#gp-panel").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  const api = gp.api, score = api?.score;
  if (b.dataset.val !== undefined) {
    const key = b.closest("[data-v]").dataset.v;
    gp.view[key] = b.dataset.val;
    gp.saveView();
    gp.rerender(false);
    gp.renderPanel();
  }
  if (b.hasAttribute("data-export")) gp.exportGp();
  if (b.hasAttribute("data-reset")) {
    gp.view = { ...VIEW_DEFAULTS, tracks: gp.view.tracks };
    gp.saveView();
    gp.rerender(false);
    gp.renderPanel();
  }
});
// colours follow the theme
new MutationObserver(() => gp.api && gp.rerender(false)).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

// ---- reading imported scores --------------------------------------------------------------
// After an import, tabz asks the window to read each new score once: title, artist, tempo,
// tuning, capo and its parts, with the same library that draws it.
// How a part is played and how hard it is, from its notes. Style: lots of 3+ note chords is
// strumming; bass under a melody, or arpeggios across the strings, is fingerstyle; other
// single-note playing is riffs and lead. Difficulty grows with notes per second, techniques
// (bends, taps, harmonics, slides), reach up the neck and wide stretches, and muted hits.
function partStats(score, track) {
  const staff = track.staves[0], n = staff.tuning.length;
  let beats = 0, notes = 0, chords = 0, bassMelody = 0, dead = 0, tech = 0, maxFret = 0, stretch = 0, crossings = 0;
  for (const bar of staff.bars) {
    let lastString = null;
    for (const voice of bar.voices) {
      for (const b of voice.beats) {
        if (b.isRest || !b.notes.length) continue;
        beats++;
        notes += b.notes.length;
        const fretted = b.notes.filter((x) => !x.isDead && x.fret > 0).map((x) => x.fret);
        if (b.notes.length >= 3) chords++;
        const strings = b.notes.map((x) => x.string);
        if (b.notes.length === 2 && Math.max(...strings) - Math.min(...strings) >= 3) bassMelody++;
        if (b.notes.length === 1) {
          if (lastString != null && lastString !== strings[0]) crossings++;
          lastString = strings[0];
        }
        for (const x of b.notes) {
          if (x.isDead) dead++;
          if (x.hasBend || x.isHarmonic || x.slideOutType || x.isHammerPullOrigin || x.isLeftHandTapped || b.tap || x.isTrill) tech++;
          maxFret = Math.max(maxFret, x.fret || 0);
        }
        if (fretted.length > 1) stretch = Math.max(stretch, Math.max(...fretted) - Math.min(...fretted));
      }
    }
  }
  if (!beats) return null;
  // seconds: every bar at the score's tempo
  const sec = score.masterBars.reduce((t, mb) => t + (mb.calculateDuration() / 960) * (60 / (score.tempo || 120)), 0) || 1;
  const nps = notes / sec, chordRatio = chords / beats, single = 1 - chords / beats;
  const acoustic = [24, 25].includes(track.playbackInfo.program);
  let style = chordRatio >= 0.45 ? "Chords & strumming"
    : (bassMelody / beats >= 0.12 || (acoustic && crossings / beats >= 0.5)) ? "Fingerstyle" : "Riffs & lead";
  if (n <= 5 && !acoustic) style = "Bass line";
  // speed by attacks, not notes: a strummed six-string chord is one movement
  const bps = beats / sec;
  let d = Math.min(2.6, Math.max(0, (bps - 1.2) / 1.6));
  d += Math.min(1, (tech / Math.max(1, staff.bars.length)) / 1.2);
  d += maxFret > 12 ? 0.4 : 0;
  d += stretch >= 5 ? 0.4 : 0;
  d += Math.min(0.5, (dead / notes) * 2);
  if (style === "Fingerstyle") d += 0.3;              // two parts at once
  return {
    style, percussive: dead / notes > 0.12, level: Math.max(1, Math.min(5, 1 + Math.round(d))),
    nps: Math.round(nps * 10) / 10, bps: Math.round(bps * 10) / 10, chords: Math.round(chordRatio * 100) / 100, single: Math.round(single * 100) / 100,
  };
}

function scoreMeta(score) {
  const guitars = score.tracks.filter((t) => !t.staves[0].isPercussion);
  const first = guitars.find(isGuitar) || guitars[0];
  const tuning = first ? tuningName(first.staves[0]) : null;
  return {
    stats: first ? partStats(score, first) : null,
    song: score.title, artist: score.artist, album: score.album, tempo: Math.round(score.tempo) || null,
    tuning: tuning && tuning.replace(/ tuning$/i, ""),
    capo: first?.staves[0].capo || null,
    programs: score.tracks.filter((t) => !t.staves[0].isPercussion).map((t) => t.playbackInfo.program),
    tracks: score.tracks.map((t) => ({ name: t.name, program: t.playbackInfo.program, drums: t.staves[0].isPercussion, strings: t.staves[0].tuning.length })),
  };
}
async function gpSendMeta(sid, score) {
  try { await api().gp_meta(sid, scoreMeta(score)); } catch {}
}
let gpScanning = false;
window.gpScanPending = async () => {
  if (gpScanning) return;
  gpScanning = true;
  try {
    for (let round = 0; round < 3; round++) {
      const todo = await api().gp_pending();
      if (!todo.length) break;
      for (const [k, it] of todo.entries()) {
        try {
          let score;
          if (it.tex) {                          // notes read from pictures of tab
            const imp = new AT.importer.AlphaTexImporter();
            imp.initFromString(await (await fetch(it.src)).text(), new AT.Settings());
            score = imp.readScore();
          } else score = AT.importer.ScoreLoader.loadScoreFromBytes(new Uint8Array(await (await fetch(it.src)).arrayBuffer()));
          await api().gp_meta(it.song, it.tex ? { stats: scoreMeta(score).stats } : scoreMeta(score));
        } catch {
          await api().gp_meta(it.song, {});      // unreadable: don't ask again
        }
        if (k % 25 === 24) refreshLibrary();
      }
      refreshLibrary();
    }
  } finally {
    gpScanning = false;
  }
};
