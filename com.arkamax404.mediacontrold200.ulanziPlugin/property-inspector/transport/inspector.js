import { DEFAULT_ICON_COLOR, normalizeIconColor } from "../shared/icon-color.js";

const TRANSPORT_ACTIONS = new Set(["previous", "toggle", "next"]);

export function startInspector(sdk, documentRef) {
  const form = documentRef.querySelector("#transport-settings");
  const color = documentRef.querySelector("#icon-color");
  const colorHex = documentRef.querySelector("#icon-color-hex");
  if (!form || !color || !colorHex) return;
  const action = TRANSPORT_ACTIONS.has(documentRef.documentElement.dataset.action)
    ? documentRef.documentElement.dataset.action : "toggle";
  let iconColor = DEFAULT_ICON_COLOR;

  const apply = (raw) => {
    iconColor = normalizeIconColor(raw?.iconColor);
    color.value = iconColor;
    colorHex.value = iconColor;
  };
  const send = (source) => {
    iconColor = normalizeIconColor(source === color ? color.value : colorHex.value);
    color.value = iconColor;
    colorHex.value = iconColor;
    sdk.sendParamFromPlugin({ iconColor });
  };

  sdk.onAdd((event) => apply(event?.param));
  sdk.onParamFromApp((event) => apply(event?.param));
  sdk.onParamFromPlugin((event) => apply(event?.param));
  sdk.onDidReceiveSettings?.((event) => apply(event?.settings));
  form.addEventListener("change", (event) => {
    if (event.target === color || event.target === colorHex) send(event.target);
  });
  apply({});
  sdk.connect(`com.arkamax404.ulanzi.mediacontrol.${action}`);
}

if (typeof document !== "undefined" && typeof $UD !== "undefined") {
  startInspector($UD, document);
}
