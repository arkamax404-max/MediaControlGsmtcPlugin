export const INSPECTOR_DEFAULTS = Object.freeze({
  progressColor: "#1DB954",
  trackColor: "#333333",
  textColor: "#FFFFFF",
  backgroundColor: "#000000",
  strokeWidth: 14,
});

const COLOR_PATTERN = /^#[0-9A-Fa-f]{6}$/;
const COLOR_NAMES = ["progressColor", "trackColor", "textColor", "backgroundColor"];

export function normalizeInspectorSettings(raw = {}) {
  const normalized = {};
  for (const name of COLOR_NAMES) {
    const value = String(raw?.[name] || "");
    normalized[name] = COLOR_PATTERN.test(value) ? value.toUpperCase() : INSPECTOR_DEFAULTS[name];
  }
  const numericWidth = Number(raw?.strokeWidth);
  const width = Number.isFinite(numericWidth) ? Math.round(numericWidth) : INSPECTOR_DEFAULTS.strokeWidth;
  normalized.strokeWidth = Math.max(6, Math.min(30, width));
  return normalized;
}

export function serializeInspectorSettings(values = {}) {
  const raw = { ...values };
  for (const name of COLOR_NAMES) {
    const hex = values[`${name}Hex`];
    if (COLOR_PATTERN.test(String(hex || ""))) raw[name] = hex;
  }
  return normalizeInspectorSettings(raw);
}

function startInspector(sdk, documentRef) {
  const form = documentRef.querySelector("#progress-settings");
  if (!form) return;

  const apply = (raw) => {
    const settings = normalizeInspectorSettings(raw);
    for (const name of COLOR_NAMES) {
      form.elements[name].value = settings[name];
      form.elements[`${name}Hex`].value = settings[name];
    }
    form.elements.strokeWidth.value = String(settings.strokeWidth);
  };

  const send = (source) => {
    if (source?.name?.endsWith("Hex") && COLOR_PATTERN.test(source.value)) {
      form.elements[source.name.slice(0, -3)].value = source.value;
    } else if (COLOR_NAMES.includes(source?.name)) {
      form.elements[`${source.name}Hex`].value = source.value;
    }
    const settings = serializeInspectorSettings(Object.fromEntries(new FormData(form)));
    apply(settings);
    sdk.sendParamFromPlugin(settings);
  };

  sdk.onAdd((event) => apply(event?.param));
  sdk.onParamFromApp((event) => apply(event?.param));
  sdk.onParamFromPlugin((event) => apply(event?.param));
  sdk.onDidReceiveSettings?.((event) => apply(event?.settings));
  form.addEventListener("change", (event) => send(event.target));
  apply(INSPECTOR_DEFAULTS);
  sdk.connect("com.ulanzi.ulanzistudio.spotifygsm.progress");
}

if (typeof document !== "undefined" && typeof $UD !== "undefined") {
  startInspector($UD, document);
}
