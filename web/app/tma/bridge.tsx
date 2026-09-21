/**
 * The Telegram bridge script, declared once, in the subtree that is the Mini App.
 *
 * `telegram-web-app.js` is what gives the page `window.Telegram.WebApp`: `initData` (the only credential the webview
 * has), `MainButton`, the BackButton, the closing confirmation and `HapticFeedback`. Loading it anywhere else hands
 * `initDataUnsafe` and the haptics API to a page that has no business calling them, which is why the P08 gate greps
 * for this URL outside `app/tma/` and fails the build when it finds it.
 *
 * It lives here, and not in the two layouts that render it, because there are two documents that need it and only
 * one rule about when:
 *
 *  - on the main site, `app/tma/layout.tsx` renders it for `/tma` alone;
 *  - on the Mini App's own deployment (`PGM_SURFACE=miniapp`) every path is the Mini App, so `app/layout.tsx`
 *    renders it for the whole document — exactly one of the two fires, and a second `<script>` with the same src
 *    would be a second fetch and a second `WebApp` assignment.
 *
 * The `async` is deliberate: the bridge assigns `window.Telegram` when it runs, and every consumer in this app reads
 * it behind a presence check and falls back to the read-only sentence. Waiting on it would block first paint for a
 * script whose absence is already a supported state.
 */
export function TelegramBridge() {
  return <script src="https://telegram.org/js/telegram-web-app.js" async />;
}
