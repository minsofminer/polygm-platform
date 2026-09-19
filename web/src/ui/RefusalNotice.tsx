"use client";
import { ROUTES, type RouteKey } from "@/api/routes";
import { t } from "@/i18n/t";

/**
 * What the shell shows for a capability the API does not serve yet: the reason, the route, and the launch
 * item that owns it. Never a spinner, never a mocked success, never a silent empty state — those three are
 * how an unbuilt feature survives to launch day inside a product that looks finished.
 */
export function RefusalNotice({ route, extra }: { route: RouteKey; extra?: string }) {
  const decl = ROUTES[route];
  return (
    <div className="refusal" role="note" data-route={decl.path} data-owner={decl.owner}>
      <strong>{decl.built ? `${decl.method} ${decl.path} is refusing` : `${decl.method} ${decl.path} is not served yet`}</strong>
      <span>
        {extra ?? t("billing.checkout.unavailable")} Launch item {decl.owner}. The screen stays honest rather
        than showing you a placeholder it cannot back.
      </span>
    </div>
  );
}
