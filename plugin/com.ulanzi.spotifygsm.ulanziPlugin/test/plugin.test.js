import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  ANIMATION_INTERVAL_MS,
  BRIDGE_ORIGIN,
  DEFAULT_PROGRESS_SETTINGS,
  SpotifyGSMTCPlugin,
  actionFromEvent,
  escapeXml,
  extrapolatePosition,
  formatRemaining,
  normalizeBridgeState,
  normalizeProgressSettings,
  renderProgressSvg,
  svgDataUri,
} from "../src/plugin.js";
import {
  normalizeInspectorSettings,
  serializeInspectorSettings,
} from "../property-inspector/progress/inspector.js";

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

function state(overrides = {}) {
  return {
    available: true,
    revision: 4,
    is_playing: true,
    title: "Track",
    artist: "Artist",
    thumbnail: "data:image/png;base64,Y292ZXI=",
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

test("maps all eight declared action UUIDs without changing existing UUIDs", () => {
  assert.equal(actionFromEvent({ uuid: "com.ulanzi.ulanzistudio.spotifygsm.next" }), "next");
  assert.equal(actionFromEvent({ uuid: "com.ulanzi.ulanzistudio.spotifygsm.volume-up" }), "volume-up");
  assert.equal(actionFromEvent({ uuid: "com.ulanzi.ulanzistudio.spotifygsm.volume-down" }), "volume-down");
  assert.equal(actionFromEvent({ uuid: "com.ulanzi.ulanzistudio.spotifygsm.mute-toggle" }), "mute-toggle");
  assert.equal(actionFromEvent({ uuid: "com.ulanzi.ulanzistudio.spotifygsm.progress" }), "progress");
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

test("renders dynamic cover payload and deduplicates the same revision", () => {
  const sdk = createSdk();
  const plugin = new SpotifyGSMTCPlugin({ sdk });
  plugin.contexts.set("cover", "nowplaying");
  const current = {
    available: true, revision: 9, isPlaying: true,
    title: "Track", artist: "Artist", thumbnail: "data:image/png;base64,YQ==",
  };
  plugin.render("cover", "nowplaying", current);
  plugin.render("cover", "nowplaying", current);
  assert.deepEqual(sdk.calls, [["base64", "cover", current.thumbnail, "Track\nArtist"]]);
});

test("uses one quiet offline fallback and recovers", async () => {
  const sdk = createSdk();
  let fail = true;
  const plugin = new SpotifyGSMTCPlugin({
    sdk,
    fetchImpl: async () => {
      if (fail) throw new Error("offline");
      return { ok: true, async json() { return state(); } };
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

test("targets commands and polling only at the fixed loopback bridge", async () => {
  const sdk = createSdk();
  const requests = [];
  const plugin = new SpotifyGSMTCPlugin({
    sdk,
    fetchImpl: async (url, options) => {
      requests.push([url, options.method]);
      return { ok: true, async json() { return state(); } };
    },
    now: () => Date.parse("2026-08-23T12:00:01.000Z"),
  });
  plugin.contexts.set("next-key", "next");
  await plugin.run({ context: "next-key" });
  assert.deepEqual(requests, [
    [`${BRIDGE_ORIGIN}/command/next`, "POST"],
    [`${BRIDGE_ORIGIN}/state`, "GET"],
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
