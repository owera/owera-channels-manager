import type { Status } from "./api";

// Each status gets a label, a hex accent, and a tailwind text/border/bg recipe.
export const STATUS_META: Record<
  Status,
  { label: string; hex: string; pulse?: boolean }
> = {
  draft: { label: "IDEAS", hex: "#8a8f98" },
  queued: { label: "QUEUED", hex: "#6c7681" },
  rendering: { label: "RENDERING", hex: "#56c8e6", pulse: true },
  rendered: { label: "RENDERED", hex: "#7aa2f7" },
  review: { label: "REVIEW", hex: "#f5a524" },
  approved: { label: "APPROVED", hex: "#a78bfa" },
  publishing: { label: "PUBLISHING", hex: "#56c8e6", pulse: true },
  published: { label: "LIVE", hex: "#c9f24e" },
  failed: { label: "FAILED", hex: "#f7768e" },
  rejected: { label: "REJECTED", hex: "#5a5f66" },
};

// Board column order
export const BOARD_COLUMNS: Status[] = [
  "draft", "queued", "rendering", "rendered", "review", "approved", "publishing", "published",
];

export const TERMINAL_COLUMNS: Status[] = ["failed", "rejected"];
