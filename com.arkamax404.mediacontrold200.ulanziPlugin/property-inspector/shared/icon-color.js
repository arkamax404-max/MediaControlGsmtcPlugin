export const DEFAULT_ICON_COLOR = "#1DB954";

const COLOR_PATTERN = /^#[0-9A-Fa-f]{6}$/;

export function normalizeIconColor(value) {
  return COLOR_PATTERN.test(String(value || ""))
    ? String(value).toUpperCase() : DEFAULT_ICON_COLOR;
}
