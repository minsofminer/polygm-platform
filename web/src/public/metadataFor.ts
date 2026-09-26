/**
 * The metadata a leaderboard page presents, built from the payload the page itself reads.
 *
 * This helper used to be an export of each page file, which is how three copies of it came to exist — and how the
 * production build broke without anyone noticing: Next generates a type for every page module that allows only the
 * exports the framework recognises, so an extra `export function` in `app/**\/page.tsx` fails the build's type check
 * (`Property 'metadataFor' is incompatible with index signature`) while still working at runtime. `next build` in
 * this build environment is killed by memory pressure before its type check runs, and the CI job that would have
 * caught it had never executed — so the only thing standing between that and a deploy was luck.
 *
 * Keeping it here preserves the intent — the canonical URL, the robots answer and the description are the API's,
 * not a second opinion assembled in the route — and lets page files export only page things.
 */
import type { Metadata } from "next";

export type PageMeta = {
  label?: string;
  board: string;
  formula: string;
  gate: string;
  url: string;
  robots: string;
  card: { brand: string };
};

export function metadataFor(page: PageMeta): Metadata {
  return {
    title: `${page.label || page.board} leaderboard`,
    description: `${page.formula} Eligibility: ${page.gate}`,
    alternates: { canonical: page.url },
    robots: page.robots.startsWith("index") ? { index: true, follow: true } : { index: false, follow: true },
    openGraph: { type: "website", url: page.url, siteName: page.card.brand },
  };
}
