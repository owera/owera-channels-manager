// "Segurado" (held) responses — CoS 08/10.
//
// The server answers some approve / produce / create-with-queue calls with
// HTTP 409 and a detail that starts with "held" (e.g. #87's
// "held: needs vm_pass — the VM's Gate B PASS on this render is required …",
// and #85's produce holds). That is NOT an error: the video is parked on
// purpose until a condition is met (the VM's PASS, a slot, …). The board shows
// it as a neutral "Segurado: <reason>" state and never as a failure.
// Keyed generically on 409 + a detail starting with "held" (case-insensitive),
// so any future hold works without a UI change.

export type HeldNotice = { id: number; videoId: number | null; path: string; reason: string; at: number };

/** The hold reason of an HTTP response, or null when it is not a hold. */
export function heldReason(status: number, body: unknown): string | null {
  if (status !== 409) return null;
  let d: unknown = body;
  if (d && typeof d === "object" && "detail" in (d as any)) d = (d as any).detail;
  if (d && typeof d === "object") d = (d as any).reason ?? (d as any).message ?? (d as any).detail;
  if (typeof d !== "string") return null;
  const t = d.trim();
  if (!/^held\b/i.test(t)) return null;
  // "held: needs vm_pass — …" → "needs vm_pass — …"
  return t.replace(/^held\s*[:\-—–]?\s*/i, "") || "held";
}

/** /videos/{id}/approve → id; bulk / create paths → null. */
export function videoIdOf(path: string): number | null {
  const m = /^\/videos\/(\d+)(?:\/|$)/.exec(path);
  return m ? Number(m[1]) : null;
}

export class HeldError extends Error {
  readonly held = true;
  constructor(readonly reason: string, readonly videoId: number | null, readonly path: string) {
    super(`Segurado: ${reason}`);
    this.name = "HeldError";
  }
}

export const isHeld = (e: unknown): e is HeldError => !!e && (e as any).held === true;

// ---- tiny store (no extra dependency) ----
const HOLD_TTL_MS = 10 * 60 * 1000;
let notices: HeldNotice[] = [];
let seq = 0;
const subs = new Set<() => void>();

function emit() { subs.forEach((f) => f()); }

export function pushHeld(e: HeldError) {
  const now = Date.now();
  notices = [
    { id: ++seq, videoId: e.videoId, path: e.path, reason: e.reason, at: now },
    ...notices.filter((n) => now - n.at < HOLD_TTL_MS && !(e.videoId != null && n.videoId === e.videoId)),
  ].slice(0, 20);
  emit();
}

export function dismissHeld(id: number) {
  notices = notices.filter((n) => n.id !== id);
  emit();
}

export function subscribeHeld(f: () => void) {
  subs.add(f);
  return () => { subs.delete(f); };
}

export const heldSnapshot = () => notices;
