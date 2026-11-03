/**
 * Format a byte count for the unified storage budget.
 * Free budgets read in MB and Pro budgets in GB, so a 20 GiB Pro budget shows
 * "20 GB" (as the Pro pitch says), not "20480 MB".
 * @example 524_288_000 → "500 MB", 21_474_836_480 → "20 GB", 524_288 → "512 KB"
 */
export function formatStorageSize(bytes: number | undefined | null): string {
  if (bytes == null || !Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let exp = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const round = (e: number) => {
    const value = bytes / 1024 ** e;
    return value >= 100 || e === 0 ? Math.round(value) : Math.round(value * 10) / 10;
  };
  let rounded = round(exp);
  // Rounding can reach 1024 (1 GiB - 1 B → "1024 MB"); show the next unit instead.
  if (rounded >= 1024 && exp < units.length - 1) rounded = round(++exp);
  return `${rounded} ${units[exp]}`;
}
