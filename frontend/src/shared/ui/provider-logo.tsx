"use client";

import * as React from "react";
import {
  Anthropic,
  Cerebras,
  Cohere,
  Crusoe,
  DeepInfra,
  DeepSeek,
  Fireworks,
  Friendli,
  Gemini,
  Groq,
  Meta,
  Minimax,
  Mistral,
  Moonshot,
  Morph,
  Nebius,
  Novita,
  Nvidia,
  OpenAI,
  OpenRouter,
  Qwen,
  SambaNova,
  Stepfun,
  Tencent,
  Together,
  Upstage,
  XAI,
  XiaomiMiMo,
  ZAI,
} from "@lobehub/icons";
import { getRuntimeEnv } from "@/shared/lib/runtime-env";
import { GmiCloud, Reflection, ThinkingMachines } from "@/shared/ui/brand-marks";
import { Plug } from "@/shared/ui/icons";

type LoadedLogo = { url: string; mono: boolean } | null;

// One fetch per slug for the whole page: every row that shows a lab shares it.
const logoRequests = new Map<string, Promise<LoadedLogo>>();

/**
 * Fetch a lab's SVG once, and hand it out as a same-origin object URL.
 *
 * The object URL keeps the third-party SVG inert (an image or a mask never
 * runs its scripts) and lets a CSS mask use it without a CORS check.
 */
function loadLogo(slug: string): Promise<LoadedLogo> {
  let request = logoRequests.get(slug);
  if (!request) {
    request = fetch(`${getRuntimeEnv().apiUrl}/models/logos/${encodeURIComponent(slug)}`)
      .then(async (response) => {
        if (!response.ok) return null;
        const svg = await response.text();
        const blob = new Blob([svg], { type: "image/svg+xml" });
        return { url: URL.createObjectURL(blob), mono: /<svg data-tone="mono"/.test(svg) };
      })
      .catch(() => null);
    logoRequests.set(slug, request);
  }
  return request;
}

/**
 * Brand mark for a model lab or BYOK provider slug.
 *
 * The backend serves the provider's current icon from the LobeHub icon set
 * (`/models/logos/{slug}`), so a redrawn logo appears without a deploy. Each
 * lab has one file for both themes: a full-color mark shows as is, and a
 * one-color mark is a mask painted in the text color. When the fetch fails,
 * the slug maps onto its bundled @lobehub/icons avatar or a local brand mark.
 * An unknown slug (a custom endpoint) falls back to a neutral plug tile, so
 * the row still reads as a connection. `dir="ltr"` keeps the mark upright
 * under RTL.
 */
export function ProviderLogo({ slug, size = 28 }: { slug: string; size?: number }) {
  const [loaded, setLoaded] = React.useState<{ slug: string; logo: LoadedLogo } | null>(null);

  React.useEffect(() => {
    let cancelled = false;
    void loadLogo(slug).then((logo) => {
      if (!cancelled) setLoaded({ slug, logo });
    });
    return () => {
      cancelled = true;
    };
  }, [slug]);

  const logo = loaded?.slug === slug ? loaded.logo : undefined;
  // Until the fetch settles, hold the space so the row does not shift.
  if (logo === undefined) {
    return <span className="inline-flex shrink-0" style={{ width: size, height: size }} />;
  }
  if (logo) {
    return (
      <span dir="ltr" className="inline-flex shrink-0" style={{ width: size, height: size }}>
        {logo.mono ? (
          <span
            className="size-full bg-current"
            style={{
              maskImage: `url(${logo.url})`,
              maskSize: "contain",
              maskRepeat: "no-repeat",
              maskPosition: "center",
            }}
          />
        ) : (
          <img
            src={logo.url}
            alt=""
            width={size}
            height={size}
            decoding="async"
            className="size-full object-contain"
          />
        )}
      </span>
    );
  }
  const brand = renderBrand(slug, size);
  if (brand) {
    return (
      <span dir="ltr" className="inline-flex shrink-0">
        {brand}
      </span>
    );
  }
  return (
    <span
      dir="ltr"
      className="inline-flex shrink-0 items-center justify-center rounded-md bg-muted text-muted-foreground"
      style={{ width: size, height: size }}
    >
      <Plug style={{ width: size * 0.5, height: size * 0.5 }} aria-hidden="true" />
    </span>
  );
}

/** Return the brand avatar for a known slug, or null to trigger the fallback tile. */
function renderBrand(slug: string, size: number): React.ReactNode {
  switch (slug) {
    case "openai":
      return <OpenAI.Avatar size={size} />;
    case "anthropic":
      return <Anthropic.Avatar size={size} />;
    case "google":
      return <Gemini.Avatar size={size} />;
    case "xai":
      return <XAI.Avatar size={size} />;
    case "zai":
      return <ZAI.Avatar size={size} />;
    case "deepseek":
      return <DeepSeek.Avatar size={size} />;
    case "meta":
      return <Meta.Avatar size={size} />;
    case "minimax":
      return <Minimax.Avatar size={size} />;
    case "mistral":
      return <Mistral.Avatar size={size} />;
    case "qwen":
      return <Qwen.Avatar size={size} />;
    case "groq":
      return <Groq.Avatar size={size} />;
    case "xiaomi":
      return <XiaomiMiMo.Avatar size={size} />;
    case "moonshot":
    case "moonshotai":
      return <Moonshot.Avatar size={size} />;
    case "together":
      return <Together.Avatar size={size} />;
    case "fireworks":
      return <Fireworks.Avatar size={size} />;
    case "cohere":
      return <Cohere.Avatar size={size} />;
    case "cerebras":
      return <Cerebras.Avatar size={size} />;
    case "novita":
      return <Novita.Avatar size={size} />;
    case "deepinfra":
      return <DeepInfra.Avatar size={size} />;
    case "sambanova":
      return <SambaNova.Avatar size={size} />;
    case "nebius":
      return <Nebius.Avatar size={size} />;
    case "crusoe":
      return <Crusoe.Avatar size={size} />;
    case "friendliai":
      return <Friendli.Avatar size={size} />;
    case "morph":
      return <Morph.Avatar size={size} />;
    case "openrouter":
      return <OpenRouter.Avatar size={size} />;
    case "gmi":
      return <GmiCloud.Avatar size={size} />;
    case "nvidia":
      return <Nvidia.Avatar size={size} />;
    case "stepfun":
      return <Stepfun.Avatar size={size} />;
    case "tencent":
      return <Tencent.Avatar size={size} />;
    case "upstage":
      return <Upstage.Avatar size={size} />;
    case "reflection":
      return <Reflection.Avatar size={size} />;
    case "thinkingmachines":
      return <ThinkingMachines.Avatar size={size} />;
    default:
      return null;
  }
}
