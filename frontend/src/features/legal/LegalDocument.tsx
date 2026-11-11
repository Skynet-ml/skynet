/**
 * Presentational renderer for the public Terms and Privacy documents.
 *
 * Server component. The document data files hold catalog keys; this renderer
 * resolves them in the UI language and provides a standalone, print-friendly
 * reading surface in that language's direction. English is the source text;
 * every other language shows a notice that the English version controls
 * (SKY-629).
 */

import Link from "next/link";
import type { Icon } from "@phosphor-icons/react";
import {
  CalendarBlank,
  CheckCircle,
  Clock,
  Cpu,
  CreditCard,
  Database,
  Envelope,
  FileText,
  Globe,
  Key,
  Lock,
  Scroll,
  ShieldCheck,
  User,
  Users,
} from "@/shared/ui/icons";
import { AnimatedWordmark } from "@/shared/ui/animated-wordmark";
import { dirForLocale } from "@/shared/lib/locale";
import { msg, type MessageKey } from "@/shared/lib/messages";
import { getActiveIntlLocale, getActiveLocale } from "@/shared/lib/runtime-locale";
import { LegalActionLink } from "./LegalActionLink";
import { LEGAL_CONFIG, SOURCE_CODE_URL } from "./legal-config";
import type { LegalBlock, LegalDocument as LegalDoc } from "./types";
import styles from "./legal-document.module.css";

type LegalDocumentKind = "terms" | "privacy";

// Values the catalog copy interpolates; the translations keep them verbatim.
const LEGAL_PARAMS = {
  websiteLabel: LEGAL_CONFIG.websiteLabel,
  legalEntity: LEGAL_CONFIG.legalEntity,
  contactEmail: LEGAL_CONFIG.contactEmail,
  privacyEmail: LEGAL_CONFIG.privacyEmail,
  sandboxVendor: LEGAL_CONFIG.sandboxVendor,
  sourceCodeUrl: SOURCE_CODE_URL,
};

/** Resolve one legal catalog key in the active UI language. */
function legalText(key: MessageKey): string {
  return msg(key, LEGAL_PARAMS);
}

/** Format an ISO `YYYY-MM-DD` config date in the active UI language. */
function formatLegalDate(isoDate: string): string {
  return new Intl.DateTimeFormat(getActiveIntlLocale(), { dateStyle: "long", timeZone: "UTC" }).format(
    new Date(`${isoDate}T00:00:00Z`),
  );
}

/** Select a semantic icon for a legal section from its English anchor id. */
function iconForSection(value: string): Icon {

  if (value.includes("account") || value.includes("eligibility") || value.includes("children")) {
    return User;
  }
  if (value.includes("content") || value.includes("information") || value.includes("retention")) {
    return Database;
  }
  if (value.includes("provider") || value.includes("ai-output")) return Cpu;
  if (value.includes("key")) return Key;
  if (value.includes("billing") || value.includes("credit") || value.includes("refund")) {
    return CreditCard;
  }
  if (value.includes("security") || value.includes("acceptable")) return ShieldCheck;
  if (value.includes("right") || value.includes("choice")) return Users;
  if (value.includes("cookie")) return Globe;
  if (value.includes("contact")) return Envelope;
  if (value.includes("availability") || value.includes("change")) return Clock;
  if (value.includes("service") || value.includes("transfer")) return Globe;
  if (value.includes("termination") || value.includes("liability")) return Lock;
  if (value.includes("property") || value.includes("governing") || value.includes("dispute")) {
    return Scroll;
  }
  if (value.includes("indemnification") || value.includes("disclaimer")) return ShieldCheck;
  return FileText;
}

/**
 * Estimate the time needed to read the full legal document.
 *
 * Counts words with `Intl.Segmenter`, because Chinese, Japanese and similar
 * scripts do not separate words with spaces.
 */
function readingMinutes(document: LegalDoc): number {
  const keys = document.sections.flatMap((section) =>
    section.blocks.flatMap((block) => (block.kind === "list" ? block.items : [block.key])),
  );
  const text = [document.intro, ...keys].map(legalText).join(" ");
  const segmenter = new Intl.Segmenter(getActiveIntlLocale(), { granularity: "word" });
  let words = 0;
  for (const segment of segmenter.segment(text)) if (segment.isWordLike) words++;
  return Math.max(1, Math.ceil(words / 225));
}

/** Render one paragraph, subheading, or bulleted list within a section. */
function Block({ block }: { block: LegalBlock }) {
  if (block.kind === "subheading") {
    return (
      <h3 className="pt-3 !text-lg font-semibold tracking-tight text-foreground">{legalText(block.key)}</h3>
    );
  }
  if (block.kind === "list") {
    return (
      <ul className="grid gap-3 text-base leading-7 text-foreground/75">
        {block.items.map((item, index) => (
          <li key={index} className="grid grid-cols-[0.75rem_minmax(0,1fr)] items-start gap-3">
            <span aria-hidden="true" className="mt-[0.68rem] size-1.5 rounded-full bg-muted-foreground" />
            <span>{legalText(item)}</span>
          </li>
        ))}
      </ul>
    );
  }
  return <p className="text-base leading-7 text-foreground/75">{legalText(block.key)}</p>;
}

/** Render a complete legal document with navigation and metadata. */
export function LegalDocument({ document, kind }: { document: LegalDoc; kind: LegalDocumentKind }) {
  const { sections } = document;
  const locale = getActiveLocale();
  // English (and its regional variants) is the source text; every other
  // language is a translation that the English version controls.
  const isTranslation = locale !== "en" && !locale.startsWith("en-");
  const isPrivacy = kind === "privacy";
  const DocumentIcon = isPrivacy ? ShieldCheck : Scroll;
  const eyebrow = msg(isPrivacy ? "legal.chrome.privacy_eyebrow" : "legal.chrome.terms_eyebrow");
  const contactEmail = isPrivacy ? LEGAL_CONFIG.privacyEmail : LEGAL_CONFIG.legalEmail;
  const minutes = readingMinutes(document);
  const backToApp = msg("legal.chrome.back_to_app");

  return (
    <div
      dir={dirForLocale(locale)}
      className="h-dvh scroll-smooth overflow-x-hidden overflow-y-auto bg-background motion-reduce:scroll-auto text-foreground print:h-auto print:overflow-visible"
    >
      <header className="sticky top-0 z-20 border-b border-border/60 bg-background/95 backdrop-blur-md print:static print:bg-white">
        <div className="mx-auto flex min-h-16 w-full max-w-[96rem] items-center justify-between gap-4 px-4 sm:px-8 lg:px-12 xl:px-16">
          <LegalActionLink kind="home" href="/" label={backToApp} />

          <Link
            href="/"
            aria-label={msg("legal.chrome.home_aria")}
            className="flex min-h-[44px] items-center gap-3 rounded-lg px-1 text-foreground outline-none transition-colors hover:text-primary focus-visible:ring-2 focus-visible:ring-primary/60"
          >
            <AnimatedWordmark
              size={16}
              withMark
              className="cursor-pointer"
              autoMorph
              autoMorphDuration={10000}
              morphSpeed={250}
            />
          </Link>
        </div>
      </header>

      <article className="mx-auto w-full max-w-[96rem] px-4 py-6 sm:px-8 sm:py-8 lg:px-12 lg:py-12 xl:px-16">
        {isTranslation ? (
          <p role="note" className="mb-4 text-sm leading-6 text-foreground/65">
            {msg("legal.chrome.translation_notice")}
          </p>
        ) : null}
        <section
          aria-labelledby="legal-document-title"
          className="relative overflow-hidden rounded-[2rem] bg-primary-fill px-6 py-8 text-primary-foreground sm:px-10 sm:py-12 lg:px-14 lg:py-16 print:rounded-none print:border-b print:border-black/20 print:bg-white print:text-black print:shadow-none"
        >
          <DocumentIcon
            className="pointer-events-none absolute -bottom-12 end-4 size-56 rotate-[-8deg] text-primary-foreground/[0.055] sm:end-10 sm:size-72 lg:-bottom-16 lg:end-16 lg:size-[22rem] print:hidden"
            weight="thin"
            aria-hidden="true"
          />

          <div className="relative grid gap-10 lg:grid-cols-[minmax(0,1fr)_18rem] lg:items-end">
            <div className="max-w-4xl">
              <div className="flex items-center gap-3 text-primary-foreground/80">
                <span className="grid size-9 place-items-center rounded-xl border border-background/15 bg-background/10">
                  <DocumentIcon className="size-4" aria-hidden="true" />
                </span>
                <span className="text-xs font-semibold uppercase tracking-[0.16em]">{eyebrow}</span>
              </div>
              <h1
                id="legal-document-title"
                className={`${styles.title} mt-7 max-w-4xl !text-[clamp(2.5rem,12vw,4.75rem)] !leading-[0.96] font-bold tracking-[-0.055em]`}
              >
                {legalText(document.title)}
              </h1>
              <p className="mt-7 max-w-[72ch] text-base leading-7 text-primary-foreground/85 sm:text-lg sm:leading-8 print:text-black/75">
                {legalText(document.intro)}
              </p>
            </div>

            <dl className="grid grid-cols-2 gap-x-5 gap-y-6 border-t border-background/15 pt-6 text-sm lg:grid-cols-1 lg:border-s lg:border-t-0 lg:ps-8 lg:pt-0 print:border-black/15">
              <div className="flex items-start gap-3">
                <CalendarBlank
                  className="mt-0.5 size-4 shrink-0 text-primary-foreground/80 print:text-black/60"
                  aria-hidden="true"
                />
                <div>
                  <dt className="text-xs uppercase tracking-[0.12em] text-primary-foreground/65 print:text-black/55">
                    {msg("legal.chrome.last_updated")}
                  </dt>
                  <dd className="mt-1 font-semibold text-primary-foreground print:text-black">
                    {formatLegalDate(LEGAL_CONFIG.lastUpdated)}
                  </dd>
                </div>
              </div>
              <div className="flex items-start gap-3">
                <CheckCircle
                  className="mt-0.5 size-4 shrink-0 text-primary-foreground/80 print:text-black/60"
                  aria-hidden="true"
                />
                <div>
                  <dt className="text-xs uppercase tracking-[0.12em] text-primary-foreground/65 print:text-black/55">
                    {msg("legal.chrome.effective")}
                  </dt>
                  <dd className="mt-1 font-semibold text-primary-foreground print:text-black">
                    {formatLegalDate(LEGAL_CONFIG.effectiveDate)}
                  </dd>
                </div>
              </div>
              <div className="flex items-start gap-3">
                <Clock
                  className="mt-0.5 size-4 shrink-0 text-primary-foreground/80 print:text-black/60"
                  aria-hidden="true"
                />
                <div>
                  <dt className="text-xs uppercase tracking-[0.12em] text-primary-foreground/65 print:text-black/55">
                    {msg("legal.chrome.reading_time")}
                  </dt>
                  <dd className="mt-1 font-semibold text-primary-foreground print:text-black">
                    {msg("legal.chrome.minutes", { count: minutes })}
                  </dd>
                </div>
              </div>
              <div className="flex items-start gap-3">
                <FileText
                  className="mt-0.5 size-4 shrink-0 text-primary-foreground/80 print:text-black/60"
                  aria-hidden="true"
                />
                <div>
                  <dt className="text-xs uppercase tracking-[0.12em] text-primary-foreground/65 print:text-black/55">
                    {msg("legal.chrome.current")}
                  </dt>
                  <dd className="mt-1 font-semibold text-primary-foreground print:text-black">
                    {msg("legal.chrome.sections", { count: sections.length })}
                  </dd>
                </div>
              </div>
            </dl>
          </div>
        </section>

        <div className="grid gap-12 py-12 lg:grid-cols-[18rem_minmax(0,1fr)] lg:gap-16 lg:py-16 xl:grid-cols-[20rem_minmax(0,1fr)] xl:gap-24">
          <aside className="print:hidden">
            <nav
              aria-label={msg("legal.chrome.contents_aria")}
              className="border-y border-border/70 py-6 lg:sticky lg:top-24 lg:max-h-[calc(100dvh-7rem)] lg:overflow-y-auto lg:overscroll-contain"
            >
              <div className="mb-5 flex items-center justify-between gap-4">
                <p className="text-xs font-bold uppercase tracking-[0.15em] text-foreground">
                  {msg("legal.chrome.contents")}
                </p>
                <span className="rounded-md bg-[#E8DFD4] dark:bg-[#4b4237] px-2.5 py-1 font-mono text-[0.6875rem] text-primary">
                  {sections.length}
                </span>
              </div>
              <ol className="grid gap-1.5 sm:grid-cols-2 lg:grid-cols-1">
                {sections.map((section, index) => (
                  <li key={section.id}>
                    <a
                      href={`#${section.id}`}
                      className="group grid min-h-[44px] grid-cols-[1.75rem_minmax(0,1fr)] items-start gap-2 rounded-lg px-2 py-2 text-sm text-foreground/60 outline-none transition-colors hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-primary/60"
                    >
                      <span className="font-mono text-[0.6875rem] leading-5 text-[#9B8877] dark:text-[#cebaa8]">
                        {String(index + 1).padStart(2, "0")}
                      </span>
                      <span className="leading-5">{legalText(section.heading)}</span>
                    </a>
                  </li>
                ))}
              </ol>
            </nav>
          </aside>

          <div className="min-w-0 max-w-[78ch]">
            {sections.map((section, index) => {
              const SectionIcon = iconForSection(section.id);
              return (
                <section
                  key={section.id}
                  id={section.id}
                  className="scroll-mt-24 border-b border-border/60 py-10 first:pt-0 last:border-b-0 last:pb-0 print:break-inside-avoid"
                >
                  <div className="grid grid-cols-[2.75rem_minmax(0,1fr)] items-start gap-4 sm:grid-cols-[3rem_minmax(0,1fr)] sm:gap-5">
                    <span className="grid size-11 place-items-center rounded-xl border border-border bg-muted text-[var(--text-2)] sm:size-12">
                      <SectionIcon className="size-5" aria-hidden="true" />
                    </span>
                    <div className="pt-0.5">
                      <p className="font-mono text-[0.6875rem] font-semibold uppercase tracking-[0.14em] text-[#9B8877] dark:text-[#cebaa8]">
                        {String(index + 1).padStart(2, "0")}
                      </p>
                      <h2 className="mt-1 !text-2xl !leading-tight font-bold tracking-[-0.025em] text-foreground sm:!text-[1.75rem]">
                        {legalText(section.heading)}
                      </h2>
                    </div>
                  </div>
                  <div className="mt-6 grid gap-5 sm:ps-[4.25rem]">
                    {section.blocks.map((block, blockIndex) => (
                      <Block key={blockIndex} block={block} />
                    ))}
                  </div>
                </section>
              );
            })}
          </div>
        </div>

        <footer className="grid overflow-hidden rounded-[2rem] border border-border/70 bg-background print:rounded-none">
          <div className="flex flex-col justify-between gap-8 p-6 sm:p-8 lg:p-10">
            <div>
              <span className="grid size-11 place-items-center rounded-xl bg-muted text-[var(--text-2)]">
                <Envelope className="size-5" aria-hidden="true" />
              </span>
              <h2 className="mt-6 text-2xl font-bold tracking-tight text-foreground">
                {msg("legal.chrome.questions")}
              </h2>
              <p className="mt-3 max-w-[52ch] text-base leading-7 text-foreground/65">
                {msg("legal.chrome.contact_prompt")}
              </p>
            </div>
            <LegalActionLink kind="mail" href={`mailto:${contactEmail}`} label={contactEmail} />
          </div>
        </footer>
      </article>
    </div>
  );
}
