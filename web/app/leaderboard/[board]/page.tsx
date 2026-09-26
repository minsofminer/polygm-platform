import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { t } from "@/i18n/public";
import { BoardView } from "@/public/BoardView";
import { PublicState } from "@/public/Chrome";
import { loadBoard } from "@/public/boardRoute";
import { metadataFor } from "@/public/metadataFor";

export const dynamic = "force-dynamic";

/**
 * A board as a page. The rules above the rows, the rows, and the integrity counts that produced them — see
 * `src/public/BoardView.tsx` for why the rules come first.
 *
 * `generateMetadata` reads the same payload the page does, so the canonical URL, the robots answer and the
 * description are the API's, not a second opinion assembled in the route.
 */
type Params = { board: string };

export async function generateMetadata({ params }: { params: Promise<Params> }): Promise<Metadata> {
  const { board } = await params;
  const read = await loadBoard(board);
  if (read.ok === false) return { title: t("public.state.notFound"), robots: { index: false, follow: false } };
  return metadataFor(read.data);
}

export default async function PublicBoardPage({ params }: { params: Promise<Params> }) {
  const { board } = await params;
  const read = await loadBoard(board);
  if (read.ok === false) {
    if (read.status === 404 || read.status === 422) notFound();
    return <PublicState title={t("public.state.errorTitle")} body={t("public.state.errorBody")} />;
  }
  return <BoardView page={read.data} />;
}
