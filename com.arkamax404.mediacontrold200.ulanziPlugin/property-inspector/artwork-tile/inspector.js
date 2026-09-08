import {
  DEFAULT_AUDIO_TARGET, normalizeAudioSources, normalizeAudioTarget,
} from "../shared/audio-source.js";

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
  };
}

function startInspector(sdk, documentRef) {
  const actionSelect = documentRef.querySelector("#secondary-action");
  const sourceSelect = documentRef.querySelector("#audio-target");
  const sourceFields = documentRef.querySelector("#audio-source-fields");
  if (!actionSelect || !sourceSelect || !sourceFields) return;
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
    sourceFields.hidden = !AUDIO_ACTIONS.has(settings.secondaryAction);
    renderSources();
  };
  const requestSources = () => sdk.sendToPlugin({ type: "requestAudioSources" });
  const send = () => {
    settings = normalizeTileSettings({
      secondaryAction: actionSelect.value, audioTarget: sourceSelect.value,
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
  actionSelect.addEventListener("change", send);
  sourceSelect.addEventListener("change", send);
  apply({});
  sdk.connect(`com.arkamax404.ulanzi.mediacontrol.${action}`);
}

if (typeof document !== "undefined" && typeof $UD !== "undefined") {
  startInspector($UD, document);
}
