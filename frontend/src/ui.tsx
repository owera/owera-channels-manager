import { ReactNode, useEffect } from "react";
import { createPortal } from "react-dom";
import { motion, AnimatePresence } from "framer-motion";
import { STATUS_META } from "./status";
import type { Status } from "./api";

export function StatusChip({ status, sm }: { status: Status; sm?: boolean }) {
  const m = STATUS_META[status];
  return (
    <span
      className="inline-flex items-center gap-1.5 font-mono uppercase tracking-wider rounded-sm border"
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
  return <div className="label mb-3">{children}</div>;
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

export function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="text-center py-16 text-fog-300 font-mono text-sm border border-dashed border-ink-line rounded-md">
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
