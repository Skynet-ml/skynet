export type MacArch = "arm64" | "x64";

export function macDownloadUrl(arch: MacArch): string {
  return `https://github.com/Skynet-ml/skynet/releases/latest/download/Skynet-${arch}.dmg`;
}
