import { notFound } from "next/navigation";
import { OG_CONTENT_TYPE, OG_SIZE, renderCard } from "@/public/og";
import { loadBoard } from "@/public/boardRoute";

/** `/leaderboard/<board>/opengraph-image` — the card, at the URL the API publishes for it. */
export const revalidate = 86_400;
export const alt = "Openout leaderboard card";
export const size = OG_SIZE;
export const contentType = OG_CONTENT_TYPE;

export default async function BoardCard({ params }: { params: Promise<{ board: string }> }) {
  const { board } = await params;
  const read = await loadBoard(board);
  if (read.ok === false) notFound();
  const image = renderCard(read.data.card);
  if (!image) notFound();
  return image;
}
