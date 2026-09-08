export const NOW_PLAYING_DEFAULTS = Object.freeze({ showProgress: true });

export function normalizeNowPlayingSettings(raw = {}) {
  return { showProgress: typeof raw?.showProgress === "boolean" ? raw.showProgress : true };
}

function startInspector(sdk, documentRef) {
  const form = documentRef.querySelector("#nowplaying-settings");
  if (!form) return;
  const apply = (raw) => {
    const settings = normalizeNowPlayingSettings(raw);
    form.elements.showProgress.checked = settings.showProgress;
  };
  form.addEventListener("change", () => {
    const settings = { showProgress: Boolean(form.elements.showProgress.checked) };
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
