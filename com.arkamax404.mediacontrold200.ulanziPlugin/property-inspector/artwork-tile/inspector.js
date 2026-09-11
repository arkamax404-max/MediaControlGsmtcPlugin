import {
  DEFAULT_AUDIO_TARGET, normalizeAudioSources, normalizeAudioTarget,
} from "../shared/audio-source.js";
import { DEFAULT_ICON_COLOR, normalizeIconColor } from "../shared/icon-color.js";

export const DEFAULT_BADGE_COLOR = DEFAULT_ICON_COLOR;

export const SECONDARY_ACTIONS = Object.freeze([
  "none", "previous", "toggle", "next", "volume-up", "volume-down", "mute-toggle",
]);
const AUDIO_ACTIONS = new Set(["volume-up", "volume-down", "mute-toggle"]);
const TILE_ACTIONS = new Set([
  "artwork-top-left", "artwork-top-right", "artwork-bottom-left", "artwork-bottom-right",
]);

export function normalizeTileSettings(raw = {}) {
  return {
    secondaryAction: SECONDARY_ACTIONS.includes(raw?.secondaryAction)
      ? raw.secondaryAction : "none",
    audioTarget: normalizeAudioTarget(raw?.audioTarget) || DEFAULT_AUDIO_TARGET,
    badgeColor: normalizeIconColor(raw?.badgeColor),
  };
}

function startInspector(sdk, documentRef) {
  const actionSelect = documentRef.querySelector("#secondary-action");
  const sourceSelect = documentRef.querySelector("#audio-target");
  const sourceFields = documentRef.querySelector("#audio-source-fields");
  const badgeColor = documentRef.querySelector("#badge-color");
  const badgeColorHex = documentRef.querySelector("#badge-color-hex");
  if (!actionSelect || !sourceSelect || !sourceFields || !badgeColor || !badgeColorHex) return;
  const action = TILE_ACTIONS.has(documentRef.documentElement.dataset.action)
    ? documentRef.documentElement.dataset.action : "artwork-top-left";
  let settings = normalizeTileSettings();
  let sources = [];

  const renderSources = () => {
    const options = [...sources];
    if (!options.some((item) => item.target === settings.audioTarget)) {
      const label = settings.audioTarget === "system" ? "System volume (not active)"
        : settings.audioTarget === DEFAULT_AUDIO_TARGET ? "Spotify (not active)"
          : `${settings.audioTarget.slice(8)} (not active)`;
      options.push({ target: settings.audioTarget, label });
    }
    sourceSelect.replaceChildren(...options.map((item) => {
      const option = documentRef.createElement("option");
      option.value = item.target;
      option.textContent = item.label;
      return option;
    }));
    sourceSelect.value = settings.audioTarget;
  };
  const apply = (raw) => {
    settings = normalizeTileSettings(raw);
    actionSelect.value = settings.secondaryAction;
    badgeColor.value = settings.badgeColor;
    badgeColorHex.value = settings.badgeColor;
    sourceFields.hidden = !AUDIO_ACTIONS.has(settings.secondaryAction);
    renderSources();
  };
  const requestSources = () => sdk.sendToPlugin({ type: "requestAudioSources" });
  const send = (eventTarget) => {
    settings = normalizeTileSettings({
      secondaryAction: actionSelect.value, audioTarget: sourceSelect.value,
      badgeColor: eventTarget === badgeColorHex ? badgeColorHex.value : badgeColor.value,
    });
    apply(settings);
    sdk.sendParamFromPlugin(settings);
    if (AUDIO_ACTIONS.has(settings.secondaryAction)) requestSources();
  };

  sdk.onConnected(() => requestSources());
  sdk.onAdd((event) => { apply(event?.param); requestSources(); });
  sdk.onParamFromApp((event) => apply(event?.param));
  sdk.onParamFromPlugin((event) => apply(event?.param));
  sdk.onDidReceiveSettings?.((event) => apply(event?.settings));
  sdk.onSendToPropertyInspector((event) => {
    sources = normalizeAudioSources(event?.payload?.audioSources);
    renderSources();
  });
  const changed = (event) => {
    send(event.target);
  };
  actionSelect.addEventListener("change", changed);
  sourceSelect.addEventListener("change", changed);
  badgeColor.addEventListener("change", changed);
  badgeColorHex.addEventListener("change", changed);
  apply({});
  sdk.connect(`com.arkamax404.ulanzi.mediacontrol.${action}`);
}

if (typeof document !== "undefined" && typeof $UD !== "undefined") {
  startInspector($UD, document);
}
