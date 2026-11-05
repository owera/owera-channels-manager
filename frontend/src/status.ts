import type { Status } from "./api";

// Each status gets a label, a hex accent, and a tailwind text/border/bg recipe.
export const STATUS_META: Record<
  Status,
  { label: string; hex: string; pulse?: boolean }
> = {
  draft: { label: "Ideas", hex: "#8a8f98" },
  queued: { label: "Queued", hex: "#6c7681" },
  rendering: { label: "Rendering", hex: "#56c8e6", pulse: true },
  rendered: { label: "Rendered", hex: "#7aa2f7" },
  review: { label: "Review", hex: "#f5a524" },
  approved: { label: "Approved", hex: "#a78bfa" },
  publishing: { label: "Publishing", hex: "#56c8e6", pulse: true },
  published: { label: "Live", hex: "#c9f24e" },
  failed: { label: "Failed", hex: "#f7768e" },
  rejected: { label: "Rejected", hex: "#5a5f66" },
};

// Board column order
export const BOARD_COLUMNS: Status[] = [
  "draft", "queued", "rendering", "rendered", "review", "approved", "publishing", "published",
];

export const TERMINAL_COLUMNS: Status[] = ["failed", "rejected"];
