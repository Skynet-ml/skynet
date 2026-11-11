// The chat's slash commands. Only React types here, so `node --test` covers them.
//
// Every agent runs on the Pi harness (SKY-327), one RPC process per turn. Pi
// runs its built-in commands only in its own terminal UI, so the composer
// runs the ones that fit a hosted chat itself, on the controls the chat
// already has (SKY-604).

import type { ComponentType, ReactNode } from "react";

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
   * Left out of the menu because the chat cannot run it now (a model without
   * effort levels, a chat not saved yet). A typed call still matches, so the
   * composer shows an error and does not send it to the agent as text.
   */
  hidden?: boolean;
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
  const typed = text.slice(space).trim().toLowerCase();
  const items = command.options.filter(
    (o) => o.value.toLowerCase().includes(typed) || o.label.toLowerCase().includes(typed),
  );
  const exact = resolveSlashOption(typed, items);
  const first = items.findIndex((o) => o.value === exact);
  if (first > 0) items.unshift(...items.splice(first, 1));
  return items.length ? { kind: "options", command, items } : null;
}

/**
 * The option an argument names: an exact value, else an exact label, else
 * the only option that contains it. Case does not matter. `null` when it
 * names none or more than one.
 */
export function resolveSlashOption(arg: string, options: readonly SlashOption[]): string | null {
  const typed = arg.trim().toLowerCase();
  if (!typed) return null;
  const exact =
    options.find((o) => o.value.toLowerCase() === typed) ??
    options.find((o) => o.label.toLowerCase() === typed);
  if (exact) return exact.value;
  const partial = options.filter(
    (o) => o.value.toLowerCase().includes(typed) || o.label.toLowerCase().includes(typed),
  );
  return partial.length === 1 ? partial[0]!.value : null;
}

/**
 * Whether Enter on a menu row runs it: an option, or a command whose full
 * name is typed. Enter on a partial name only completes it, as Tab does, so
 * "/n" never runs /new by accident.
 */
export function enterRunsSlash(menu: SlashMenu, index: number, text: string): boolean {
  if (menu.kind === "options") return true;
  return menu.items[index]?.name === text.trim().slice(1).toLowerCase();
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
