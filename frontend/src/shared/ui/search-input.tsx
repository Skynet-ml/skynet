import type { ComponentProps } from "react";

import { MagnifyingGlass } from "@/shared/ui/icons";
import { Input } from "@/shared/ui/primitives/input";
import { Spinner } from "@/shared/ui/spinner";
import { TOUCH_FIELD, TOUCH_FIELD_SM } from "@/shared/ui/touch";
import { cn } from "@/shared/lib/utils";

/**
 * A form-field search box in the BoardUI style: a pill with a leading
 * magnifier that turns into a spinner while ``busy``. ``size="sm"`` fits popovers and
 * drawer headers.
 */
export function SearchInput({
  size = "default",
  busy = false,
  className,
  ...props
}: Omit<ComponentProps<"input">, "size"> & {
  size?: "default" | "sm";
  busy?: boolean;
}) {
  const sm = size === "sm";
  const iconClass = sm ? "size-3.5" : "size-4";
  return (
    <div className="relative">
      <Input
        {...props}
        className={cn(
          sm ? TOUCH_FIELD_SM : TOUCH_FIELD,
          "rounded-full border-transparent bg-muted backdrop-blur-none hover:border-border focus-visible:border-ring",
          sm ? "ps-8" : "ps-9",
          className,
        )}
      />
      <span
        className={cn(
          "pointer-events-none absolute top-1/2 -translate-y-1/2 text-muted-foreground",
          sm ? "start-2.5" : "start-3",
        )}
        aria-hidden="true"
      >
        {busy ? (
          <Spinner size={sm ? "xs" : "sm"} className="text-muted-foreground" />
        ) : (
          <MagnifyingGlass className={iconClass} />
        )}
      </span>
    </div>
  );
}
