export { UserPrefsProvider, useUserPrefs } from "./hooks/use-user-prefs";
export { SettingsModalProvider, useSettingsModal } from "./hooks/use-settings-modal";
export { SettingsModal } from "./components/SettingsModal.lazy";
export { parseAgentPreferencePatch, readPref } from "./lib/prefs";
export { SETTINGS_TABS, visibleSettingsTabs, type SettingsTab } from "./lib/tabs";
