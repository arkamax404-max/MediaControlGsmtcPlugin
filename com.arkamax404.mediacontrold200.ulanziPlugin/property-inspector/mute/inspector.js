import { DEFAULT_ICON_COLOR, normalizeIconColor } from "../shared/icon-color.js";
import {
  DEFAULT_AUDIO_TARGET, normalizeAudioSources, normalizeAudioTarget,
} from "../shared/audio-source.js";

export { DEFAULT_AUDIO_TARGET, normalizeAudioSources, normalizeAudioTarget };
export const DEFAULT_AUDIO_ICON_COLOR = DEFAULT_ICON_COLOR;
const AUDIO_ACTIONS = new Set(["volume-up", "volume-down", "mute-toggle"]);

export function normalizeAudioIconColor(value) {
  return normalizeIconColor(value);
}

function startInspector(sdk, documentRef) {
  const select = documentRef.querySelector("#audio-target");
  const color = documentRef.querySelector("#icon-color");
  const colorHex = documentRef.querySelector("#icon-color-hex");
  if (!select || !color || !colorHex) return;
  let selected = DEFAULT_AUDIO_TARGET;
  let iconColor = DEFAULT_AUDIO_ICON_COLOR;
  let sources = [];
  const action = AUDIO_ACTIONS.has(documentRef.documentElement.dataset.action)
    ? documentRef.documentElement.dataset.action : "mute-toggle";

  const render = () => {
    const options = [...sources];
    if (!options.some((item) => item.target === selected)) {
      const label = selected === "system" ? "System volume (not active)"
        : selected === DEFAULT_AUDIO_TARGET ? "Spotify (not active)"
          : `${selected.slice(8)} (not active)`;
      options.push({ target: selected, label });
    }
    select.replaceChildren(...options.map((item) => {
      const option = documentRef.createElement("option");
      option.value = item.target;
      option.textContent = item.label;
      return option;
    }));
    select.value = selected;
  };

  const apply = (raw) => {
    selected = normalizeAudioTarget(raw?.audioTarget) || DEFAULT_AUDIO_TARGET;
    iconColor = normalizeAudioIconColor(raw?.iconColor);
    color.value = iconColor;
    colorHex.value = iconColor;
    render();
  };
  const requestSources = () => sdk.sendToPlugin({ type: "requestAudioSources" });

  sdk.onConnected(() => requestSources());
  sdk.onAdd((event) => { apply(event?.param); requestSources(); });
  sdk.onParamFromApp((event) => apply(event?.param));
  sdk.onParamFromPlugin((event) => apply(event?.param));
  sdk.onDidReceiveSettings?.((event) => apply(event?.settings));
  sdk.onSendToPropertyInspector((event) => {
    sources = normalizeAudioSources(event?.payload?.audioSources);
    render();
  });
  documentRef.querySelector("form")?.addEventListener("change", (event) => {
    if (event.target === select) {
      selected = normalizeAudioTarget(select.value) || DEFAULT_AUDIO_TARGET;
    } else if (event.target === color) {
      iconColor = normalizeAudioIconColor(color.value);
      colorHex.value = iconColor;
    } else if (event.target === colorHex) {
      iconColor = normalizeAudioIconColor(colorHex.value);
      color.value = iconColor;
      colorHex.value = iconColor;
    } else {
      return;
    }
    sdk.sendParamFromPlugin({ audioTarget: selected, iconColor });
  });
  apply({});
  sdk.connect(`com.arkamax404.ulanzi.mediacontrol.${action}`);
}

if (typeof document !== "undefined" && typeof $UD !== "undefined") {
  startInspector($UD, document);
}
