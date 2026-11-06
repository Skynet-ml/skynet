import type { MessageKey } from "../../../shared/lib/generated/ui-catalog";

export type PlatformId = "windows" | "ios" | "android";

export type DownloadPlatform = {
  id: PlatformId;
  name: MessageKey;
  description: MessageKey;
  icon: "desktop" | "phone";
};

export const DOWNLOAD_PLATFORMS: readonly DownloadPlatform[] = [
  {
    id: "windows",
    name: "downloads.windows",
    description: "downloads.desktop_description",
    icon: "desktop",
  },
  {
    id: "ios",
    name: "downloads.ios",
    description: "downloads.mobile_description",
    icon: "phone",
  },
  {
    id: "android",
    name: "downloads.android",
    description: "downloads.mobile_description",
    icon: "phone",
  },
];

export type MacArch = "arm64" | "x64";

export function macDownloadUrl(arch: MacArch): string {
  return `https://github.com/Skynet-ml/skynet/releases/latest/download/Skynet-${arch}.dmg`;
}

type ArchNavigator = {
  userAgentData?: { getHighEntropyValues(hints: string[]): Promise<{ architecture?: string }> };
};

// Only Chromium exposes the CPU architecture. Safari and Firefox report Intel on every Mac,
// so they get Apple silicon, which every Mac sold since 2023 uses.
export async function detectMacArch(nav: ArchNavigator): Promise<MacArch> {
  try {
    const { architecture } = (await nav.userAgentData?.getHighEntropyValues(["architecture"])) ?? {};
    return architecture === "x86" ? "x64" : "arm64";
  } catch {
    return "arm64";
  }
}
