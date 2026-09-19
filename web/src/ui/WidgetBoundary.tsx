"use client";
import { Component, type ErrorInfo, type ReactNode } from "react";
import { t } from "@/i18n/t";

type Props = { children: ReactNode; label: string; fallback?: ReactNode };
type State = { failed: boolean };

/**
 * Error boundaries at two granularities (P08 D1): every route has `app/error.tsx`, and every widget inside
 * the frame has one of these. A broken order book must not blank the terminal — the tape and the ticket are
 * how a user exits a position, and a crash that hides them is worse than a crash that shows an empty panel.
 *
 * The boundary also says which widget died. "Something went wrong" over a blank rectangle is how a data
 * outage gets mistaken for "no data".
 */
export class WidgetBoundary extends Component<Props, State> {
  override state: State = { failed: false };

  static getDerivedStateFromError(): State {
    return { failed: true };
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    // The component stack is what identifies the widget in a log; the message alone is a React generic.
    console.error(`widget ${this.props.label} failed`, error.message, info.componentStack?.split("\n").slice(0, 4).join(" "));
  }

  override render(): ReactNode {
    if (!this.state.failed) return this.props.children;
    if (this.props.fallback !== undefined) return this.props.fallback;
    return (
      <div className="unavailable" role="alert">
        <strong>{t("widget.boundary.failed", { label: this.props.label })}</strong>
        <span>{t("widget.boundary.explain")}</span>
      </div>
    );
  }
}
