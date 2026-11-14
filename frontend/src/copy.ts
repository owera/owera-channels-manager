import { wizardT as t } from "./wizardI18n";

/** Buyer-facing labels — plain English, no internal jargon. */

export function oauthStatusLabel(status: string): string {
  switch (status) {
    case "connected":
      return t("status.oauth.connected");
    case "expired":
      return t("status.oauth.expired");
    case "error":
      return t("status.oauth.error");
    case "disconnected":
      return t("status.oauth.disconnected");
    default:
      return status.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  }
}

/** Server hold reasons often mention vm_pass / Gate B — never show that raw to buyers. */
export function humanizeHeldReason(raw: string): string {
  let s = raw.trim();
  if (!s) return "This step is paused until a requirement is met.";

  s = s.replace(/^needs\s+vm_pass\s*[—–-]\s*/i, "");
  s = s.replace(/\bvm_pass\b/gi, "quality check");
  s = s.replace(/\bgate\s*b\b/gi, "quality check");
  s = s.replace(/the vm'?s\s+gate\s+b\s+pass\s+on\s+this\s+render\s+is\s+required\.?/gi, "");
  s = s.replace(/the vm'?s\s+pass\s+on\s+this\s+render\s+is\s+required\.?/gi, "");
  s = s.replace(/\s*[—–-]\s*$/, "").trim();

  if (/^needs\s/i.test(s) || s.length < 8) {
    return "Waiting on a quality check before this video can move forward.";
  }
  return s.charAt(0).toUpperCase() + s.slice(1);
}
