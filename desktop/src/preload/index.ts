import { contextBridge, ipcRenderer, type IpcRendererEvent } from "electron";

// The same window later shows the web app from http://127.0.0.1; the bridge
// exists only on the bundled onboarding page.
if (window.location.protocol === "file:") {
  contextBridge.exposeInMainWorld("skynetDesktop", {
    getState: () => ipcRenderer.invoke("onboarding:get-state"),
    recheck: () => ipcRenderer.invoke("onboarding:recheck"),
    continueAnyway: () => ipcRenderer.invoke("onboarding:continue"),
    copy: (text: string) => ipcRenderer.invoke("onboarding:copy", text),
    fit: (height: number) => ipcRenderer.invoke("onboarding:fit", height),
    openLogs: () => ipcRenderer.invoke("onboarding:open-logs"),
    openDockerDownload: () => ipcRenderer.invoke("onboarding:open-docker"),
    signIn: () => ipcRenderer.invoke("onboarding:sign-in"),
    onState: (listener: (state: unknown) => void) => {
      const handler = (_event: IpcRendererEvent, state: unknown) => listener(state);
      ipcRenderer.on("onboarding:state", handler);
      return () => ipcRenderer.removeListener("onboarding:state", handler);
    },
  });
} else {
  // The web app marks its dark theme with a "dark" class on <html>. The main
  // process picks the Dock icon and the window background from it.
  let sent: string | null = null;
  const report = () => {
    const theme = document.documentElement.classList.contains("dark") ? "dark" : "light";
    if (theme === sent) return;
    sent = theme;
    ipcRenderer.send("theme:changed", theme);
  };
  window.addEventListener("DOMContentLoaded", () => {
    report();
    new MutationObserver(report).observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
  });
}
