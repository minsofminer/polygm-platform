/**
 * Buttons in this product have three states, not two: idle, pending, and refused-with-a-reason. The
 * pending state is the button's own, because "the app is frozen" and "the order is in flight" are different
 * sentences and the second one is the true one during a trade.
 */
"use client";
import type { ButtonHTMLAttributes, ReactNode } from "react";
import { t } from "@/i18n/t";

export type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "default" | "primary" | "danger";
  pending?: boolean;
  /** The reason a disabled button is disabled, shown rather than implied. */
  why?: string;
  children: ReactNode;
};

export function Button({ variant = "default", pending, why, children, disabled, ...rest }: ButtonProps) {
  return (
    <button
      {...rest}
      className="button"
      data-variant={variant}
      data-pending={pending ? "true" : undefined}
      disabled={disabled || pending}
      aria-busy={pending || undefined}
      aria-disabled={why ? true : undefined}
      title={why}
    >
      {pending ? <span className="a11y-only">{t("common.state.loading")}</span> : null}
      {children}
    </button>
  );
}
