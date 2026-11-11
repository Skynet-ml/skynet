// The chat's slash commands. Kept free of React so `node --test` covers them.
//
// Every agent runs on the Pi harness (SKY-327), one RPC process per turn. Pi
// runs its built-in commands only in its own terminal UI, so the composer
// runs the ones that fit a hosted chat itself, on the controls the chat
// already has (SKY-604).

/** One value a command's argument may take. */
export interface SlashOption {
  value: string;
  label: string;
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
}

/** What the slash menu lists for a draft. */
export type SlashMenu =
  | { kind: "commands"; items: readonly SlashCommand[] }
  | { kind: "options"; command: SlashCommand; items: readonly SlashOption[] };

const CALL = /^\/(\S+)(?:\s+([\s\S]*))?$/;

/**
 * The command a draft calls and its argument, or `null` when the draft does
 * not start with the name of an offered command. Other text that starts with
 * a slash (a path, say) stays a normal message.
 */
export function parseSlashCommand(
  text: string,
  commands: readonly SlashCommand[],
): { command: SlashCommand; arg: string } | null {
  const match = CALL.exec(text.trim());
  if (!match) return null;
  const name = match[1]!.toLowerCase();
  const command = commands.find((c) => c.name === name);
  return command ? { command, arg: (match[2] ?? "").trim() } : null;
}

/**
 * The menu for a one-line draft that starts with a slash: the commands whose
 * name starts with what was typed, or, after a command with options and a
 * space, the options that contain the typed argument. `null` hides the menu.
 */
export function slashMenu(text: string, commands: readonly SlashCommand[]): SlashMenu | null {
  if (!text.startsWith("/") || text.includes("\n")) return null;
  const space = text.search(/\s/);
  if (space < 0) {
    const typed = text.slice(1).toLowerCase();
    const items = commands.filter((c) => c.name.startsWith(typed));
    return items.length ? { kind: "commands", items } : null;
  }
  const command = commands.find((c) => c.name === text.slice(1, space).toLowerCase());
  if (!command?.options?.length) return null;
  const typed = text.slice(space).trim().toLowerCase();
  const items = command.options.filter(
    (o) => o.value.toLowerCase().includes(typed) || o.label.toLowerCase().includes(typed),
  );
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
