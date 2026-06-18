import { useState } from "react";
import { Link } from "react-router-dom";
import { motion } from "framer-motion";
import { useTrends, useChannels, useMut, type TrendSignal, type TrendStatus } from "../api";
import { Dot, Empty } from "../ui";

const STATUS_HEX: Record<TrendStatus, string> = {
  researched: "#56c8e6", watching: "#f5a524", adopted: "#c9f24e", rejected: "#6c7681",
};
const MOMENTUM_HEX: Record<string, string> = {
  hot: "#f7768e", rising: "#c9f24e", evergreen: "#56c8e6", fading: "#6c7681",
};

function scoreHex(s: number) {
  return s >= 70 ? "#c9f24e" : s >= 40 ? "#f5a524" : "#6c7681";
}

function Badge({ text, hex }: { text: string; hex: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 font-mono uppercase tracking-wider rounded-sm border text-[10px] px-2 py-0.5"
      style={{ color: hex, borderColor: `${hex}40`, background: `${hex}14` }}>
      {text}
    </span>
  );
}

function Kpi({ value, label, accent }: { value: React.ReactNode; label: string; accent?: string }) {
  return (
    <div className="panel px-5 py-4">
      <div className="font-display font-extrabold leading-none" style={{ fontSize: 30, color: accent || "#eef1f4" }}>{value}</div>
      <div className="label mt-2">{label}</div>
    </div>
  );
}

function TrendRow({ t, channelName, channels, i }: {
  t: TrendSignal; channelName: (id: number | null) => string;
  channels: { id: number; name: string }[]; i: number;
}) {
  const m = useMut();
  const [pick, setPick] = useState<number | "">(t.channel_id ?? (channels[0]?.id ?? ""));
  const open = t.status === "researched" || t.status === "watching";
  const busy = m.adoptTrend.isPending || m.updateTrend.isPending;

  return (
    <motion.div initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: i * 0.02 }}
      className="grid grid-cols-[1fr_auto] gap-3 px-4 py-3 items-start">
      <div className="min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="font-display font-bold text-fog-50 text-sm">{t.term}</span>
          <Badge text={t.status} hex={STATUS_HEX[t.status]} />
          {t.momentum && <Badge text={t.momentum} hex={MOMENTUM_HEX[t.momentum] || "#6c7681"} />}
          <span className="label">{t.content_format}{t.language ? ` · ${t.language}` : ""}</span>
          {t.channel_id && <span className="label text-fog-300">→ {channelName(t.channel_id)}</span>}
        </div>
        {t.description && <div className="text-[12px] text-fog-300 mt-1 line-clamp-2">{t.description}</div>}
        {t.decision_reason && <div className="text-[11px] text-fog-400 mt-1 italic">“{t.decision_reason}”</div>}
        <div className="flex items-center gap-3 mt-1.5">
          {t.source && <span className="label text-fog-500">{t.source}</span>}
          {t.status === "adopted" && t.adopted_topic_id && (
            <Link to={`/board/${t.channel_id}`} className="label text-signal hover:underline">
              adopted → topic {t.adopted_topic_id}
            </Link>
          )}
        </div>
      </div>

      <div className="flex items-center gap-3 shrink-0">
        <div className="text-right">
          <div className="font-display font-extrabold leading-none tabular-nums" style={{ fontSize: 22, color: scoreHex(t.score) }}>
            {Math.round(t.score)}
          </div>
          <div className="label !text-[8px] mt-0.5">score</div>
        </div>
        {open && (
          <div className="flex flex-col items-end gap-1.5">
            <div className="flex items-center gap-1.5">
              {!t.channel_id && (
                <select value={pick} onChange={(e) => setPick(Number(e.target.value))}
                  className="bg-ink-700 border border-ink-line rounded-sm text-[11px] px-1.5 py-1 text-fog-100">
                  {channels.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                </select>
              )}
              <button disabled={busy || (!t.channel_id && !pick)}
                onClick={() => m.adoptTrend.mutate({ id: t.id, body: { channel_id: t.channel_id || pick || undefined } })}
                className="btn btn-signal !py-1 !px-3 text-[11px]">Adopt</button>
            </div>
            <button disabled={busy}
              onClick={() => m.updateTrend.mutate({ id: t.id, body: { status: "rejected" } })}
              className="btn btn-ghost !py-0.5 !px-3 text-[10px]">Reject</button>
          </div>
        )}
      </div>
    </motion.div>
  );
}

export default function Trends() {
  const { data: trends } = useTrends();
  const { data: channels } = useChannels();
  const [filter, setFilter] = useState<TrendStatus | "all">("all");

  const chList = (channels ?? []).map((c) => ({ id: c.id, name: c.name }));
  const channelName = (id: number | null) =>
    (channels?.find((c) => c.id === id)?.name) ?? "portfolio";

  const all = trends ?? [];
  const count = (s: TrendStatus) => all.filter((t) => t.status === s).length;
  const shown = filter === "all" ? all : all.filter((t) => t.status === filter);

  const FILTERS: (TrendStatus | "all")[] = ["all", "researched", "watching", "adopted", "rejected"];

  return (
    <div className="p-8 max-w-[1400px]">
      <header className="mb-6">
        <div className="label mb-2">// trends</div>
        <h1 className="font-display font-extrabold text-4xl text-fog-50 tracking-tight">Trend Radar</h1>
        <p className="text-fog-300 text-sm mt-2 max-w-2xl">
          What the growth agent is researching across the AI/tech niche, scored for fit. Adopting a
          trend spins up a topic, seeds ideas, and auto-produces a few. The agent adopts the top picks
          itself each run — you can adopt or reject any here too.
        </p>
      </header>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-6">
        <Kpi value={count("researched") + count("watching")} label="open candidates" accent="#56c8e6" />
        <Kpi value={count("adopted")} label="adopted" accent={count("adopted") ? "#c9f24e" : undefined} />
        <Kpi value={count("watching")} label="watching" accent={count("watching") ? "#f5a524" : undefined} />
        <Kpi value={all.length} label="tracked total" />
      </div>

      <div className="flex items-center gap-2 mb-3">
        {FILTERS.map((f) => (
          <button key={f} onClick={() => setFilter(f)}
            className={`font-mono text-[10px] uppercase tracking-wider px-2.5 py-1 rounded-sm border transition-colors ${
              filter === f ? "text-signal border-signal/40 bg-signal/5" : "text-fog-400 border-ink-line hover:text-fog-200"
            }`}>
            {f}{f !== "all" ? ` ${count(f as TrendStatus)}` : ""}
          </button>
        ))}
      </div>

      {shown.length ? (
        <div className="panel divide-y divide-ink-line/60">
          {shown.map((t, i) => (
            <TrendRow key={t.id} t={t} channelName={channelName} channels={chList} i={i} />
          ))}
        </div>
      ) : (
        <Empty>
          {all.length
            ? "no trends in this filter"
            : "no trends yet — the growth agent records them on its daily run"}
        </Empty>
      )}
    </div>
  );
}
