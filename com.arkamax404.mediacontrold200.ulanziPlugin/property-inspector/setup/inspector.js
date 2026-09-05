export function normalizeSetupStatus(raw = {}) {
  const accepted = new Set(["Ready", "Launching", "Waiting for Studio to close", "Installed", "Restored", "Repair required", "Failed"]);
  const status = accepted.has(raw?.status) ? raw.status : "Ready";
  const reason = String(raw?.reason || "Press the assigned D200 key to verify this page")
    .trim().slice(0, 160);
  const profileName = String(raw?.profileName || "").trim().slice(0, 96);
  return { status, reason, profileName };
}

export function startInspector(sdk, documentRef, timers = {}) {
  const statusNode = documentRef.querySelector("#status");
  const reasonNode = documentRef.querySelector("#reason");
  const profileNode = documentRef.querySelector("#profile");
  const operation = documentRef.querySelector("#operation");
  if (!statusNode || !reasonNode || !profileNode || !operation) return;
  const apply = (raw) => {
    const value = normalizeSetupStatus(raw);
    statusNode.textContent = value.status;
    reasonNode.textContent = value.reason;
    profileNode.textContent = value.profileName ? `Profile: ${value.profileName}` : "";
    statusNode.style.color = value.status === "Failed" ? "#ff6b6b" : "#1db954";
  };
  const request = () => sdk.sendToPlugin({ type: "requestSetupStatus" });
  const applyOperation = (raw) => {
    const value = raw?.operation;
    if (["install", "repair", "restore"].includes(value)) operation.value = value;
  };
  operation.addEventListener("change", () => {
    sdk.sendParamFromPlugin({ operation: operation.value });
  });
  sdk.onConnected(request);
  sdk.onAdd((event) => {
    applyOperation(event?.param);
    request();
  });
  sdk.onParamFromApp((event) => applyOperation(event?.param));
  sdk.onParamFromPlugin?.((event) => applyOperation(event?.param));
  sdk.onDidReceiveSettings?.((event) => applyOperation(event?.settings));
  sdk.onSendToPropertyInspector((event) => apply(event?.payload?.setupStatus));
  const setIntervalImpl = timers.setIntervalImpl || documentRef.defaultView?.setInterval?.bind(documentRef.defaultView);
  const clearIntervalImpl = timers.clearIntervalImpl || documentRef.defaultView?.clearInterval?.bind(documentRef.defaultView);
  const interval = setIntervalImpl?.(request, 1000);
  if (interval !== undefined && clearIntervalImpl) {
    documentRef.defaultView?.addEventListener?.("beforeunload", () => clearIntervalImpl(interval), { once: true });
  }
  apply({});
  sdk.connect("com.arkamax404.ulanzi.mediacontrol.setup-large-display");
}

if (typeof document !== "undefined" && typeof $UD !== "undefined") {
  startInspector($UD, document);
}
