export const DEFAULT_AUDIO_TARGET = "process:spotify.exe";
const AUDIO_ACTIONS = new Set(["volume-up", "volume-down", "mute-toggle"]);

export function normalizeAudioTarget(value) {
  if (value === "system") return value;
  if (typeof value !== "string" || !value.startsWith("process:")) return null;
  const process = value.slice(8).trim().toLocaleLowerCase("en-US");
  if (!process || Array.from(process).length > 128 || /[\\/\x00-\x1f]/.test(process)) return null;
  return `process:${process}`;
}

export function normalizeAudioSources(raw) {
  if (!Array.isArray(raw)) return [];
  const seen = new Set();
  return raw.slice(0, 64).flatMap((item) => {
    const target = normalizeAudioTarget(item?.target);
    const label = String(item?.label || "").trim().slice(0, 48);
    if (!target || !label || seen.has(target)) return [];
    seen.add(target);
    return [{ target, label }];
  });
}

function startInspector(sdk, documentRef) {
  const select = documentRef.querySelector("#audio-target");
  if (!select) return;
  let selected = DEFAULT_AUDIO_TARGET;
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
  select.addEventListener("change", () => {
    selected = normalizeAudioTarget(select.value) || DEFAULT_AUDIO_TARGET;
    sdk.sendParamFromPlugin({ audioTarget: selected });
  });
  apply({});
  sdk.connect(`com.arkamax404.ulanzi.mediacontrol.${action}`);
}

if (typeof document !== "undefined" && typeof $UD !== "undefined") {
  startInspector($UD, document);
}
