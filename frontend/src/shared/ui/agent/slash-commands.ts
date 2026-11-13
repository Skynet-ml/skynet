// The chat's slash commands. Only React types and the effort rules here, so
// `node --test` covers them.
//
// Every agent runs on the Pi harness (SKY-327), one RPC process per turn. Pi
// runs its built-in commands only in its own terminal UI, so the composer
// runs the ones that fit a hosted chat itself, on the controls the chat
// already has (SKY-604).

import type { ComponentType, ReactNode } from "react";
import { effortsFor, supportsEffortSlider } from "@/shared/lib/model-efforts";
import type { CatalogModel } from "@/shared/types/api";

/** One value a command's argument may take. */
export interface SlashOption {
  value: string;
  label: string;
  /** Shown before the label, e.g. a model's provider logo. */
  icon?: ReactNode;
}

/** A slash command the composer offers. */
export interface SlashCommand {
  /** The name after the slash, as Pi spells it. */
  name: string;
  description: string;
  /** Shown after the name in the menu, e.g. "<model>"; absent when the command takes no argument. */
  argHint?: string;
  /** The values the argument may take; the menu lists them after "/name ". */
  options?: readonly SlashOption[];
  /** The option in use now; the menu marks it with a check. */
  current?: string;
  /** Shown before the name in the menu. */
  icon?: ComponentType<{ className?: string }>;
  /**
   * The chat opens a picker it already has for this command (the model
   * menu), so a pick from the menu runs the command with no argument.
   */
  picker?: boolean;
  /**
   * The command needs typed text (a chat name), so a pick from the menu
   * fills the draft with "/name " instead of running it.
   */
  needsText?: boolean;
  /**
   * Left out of the menu because the chat cannot run it now (a model without
   * effort levels, a chat not saved yet). A typed call still matches, so the
   * composer shows an error and does not send it to the agent as text.
   */
  hidden?: boolean;
  /**
   * The option values are ids the rows do not show (message indexes), so the
   * menu filter and typed text match only labels. A menu pick sends the value.
   */
  labelsOnly?: boolean;
}

/** What the slash menu lists for a draft. */
export type SlashMenu =
  | { kind: "commands"; items: readonly SlashCommand[] }
  | { kind: "options"; command: SlashCommand; items: readonly SlashOption[] };

const CALL = /^\/(\S+)(?:\s+([\s\S]*))?$/;

/**
 * The command a draft calls and its argument, or `null` when the draft does
 * not start with the name of an offered command. Other text that starts with
 * a slash (a path, say) stays a normal message. A command that takes no
 * argument matches only alone, as in Pi's terminal, so "/new idea" is a message.
 */
export function parseSlashCommand(
  text: string,
  commands: readonly SlashCommand[],
): { command: SlashCommand; arg: string } | null {
  const match = CALL.exec(text.trim());
  if (!match) return null;
  const name = match[1]!.toLowerCase();
  const command = commands.find((c) => c.name === name);
  if (!command) return null;
  const arg = (match[2] ?? "").trim();
  return arg && !command.argHint ? null : { command, arg };
}

/**
 * How many characters at the start of a draft the composer marks as a
 * command: "/name" when the draft runs as an offered command, else 0. Text
 * before the slash (a space, say) gets no mark, so the mark always lines up
 * with the typed text.
 */
export function slashTokenLength(text: string, commands: readonly SlashCommand[]): number {
  // A hidden command still matches, but it only shows an error, so it gets no mark.
  const parsed = text.startsWith("/") ? parseSlashCommand(text, commands) : null;
  if (!parsed || parsed.command.hidden) return 0;
  return /^\/\S+/.exec(text)![0].length;
}

/**
 * The menu for a one-line draft that starts with a slash: the commands whose
 * name starts with what was typed, or, after a command with options and a
 * space, the options that contain the typed argument. The option the
 * argument names exactly comes first, so Enter picks what resolveSlashOption
 * would. `null` hides the menu.
 */
export function slashMenu(text: string, commands: readonly SlashCommand[]): SlashMenu | null {
  if (!text.startsWith("/") || text.includes("\n")) return null;
  const space = text.search(/\s/);
  if (space < 0) {
    const typed = text.slice(1).toLowerCase();
    const items = commands.filter((c) => !c.hidden && c.name.startsWith(typed));
    return items.length ? { kind: "commands", items } : null;
  }
  const command = commands.find((c) => c.name === text.slice(1, space).toLowerCase());
  if (!command?.options?.length || command.hidden) return null;
  const menu = optionsMenu(command, text.slice(space));
  // An empty labelsOnly menu stays open, so its no-match line shows that the
  // typed text names no row, and Enter does not send it.
  return menu.items.length || command.labelsOnly ? menu : null;
}

/**
 * The options of `command` that contain `typed`, for the menu after
 * "/name " or for the picker a menu pick opens. The option that `typed`
 * names exactly comes first, so Enter picks what resolveSlashOption would.
 * With `labelsOnly` the values stay hidden, so only labels match and rank.
 * The list can be empty.
 */
export function optionsMenu(command: SlashCommand, typed: string): SlashMenu {
  const query = squash(typed);
  const items = (command.options ?? []).filter((o) => optionMatches(o, query, command.labelsOnly));
  const exact = command.labelsOnly
    ? items.find((o) => o.label.toLowerCase() === query)?.value
    : resolveSlashOption(query, items);
  const first = items.findIndex((o) => o.value === exact);
  if (first > 0) items.unshift(...items.splice(first, 1));
  return { kind: "options", command, items };
}

/** Typed text as labels hold it: lower case, with runs of whitespace as one space (fork labels collapse them). */
function squash(text: string): string {
  return text.trim().replace(/\s+/g, " ").toLowerCase();
}

function optionMatches(option: SlashOption, typed: string, labelsOnly = false): boolean {
  return (
    (!labelsOnly && option.value.toLowerCase().includes(typed)) ||
    option.label.toLowerCase().includes(typed)
  );
}

/**
 * The option an argument names: an exact value, else an exact label, else
 * the only option that contains it. Case does not matter. `null` when it
 * names none or more than one. With `labelsOnly`, only labels match: the user
 * never sees the values, so a typed digit must not pick a hidden one.
 */
export function resolveSlashOption(
  arg: string,
  options: readonly SlashOption[],
  labelsOnly = false,
): string | null {
  const typed = squash(arg);
  if (!typed) return null;
  const exact =
    (labelsOnly ? undefined : options.find((o) => o.value.toLowerCase() === typed)) ??
    options.find((o) => o.label.toLowerCase() === typed);
  if (exact) return exact.value;
  const partial = options.filter((o) => optionMatches(o, typed, labelsOnly));
  return partial.length === 1 ? partial[0]!.value : null;
}

/**
 * The value a labelsOnly command runs with, or null when typed text names no
 * row. A menu pick sends its row's value, because two rows can share a label
 * (two "hi" messages). Typed text matches labels only: the values are hidden.
 */
export function labelsOnlyValue(
  command: SlashCommand,
  arg: string,
  fromMenu: boolean,
): string | null {
  return fromMenu ? arg : resolveSlashOption(arg, command.options ?? [], true);
}

/**
 * What Enter or a click on a menu row does: run the command (or the picked
 * option), open the command's options in the menu, or fill the draft for a
 * command that needs typed text.
 */
export function slashPickAction(menu: SlashMenu, index: number): "run" | "options" | "fill" {
  if (menu.kind === "options") return "run";
  const command = menu.items[index];
  if (command?.options?.length && !command.picker) return "options";
  return command?.needsText ? "fill" : "run";
}

/** The menu row a key moves the highlight to, wrapping at both ends; `undefined` for other keys. */
export function stepSlashMenu(index: number, count: number, key: string): number | undefined {
  if (count < 1) return undefined;
  if (key === "ArrowDown") return (index + 1) % count;
  if (key === "ArrowUp") return (index - 1 + count) % count;
  return undefined;
}

/**
 * The draft after the user picks a menu row: a command's name with a space
 * when it takes an argument, or the command with the picked option.
 */
export function completeSlash(menu: SlashMenu, index: number): string {
  if (menu.kind === "options") return `/${menu.command.name} ${menu.items[index]!.value}`;
  const command = menu.items[index]!;
  return command.argHint ? `/${command.name} ` : `/${command.name}`;
}

/**
 * The models /model and /thinking read, by the model menu's rule: an
 * available model that takes the effort slider.
 */
export function slashModels(models: readonly CatalogModel[]): CatalogModel[] {
  return models.filter((m) => m.available !== false && supportsEffortSlider(m));
}

/** The levels /thinking offers for a model: the model menu's slider levels, without "none". */
export function slashEfforts(model: string | null, models: readonly CatalogModel[]): string[] {
  if (!models.find((m) => m.value === model)?.supports_thinking) return [];
  return effortsFor(model, models).filter((level) => level !== "none");
}

/**
 * Whether fast mode stays on after /model picks a model, by the model menu's
 * rule: a local CLI model keeps it when it supports fast mode, and an
 * OpenRouter model keeps it until the menu's fast-mode check refuses it.
 */
export function slashKeepsFast(
  model: string,
  row: CatalogModel | undefined,
  cliRow: boolean,
): boolean {
  return row && cliRow ? !!row.supports_fast : model.startsWith("openrouter/");
}
