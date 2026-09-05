export const LARGEITEM_DEFAULTS = Object.freeze({
  showArtwork: true,
  pausedArtwork: "grayscale",
  showProgress: true,
  showElapsed: false,
  showRemaining: true,
  backgroundColor: "#0B0D10",
  primaryColor: "#FFFFFF",
  secondaryColor: "#B8BEC8",
  accentColor: "#1DB954",
  fit: "contain",
  SmallViewMode: 2,
});

const COLORS = ["backgroundColor", "primaryColor", "secondaryColor", "accentColor"];
const BOOLEANS = ["showArtwork", "showProgress", "showElapsed", "showRemaining"];
const COLOR_PATTERN = /^#[0-9A-Fa-f]{6}$/;

export function normalizeLargeItemSettings(raw = {}) {
  const settings = { ...LARGEITEM_DEFAULTS };
  for (const name of BOOLEANS) {
    if (typeof raw?.[name] === "boolean") settings[name] = raw[name];
  }
  for (const name of COLORS) {
    if (COLOR_PATTERN.test(String(raw?.[name] || ""))) settings[name] = raw[name].toUpperCase();
  }
  if (["color", "grayscale"].includes(raw?.pausedArtwork)) settings.pausedArtwork = raw.pausedArtwork;
  if (["contain", "cover"].includes(raw?.fit)) settings.fit = raw.fit;
  return settings;
}

export function serializeLargeItemSettings(form) {
  const raw = {};
  for (const name of BOOLEANS) raw[name] = Boolean(form.elements[name]?.checked);
  for (const name of COLORS) {
    const hex = form.elements[`${name}Hex`]?.value;
    raw[name] = COLOR_PATTERN.test(String(hex || "")) ? hex : form.elements[name]?.value;
  }
  raw.pausedArtwork = form.elements.pausedArtwork?.value;
  raw.fit = form.elements.fit?.value;
  return normalizeLargeItemSettings(raw);
}

function startInspector(sdk, documentRef) {
  const form = documentRef.querySelector("#largeitem-settings");
  if (!form) return;
  const apply = (raw) => {
    const settings = normalizeLargeItemSettings(raw);
    for (const name of BOOLEANS) form.elements[name].checked = settings[name];
    for (const name of COLORS) {
      form.elements[name].value = settings[name];
      form.elements[`${name}Hex`].value = settings[name];
    }
    form.elements.pausedArtwork.value = settings.pausedArtwork;
    form.elements.fit.value = settings.fit;
  };
  form.addEventListener("change", (event) => {
    const source = event.target;
    if (source?.name?.endsWith("Hex") && COLOR_PATTERN.test(source.value)) {
      form.elements[source.name.slice(0, -3)].value = source.value;
    } else if (COLORS.includes(source?.name)) {
      form.elements[`${source.name}Hex`].value = source.value;
    }
    const settings = serializeLargeItemSettings(form);
    apply(settings);
    sdk.sendParamFromPlugin(settings);
  });
  sdk.onAdd((event) => apply(event?.param));
  sdk.onParamFromApp((event) => apply(event?.param));
  sdk.onParamFromPlugin((event) => apply(event?.param));
  sdk.onDidReceiveSettings?.((event) => apply(event?.settings));
  apply(LARGEITEM_DEFAULTS);
  sdk.connect("com.arkamax404.ulanzi.mediacontrol.largeitem-nowplaying");
}

if (typeof document !== "undefined" && typeof $UD !== "undefined") {
  startInspector($UD, document);
}
