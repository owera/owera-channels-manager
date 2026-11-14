import { ReactNode, useEffect, useSyncExternalStore } from "react";
import { createPortal } from "react-dom";
import { motion, AnimatePresence } from "framer-motion";
import { STATUS_META } from "./status";
import type { Status } from "./api";
import { humanizeHeldReason } from "./copy";
import { dismissHeld, heldSnapshot, subscribeHeld, type HeldNotice } from "./held";

export function StatusChip({ status, sm }: { status: Status; sm?: boolean }) {
  const m = STATUS_META[status];
  return (
    <span
      className="inline-flex items-center gap-1.5 text-[11px] font-medium tracking-wide rounded-sm border"
      style={{
        color: m.hex,
        borderColor: `${m.hex}40`,
        background: `${m.hex}14`,
        fontSize: sm ? 9 : 10,
        padding: sm ? "1px 6px" : "2px 8px",
      }}
    >
      <Dot hex={m.hex} pulse={m.pulse} />
      {m.label}
    </span>
  );
}

export function Dot({ hex, pulse }: { hex: string; pulse?: boolean }) {
  return (
    <span
      className={`inline-block w-1.5 h-1.5 rounded-full ${pulse ? "animate-pulseDot" : ""}`}
      style={{ background: hex, boxShadow: `0 0 6px ${hex}` }}
    />
  );
}

export function SectionLabel({ children }: { children: ReactNode }) {
  const text = typeof children === "string" ? children.replace(/^\/\/\s*/, "") : children;
  return (
    <h3 className="text-[11px] font-medium uppercase tracking-[0.2em] text-fog-400 mb-3">{text}</h3>
  );
}

export function Stat({ value, label, accent }: { value: ReactNode; label: string; accent?: string }) {
  return (
    <div>
      <div className="font-display font-extrabold leading-none" style={{ fontSize: 30, color: accent || "#eef1f4" }}>
        {value}
      </div>
      <div className="label mt-1.5">{label}</div>
    </div>
  );
}

export function Modal({
  open, onClose, title, children, wide,
}: { open: boolean; onClose: () => void; title: string; children: ReactNode; wide?: boolean }) {
  useEffect(() => {
    const h = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    if (open) window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  }, [open, onClose]);
  // Render into <body> so the fixed overlay isn't trapped by an ancestor's
  // containing block — `.panel` uses backdrop-filter, which (like transform)
  // re-roots position:fixed onto that panel instead of the viewport.
  return createPortal(
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/70 backdrop-blur-sm p-4 sm:p-10"
          initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
          onClick={onClose}
        >
          <motion.div
            className={`panel w-full ${wide ? "max-w-3xl" : "max-w-lg"} my-auto`}
            initial={{ y: 14, opacity: 0, scale: 0.99 }}
            animate={{ y: 0, opacity: 1, scale: 1 }}
            exit={{ y: 8, opacity: 0 }}
            transition={{ type: "spring", stiffness: 320, damping: 28 }}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between px-5 py-3.5 border-b border-ink-line">
              <h3 className="font-display font-bold text-fog-50 text-lg">{title}</h3>
              <button className="text-fog-300 hover:text-white text-xl leading-none" onClick={onClose}>×</button>
            </div>
            <div className="p-5">{children}</div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>,
    document.body,
  );
}

export function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <label className="block mb-4">
      <div className="label mb-1.5">{label}</div>
      {children}
      {hint && <div className="text-[11px] text-fog-400 mt-1">{hint}</div>}
    </label>
  );
}

export function Empty({ children, compact }: { children: ReactNode; compact?: boolean }) {
  return (
    <div
      className={`text-center ${compact ? "py-10 px-6" : "py-16 px-8"} text-fog-300 text-sm leading-relaxed border border-dashed border-ink-line/80 rounded-md bg-ink-800/30`}
    >
      {children}
    </div>
  );
}

export function Toggle({ on, onChange }: { on: boolean; onChange: (v: boolean) => void }) {
  return (
    <button
      type="button"
      onClick={() => onChange(!on)}
      className={`relative w-10 h-5 rounded-full transition-colors ${on ? "bg-signal" : "bg-ink-500"}`}
    >
      {/* left-0 anchors the knob to the track's left edge; without it the empty
          inline span centers (text-align:center on <button>) and the transform
          pushes the ON knob off the right edge. */}
      <span
        className="absolute left-0 top-0.5 w-4 h-4 rounded-full bg-white shadow transition-transform"
        style={{ transform: on ? "translateX(22px)" : "translateX(2px)" }}
      />
    </button>
  );
}

// ---- "Segurado" (held) state: 409 "held…" from approve / produce ----
export function useHeld(): HeldNotice[] {
  return useSyncExternalStore(subscribeHeld, heldSnapshot, heldSnapshot);
}

const HELD_HEX = "#f5a524";

/** Neutral amber badge — a hold is not an error. */
export function HeldBadge({ reason }: { reason: string }) {
  const text = humanizeHeldReason(reason);
  return (
    <div className="mt-2 flex items-start gap-1.5 text-[11px] leading-snug rounded-sm border px-2 py-1.5"
      style={{ color: HELD_HEX, borderColor: `${HELD_HEX}40`, background: `${HELD_HEX}14` }}
      title={text} data-held="1">
      <span className="shrink-0">⏸</span>
      <span className="line-clamp-3"><span className="font-medium text-fog-100">On hold.</span> {text}</span>
    </div>
  );
}

/** Global toasts for holds (bulk produce / review approve have no card). */
export function HeldToasts() {
  const notices = useHeld();
  useEffect(() => {
    if (!notices.length) return;
    const t = setTimeout(() => dismissHeld(notices[notices.length - 1].id), 8000);
    return () => clearTimeout(t);
  }, [notices]);
  const recent = notices.filter((n) => Date.now() - n.at < 8000).slice(0, 4);
  if (!recent.length) return null;
  return createPortal(
    <div className="fixed right-4 bottom-20 md:bottom-4 z-[60] flex flex-col gap-2 max-w-sm">
      {recent.map((n) => (
        <div key={n.id} role="status" className="panel px-3 py-2 text-xs flex items-start gap-2"
          style={{ color: HELD_HEX, borderColor: `${HELD_HEX}55` }}>
          <span>⏸</span>
          <span className="flex-1 leading-snug">
            <span className="font-medium text-fog-100">On hold</span>
            {n.videoId != null ? ` · video ${n.videoId}` : ""}: {humanizeHeldReason(n.reason)}
          </span>
          <button className="text-fog-300 hover:text-white" onClick={() => dismissHeld(n.id)}>×</button>
        </div>
      ))}
    </div>,
    document.body,
  );
}

// Policy links required by the Google OAuth verification / YouTube API Services
// Developer Policies (III.A.1, III.A.2.a): privacy policy reachable at all times,
// plus Terms and the YouTube Terms of Service.
export const POLICY_LINKS = {
  privacy: "https://owera.com/privacy/",
  terms: "https://owera.com/terms/",
  youtubeTerms: "https://www.youtube.com/t/terms",
} as const;

function ExtLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <a href={href} target="_blank" rel="noopener noreferrer" className="underline hover:text-fog-100">
      {children}
    </a>
  );
}

export function PolicyLinks({ className = "" }: { className?: string }) {
  return (
    <nav aria-label="Legal" className={`text-[11px] text-fog-400 flex flex-wrap gap-x-2 gap-y-1 ${className}`}>
      <ExtLink href={POLICY_LINKS.privacy}>Privacy</ExtLink>
      <span aria-hidden>·</span>
      <ExtLink href={POLICY_LINKS.terms}>Terms</ExtLink>
      <span aria-hidden>·</span>
      <ExtLink href={POLICY_LINKS.youtubeTerms}>YouTube Terms of Service</ExtLink>
    </nav>
  );
}

export function ConnectConsentNote({ className = "" }: { className?: string }) {
  return (
    <p className={`text-xs text-fog-400 leading-relaxed ${className}`}>
      By connecting, you agree to the Owera <ExtLink href={POLICY_LINKS.terms}>Terms</ExtLink>, the{" "}
      <ExtLink href={POLICY_LINKS.privacy}>Privacy notice</ExtLink> and the{" "}
      <ExtLink href={POLICY_LINKS.youtubeTerms}>YouTube Terms of Service</ExtLink>.
    </p>
  );
}

/** S8: "Drafts paused: …" banner from GET /api/onboarding/status `llm_blocked`. */
export function LlmBlockedBanner({ blocked }: { blocked?: { message: string; reason: string } | null }) {
  if (!blocked) return null;
  return (
    <div role="alert" data-testid="llm-blocked-banner"
      className="mb-6 panel px-4 py-3 border-amber-400/40 text-sm text-fog-100 leading-relaxed">
      {blocked.message}
    </div>
  );
}
