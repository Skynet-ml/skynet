import { ArrowLeft, Envelope } from "@/shared/ui/icons";
import { LinkButton } from "@/shared/ui/link-button";

/**
 * Outline link-button for the legal pages: "Return to Skynet" in the header
 * and the contact mailto in the footer. Built on `LinkButton`, which keeps the
 * link a real element across the Server Component boundary of `LegalDocument`.
 *
 * Args:
 *   kind: `"home"` links back into the app; `"mail"` opens a mailto.
 *   href: Link target.
 *   label: Visible text (hidden below `sm` for `"home"`, which keeps it as the aria-label).
 */
export function LegalActionLink({
  kind,
  href,
  label,
}: {
  kind: "home" | "mail";
  href: string;
  label: string;
}) {
  if (kind === "home") {
    return (
      <LinkButton href={href} size="sm" className="min-h-[44px] lg:min-h-0" aria-label={label}>
        <ArrowLeft className="size-4 rtl:rotate-180" aria-hidden="true" />
        <span className="hidden sm:inline">{label}</span>
      </LinkButton>
    );
  }
  return (
    <LinkButton href={href} size="sm" className="min-h-[44px] max-w-full lg:min-h-0">
      <Envelope className="size-4" aria-hidden="true" />
      <span className="truncate">{label}</span>
    </LinkButton>
  );
}
