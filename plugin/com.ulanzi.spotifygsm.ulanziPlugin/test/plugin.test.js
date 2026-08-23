import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  ANIMATION_INTERVAL_MS,
  BRIDGE_ORIGIN,
  DEFAULT_PROGRESS_SETTINGS,
  SpotifyGSMTCPlugin,
  actionFromEvent,
  artworkDataUri,
  centeredTextPlacement,
  escapeXml,
  extrapolatePosition,
  formatProgressTime,
  formatRemaining,
  nextProgressMode,
  normalizeArtworkBundle,
  normalizeBridgeState,
  normalizeProgressSettings,
  progressTextLayout,
  renderProgressSvg,
  svgDataUri,
} from "../src/plugin.js";
import {
  normalizeInspectorSettings,
  serializeInspectorSettings,
} from "../property-inspector/progress/inspector.js";

const PNG_ARTWORK = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAIAAAABCAYAAAD0In+KAAAAEUlEQVR4nGP4z8Dwn6HhvwMAELoDvkeWIKAAAAAASUVORK5CYII=";
const GRAYSCALE_ARTWORK = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAIAAAABCAYAAAD0In+KAAAAEUlEQVR4nGP08fH5LyMj4wgADIYCeqb1R30AAAAASUVORK5CYII=";
const MOSAIC_TILES = Object.freeze([
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg==",
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNg+M9wAgADygHI/iVYRQAAAABJRU5ErkJggg==",
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYPjfAAACgwGAQ7v4AwAAAABJRU5ErkJggg==",
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4/5/BAQAHPgI/KNNAygAAAABJRU5ErkJggg==",
]);
const MOSAIC_ACTIONS = Object.freeze([
  "artwork-top-left", "artwork-top-right", "artwork-bottom-left", "artwork-bottom-right",
]);
const ARTWORK_ID_A = "a".repeat(64);
const ARTWORK_ID_B = "b".repeat(64);

function artworkBundle(id = ARTWORK_ID_A, overrides = {}) {
  return {
    id,
    color: PNG_ARTWORK,
    grayscale: GRAYSCALE_ARTWORK,
    tiles: MOSAIC_TILES,
    ...overrides,
  };
}

function createSdk() {
  const calls = [];
  const handlers = {};
  return {
    calls, handlers,
    onConnected(fn) { handlers.connected = fn; },
    onAdd(fn) { handlers.add = fn; },
    onRun(fn) { handlers.run = fn; },
    onClear(fn) { handlers.clear = fn; },
    onSetActive(fn) { handlers.setactive = fn; },
    onParamFromApp(fn) { handlers.paramfromapp = fn; },
    onParamFromPlugin(fn) { handlers.paramfromplugin = fn; },
    onDidReceiveSettings(fn) { handlers.settings = fn; },
    onClose(fn) { handlers.close = fn; },
    setPathIcon(...args) { calls.push(["path", ...args]); },
    setBaseDataIcon(...args) { calls.push(["base64", ...args]); },
    setSettings(...args) { calls.push(["settings", ...args]); },
  };
}

function response(payload, ok = true) {
  return { ok, async json() { return payload; } };
}

function state(overrides = {}) {
  return {
    available: true,
    revision: 4,
    is_playing: true,
    title: "Track",
    artist: "Artist",
    artwork_id: null,
    audio_available: true,
    volume_percent: 65,
    is_muted: false,
    audio_session_count: 1,
    audio_mixed: false,
    timeline_available: true,
    position_seconds: 30,
    duration_seconds: 180,
    playback_rate: 1,
    position_updated_at: "2026-08-23T12:00:00.000Z",
    updated_at: "2026-08-23T12:00:00.000Z",
    ...overrides,
  };
}

test("maps all twelve declared action UUIDs without changing existing UUIDs", () => {
  assert.equal(actionFromEvent({ uuid: "com.ulanzi.ulanzistudio.spotifygsm.nowplaying" }), "nowplaying");
  assert.equal(actionFromEvent({ uuid: "com.ulanzi.ulanzistudio.spotifygsm.previous" }), "previous");
  assert.equal(actionFromEvent({ uuid: "com.ulanzi.ulanzistudio.spotifygsm.toggle" }), "toggle");
  assert.equal(actionFromEvent({ uuid: "com.ulanzi.ulanzistudio.spotifygsm.next" }), "next");
  assert.equal(actionFromEvent({ uuid: "com.ulanzi.ulanzistudio.spotifygsm.volume-up" }), "volume-up");
  assert.equal(actionFromEvent({ uuid: "com.ulanzi.ulanzistudio.spotifygsm.volume-down" }), "volume-down");
  assert.equal(actionFromEvent({ uuid: "com.ulanzi.ulanzistudio.spotifygsm.mute-toggle" }), "mute-toggle");
  assert.equal(actionFromEvent({ uuid: "com.ulanzi.ulanzistudio.spotifygsm.progress" }), "progress");
  for (const action of MOSAIC_ACTIONS) {
    assert.equal(actionFromEvent({ uuid: `com.ulanzi.ulanzistudio.spotifygsm.${action}` }), action);
  }
  assert.equal(actionFromEvent({ uuid: "com.ulanzi.ulanzistudio.spotifygsm.volume" }), null);
});

test("normalizes media and audio availability independently", () => {
  const normalized = normalizeBridgeState(
    state({ available: false, audio_available: true, volume_percent: 65 }),
    Date.parse("2026-08-23T12:00:01.000Z"),
  );
  assert.equal(normalized.online, true);
  assert.equal(normalized.available, false);
  assert.equal(normalized.audioAvailable, true);
  assert.equal(normalized.volumePercent, 65);
});

test("normalizes timeline values and rejects non-finite data", () => {
  const now = Date.parse("2026-08-23T12:00:01.000Z");
  const valid = normalizeBridgeState(state(), now);
  assert.equal(valid.timelineAvailable, true);
  assert.equal(valid.positionSeconds, 30);
  const invalid = normalizeBridgeState(state({
    position_seconds: Number.NaN,
    duration_seconds: Number.POSITIVE_INFINITY,
    playback_rate: Number.NaN,
  }), now);
  assert.equal(invalid.timelineAvailable, false);
});

test("extrapolates playing rates, freezes pause, and clamps at duration", () => {
  const anchor = 1_000_000;
  const playing = {
    timelineAvailable: true, isPlaying: true, positionSeconds: 20,
    durationSeconds: 30, playbackRate: 2, positionUpdatedAt: anchor,
  };
  assert.equal(extrapolatePosition(playing, anchor + 3000), 26);
  assert.equal(extrapolatePosition(playing, anchor + 10000), 30);
  assert.equal(extrapolatePosition({ ...playing, isPlaying: false }, anchor + 10000), 20);
});

test("formats remaining time with ceil and hour support", () => {
  assert.equal(formatRemaining(0), "0:00");
  assert.equal(formatRemaining(0.01), "0:01");
  assert.equal(formatRemaining(65), "1:05");
  assert.equal(formatRemaining(3661), "1:01:01");
});

test("cycles progress modes and formats each mode with its rounding semantics", () => {
  assert.equal(nextProgressMode("remaining"), "elapsed");
  assert.equal(nextProgressMode("elapsed"), "total");
  assert.equal(nextProgressMode("total"), "remaining");
  assert.equal(nextProgressMode(undefined), "elapsed");

  assert.equal(formatProgressTime("remaining", 60.9, 3661.2), "1:00:01");
  assert.equal(formatProgressTime("elapsed", 60.9, 3661.2), "1:00");
  assert.equal(formatProgressTime("total", 60.9, 3661.2), "1:01:02");
  assert.equal(formatProgressTime("elapsed", 4000, 3661.2), "1:01:01");
  assert.equal(formatProgressTime("remaining", -1, 65), "1:05");
});

test("builds secure 196px circular SVG and data URI", () => {
  const svg = renderProgressSvg({
    progress: 0.5,
    text: "<&\"'>",
    settings: { ...DEFAULT_PROGRESS_SETTINGS, progressColor: "#ABCDEF" },
  });
  assert.match(svg, /width="196" height="196" viewBox="0 0 196 196"/);
  assert.match(svg, /stroke="#ABCDEF"/);
  assert.match(svg, /rotate\(-90 98 98\)/);
  assert.match(svg, /stroke-linecap="round"/);
  assert.ok(!svg.includes("<&"));
  assert.equal(escapeXml("<&\"'>"), "&lt;&amp;&quot;&apos;&gt;");
  const uri = svgDataUri(svg);
  assert.match(uri, /^data:image\/svg\+xml;base64,/);
  assert.equal(Buffer.from(uri.split(",")[1], "base64").toString("utf8"), svg);
});

test("sizes duration labels within the padded circle chord at default and max stroke", () => {
  const expectedSizes = {
    14: { "3:02": 42, "0:00": 42, "10:00": 42, "59:59": 42,
      "1:00:00": 32, "12:34:56": 28 },
    30: { "3:02": 42, "0:00": 42, "10:00": 36, "59:59": 36,
      "1:00:00": 28, "12:34:56": 24 },
  };

  for (const strokeWidth of [14, 30]) {
    for (const [text, expectedFontSize] of Object.entries(expectedSizes[strokeWidth])) {
      const layout = progressTextLayout(text, strokeWidth);
      assert.equal(layout.fontSize, expectedFontSize, `${text} at stroke ${strokeWidth}`);
      assert.ok(layout.renderedWidth <= layout.maxWidth, `${text} fits stroke ${strokeWidth}`);
      assert.equal(layout.constrained, false);
    }
  }
  assert.deepEqual(progressTextLayout("12:34:56", 99), progressTextLayout("12:34:56", 30));
});

test("uses deterministic production sizing and constrains impossible hour labels", () => {
  const first = renderProgressSvg({ text: "12:34:56", settings: { strokeWidth: 30 } });
  const second = renderProgressSvg({ text: "12:34:56", settings: { strokeWidth: 30 } });
  assert.equal(first, second);
  assert.match(first, /font-size="24"/);
  assert.ok(!first.includes("textLength="));

  const extreme = "123456789012345678901234567890:00:00";
  const layout = progressTextLayout(extreme, 30);
  const svg = renderProgressSvg({ text: extreme, settings: { strokeWidth: 30 } });
  assert.equal(layout.fontSize, 16);
  assert.equal(layout.constrained, true);
  assert.equal(layout.renderedWidth, layout.maxWidth);
  assert.match(svg, /font-size="16"[^>]*textLength="[\d.]+" lengthAdjust="spacingAndGlyphs"/);
});

test("centers progress labels with canvas alignment and measured glyph bounds", () => {
  const context = {
    measureText(text) {
      assert.ok(["3:02", "0:00", "12:34:56"].includes(text));
      return this.font.includes("28px")
        ? { actualBoundingBoxAscent: 21, actualBoundingBoxDescent: 6 }
        : { actualBoundingBoxAscent: 31, actualBoundingBoxDescent: 9 };
    },
  };

  for (const [text, fontSize, baseline] of [
    ["3:02", 42, 109], ["0:00", 42, 109], ["12:34:56", 28, 105.5],
  ]) {
    const svg = renderProgressSvg({ text, textContext: context });
    assert.match(svg, new RegExp(`<text x="98" y="${baseline}"[^>]*font-size="${fontSize}"`));
  }
  assert.equal(context.textAlign, "center");
  assert.equal(context.textBaseline, "alphabetic");
  assert.equal(context.font, "700 28px Arial, sans-serif");
});

test("uses a deterministic em-box baseline for production and unusable glyph bounds", () => {
  const productionSvg = renderProgressSvg({ text: "3:02" });
  assert.match(productionSvg, /<text x="98" y="110\.6"/);

  for (const metrics of [
    { width: 84 },
    { actualBoundingBoxAscent: 0, actualBoundingBoxDescent: 0 },
    { actualBoundingBoxAscent: -1, actualBoundingBoxDescent: 9 },
  ]) {
    const context = { measureText: () => metrics };
    assert.deepEqual(centeredTextPlacement(context, "0:00", 42), { x: 98, y: 110.6 });
  }
  assert.match(productionSvg, /text-anchor="middle"/);
  assert.ok(!productionSvg.includes("dominant-baseline"));
});

test("cycles progress contexts independently with immediate local-only renders", async () => {
  const sdk = createSdk();
  let requests = 0;
  const plugin = new SpotifyGSMTCPlugin({
    sdk,
    fetchImpl: async () => { requests += 1; throw new Error("unexpected request"); },
    now: () => 5000,
  });
  const settings = normalizeProgressSettings();
  plugin.contexts.set("one", { action: "progress", active: true, settings, mode: "remaining" });
  plugin.contexts.set("two", { action: "progress", active: true, settings, mode: "remaining" });
  plugin.lastState = { online: true, available: true, timelineAvailable: true,
    isPlaying: true, positionSeconds: 60.9, durationSeconds: 125.4,
    playbackRate: 1, positionUpdatedAt: 5000 };
  plugin.render("one", "progress", plugin.lastState, true);
  plugin.render("two", "progress", plugin.lastState, true);
  const seededRenders = sdk.calls.length;
  plugin.rendered.delete("one");
  plugin.render("one", "progress", plugin.lastState);
  assert.equal(sdk.calls.length, seededRenders, "normal advancing render is throttled");

  const renderedSvg = () => Buffer.from(
    sdk.calls.at(-1)[2].split(",")[1], "base64",
  ).toString("utf8");
  const arcs = [];
  for (const [mode, text] of [["elapsed", "1:00"], ["total", "2:06"], ["remaining", "1:05"]]) {
    const rendersBefore = sdk.calls.length;
    assert.equal(await plugin.run({ context: "one" }), true);
    assert.equal(plugin.entry("one").mode, mode);
    assert.equal(sdk.calls.length, rendersBefore + 1);
    const svg = renderedSvg();
    assert.match(svg, new RegExp(`>${text}</text>`));
    arcs.push(svg.match(/stroke-dasharray="([^"]+)"/)?.[1]);
  }
  assert.ok(arcs[0]);
  assert.deepEqual(arcs, [arcs[0], arcs[0], arcs[0]]);
  assert.equal(plugin.entry("two").mode, "remaining");
  assert.equal(await plugin.run({ context: "two" }), true);
  assert.equal(plugin.entry("two").mode, "elapsed");
  assert.equal(plugin.entry("one").mode, "remaining");
  assert.equal(requests, 0);
  assert.equal(sdk.calls.filter(([type]) => type === "settings").length, 0);
});

test("keeps total text stable while one-second animation advances the ring", () => {
  const sdk = createSdk();
  let clock = 5000;
  let nextId = 0;
  const timers = new Map();
  const plugin = new SpotifyGSMTCPlugin({
    sdk,
    now: () => clock,
    setIntervalImpl(fn, ms) { const id = ++nextId; timers.set(id, { fn, ms }); return id; },
    clearIntervalImpl(id) { timers.delete(id); },
  });
  plugin.contexts.set("progress", {
    action: "progress", active: true, settings: normalizeProgressSettings(), mode: "total",
  });
  plugin.lastState = { online: true, available: true, timelineAvailable: true,
    isPlaying: true, positionSeconds: 8, durationSeconds: 10,
    playbackRate: 1, positionUpdatedAt: clock };

  plugin.render("progress", "progress", plugin.lastState, true);
  const first = Buffer.from(sdk.calls.at(-1)[2].split(",")[1], "base64").toString("utf8");
  plugin.manageAnimation();
  assert.equal(timers.size, 1);
  const timerId = plugin.animationTimer;
  const timer = timers.get(timerId);
  assert.equal(timer.ms, ANIMATION_INTERVAL_MS);
  clock += ANIMATION_INTERVAL_MS;
  timer.fn();
  const second = Buffer.from(sdk.calls.at(-1)[2].split(",")[1], "base64").toString("utf8");

  assert.match(first, />0:10</);
  assert.match(second, />0:10</);
  assert.notEqual(
    first.match(/stroke-dasharray="([^"]+)"/)?.[1],
    second.match(/stroke-dasharray="([^"]+)"/)?.[1],
  );
  assert.equal(sdk.calls.length, 2);
  assert.equal(plugin.animationTimer, timerId);
  assert.equal(timers.size, 1);
  assert.equal(timers.get(timerId), timer);
  plugin.stop();
  assert.equal(timers.size, 0);
});

test("cycles mode without replacing offline or missing-timeline labels", async () => {
  const sdk = createSdk();
  const plugin = new SpotifyGSMTCPlugin({
    sdk,
    fetchImpl: async () => { throw new Error("unexpected request"); },
  });
  const settings = normalizeProgressSettings();
  plugin.contexts.set("progress", {
    action: "progress", active: true, settings, mode: "remaining",
  });
  plugin.lastState = { online: false };
  assert.equal(await plugin.run({ context: "progress" }), true);
  let svg = Buffer.from(sdk.calls.at(-1)[2].split(",")[1], "base64").toString("utf8");
  assert.match(svg, />Offline</);
  assert.equal(plugin.entry("progress").mode, "elapsed");

  plugin.lastState = { online: true, available: true, timelineAvailable: false };
  assert.equal(await plugin.run({ context: "progress" }), true);
  svg = Buffer.from(sdk.calls.at(-1)[2].split(",")[1], "base64").toString("utf8");
  assert.match(svg, />No timeline</);
  assert.equal(plugin.entry("progress").mode, "total");
});

test("initializes and recreates progress contexts in remaining mode", async () => {
  const sdk = createSdk();
  let nextId = 0;
  const plugin = new SpotifyGSMTCPlugin({
    sdk,
    fetchImpl: async () => ({ ok: true, async json() { return state({ is_playing: false }); } }),
    now: () => Date.parse("2026-08-23T12:00:01.000Z"),
    setIntervalImpl: () => ++nextId,
    clearIntervalImpl() {},
  });
  const event = {
    uuid: "com.ulanzi.ulanzistudio.spotifygsm.progress",
    context: "progress",
    param: DEFAULT_PROGRESS_SETTINGS,
  };
  plugin.add(event);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(plugin.entry("progress").mode, "remaining");
  plugin.entry("progress").mode = "total";
  plugin.clear({ param: [{ context: "progress" }] });
  assert.equal(plugin.entry("progress"), undefined);
  plugin.add(event);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(plugin.entry("progress").mode, "remaining");
  plugin.entry("progress").mode = "elapsed";
  plugin.stop();
  assert.equal(plugin.entry("progress"), undefined);
  plugin.add(event);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(plugin.entry("progress").mode, "remaining");
  assert.equal(sdk.calls.filter(([type]) => type === "settings").length, 0);
  plugin.stop();
});

test("uses one global animation timer, limits updates, deduplicates, and ends once", () => {
  const sdk = createSdk();
  let clock = 1_000_000;
  let nextId = 0;
  const timers = new Map();
  const cleared = [];
  const plugin = new SpotifyGSMTCPlugin({
    sdk,
    now: () => clock,
    setIntervalImpl(fn, ms) { const id = ++nextId; timers.set(id, { fn, ms }); return id; },
    clearIntervalImpl(id) { cleared.push(id); timers.delete(id); },
  });
  const settings = normalizeProgressSettings();
  plugin.contexts.set("one", { action: "progress", active: true, settings });
  plugin.contexts.set("two", { action: "progress", active: true, settings });
  plugin.lastState = {
    online: true, available: true, timelineAvailable: true, isPlaying: true,
    positionSeconds: 8, durationSeconds: 10, playbackRate: 1, positionUpdatedAt: clock,
  };
  plugin.manageAnimation();
  plugin.manageAnimation();
  assert.equal(timers.size, 1);
  assert.equal([...timers.values()][0].ms, ANIMATION_INTERVAL_MS);
  plugin.animationTick();
  const firstCount = sdk.calls.length;
  plugin.animationTick();
  assert.equal(sdk.calls.length, firstCount);
  clock += 1000;
  plugin.animationTick();
  assert.equal(sdk.calls.length, firstCount + 2);
  clock += 1000;
  plugin.animationTick();
  const endedCount = sdk.calls.length;
  plugin.animationTick();
  assert.equal(sdk.calls.length, endedCount);
  assert.equal(plugin.animationTimer, null);
  assert.equal(cleared.length, 1);
});

test("renders offline, missing timeline, pause, and end states clearly", () => {
  const sdk = createSdk();
  const plugin = new SpotifyGSMTCPlugin({ sdk, now: () => 5000 });
  const settings = normalizeProgressSettings();
  plugin.contexts.set("progress", { action: "progress", active: true, settings });
  const renderText = (stateValue) => {
    plugin.rendered.clear();
    plugin.render("progress", "progress", stateValue, true);
    const uri = sdk.calls.at(-1)[2];
    return Buffer.from(uri.split(",")[1], "base64").toString("utf8");
  };
  assert.match(renderText({ online: false }), />Offline</);
  assert.match(renderText({ online: true, available: true, timelineAvailable: false }), />No timeline</);
  assert.match(renderText({ online: true, available: true, timelineAvailable: true,
    isPlaying: false, positionSeconds: 4, durationSeconds: 10,
    playbackRate: 1, positionUpdatedAt: 0 }), />0:06</);
  assert.match(renderText({ online: true, available: true, timelineAvailable: true,
    isPlaying: true, positionSeconds: 10, durationSeconds: 10,
    playbackRate: 1, positionUpdatedAt: 5000 }), />0:00</);
});

test("setactive, clear, close, and stop clean contexts and global timers", () => {
  const sdk = createSdk();
  let nextId = 0;
  const timers = new Set();
  const plugin = new SpotifyGSMTCPlugin({
    sdk,
    now: () => 1000,
    setIntervalImpl() { const id = ++nextId; timers.add(id); return id; },
    clearIntervalImpl(id) { timers.delete(id); },
  });
  plugin.connect();
  plugin.contexts.set("progress", {
    action: "progress", active: true, settings: normalizeProgressSettings(),
  });
  plugin.lastState = { online: true, available: true, timelineAvailable: true,
    isPlaying: true, positionSeconds: 1, durationSeconds: 10,
    playbackRate: 1, positionUpdatedAt: 1000 };
  plugin.startPolling();
  plugin.manageAnimation();
  assert.equal(timers.size, 2);
  sdk.handlers.setactive({ context: "progress", active: false });
  assert.equal(plugin.entry("progress").active, false);
  assert.equal(timers.size, 1);
  sdk.handlers.setactive({ context: "progress", active: true });
  assert.equal(timers.size, 2);
  sdk.handlers.clear({ param: [{ context: "progress" }] });
  assert.equal(plugin.contexts.size, 0);
  assert.equal(timers.size, 0);
  sdk.handlers.close();
  plugin.stop();
  assert.equal(timers.size, 0);
});

test("normalizes and persists progress settings once with immediate render", async () => {
  const sdk = createSdk();
  let nextId = 0;
  const plugin = new SpotifyGSMTCPlugin({
    sdk,
    fetchImpl: async () => ({ ok: true, async json() { return state(); } }),
    now: () => Date.parse("2026-08-23T12:00:01.000Z"),
    setIntervalImpl() { return ++nextId; },
    clearIntervalImpl() {},
  });
  plugin.connect();
  sdk.handlers.add({
    uuid: "com.ulanzi.ulanzistudio.spotifygsm.progress",
    context: "progress",
    param: { progressColor: "javascript:bad", strokeWidth: 99 },
  });
  await new Promise((resolve) => setImmediate(resolve));
  const initialWrites = sdk.calls.filter(([type]) => type === "settings");
  assert.equal(initialWrites.length, 1);
  assert.deepEqual(initialWrites[0][2], "progress");
  assert.deepEqual(initialWrites[0][1], { ...DEFAULT_PROGRESS_SETTINGS, strokeWidth: 30 });
  const rendersBefore = sdk.calls.filter(([type]) => type === "base64").length;
  sdk.handlers.paramfromplugin({
    context: "progress",
    param: { ...DEFAULT_PROGRESS_SETTINGS, progressColor: "#abcdef", strokeWidth: "8" },
  });
  assert.equal(sdk.calls.filter(([type]) => type === "settings").length, 2);
  assert.equal(sdk.calls.filter(([type]) => type === "base64").length, rendersBefore + 1);
  assert.equal(plugin.entry("progress").settings.progressColor, "#ABCDEF");
  plugin.stop();
});

test("inspector normalization and serialization match plugin settings", () => {
  assert.deepEqual(normalizeInspectorSettings({}), DEFAULT_PROGRESS_SETTINGS);
  assert.deepEqual(serializeInspectorSettings({
    progressColor: "#000000", progressColorHex: "#abcdef",
    trackColorHex: "bad", textColor: "#123456", backgroundColor: "#654321",
    strokeWidth: "5.6",
  }), {
    progressColor: "#ABCDEF", trackColor: "#333333", textColor: "#123456",
    backgroundColor: "#654321", strokeWidth: 6,
  });
  assert.deepEqual(normalizeProgressSettings({ strokeWidth: Number.NaN }), DEFAULT_PROGRESS_SETTINGS);
});

test("sends direct color and grayscale PNGs and restores the identical color URI", () => {
  const sdk = createSdk();
  const plugin = new SpotifyGSMTCPlugin({ sdk });
  plugin.contexts.set("cover", "nowplaying");
  plugin.artworkBundle = normalizeArtworkBundle(artworkBundle(), ARTWORK_ID_A);
  const thumbnail = PNG_ARTWORK;
  const current = {
    available: true, revision: 9, isPlaying: true, artworkId: ARTWORK_ID_A,
    title: "Track", artist: "Artist",
  };
  plugin.render("cover", "nowplaying", current);
  plugin.render("cover", "nowplaying", current);
  plugin.render("cover", "nowplaying", { ...current, isPlaying: false });
  plugin.render("cover", "nowplaying", current);

  assert.equal(plugin.artworkBundle.color, thumbnail, "the validated bundle remains untouched");
  assert.deepEqual(sdk.calls[0], ["base64", "cover", thumbnail, "Track\nArtist"]);
  assert.deepEqual(sdk.calls[1], ["base64", "cover", GRAYSCALE_ARTWORK, "Track\nArtist"]);
  assert.deepEqual(sdk.calls[2], ["base64", "cover", thumbnail, "Track\nArtist"]);
  assert.ok(sdk.calls.every(([, , uri]) => !uri.startsWith("data:image/svg+xml")));
});

test("accepts only canonical bounded PNG bridge artwork byte-for-byte", () => {
  assert.equal(artworkDataUri(PNG_ARTWORK), PNG_ARTWORK);
  assert.equal(artworkDataUri(GRAYSCALE_ARTWORK), GRAYSCALE_ARTWORK);
  assert.equal(artworkDataUri("data:image/png;base64,iVBORw0KGgo="), null);
  const corrupted = Buffer.from(PNG_ARTWORK.split(",")[1], "base64");
  corrupted[corrupted.length - 5] ^= 1;
  assert.equal(artworkDataUri(
    `data:image/png;base64,${corrupted.toString("base64")}`,
  ), null);
  const original = Buffer.from(PNG_ARTWORK.split(",")[1], "base64");
  const duplicateHeader = Buffer.concat([original.subarray(0, 33),
    original.subarray(8, 33), original.subarray(33)]);
  assert.equal(artworkDataUri(
    `data:image/png;base64,${duplicateHeader.toString("base64")}`,
  ), null);
});

test("rejects malformed or disguised artwork into the existing fallback", () => {
  const script = Buffer.from("<script>alert(1)</script>").toString("base64");
  const svg = Buffer.from("<svg><script/></svg>").toString("base64");
  const invalidArtwork = [
    "https://remote.invalid/cover.png",
    "javascript:alert(1)",
    `data:image/svg+xml;base64,${svg}`,
    `data:image/bmp;base64,${Buffer.from("BM").toString("base64")}`,
    `data:image/jpeg;base64,${Buffer.from([0xFF, 0xD8, 0xFF]).toString("base64")}`,
    `data:image/gif;base64,${Buffer.from("GIF89a", "ascii").toString("base64")}`,
    `data:image/webp;base64,${Buffer.from("RIFF0000WEBP", "ascii").toString("base64")}`,
    `data:image/PNG;base64,${PNG_ARTWORK.split(",")[1]}`,
    `data:image/png;charset=utf-8;base64,${PNG_ARTWORK.split(",")[1]}`,
    `data:image/png;base64, ${PNG_ARTWORK.split(",")[1]}`,
    "data:image/png;base64,",
    "data:image/png;base64,AB==",
    "data:image/png;base64,@@@@",
    'data:image/png;base64,iVBORw0KGgo=" onload="alert(1)',
    'data:image/png;base64,iVBORw0KGgo="><script>alert(1)</script>',
    `data:image/png;base64,${script}`,
    `data:image/jpeg;base64,${script}`,
    `data:image/gif;base64,${script}`,
    `data:image/webp;base64,${script}`,
    `data:image/png;base64,${"A".repeat(1_333_340)}`,
    `${PNG_ARTWORK}trailing`,
    `data:image/png;base64,${Buffer.from(
      Buffer.from(PNG_ARTWORK.split(",")[1], "base64").subarray(0, -1),
    ).toString("base64")}`,
  ];
  const sdk = createSdk();
  const plugin = new SpotifyGSMTCPlugin({ sdk });
  for (const [revision, thumbnail] of invalidArtwork.entries()) {
    assert.equal(artworkDataUri(thumbnail), null);
    plugin.render(`cover-${revision}`, "nowplaying", {
      online: true, available: true, revision, isPlaying: false,
      title: "Track", artist: "Artist", thumbnail,
    });
  }
  assert.equal(sdk.calls.length, invalidArtwork.length);
  assert.ok(sdk.calls.every(([type, , path, text]) => type === "path"
    && path === "./assets/music.svg" && text === "Track\nArtist"));
});

test("normalizes strict artwork IDs and atomic six-PNG bundles", () => {
  const now = Date.parse("2026-08-23T12:00:01.000Z");
  assert.equal(normalizeBridgeState(state({ artwork_id: ARTWORK_ID_A }), now).artworkId,
    ARTWORK_ID_A);
  for (const invalid of ["A".repeat(64), "a".repeat(63), `../${ARTWORK_ID_A}`]) {
    assert.equal(normalizeBridgeState(state({ artwork_id: invalid }), now).artworkId, null);
  }
  const valid = normalizeArtworkBundle(artworkBundle(), ARTWORK_ID_A);
  assert.equal(valid.color, PNG_ARTWORK);
  assert.equal(valid.grayscale, GRAYSCALE_ARTWORK);
  assert.deepEqual(valid.tiles, MOSAIC_TILES);
  assert.equal(normalizeArtworkBundle(artworkBundle(ARTWORK_ID_B), ARTWORK_ID_A), null);
  assert.equal(normalizeArtworkBundle(artworkBundle(ARTWORK_ID_A, {
    tiles: [...MOSAIC_TILES.slice(0, 3), "data:image/png;base64,iVBORw0KGgo="],
  }), ARTWORK_ID_A), null);
});

test("renders each direct color PNG tile in exact TL TR BL BR order", () => {
  const sdk = createSdk();
  const plugin = new SpotifyGSMTCPlugin({ sdk });
  plugin.artworkBundle = normalizeArtworkBundle(artworkBundle(), ARTWORK_ID_A);
  const current = {
    online: true, available: true, revision: 1, isPlaying: true, artworkId: ARTWORK_ID_A,
  };
  MOSAIC_ACTIONS.forEach((action, index) => {
    plugin.contexts.set(`tile-${index}`, action);
    plugin.render(`tile-${index}`, action, current);
  });
  assert.deepEqual(sdk.calls, MOSAIC_TILES.map((tile, index) => [
    "base64", `tile-${index}`, tile, "",
  ]));
  assert.ok(sdk.calls.every(([, , uri]) => uri.startsWith("data:image/png;base64,")));
  assert.ok(sdk.calls.every(([, , uri]) => !uri.startsWith("data:image/svg+xml")));
});

test("rejects a missing or one-invalid bundle atomically into local fallbacks", () => {
  for (const bundle of [null, normalizeArtworkBundle(artworkBundle(ARTWORK_ID_A, {
    tiles: [...MOSAIC_TILES.slice(0, 3), "data:image/png;base64,iVBORw0KGgo="],
  }), ARTWORK_ID_A)]) {
    const sdk = createSdk();
    const plugin = new SpotifyGSMTCPlugin({ sdk });
    plugin.artworkBundle = bundle;
    const current = { online: true, available: true, artworkId: ARTWORK_ID_A };
    MOSAIC_ACTIONS.forEach((action, index) => plugin.render(`tile-${index}`, action, current));
    assert.deepEqual(sdk.calls.map(([type, , path]) => [type, path]), [
      ["path", "./assets/artwork-top-left.svg"],
      ["path", "./assets/artwork-top-right.svg"],
      ["path", "./assets/artwork-bottom-left.svg"],
      ["path", "./assets/artwork-bottom-right.svg"],
    ]);
  }
});

test("mosaic presses are inert and pause does not alter or rerender color tiles", async () => {
  const sdk = createSdk();
  let requests = 0;
  const plugin = new SpotifyGSMTCPlugin({
    sdk,
    fetchImpl: async () => { requests += 1; throw new Error("unexpected request"); },
  });
  plugin.artworkBundle = normalizeArtworkBundle(artworkBundle(), ARTWORK_ID_A);
  const playing = { online: true, available: true, revision: 1, isPlaying: true,
    artworkId: ARTWORK_ID_A };
  for (const [index, action] of MOSAIC_ACTIONS.entries()) {
    const context = `tile-${index}`;
    plugin.contexts.set(context, action);
    plugin.render(context, action, playing);
  }
  const before = structuredClone(sdk.calls);
  for (const [index, action] of MOSAIC_ACTIONS.entries()) {
    assert.equal(await plugin.run({
      context: `tile-${index}`,
      uuid: `com.ulanzi.ulanzistudio.spotifygsm.${action}`,
    }), false);
    plugin.render(`tile-${index}`, action, { ...playing, revision: 2, isPlaying: false });
  }
  assert.equal(requests, 0);
  assert.deepEqual(sdk.calls, before);
  assert.equal(sdk.calls.filter(([type]) => type === "settings").length, 0);
});

test("uses one quiet offline fallback and recovers", async () => {
  const sdk = createSdk();
  let fail = true;
  const plugin = new SpotifyGSMTCPlugin({
    sdk,
    fetchImpl: async (url) => {
      if (fail) throw new Error("offline");
      return url.endsWith("/state")
        ? response(state({ artwork_id: ARTWORK_ID_A }))
        : response(artworkBundle());
    },
    now: () => Date.parse("2026-08-23T12:00:01.000Z"),
  });
  plugin.contexts.set("cover", "nowplaying");
  await plugin.poll();
  await plugin.poll();
  assert.deepEqual(sdk.calls, [["path", "cover", "./assets/offline.svg", "Offline"]]);
  fail = false;
  await plugin.poll();
  assert.equal(sdk.calls.at(-1)[0], "base64");
});

test("fetches one shared bundle per artwork ID and skips it on unchanged polls", async () => {
  const sdk = createSdk();
  const requests = [];
  const plugin = new SpotifyGSMTCPlugin({
    sdk,
    fetchImpl: async (url) => {
      requests.push(url);
      return url.endsWith("/state")
        ? response(state({ artwork_id: ARTWORK_ID_A }))
        : response(artworkBundle());
    },
    now: () => Date.parse("2026-08-23T12:00:01.000Z"),
  });
  plugin.contexts.set("cover", "nowplaying");
  MOSAIC_ACTIONS.forEach((action, index) => plugin.contexts.set(`tile-${index}`, action));
  await plugin.poll();
  await plugin.poll();
  assert.deepEqual(requests, [
    `${BRIDGE_ORIGIN}/state`,
    `${BRIDGE_ORIGIN}/artwork/${ARTWORK_ID_A}`,
    `${BRIDGE_ORIGIN}/state`,
  ]);
  assert.equal(plugin.artworkBundle.id, ARTWORK_ID_A);
  assert.deepEqual(sdk.calls.slice(-5).map(([type]) => type), Array(5).fill("base64"));
});

test("bundle failure falls back atomically and retries on the next poll", async () => {
  const sdk = createSdk();
  let bundleAttempts = 0;
  const plugin = new SpotifyGSMTCPlugin({
    sdk,
    fetchImpl: async (url) => {
      if (url.endsWith("/state")) return response(state({ artwork_id: ARTWORK_ID_A }));
      bundleAttempts += 1;
      return bundleAttempts === 1 ? response({}, false) : response(artworkBundle());
    },
    now: () => Date.parse("2026-08-23T12:00:01.000Z"),
  });
  plugin.contexts.set("cover", "nowplaying");
  MOSAIC_ACTIONS.forEach((action, index) => plugin.contexts.set(`tile-${index}`, action));
  await plugin.poll();
  assert.equal(plugin.artworkBundle, null);
  assert.deepEqual(sdk.calls.slice(-5).map(([type]) => type), Array(5).fill("path"));
  await plugin.poll();
  assert.equal(bundleAttempts, 2);
  assert.equal(plugin.artworkBundle.id, ARTWORK_ID_A);
  assert.deepEqual(sdk.calls.slice(-5).map(([type]) => type), Array(5).fill("base64"));
});

test("ID changes clear old artwork before fetch and stale responses cannot install", async () => {
  const sdk = createSdk();
  let releaseBundle;
  const pendingBundle = new Promise((resolve) => { releaseBundle = resolve; });
  const plugin = new SpotifyGSMTCPlugin({
    sdk,
    fetchImpl: async (url) => {
      if (url.endsWith("/state")) return response(state({ artwork_id: ARTWORK_ID_B }));
      await pendingBundle;
      return response(artworkBundle(ARTWORK_ID_B));
    },
    now: () => Date.parse("2026-08-23T12:00:01.000Z"),
  });
  plugin.contexts.set("cover", "nowplaying");
  plugin.lastState = normalizeBridgeState(
    state({ artwork_id: ARTWORK_ID_A }), Date.parse("2026-08-23T12:00:01.000Z"),
  );
  plugin.artworkBundle = normalizeArtworkBundle(artworkBundle(), ARTWORK_ID_A);
  const polling = plugin.poll();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(plugin.lastState.artworkId, ARTWORK_ID_B);
  assert.equal(plugin.artworkBundle, null);
  assert.deepEqual(sdk.calls.at(-1), ["path", "cover", "./assets/music.svg", "Track\nArtist"]);
  releaseBundle();
  await polling;
  assert.equal(plugin.artworkBundle.id, ARTWORK_ID_B);

  let releaseStale;
  const staleGate = new Promise((resolve) => { releaseStale = resolve; });
  plugin.fetchImpl = async () => { await staleGate; return response(artworkBundle()); };
  plugin.lastState = { ...plugin.lastState, artworkId: ARTWORK_ID_A };
  plugin.artworkBundle = null;
  const stale = plugin.fetchArtwork(ARTWORK_ID_A);
  plugin.lastState = { ...plugin.lastState, artworkId: ARTWORK_ID_B };
  releaseStale();
  assert.equal(await stale, false);
  assert.equal(plugin.artworkBundle, null);
});

test("routes nowplaying and toggle through shared toggle flow and rerenders polled state", async () => {
  const sdk = createSdk();
  const requests = [];
  const plugin = new SpotifyGSMTCPlugin({
    sdk,
    fetchImpl: async (url, options) => {
      requests.push([url, options.method]);
      return response(state({
        is_playing: false, revision: 5, artwork_id: ARTWORK_ID_A,
      }));
    },
    now: () => Date.parse("2026-08-23T12:00:01.000Z"),
  });
  plugin.contexts.set("cover-key", "nowplaying");
  plugin.contexts.set("toggle-key", "toggle");
  plugin.lastState = normalizeBridgeState(
    state({ artwork_id: ARTWORK_ID_A }), Date.parse("2026-08-23T12:00:01.000Z"),
  );
  plugin.artworkBundle = normalizeArtworkBundle(artworkBundle(), ARTWORK_ID_A);
  plugin.renderAll();
  await plugin.run({ context: "cover-key" });
  assert.equal(plugin.lastState.isPlaying, false);
  const pausedUri = sdk.calls.findLast(([type, context]) => (
    type === "base64" && context === "cover-key"
  ))[2];
  assert.equal(pausedUri, GRAYSCALE_ARTWORK);
  await plugin.run({ context: "toggle-key" });
  assert.deepEqual(requests, [
    [`${BRIDGE_ORIGIN}/command/toggle`, "POST"],
    [`${BRIDGE_ORIGIN}/state`, "GET"],
    [`${BRIDGE_ORIGIN}/command/toggle`, "POST"],
    [`${BRIDGE_ORIGIN}/state`, "GET"],
  ]);
});

test("failed toggle commands preserve last state and rendered output", async () => {
  for (const [action, failure] of [
    ["nowplaying", "non-ok"], ["nowplaying", "exception"],
    ["toggle", "non-ok"], ["toggle", "exception"],
  ]) {
    const sdk = createSdk();
    const plugin = new SpotifyGSMTCPlugin({
      sdk,
      fetchImpl: async () => {
        if (failure === "exception") throw new Error("command unavailable");
        return { ok: false };
      },
    });
    plugin.contexts.set("control", action);
    plugin.lastState = {
      online: true, available: true, revision: 9, isPlaying: true,
      title: "Track", artist: "Artist", artworkId: ARTWORK_ID_A,
    };
    plugin.artworkBundle = normalizeArtworkBundle(artworkBundle(), ARTWORK_ID_A);
    plugin.renderAll();
    const previousState = plugin.lastState;
    const previousCalls = structuredClone(sdk.calls);

    const scenario = `${action} ${failure}`;
    assert.equal(await plugin.run({ context: "control" }), false, scenario);
    assert.equal(plugin.lastState, previousState, scenario);
    assert.deepEqual(sdk.calls, previousCalls, scenario);
  }
});

test("preserves unrelated transport command behavior", async () => {
  const requests = [];
  const plugin = new SpotifyGSMTCPlugin({
    sdk: createSdk(),
    fetchImpl: async (url, options) => {
      requests.push([url, options.method]);
      return { ok: true, async json() { return state(); } };
    },
    now: () => Date.parse("2026-08-23T12:00:01.000Z"),
  });
  for (const action of ["previous", "next"]) {
    plugin.contexts.clear();
    plugin.contexts.set(action, action);
    await plugin.run({ context: action });
  }
  assert.deepEqual(requests.filter(([, method]) => method === "POST"), [
    [`${BRIDGE_ORIGIN}/command/previous`, "POST"],
    [`${BRIDGE_ORIGIN}/command/next`, "POST"],
  ]);
});

test("targets every audio command at its fixed loopback endpoint", async () => {
  const requests = [];
  const plugin = new SpotifyGSMTCPlugin({
    sdk: createSdk(),
    fetchImpl: async (url, options) => {
      requests.push([url, options.method]);
      return { ok: true, async json() { return state(); } };
    },
    now: () => Date.parse("2026-08-23T12:00:01.000Z"),
  });
  for (const action of ["volume-up", "volume-down", "mute-toggle"]) {
    plugin.contexts.clear();
    plugin.contexts.set(action, action);
    await plugin.run({ context: action });
  }
  assert.deepEqual(requests.filter(([, method]) => method === "POST"), [
    [`${BRIDGE_ORIGIN}/command/volume-up`, "POST"],
    [`${BRIDGE_ORIGIN}/command/volume-down`, "POST"],
    [`${BRIDGE_ORIGIN}/command/mute-toggle`, "POST"],
  ]);
});

test("renders volume, mute, mixed, no-audio, and offline states", () => {
  const sdk = createSdk();
  const plugin = new SpotifyGSMTCPlugin({ sdk });
  const audio = { online: true, available: false, audioAvailable: true,
    revision: 1, volumePercent: 65, isMuted: false, audioMixed: false };
  plugin.render("volume", "volume-up", audio);
  plugin.render("muted", "mute-toggle", { ...audio, revision: 2, isMuted: true });
  plugin.render("mixed", "volume-down", { ...audio, revision: 3, audioMixed: true });
  plugin.render("none", "volume-up", { ...audio, revision: 4, audioAvailable: false });
  plugin.render("offline", "mute-toggle", { ...audio, revision: 5, online: false });
  assert.deepEqual(sdk.calls, [
    ["path", "volume", "./assets/volume-up.svg", "65%"],
    ["path", "muted", "./assets/unmute.svg", "Muted"],
    ["path", "mixed", "./assets/volume-down.svg", "Mixed"],
    ["path", "none", "./assets/volume-up.svg", "No audio"],
    ["path", "offline", "./assets/offline.svg", "Offline"],
  ]);
});

test("stale bridge state is rendered as unavailable", async () => {
  const sdk = createSdk();
  const plugin = new SpotifyGSMTCPlugin({
    sdk,
    fetchImpl: async () => ({ ok: true, async json() { return state({ updated_at: "2026-08-23T11:00:00.000Z" }); } }),
    now: () => Date.parse("2026-08-23T12:00:00.000Z"),
  });
  plugin.contexts.set("toggle-key", "toggle");
  await plugin.poll();
  assert.deepEqual(sdk.calls, [["path", "toggle-key", "./assets/offline.svg", "Offline"]]);
});

test("prefixes every local icon path passed to the SDK", () => {
  const sdk = createSdk();
  const plugin = new SpotifyGSMTCPlugin({ sdk });
  const available = {
    available: true, revision: 1, isPlaying: false,
    title: "Track", artist: "Artist", thumbnail: null,
  };

  plugin.render("offline", "nowplaying", { available: false, revision: 1 });
  plugin.render("music", "nowplaying", available);
  plugin.render("play", "toggle", available);
  plugin.render("pause", "toggle", { ...available, revision: 2, isPlaying: true });
  plugin.render("previous", "previous", available);
  plugin.render("next", "next", available);
  plugin.render("volume-up", "volume-up", { ...available, audioAvailable: true,
    volumePercent: 50, isMuted: false, audioMixed: false });
  plugin.render("volume-down", "volume-down", { ...available, audioAvailable: true,
    volumePercent: 50, isMuted: false, audioMixed: false });
  plugin.render("mute", "mute-toggle", { ...available, audioAvailable: true,
    volumePercent: 50, isMuted: false, audioMixed: false });
  plugin.render("unmute", "mute-toggle", { ...available, revision: 2,
    audioAvailable: true, volumePercent: 50, isMuted: true, audioMixed: false });

  const paths = sdk.calls.map(([, , path]) => path);
  assert.deepEqual(paths, [
    "./assets/offline.svg",
    "./assets/music.svg",
    "./assets/play.svg",
    "./assets/pause.svg",
    "./assets/previous.svg",
    "./assets/next.svg",
    "./assets/volume-up.svg",
    "./assets/volume-down.svg",
    "./assets/mute.svg",
    "./assets/unmute.svg",
  ]);
  assert.ok(paths.every((path) => path.startsWith("./")));
});

test("custom SVG icons declare SDK-sized intrinsic dimensions", () => {
  const icons = ["music", "play", "pause", "previous", "next", "offline",
    "volume-up", "volume-down", "mute", "unmute"];

  for (const icon of icons) {
    const svg = readFileSync(new URL(`../assets/${icon}.svg`, import.meta.url), "utf8");
    const root = svg.match(/^<svg\b[^>]*>/)?.[0];
    assert.ok(root, `${icon}.svg has an SVG root`);
    assert.match(root, /\bwidth="196"/);
    assert.match(root, /\bheight="196"/);
    assert.match(root, /\bviewBox="0 0 100 100"/);
  }
});

test("manifest declares secure unique mosaic actions and keeps CodePath unchanged", () => {
  const manifest = JSON.parse(readFileSync(new URL("../manifest.json", import.meta.url), "utf8"));
  assert.equal(manifest.CodePath, "src/app.js");
  assert.equal(manifest.Version, "1.2.0");
  const uuids = [manifest.UUID, ...manifest.Actions.map(({ UUID }) => UUID)];
  assert.equal(new Set(uuids).size, uuids.length);
  readFileSync(new URL(`../${manifest.CodePath}`, import.meta.url));
  const assetPaths = new Set([
    manifest.Icon,
    manifest.CategoryIcon,
    ...manifest.Actions.flatMap((action) => [
      action.Icon,
      ...action.States.map(({ Image }) => Image),
    ]),
  ]);
  for (const assetPath of assetPaths) readFileSync(new URL(`../${assetPath}`, import.meta.url));

  const mosaic = manifest.Actions.filter(({ UUID }) => MOSAIC_ACTIONS.some(
    (action) => UUID === `com.ulanzi.ulanzistudio.spotifygsm.${action}`,
  ));
  assert.deepEqual(mosaic.map(({ Name }) => Name), [
    "Artwork Top Left", "Artwork Top Right", "Artwork Bottom Left", "Artwork Bottom Right",
  ]);
  for (const [index, action] of mosaic.entries()) {
    assert.equal(action.Icon, `assets/${MOSAIC_ACTIONS[index]}.svg`);
    assert.equal(action.States[0].Image, action.Icon);
    const svg = readFileSync(new URL(`../${action.Icon}`, import.meta.url), "utf8");
    const root = svg.match(/^<svg\b[^>]*>/)?.[0];
    assert.match(root, /\bwidth="196"/);
    assert.match(root, /\bheight="196"/);
    assert.match(root, /\bviewBox="0 0 196 196"/);
    assert.ok(!/<script\b|\bon\w+\s*=|\b(?:href|src)\s*=/i.test(svg));
    assert.equal((svg.match(/#1DB954/g) || []).length, 1);
  }
});
