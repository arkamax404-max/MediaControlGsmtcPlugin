export const NOW_PLAYING_DEFAULTS = Object.freeze({
  showProgress: true,
  accentColor: "#1DB954",
});

const COLOR_PATTERN = /^#[0-9A-Fa-f]{6}$/;

export function normalizeNowPlayingSettings(raw = {}) {
  return {
    showProgress: typeof raw?.showProgress === "boolean" ? raw.showProgress : true,
    accentColor: typeof raw?.accentColor === "string" && COLOR_PATTERN.test(raw.accentColor)
      ? raw.accentColor.toUpperCase() : NOW_PLAYING_DEFAULTS.accentColor,
  };
}

function startInspector(sdk, documentRef) {
  const form = documentRef.querySelector("#nowplaying-settings");
  if (!form) return;
  const apply = (raw) => {
    const settings = normalizeNowPlayingSettings(raw);
    form.elements.showProgress.checked = settings.showProgress;
    form.elements.accentColor.value = settings.accentColor;
    form.elements.accentColorHex.value = settings.accentColor;
  };
  form.addEventListener("change", (event) => {
    const accentColor = event.target?.name === "accentColorHex"
      ? form.elements.accentColorHex.value : form.elements.accentColor.value;
    const settings = normalizeNowPlayingSettings({
      showProgress: Boolean(form.elements.showProgress.checked), accentColor,
    });
    apply(settings);
    sdk.sendParamFromPlugin(settings);
  });
  sdk.onAdd((event) => apply(event?.param));
  sdk.onParamFromApp((event) => apply(event?.param));
  sdk.onParamFromPlugin((event) => apply(event?.param));
  sdk.onDidReceiveSettings?.((event) => apply(event?.settings));
  apply(NOW_PLAYING_DEFAULTS);
  sdk.connect("com.arkamax404.ulanzi.mediacontrol.nowplaying");
}

if (typeof document !== "undefined" && typeof $UD !== "undefined") {
  startInspector($UD, document);
}
