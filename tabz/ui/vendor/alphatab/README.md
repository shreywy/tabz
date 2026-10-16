alphaTab 1.8.4 (https://alphatab.net, MPL-2.0, see LICENSE), from the npm package
@coderline/alphatab: dist/alphaTab.min.js, the Bravura font (SIL OFL) and the Sonivox
soundfont (Apache 2.0).

One change to alphaTab.min.js: the web worker synth's `loadedMidiInfo` getter returned
itself and recursed forever (`return this.loadedMidiInfo`); it now returns its field
(`return this.Hu`, `_loadedMidiInfo` in the unminified source). Drop this when updating to a
release that fixes it.
