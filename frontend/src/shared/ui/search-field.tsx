"use client";

import * as React from "react";
import { MagnifyingGlass, X } from "@/shared/ui/icons";

import { msg } from "@/shared/lib/messages";
import { cn } from "@/shared/lib/utils";

interface SearchFieldProps {
  value: string;
  onValueChange: (next: string) => void;
  placeholder: string;
  className?: string;
}

/**
 * The house search input in the BoardUI style: a pill with a leading search
 * icon and a small round clear button that fades in once there is text.
 * Escape clears the text, or blurs the field when it is empty.
 */
export function SearchField({ value, onValueChange, placeholder, className }: SearchFieldProps) {
  const inputRef = React.useRef<HTMLInputElement | null>(null);
  const isEmpty = value.length === 0;

  return (
    <div
      className={cn(
        "group relative flex items-center gap-1 rounded-full bg-muted ps-3 pe-1.5 ring-2 ring-inset ring-transparent transition-[box-shadow,background-color] duration-150 ease-out max-lg:h-11 lg:h-9 hover:ring-border focus-within:ring-primary/45 hover:focus-within:ring-primary/45",
        className,
      )}
    >
      <MagnifyingGlass className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
      <input
        ref={inputRef}
        type="text"
        inputMode="search"
        autoComplete="off"
        spellCheck={false}
        dir="auto"
        value={value}
        onChange={(e) => onValueChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            if (value) onValueChange("");
            else inputRef.current?.blur();
          }
        }}
        placeholder={placeholder}
        aria-label={placeholder}
        className="h-full min-w-0 flex-1 bg-transparent px-1.5 py-1.5 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none"
      />
      {/* Out of the tab order: Escape does the same job from the keyboard. */}
      <button
        type="button"
        tabIndex={-1}
        aria-hidden={isEmpty || undefined}
        onMouseDown={(e) => e.preventDefault()}
        onClick={() => {
          onValueChange("");
          inputRef.current?.focus();
        }}
        aria-label={msg("search.clear")}
        className={cn(
          "inline-flex shrink-0 cursor-pointer items-center justify-center max-lg:size-[44px] lg:size-6 transition-[opacity,scale] duration-150 ease-out",
          isEmpty ? "pointer-events-none scale-50 opacity-0" : "scale-100 opacity-100",
        )}
      >
        <span className="inline-flex size-4 items-center justify-center rounded-full bg-border text-muted-foreground transition-colors hover:text-foreground">
          <X className="size-2.5" aria-hidden="true" />
        </span>
      </button>
    </div>
  );
}
