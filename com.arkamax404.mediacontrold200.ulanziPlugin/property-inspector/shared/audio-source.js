export const DEFAULT_AUDIO_TARGET = "process:spotify.exe";

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
