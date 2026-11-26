import type { NextConfig } from "next";
import { withSentryConfig } from "@sentry/nextjs";
import { withMotionStudio } from "motion-studio/next";

const nextConfig: NextConfig = {
  output: "standalone",
  reactStrictMode: true,
  compress: true,
  poweredByHeader: false,
  // CI's `npm run typecheck` is the type gate; repeating it inside `next build`
  // added about 90 seconds to every deploy.
  typescript: { ignoreBuildErrors: true },
  images: {
    formats: ["image/avif", "image/webp"],
    deviceSizes: [640, 750, 828, 1080, 1200, 1920],
    imageSizes: [16, 32, 48, 64, 96, 128, 256],
  },
  headers: async () => [
    {
      source: "/(.*)",
      headers: [
        { key: "X-Content-Type-Options", value: "nosniff" },
        { key: "X-Frame-Options", value: "DENY" },
        { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
        // HSTS: after the first HTTPS response, browsers refuse plain HTTP for
        // two years. Every skynetml.com host (apex, www, api and the staging
        // hosts) terminates TLS on the AWS load balancer, so
        // includeSubDomains stays safe. No `preload` — that opts into the
        // browser preload list, which is slow to reverse.
        {
          key: "Strict-Transport-Security",
          value: "max-age=63072000; includeSubDomains",
        },
      ],
    },
    {
      source: "/favicon.png",
      headers: [{ key: "Cache-Control", value: "public, max-age=31536000, immutable" }],
    },
    {
      source: "/robots.txt",
      headers: [{ key: "Cache-Control", value: "public, max-age=86400" }],
    },
    // Next.js already serves content-hashed /_next/static assets with
    // `immutable` long-lived caching; setting our own Cache-Control there is
    // redundant and trips a build-time warning, so it's intentionally omitted.
  ],
  // The public docs ship a Markdown twin of every page (/docs/<page>.md), for
  // readers that are agents. The route handler serves the same source file.
  rewrites: async () => [
    { source: "/docs.md", destination: "/docs-md" },
    { source: "/docs/:path(.+)\\.md", destination: "/docs-md/:path" },
  ],
  turbopack: {
    rules: {
      // The docs content loader imports its Markdown pages as strings.
      "*.md": { condition: { query: "?raw" }, type: "text" },
      // The submit IDE's monaco-vscode packages import stylesheets as
      // namespaces, which Next 16.4 rejects. See monaco-css-import-loader.cjs.
      "*.js": {
        condition: {
          all: [
            { path: /node_modules\/@codingame\/monaco-vscode-/ },
            { content: /import \* as [\w$]+ from '[^'\n]+\.css';/ },
          ],
        },
        loaders: ["./monaco-css-import-loader.cjs"],
      },
    },
  },
  experimental: {
    optimizePackageImports: [
      "motion",
      "react-toastify",
      "radix-ui",
      "@radix-ui/react-direction",
      "class-variance-authority",
      "clsx",
      "tailwind-merge",
      "recharts",
      "@fontsource-variable/heebo",
      "@fontsource-variable/geist",
      "@fontsource-variable/jetbrains-mono",
      "@fontsource-variable/noto-sans-arabic",
      "@fontsource-variable/vazirmatn",
      "@fontsource-variable/noto-sans-devanagari",
      "@fontsource-variable/noto-sans-jp",
      "@fontsource-variable/noto-sans-kr",
      "@fontsource-variable/noto-sans-sc",
      "@uiw/react-codemirror",
      "@codemirror/lang-python",
      "xlsx",
      "@lobehub/icons",
    ],
  },
};

// Source maps are not generated or uploaded (SKY-193). The image build gets no
// Sentry auth token, so the plugin also creates no Sentry release. `release`
// keeps its default on purpose: `create: false` would also stop the plugin from
// baking the detected release name (the commit SHA) into the bundle.
const sentryConfig = withSentryConfig(nextConfig, {
  silent: true,
  sourcemaps: { disable: true },
  webpack: { treeshake: { removeDebugLogging: true } },
  telemetry: false,
});

// Motion Studio (https://motion.dev/docs/studio) edits animations in the running
// app. It changes the config only under `next dev`; production builds are as before.
// Its config type wants an index signature that NextConfig's turbopack type lacks.
export default withMotionStudio(sentryConfig as Parameters<typeof withMotionStudio>[0]);
