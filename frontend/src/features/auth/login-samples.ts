/**
 * Decorative sample artifacts for the login backdrop.
 *
 * These are FAKE, illustrative task labels — never real user data — used purely
 * as the scattered "product halo" behind the sign-in panel. Each card is a
 * finished task shown by its name, so the halo reads like a wall of Skynet's own
 * completed runs without exposing any numbers. The label is a UI catalog key
 * (``auth.halo.*``), so the backdrop translates with the rest of the UI through
 * the active locale's catalog and fallback chain — not a hardcoded string.
 *
 * Positions are hand-placed around the edges to keep a clean centre; the wing
 * columns hug the form's left/right so the halo frames it rather than drifting
 * out to the viewport edges. The full halo shows from the `md` breakpoint up;
 * below it there isn't room to scatter cards around a near-full-width panel, so a
 * curated few carry `mobilePos` to frame the login on small portrait screens,
 * and the rest drop away (see LoginHalo).
 */

import type { MessageKey } from "@/shared/lib/generated/ui-catalog";

export interface HaloCard {
  /** Catalog key (``auth.halo.*``) resolved to the active locale at render time. */
  key: MessageKey;
  pos: { top?: string; bottom?: string; left?: string; right?: string };
  rot: number;
  /** When set, the card also frames the login on small portrait screens, placed here. */
  mobilePos?: { top?: string; bottom?: string; left?: string; right?: string };
}

export const HALO_CARDS: HaloCard[] = [
  // Top band — front row
  {
    key: "auth.halo.cuda_kernels",
    pos: { top: "-3%", left: "1%" },
    rot: -5,
    mobilePos: { top: "6%", left: "4%" },
  },
  { key: "auth.halo.video_encoding", pos: { top: "-4%", left: "15%" }, rot: 3 },
  { key: "auth.halo.fraud_rules", pos: { top: "-2%", left: "29%" }, rot: -4 },
  {
    key: "auth.halo.delivery_routes",
    pos: { top: "-3%", left: "43%" },
    rot: 4,
    mobilePos: { top: "13%", right: "5%" },
  },
  { key: "auth.halo.database_indexes", pos: { top: "-2%", left: "57%" }, rot: -3 },
  { key: "auth.halo.game_balance", pos: { top: "-4%", left: "71%" }, rot: 5 },
  { key: "auth.halo.risc_v_code", pos: { top: "-3%", right: "2%" }, rot: -5 },

  // Top band — middle row
  { key: "auth.halo.cache_policies", pos: { top: "9%", left: "7%" }, rot: 4 },
  {
    key: "auth.halo.text_to_sql",
    pos: { top: "11%", left: "26%" },
    rot: -3,
    mobilePos: { top: "20%", left: "22%" },
  },
  { key: "auth.halo.warehouse_layouts", pos: { top: "9%", left: "45%" }, rot: 5 },
  { key: "auth.halo.hyperparameters", pos: { top: "11%", left: "64%" }, rot: -4 },
  { key: "auth.halo.log_parsing", pos: { top: "9%", left: "81%" }, rot: 3 },

  // Top band — back row
  { key: "auth.halo.bin_packing", pos: { top: "19%", left: "6%" }, rot: -4 },
  { key: "auth.halo.seo_content", pos: { top: "20%", left: "47%" }, rot: 5 },
  { key: "auth.halo.audio_codecs", pos: { top: "19%", left: "80%" }, rot: -3 },

  // Left wing — outer column
  { key: "auth.halo.release_pipelines", pos: { top: "30%", left: "2%" }, rot: 5 },
  { key: "auth.halo.page_load_time", pos: { top: "44%", left: "2%" }, rot: 6 },
  { key: "auth.halo.memory_allocators", pos: { top: "58%", left: "3%" }, rot: 4 },
  { key: "auth.halo.energy_grids", pos: { top: "70%", left: "2%" }, rot: -3 },

  // Left wing — inner column
  { key: "auth.halo.ticket_routing", pos: { top: "37%", left: "17%" }, rot: -4 },
  { key: "auth.halo.search_retrieval", pos: { top: "64%", left: "17%" }, rot: -3 },

  // Right wing — outer column
  { key: "auth.halo.agent_harnesses", pos: { top: "30%", right: "4%" }, rot: -5 },
  { key: "auth.halo.cloud_costs", pos: { top: "44%", right: "5%" }, rot: -6 },
  { key: "auth.halo.shader_speed", pos: { top: "58%", right: "6%" }, rot: -4 },
  { key: "auth.halo.demand_forecasts", pos: { top: "70%", right: "5%" }, rot: 5 },

  // Right wing — inner column
  { key: "auth.halo.geocoding", pos: { top: "37%", right: "17%" }, rot: 4 },
  {
    key: "auth.halo.chess_engines",
    pos: { top: "64%", right: "17%" },
    rot: 5,
    mobilePos: { bottom: "20%", right: "20%" },
  },

  // Bottom band — back row
  { key: "auth.halo.load_balancing", pos: { bottom: "19%", left: "6%" }, rot: 4 },
  { key: "auth.halo.test_suites", pos: { bottom: "20%", left: "47%" }, rot: -3 },
  {
    key: "auth.halo.document_extraction",
    pos: { bottom: "19%", left: "80%" },
    rot: 4,
    mobilePos: { bottom: "6%", right: "4%" },
  },

  // Bottom band — middle row
  { key: "auth.halo.hash_functions", pos: { bottom: "10%", left: "9%" }, rot: -4 },
  { key: "auth.halo.scheduling", pos: { bottom: "11%", left: "25%" }, rot: 4 },
  { key: "auth.halo.antenna_design", pos: { bottom: "9%", left: "41%" }, rot: -5 },
  { key: "auth.halo.spam_filters", pos: { bottom: "11%", left: "57%" }, rot: 3 },
  { key: "auth.halo.inventory_levels", pos: { bottom: "9%", left: "72%" }, rot: -4 },
  {
    key: "auth.halo.image_classifiers",
    pos: { bottom: "11%", left: "85%" },
    rot: 4,
    mobilePos: { bottom: "13%", left: "5%" },
  },

  // Bottom band — front row
  { key: "auth.halo.sorting_algorithms", pos: { bottom: "-3%", left: "7%" }, rot: 4 },
  { key: "auth.halo.model_distillation", pos: { bottom: "2%", left: "23%" }, rot: 5 },
  { key: "auth.halo.code_translation", pos: { bottom: "-2%", left: "40%" }, rot: -3 },
  { key: "auth.halo.build_pipelines", pos: { bottom: "3%", left: "57%" }, rot: 4 },
  { key: "auth.halo.speech_recognition", pos: { bottom: "-3%", left: "73%" }, rot: 5 },
  { key: "auth.halo.solver_heuristics", pos: { bottom: "-2%", right: "4%" }, rot: 5 },
];
