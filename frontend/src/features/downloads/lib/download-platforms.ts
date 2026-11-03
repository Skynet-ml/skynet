import type { MessageKey } from "../../../shared/lib/generated/ui-catalog";

export type PlatformId = "macos" | "windows" | "linux" | "ios" | "android" | "cli";

type PlatformLink = { label: MessageKey; href: string };

export type DownloadPlatform = {
  id: PlatformId;
  name: MessageKey;
  description: MessageKey;
  icon: "apple" | "desktop" | "phone" | "terminal";
  setup?: { title: MessageKey; items: MessageKey[]; guide: PlatformLink };
} & (
  | { status: "coming-soon" }
  | {
      status: "guide" | "download";
      primary: PlatformLink;
      secondary?: PlatformLink[];
      guideNotice?: MessageKey;
    }
);

export const DOWNLOAD_PLATFORMS: readonly DownloadPlatform[] = [
  {
    id: "macos",
    name: "downloads.macos",
    description: "downloads.mac_description",
    icon: "apple",
    status: "guide",
    primary: { label: "downloads.apple_silicon", href: "/guides/mac-app" },
    secondary: [{ label: "downloads.intel", href: "/guides/mac-app" }],
    guideNotice: "downloads.release_note",
    setup: {
      title: "downloads.requirements",
      items: ["downloads.requirements_mac", "downloads.requirements_cli"],
      guide: { label: "downloads.guide", href: "/guides/mac-app" },
    },
  },
  {
    id: "windows",
    name: "downloads.windows",
    description: "downloads.desktop_description",
    icon: "desktop",
    status: "coming-soon",
  },
  {
    id: "linux",
    name: "downloads.linux",
    description: "downloads.desktop_description",
    icon: "terminal",
    status: "coming-soon",
  },
  {
    id: "ios",
    name: "downloads.ios",
    description: "downloads.mobile_description",
    icon: "phone",
    status: "coming-soon",
  },
  {
    id: "android",
    name: "downloads.android",
    description: "downloads.mobile_description",
    icon: "phone",
    status: "coming-soon",
  },
  {
    id: "cli",
    name: "downloads.cli",
    description: "downloads.cli_description",
    icon: "terminal",
    status: "coming-soon",
  },
];

export function heroActionForPlatform(id: PlatformId | null): {
  href: string;
  label: MessageKey;
  icon: DownloadPlatform["icon"] | "download";
  platform?: MessageKey;
} {
  const platform = DOWNLOAD_PLATFORMS.find((item) => item.id === id);
  if (platform && platform.status !== "coming-soon") {
    return { href: platform.primary.href, label: platform.primary.label, icon: platform.icon };
  }
  if (platform) {
    return {
      href: `#platform-${platform.id}`,
      label: "downloads.platform_options",
      icon: "download",
      platform: platform.name,
    };
  }
  return { href: "#platforms", label: "downloads.all_platforms", icon: "download" };
}

export function detectPlatform(userAgent: string, maxTouchPoints = 0): PlatformId | null {
  if (/android/i.test(userAgent)) return "android";
  if (/iphone|ipad|ipod/i.test(userAgent) || (/macintosh/i.test(userAgent) && maxTouchPoints > 1))
    return "ios";
  if (/macintosh|mac os x/i.test(userAgent)) return "macos";
  if (/windows/i.test(userAgent)) return "windows";
  // Desktop-site Android Chrome can omit Android and look identical to Linux.
  if (/linux/i.test(userAgent) && !/cros/i.test(userAgent)) return "linux";
  return null;
}
