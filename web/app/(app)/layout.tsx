import { redirect } from "next/navigation";
import type { ReactNode } from "react";
import { Shell } from "@/shell/Shell";
import { serverAuth } from "@/auth/server";

/**
 * Route protection without the logged-out flash: the decision is made here, on the server, before a byte of
 * the protected frame is sent. A client guard paints the signed-out page and then navigates away — that frame
 * is the flash. A user mid-session never sees it, because an expired access token refreshes inside the proxy
 * before the page is rendered.
 */
export default async function AppLayout({ children }: { children: ReactNode }) {
  const auth = await serverAuth();
  if (auth.state === "unauthenticated" || auth.state === "expired") redirect("/sign-in");
  return <Shell>{children}</Shell>;
}
