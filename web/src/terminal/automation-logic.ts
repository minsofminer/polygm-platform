/**
 * The automation list's confirmation sentence. Moved out of `AutomationView.tsx` because that module is
 * `"use client"`, and a pure formatter exported from it cannot be called by a server component (see
 * src/client-boundary.test.ts).
 */
import { t } from "@/i18n/terminal";
import {
  actionSummary,
  capsText,
  emptyDraft,
  builderProblems,
  draftPayload,
  feeArithmeticLines,
  fieldsFor,
  haltBanner,
  lastFiredText,
  newRow,
  nextEvalText,
  previewPayload,
  ruleStatusLabel,
  runOutcomeLabel,
  runSentence,
  templateSplit,
  type BuilderDraft,
} from "./automation";
import type { AutomationList, AutomationRule, AutomationRunRow, TemplateCatalog } from "./wire";

/** The list's own helper for the confirmation step: what the rule will do, in words, before it is armed. */
export function confirmSentence(rule: AutomationRule): string {
  return t("terminal.automation.confirm", { name: rule.name, actions: actionSummary(rule.actions), cap: rule.maxPerDay });
}
