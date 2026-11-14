import type { Status, TrendStatus } from "./api";
import { wizardT as t } from "./wizardI18n";

// Each status gets a label, a hex accent, and a tailwind text/border/bg recipe.
export const STATUS_META: Record<
  Status,
  { label: string; hex: string; pulse?: boolean }
> = {
  draft: { label: t("status.draft"), hex: "#8a8f98" },
  queued: { label: t("status.queued"), hex: "#6c7681" },
  rendering: { label: t("status.rendering"), hex: "#56c8e6", pulse: true },
  rendered: { label: t("status.rendered"), hex: "#7aa2f7" },
  review: { label: t("status.review"), hex: "#f5a524" },
  approved: { label: t("status.approved"), hex: "#a78bfa" },
  publishing: { label: t("status.publishing"), hex: "#56c8e6", pulse: true },
  published: { label: t("status.published"), hex: "#c9f24e" },
  failed: { label: t("status.failed"), hex: "#f7768e" },
  rejected: { label: t("status.rejected"), hex: "#5a5f66" },
};

// Board column order
export const BOARD_COLUMNS: Status[] = [
  "draft", "queued", "rendering", "rendered", "review", "approved", "publishing", "published",
];

export const TERMINAL_COLUMNS: Status[] = ["failed", "rejected"];

/** Label for a raw video status enum (CMO v2 PT labels); unknown → as-is. */
export function statusLabel(status: string): string {
  const m = (STATUS_META as Record<string, { label: string }>)[(status || "").toLowerCase()];
  return m ? m.label : status;
}

/** Trend Radar statuses (CMO v2). */
export function trendStatusLabel(status: TrendStatus | string): string {
  const k = `status.trend.${status}`;
  const v = t(k);
  return v === k ? String(status) : v;
}
