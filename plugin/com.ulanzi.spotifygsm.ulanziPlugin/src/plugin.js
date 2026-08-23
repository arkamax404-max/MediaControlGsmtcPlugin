export const BRIDGE_ORIGIN = "http://127.0.0.1:43821";
export const POLL_INTERVAL_MS = 1500;
export const ANIMATION_INTERVAL_MS = 1000;
export const STATE_MAX_AGE_MS = 15000;

export const DEFAULT_PROGRESS_SETTINGS = Object.freeze({
  progressColor: "#1DB954",
  trackColor: "#333333",
  textColor: "#FFFFFF",
  backgroundColor: "#000000",
  strokeWidth: 14,
});

const ACTIONS = Object.freeze({
  nowplaying: { command: "toggle", icon: "./assets/music.svg" },
  previous: { command: "previous", icon: "./assets/previous.svg" },
  toggle: { command: "toggle", icon: "./assets/play.svg" },
  next: { command: "next", icon: "./assets/next.svg" },
  "volume-up": { command: "volume-up", icon: "./assets/volume-up.svg" },
  "volume-down": { command: "volume-down", icon: "./assets/volume-down.svg" },
  "mute-toggle": { command: "mute-toggle", icon: "./assets/mute.svg" },
  progress: { command: null, icon: "./assets/progress.svg" },
});

const COLOR_PATTERN = /^#[0-9A-Fa-f]{6}$/;
const ARTWORK_PATTERN = /^data:image\/png;base64,([A-Za-z0-9+/]+={0,2})$/;
const MAX_ARTWORK_BYTES = 1_000_000;
const MAX_ARTWORK_BASE64_LENGTH = 4 * Math.ceil(MAX_ARTWORK_BYTES / 3);
const PROGRESS_MODES = Object.freeze(["remaining", "elapsed", "total"]);

export function actionFromEvent(event) {
  const uuid = String(event?.uuid || event?.action || "");
  const name = uuid.split(".").at(-1);
  return Object.hasOwn(ACTIONS, name) ? name : null;
}

function finiteNumber(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

export function normalizeProgressSettings(raw = {}) {
  const color = (name) => COLOR_PATTERN.test(String(raw?.[name] || ""))
    ? String(raw[name]).toUpperCase()
    : DEFAULT_PROGRESS_SETTINGS[name];
  const width = Math.round(finiteNumber(raw?.strokeWidth, DEFAULT_PROGRESS_SETTINGS.strokeWidth));
  return {
    progressColor: color("progressColor"),
    trackColor: color("trackColor"),
    textColor: color("textColor"),
    backgroundColor: color("backgroundColor"),
    strokeWidth: Math.max(6, Math.min(30, width)),
  };
}

export function normalizeBridgeState(payload, now = Date.now()) {
  const updated = Date.parse(payload?.updated_at || "");
  const fresh = Number.isFinite(updated) && now >= updated && now - updated <= STATE_MAX_AGE_MS;
  if (!payload || !fresh) {
    return { online: false, available: false, audioAvailable: false,
      timelineAvailable: false, revision: finiteNumber(payload?.revision) };
  }
  const duration = finiteNumber(payload.duration_seconds, -1);
  const position = finiteNumber(payload.position_seconds, -1);
  const rate = finiteNumber(payload.playback_rate, 1);
  const positionUpdatedAt = Date.parse(payload.position_updated_at || "");
  const timelineAvailable = payload.timeline_available === true
    && duration > 0 && position >= 0 && Number.isFinite(positionUpdatedAt);
  const thumbnail = artworkDataUri(payload.thumbnail);
  return {
    online: true,
    available: payload.available === true,
    revision: finiteNumber(payload.revision),
    isPlaying: payload.available === true && payload.is_playing === true,
    title: String(payload.title || "").trim().slice(0, 48),
    artist: String(payload.artist || "").trim().slice(0, 48),
    thumbnail,
    thumbnailGrayscale: thumbnail ? artworkDataUri(payload.thumbnail_grayscale) : null,
    audioAvailable: payload.audio_available === true,
    volumePercent: Number.isInteger(payload.volume_percent)
      ? Math.max(0, Math.min(100, payload.volume_percent))
      : null,
    isMuted: payload.is_muted === true,
    audioMixed: payload.audio_mixed === true,
    timelineAvailable,
    positionSeconds: timelineAvailable ? Math.min(position, duration) : 0,
    durationSeconds: timelineAvailable ? duration : 0,
    playbackRate: rate > 0 ? rate : 1,
    positionUpdatedAt: timelineAvailable ? positionUpdatedAt : 0,
  };
}

export function extrapolatePosition(state, now = Date.now()) {
  if (!state?.timelineAvailable) return 0;
  let position = finiteNumber(state.positionSeconds);
  if (state.isPlaying && Number.isFinite(state.positionUpdatedAt)) {
    const elapsed = Math.max(0, now - state.positionUpdatedAt) / 1000;
    position += elapsed * finiteNumber(state.playbackRate, 1);
  }
  return Math.max(0, Math.min(state.durationSeconds, position));
}

function formatDuration(total) {
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const remainder = total % 60;
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`
    : `${minutes}:${String(remainder).padStart(2, "0")}`;
}

export function formatRemaining(seconds) {
  return formatDuration(Math.max(0, Math.ceil(finiteNumber(seconds))));
}

export function nextProgressMode(mode) {
  const current = PROGRESS_MODES.includes(mode) ? mode : "remaining";
  return PROGRESS_MODES[(PROGRESS_MODES.indexOf(current) + 1) % PROGRESS_MODES.length];
}

export function formatProgressTime(mode, position, duration) {
  const safeDuration = Math.max(0, finiteNumber(duration));
  const safePosition = Math.max(0, Math.min(safeDuration, finiteNumber(position)));
  if (mode === "elapsed") return formatDuration(Math.floor(safePosition));
  if (mode === "total") return formatDuration(Math.ceil(safeDuration));
  return formatRemaining(safeDuration - safePosition);
}

export function escapeXml(value) {
  return String(value).replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&apos;",
  })[character]);
}

export function artworkDataUri(value) {
  const artwork = typeof value === "string" ? value : "";
  const match = artwork.match(ARTWORK_PATTERN);
  if (!match) return null;
  const [, encoded] = match;
  if (encoded.length % 4 !== 0 || encoded.length > MAX_ARTWORK_BASE64_LENGTH) return null;
  const bytes = Buffer.from(encoded, "base64");
  if (bytes.length === 0 || bytes.length > MAX_ARTWORK_BYTES
    || bytes.toString("base64") !== encoded) return null;
  const validSignature = bytes.length >= 8 && bytes.subarray(0, 8).equals(
    Buffer.from([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]),
  );
  return validSignature ? artwork : null;
}

export function centeredTextPlacement(context, text, fontSize, center = 98) {
  context.textAlign = "center";
  context.textBaseline = "alphabetic";
  context.font = `700 ${fontSize}px Arial, sans-serif`;
  const metrics = context.measureText(String(text));
  const measuredAscent = metrics.actualBoundingBoxAscent;
  const measuredDescent = metrics.actualBoundingBoxDescent;
  const measuredHeight = measuredAscent + measuredDescent;
  const hasBoundingBox = Number.isFinite(measuredAscent) && measuredAscent >= 0
    && Number.isFinite(measuredDescent) && measuredDescent >= 0
    && Number.isFinite(measuredHeight) && measuredHeight > 0;
  // Keep the fallback proportional to the selected font's em box.
  const ascent = hasBoundingBox ? measuredAscent : fontSize * 0.8;
  const descent = hasBoundingBox ? measuredDescent : fontSize * 0.2;
  return { x: center, y: center + (ascent - descent) / 2 };
}

export function progressTextLayout(text, strokeWidth = DEFAULT_PROGRESS_SETTINGS.strokeWidth) {
  const value = String(text);
  if (!value.includes(":")) {
    return { fontSize: value.length > 6 ? 34 : 42, constrained: false };
  }
  const safeStrokeWidth = normalizeProgressSettings({ strokeWidth }).strokeWidth;
  const availableRadius = 70 - safeStrokeWidth / 2 - 4;
  // Conservative Arial Bold advances exceed the measured Windows digit widths.
  const widthEm = [...value].reduce((width, character) => (
    width + (character === ":" || character === "." ? 0.29 : 0.58)
  ), 0);
  // The full-em height is conservative; its circle chord prevents corner overlap.
  const geometryLimit = Math.floor((2 * availableRadius) / Math.sqrt(widthEm ** 2 + 1));
  const fontSize = Math.max(16, Math.min(42, geometryLimit));
  const estimatedWidth = widthEm * fontSize;
  const maxWidth = 2 * Math.sqrt(availableRadius ** 2 - (fontSize / 2) ** 2);
  const constrained = estimatedWidth > maxWidth;
  return {
    fontSize,
    estimatedWidth,
    maxWidth,
    renderedWidth: constrained ? maxWidth : estimatedWidth,
    constrained,
  };
}

export function renderProgressSvg({ progress = 0, text = "No timeline", settings = {},
  textContext = { measureText: () => ({}) } } = {}) {
  const safe = normalizeProgressSettings(settings);
  const ratio = Math.max(0, Math.min(1, finiteNumber(progress)));
  const radius = 70;
  const circumference = 2 * Math.PI * radius;
  const arc = ratio * circumference;
  const layout = progressTextLayout(text, safe.strokeWidth);
  const placement = centeredTextPlacement(textContext, text, layout.fontSize);
  const widthConstraint = layout.constrained
    ? ` textLength="${layout.maxWidth.toFixed(3)}" lengthAdjust="spacingAndGlyphs"`
    : "";
  return `<svg xmlns="http://www.w3.org/2000/svg" width="196" height="196" viewBox="0 0 196 196">`
    + `<rect width="196" height="196" fill="${safe.backgroundColor}"/>`
    + `<circle cx="98" cy="98" r="${radius}" fill="none" stroke="${safe.trackColor}" stroke-width="${safe.strokeWidth}"/>`
    + `<circle cx="98" cy="98" r="${radius}" fill="none" stroke="${safe.progressColor}" stroke-width="${safe.strokeWidth}" stroke-linecap="round" transform="rotate(-90 98 98)" stroke-dasharray="${arc.toFixed(3)} ${circumference.toFixed(3)}"/>`
    + `<text x="${placement.x}" y="${placement.y}" fill="${safe.textColor}" font-family="Arial, sans-serif" font-size="${layout.fontSize}" font-weight="700" text-anchor="middle"${widthConstraint}>${escapeXml(text)}</text>`
    + "</svg>";
}

export function svgDataUri(svg) {
  return `data:image/svg+xml;base64,${Buffer.from(String(svg), "utf8").toString("base64")}`;
}

function settingsMatch(raw, normalized) {
  return Object.keys(DEFAULT_PROGRESS_SETTINGS).every((key) => raw?.[key] === normalized[key]);
}

export class SpotifyGSMTCPlugin {
  constructor({ sdk, fetchImpl = globalThis.fetch, setIntervalImpl = setInterval,
    clearIntervalImpl = clearInterval, now = Date.now } = {}) {
    this.sdk = sdk;
    this.fetchImpl = fetchImpl;
    this.setIntervalImpl = setIntervalImpl;
    this.clearIntervalImpl = clearIntervalImpl;
    this.now = now;
    this.contexts = new Map();
    this.rendered = new Map();
    this.progressRenderedAt = new Map();
    this.lastState = { online: false, available: false, audioAvailable: false,
      timelineAvailable: false, revision: 0 };
    this.pollTimer = null;
    this.animationTimer = null;
    this.polling = false;
  }

  connect() {
    this.sdk.onConnected(() => {});
    this.sdk.onAdd((event) => this.add(event));
    this.sdk.onRun((event) => void this.run(event));
    this.sdk.onClear((event) => this.clear(event));
    this.sdk.onSetActive((event) => this.setActive(event));
    this.sdk.onParamFromApp((event) => this.receiveSettings(event, false));
    this.sdk.onParamFromPlugin((event) => this.receiveSettings(event, true));
    this.sdk.onDidReceiveSettings?.((event) => this.receiveSettings(event, false));
    this.sdk.onClose?.(() => this.stop());
  }

  entry(context) {
    const value = this.contexts.get(context);
    if (typeof value === "string") {
      return { action: value, active: true, settings: normalizeProgressSettings() };
    }
    return value;
  }

  add(event) {
    const action = actionFromEvent(event);
    if (!action || !event?.context) return;
    const settings = normalizeProgressSettings(event.param);
    this.contexts.set(event.context, {
      action,
      active: true,
      settings,
      ...(action === "progress" ? { mode: "remaining" } : {}),
    });
    if (action === "progress" && !settingsMatch(event.param, settings)) {
      this.sdk.setSettings?.(settings, event.context);
    }
    this.render(event.context, action, this.lastState, true);
    this.startPolling();
    void this.poll();
  }

  clear(event) {
    for (const item of event?.param || []) {
      if (item.context) {
        this.contexts.delete(item.context);
        this.rendered.delete(item.context);
        this.progressRenderedAt.delete(item.context);
      }
    }
    if (this.contexts.size === 0) this.stop();
    else this.manageAnimation();
  }

  setActive(event) {
    const entry = this.entry(event?.context);
    if (!entry) return;
    entry.active = event.active !== false;
    if (entry.active) this.render(event.context, entry.action, this.lastState, true);
    this.manageAnimation();
  }

  receiveSettings(event, persist) {
    const entry = this.entry(event?.context);
    if (!entry || entry.action !== "progress") return;
    const raw = event.param || event.settings || {};
    entry.settings = normalizeProgressSettings(raw);
    if (persist || !settingsMatch(raw, entry.settings)) {
      this.sdk.setSettings?.(entry.settings, event.context);
    }
    this.rendered.delete(event.context);
    this.render(event.context, entry.action, this.lastState, true);
  }

  async run(event) {
    const entry = this.entry(event?.context);
    const action = entry?.action || actionFromEvent(event);
    if (action === "progress" && entry) {
      entry.mode = nextProgressMode(entry.mode);
      this.rendered.delete(event.context);
      this.render(event.context, action, this.lastState, true);
      return true;
    }
    const command = ACTIONS[action]?.command;
    if (!command) return false;
    try {
      const response = await this.fetchImpl(`${BRIDGE_ORIGIN}/command/${command}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
        signal: AbortSignal.timeout(1000),
      });
      if (!response.ok) return false;
      await this.poll();
      return true;
    } catch {
      return false;
    }
  }

  startPolling() {
    if (this.pollTimer !== null) return;
    this.pollTimer = this.setIntervalImpl(() => void this.poll(), POLL_INTERVAL_MS);
  }

  stop() {
    if (this.pollTimer !== null) this.clearIntervalImpl(this.pollTimer);
    if (this.animationTimer !== null) this.clearIntervalImpl(this.animationTimer);
    this.pollTimer = null;
    this.animationTimer = null;
    this.contexts.clear();
    this.rendered.clear();
    this.progressRenderedAt.clear();
  }

  async poll() {
    if (this.polling || this.contexts.size === 0) return;
    this.polling = true;
    try {
      const response = await this.fetchImpl(`${BRIDGE_ORIGIN}/state`, {
        method: "GET",
        signal: AbortSignal.timeout(1000),
      });
      if (!response.ok) throw new Error("Bridge unavailable");
      this.lastState = normalizeBridgeState(await response.json(), this.now());
      this.renderAll();
    } catch {
      this.setOffline();
    } finally {
      this.polling = false;
      this.manageAnimation();
    }
  }

  renderAll() {
    for (const [context] of this.contexts) {
      const entry = this.entry(context);
      if (entry) this.render(context, entry.action, this.lastState);
    }
  }

  setOffline() {
    this.lastState = { online: false, available: false, audioAvailable: false,
      timelineAvailable: false, revision: this.lastState.revision };
    this.renderAll();
    this.manageAnimation();
  }

  shouldAnimate() {
    if (!this.lastState.online || !this.lastState.available
      || !this.lastState.timelineAvailable || !this.lastState.isPlaying) return false;
    if (extrapolatePosition(this.lastState, this.now()) >= this.lastState.durationSeconds) return false;
    for (const [context] of this.contexts) {
      const entry = this.entry(context);
      if (entry?.action === "progress" && entry.active) return true;
    }
    return false;
  }

  manageAnimation() {
    if (this.shouldAnimate()) {
      if (this.animationTimer === null) {
        this.animationTimer = this.setIntervalImpl(
          () => this.animationTick(), ANIMATION_INTERVAL_MS,
        );
      }
    } else if (this.animationTimer !== null) {
      this.clearIntervalImpl(this.animationTimer);
      this.animationTimer = null;
    }
  }

  animationTick() {
    for (const [context] of this.contexts) {
      const entry = this.entry(context);
      if (entry?.action === "progress" && entry.active) {
        this.render(context, entry.action, this.lastState);
      }
    }
    this.manageAnimation();
  }

  renderProgress(context, state, settings, mode, force = false) {
    const now = this.now();
    let text = "Offline";
    let progress = 0;
    let advancing = false;
    if (state.online && state.available && state.timelineAvailable) {
      const position = extrapolatePosition(state, now);
      progress = position / state.durationSeconds;
      text = formatProgressTime(mode, position, state.durationSeconds);
      advancing = state.isPlaying && position < state.durationSeconds;
    } else if (state.online) {
      text = "No timeline";
    }
    const svg = renderProgressSvg({ progress, text, settings });
    const signature = svg;
    if (this.rendered.get(context) === signature) return;
    const lastRendered = this.progressRenderedAt.get(context) ?? -Infinity;
    if (!force && advancing && now - lastRendered < ANIMATION_INTERVAL_MS) return;
    this.rendered.set(context, signature);
    this.progressRenderedAt.set(context, now);
    this.sdk.setBaseDataIcon(context, svgDataUri(svg), "");
  }

  render(context, action, state, force = false) {
    const entry = this.entry(context);
    if (action === "progress") {
      if (entry?.active === false) return;
      this.renderProgress(context, state, entry?.settings, entry?.mode, force);
      return;
    }
    const signature = [state.online, state.available, state.audioAvailable, state.revision,
      state.isPlaying, state.volumePercent, state.isMuted, state.audioMixed].join(":");
    if (this.rendered.get(context) === signature) return;
    this.rendered.set(context, signature);

    if (state.online === false || (state.online === undefined
      && !state.available && !state.audioAvailable)) {
      this.sdk.setPathIcon(context, "./assets/offline.svg", "Offline");
      return;
    }
    if (ACTIONS[action]?.command?.startsWith("volume") || action === "mute-toggle") {
      if (!state.audioAvailable) {
        this.sdk.setPathIcon(context, ACTIONS[action].icon, "No audio");
        return;
      }
      const text = state.audioMixed ? "Mixed"
        : state.isMuted ? "Muted" : `${state.volumePercent}%`;
      const icon = action === "mute-toggle" && state.isMuted
        ? "./assets/unmute.svg" : ACTIONS[action].icon;
      this.sdk.setPathIcon(context, icon, text);
      return;
    }
    if (!state.available) {
      this.sdk.setPathIcon(context, "./assets/offline.svg", "Offline");
      return;
    }
    if (action === "nowplaying") {
      const text = [state.title, state.artist].filter(Boolean).join("\n") || "Playing";
      const colorArtwork = artworkDataUri(state.thumbnail);
      const pausedArtwork = colorArtwork
        ? artworkDataUri(state.thumbnailGrayscale) || colorArtwork
        : null;
      const artwork = state.isPlaying ? colorArtwork : pausedArtwork;
      if (artwork) this.sdk.setBaseDataIcon(context, artwork, text);
      else this.sdk.setPathIcon(context, "./assets/music.svg", text);
      return;
    }
    if (action === "toggle") {
      this.sdk.setPathIcon(
        context,
        state.isPlaying ? "./assets/pause.svg" : "./assets/play.svg",
        state.isPlaying ? "Pause" : "Play",
      );
      return;
    }
    this.sdk.setPathIcon(context, ACTIONS[action].icon, action === "previous" ? "Previous" : "Next");
  }
}
