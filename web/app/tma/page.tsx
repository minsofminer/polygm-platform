import { TmaScreen } from "@/tma/TmaScreen";

/**
 * The Mini App entry on the *main site*: the same screen the Mini App's own domain serves at `/`.
 *
 * The route exists so a shared link out of a free channel alert works on the site as well as inside Telegram —
 * and it is deliberately the same component, so "it works on the site but not in the bot" cannot happen.
 */
export default function TmaPage() {
  return <TmaScreen />;
}
