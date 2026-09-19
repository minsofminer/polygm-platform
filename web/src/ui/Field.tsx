/**
 * A field with its label, its error and its helper text wired together by ids, so the error is announced
 * and read before the input (an error rendered after the field in the DOM order is an error a screen
 * reader hears too late).
 */
"use client";
import { useId, type ReactNode } from "react";
import { t } from "@/i18n/t";

export type FieldProps = {
  label: string;
  name: string;
  type?: string;
  value: string;
  onChange: (next: string) => void;
  error?: string | null;
  help?: string;
  required?: boolean;
  disabled?: boolean;
  inputMode?: "text" | "numeric" | "decimal";
  autoComplete?: string;
  placeholder?: string;
};

export function Field({ label, name, type = "text", value, onChange, error, help, required, disabled, inputMode, autoComplete, placeholder }: FieldProps) {
  const id = useId();
  const errorId = `${id}-error`;
  const helpId = `${id}-help`;
  return (
    <label className="field" htmlFor={id}>
      <span className="field__label">
        {label}
        {required ? <span aria-hidden="true"> *</span> : null}
      </span>
      <input
        id={id}
        name={name}
        type={type}
        className="field__input"
        value={value}
        inputMode={inputMode}
        autoComplete={autoComplete}
        placeholder={placeholder}
        disabled={disabled}
        aria-invalid={error ? "true" : undefined}
        aria-describedby={[error ? errorId : null, help ? helpId : null].filter(Boolean).join(" ") || undefined}
        onChange={(e) => onChange(e.target.value)}
      />
      {error ? (
        <span className="field__error" id={errorId} role="alert">
          {error}
        </span>
      ) : null}
      {help ? (
        <span id={helpId} className="field__error" style={{ color: "var(--pgm-text-secondary)" }}>
          {help}
        </span>
      ) : null}
    </label>
  );
}

/** The money input. It keeps a string, hands out a string, and says so: a decimal-safe number input whose
 *  type is `string` is the only honest version, because a float cannot hold 0.07 and the conversion to
 *  cents belongs to `src/money/cents.ts`, which is where the arithmetic happens. */
export function MoneyField(props: Omit<FieldProps, "type" | "inputMode">) {
  return <Field {...props} type="text" inputMode="decimal" />;
}

export function Unavailable({ children, label }: { children?: ReactNode; label?: string }) {
  return (
    <div className="unavailable" role="note">
      <strong>{label ?? t("common.state.errorTitle")}</strong>
      <span>{children}</span>
    </div>
  );
}

