import { redirect } from "next/navigation";
import { serverAuth } from "@/auth/server";
import { Shell } from "@/shell/Shell";

export default async function TmaEntry() {
  const auth = await serverAuth();
  // Inside the webview a signed-out device is not a wall: `initData` is re-validated server-side on the
  // first request the page makes (src/telegram/reauth.ts). The entry page only decides where to land.
  if (auth.state === "unauthenticated") redirect("/sign-in");
  return (
    <Shell>
      <p>Opened inside Telegram: the MainButton carries trade confirmations, and nothing else does.</p>
    </Shell>
  );
}
