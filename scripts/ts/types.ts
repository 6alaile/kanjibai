import type { SourceKind } from "../evidence";

/** One fetched source within a bundle — a Reddit thread, an RSS article, etc. */
export type BundleSource = {
  id: string; // stable source_id, e.g. "reddit_thread_abc123"
  kind: SourceKind;
  url: string;
  title: string;
};

/**
 * A raw fact pulled from one source, before it becomes an EvidenceClaim.
 * Kept separate from EvidenceClaim because not every observation makes it
 * into the final brief — the brief builder decides which observations
 * become "observed" claims and which are dropped.
 */
export type Observation = {
  sourceId: string;
  text: string;
};

export type ResearchBundle = {
  bundleKind: "reddit" | "fbref";
  sources: BundleSource[];
  observations: Observation[];
  snapshotId: string;
  retrievedAt: string;
};

/** Injectable fetch signature so callers can pass a mock in tests. */
export type FetchImpl = typeof fetch;
