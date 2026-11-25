"use client";

import { useState } from "react";
import { versionForPrompt, type CandidateVersion } from "../lib/blackbox-versions";
import { RepoVersionBrowser } from "./RepoVersionBrowser";

const ignoreOpenFile = () => {};

/** A candidate-tree node of a repository run, browsed as files like the Versions tab. */
export function RepoCandidateBrowser({
  optimizationId,
  versions,
  prompt,
}: {
  optimizationId: string;
  versions: CandidateVersion[];
  prompt: Record<string, string>;
}) {
  const [path, setPath] = useState<string | null>(null);
  const version = versionForPrompt(versions, prompt);
  if (!version) return null;
  return (
    <RepoVersionBrowser
      // The drawer can move to another node while open (a live run reselects);
      // a new version starts its view fresh, scrolled to its first change.
      key={version.number}
      optimizationId={optimizationId}
      version={version}
      path={path}
      onPathChange={setPath}
      onOpenFile={ignoreOpenFile}
    />
  );
}
