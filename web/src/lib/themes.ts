export const CANONICAL_APP_THEME_OPTIONS = [
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
] as const;

export const OPTIONAL_APP_THEME_OPTIONS = [
  { value: "black-amber", label: "Black Amber" },
  { value: "gruvy-box", label: "Gruvbox" },
  { value: "gruvy-box-dark", label: "Gruvbox Dark" },
  { value: "dark-brown", label: "Dark Brown" },
  { value: "frutiger-aero-dark", label: "Frutiger Aero Dark" },
  { value: "extension-synthwave-cyber-horror", label: "Cyber Horror" },
  { value: "synthwave-84", label: "SynthWave 84" },
  { value: "gruvbox-material-light", label: "Gruvbox Material Light" },
  { value: "gruvbox-material-dark", label: "Gruvbox Material Dark" },
  { value: "cocoa-library", label: "Cocoa Library" },
  { value: "milk-tea", label: "Milk Tea" },
  { value: "moss-lantern", label: "Moss Lantern" },
  { value: "apricot-paper", label: "Apricot Paper" },
] as const;

export const APP_THEME_OPTIONS = [
  ...CANONICAL_APP_THEME_OPTIONS,
  ...OPTIONAL_APP_THEME_OPTIONS,
] as const;

export const DARK_APP_THEMES = [
  "dark",
  "black-amber",
  "gruvy-box-dark",
  "dark-brown",
  "frutiger-aero-dark",
  "extension-synthwave-cyber-horror",
  "synthwave-84",
  "gruvbox-material-dark",
  "cocoa-library",
  "moss-lantern",
] as const;

export const SYSTEM_THEME_OPTION = { value: "system", label: "System" } as const;

export const THEME_PROVIDER_THEMES = APP_THEME_OPTIONS.map((option) => option.value);

export const THEME_SWITCHER_OPTIONS = [
  SYSTEM_THEME_OPTION,
  ...CANONICAL_APP_THEME_OPTIONS,
  ...OPTIONAL_APP_THEME_OPTIONS,
] as const;
