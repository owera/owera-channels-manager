import { Link } from "react-router-dom";
import { motion } from "framer-motion";
import { useDashboard, useOnboarding, useRuns, useSettings, type DashboardRow, type Status } from "../api";
import { oauthStatusLabel } from "../copy";
import { STATUS_META } from "../status";
import { Dot, Empty, LlmBlockedBanner } from "../ui";
import { wizardT as t } from "../wizardI18n";
import { statusLabel } from "../status";

function relTime(iso: string): string {
  const diff = new Date(iso).getTime() - Date.now();
  if (diff <= 0) return "now";
  const m = Math.round(diff / 60000);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ${m % 60}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

// The pipeline stages summarized on each channel card.
const FUNNEL: Status[] = ["draft", "queued", "rendering", "review", "approved", "published"];
// CMO v2: same label map as the status chips (EN unchanged, PT added).
const FUNNEL_LABEL: Record<string, string> = Object.fromEntries(
  FUNNEL.map((s) => [s, (s === "rendering" ? t("status.rendering.short") : statusLabel(s)).toLowerCase()]));
const HOLD_LABEL: Record<string, string> = {
  paused: t("status.paused").toLowerCase(),
  budget: "budget 0",
  oauth: "reconnect",
};

function Kpi({ value, label, accent }: { value: React.ReactNode; label: string; accent?: string }) {
  return (
    <div className="panel px-5 py-4">
      <div className="font-display font-extrabold leading-none" style={{ fontSize: 34, color: accent || "#eef1f4" }}>{value}</div>
      <div className="label mt-2">{label}</div>
    </div>
  );
}

function QuotaBar({ spent, cap }: { spent: number; cap: number }) {
  const pct = Math.min(100, Math.round((spent / cap) * 100));
  return (
    <div>
      <div className="flex justify-between label mb-1"><span>youtube quota</span><span className="text-fog-200">{spent}/{cap}</span></div>
      <div className="h-1.5 bg-ink-500 rounded-full overflow-hidden">
        <div className="h-full rounded-full transition-all" style={{ width: `${pct}%`, background: pct > 80 ? "#f5a524" : "#c9f24e" }} />
      </div>
    </div>
  );
}

function ChannelCard({ row, i }: { row: DashboardRow; i: number }) {
  const c = row.channel;
  const counts = row.counts;
  const review = counts.review || 0;
  const pubPct = Math.min(100, Math.round((row.published_today / Math.max(1, c.daily_publish_budget)) * 100));
  const oauthHex = c.oauth_status === "connected" ? "#c9f24e" : c.oauth_status === "expired" ? "#f5a524" : "#6c7681";

  return (
    <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: i * 0.05 }}
      className="panel p-5 card-hover">
      <div className="flex items-start justify-between">
        <div>
          <div className="flex items-center gap-2">
            <Dot hex={c.paused ? "#f5a524" : oauthHex} pulse={!c.paused && c.oauth_status === "connected"} />
            <Link to={`/board/${c.id}`} className="font-display font-bold text-fog-50 text-lg hover:text-signal transition-colors">{c.name}</Link>
          </div>
          <div className="label mt-1 normal-case tracking-normal">{c.paused ? t("status.paused") : oauthStatusLabel(c.oauth_status)} · {row.counts.published || 0} live</div>
        </div>
        {review > 0 && (
          <Link to={`/board/${c.id}`} className="font-mono text-[10px] uppercase tracking-wider px-2 py-1 rounded-sm"
            style={{ color: "#f5a524", background: "#f5a52414", border: "1px solid #f5a52440" }}>{review} to review</Link>
        )}
      </div>

      {/* pipeline funnel */}
      <div className="grid grid-cols-6 gap-1 mt-5 mb-4">
        {FUNNEL.map((s) => (
          <div key={s} className="text-center">
            <div className="font-display font-bold text-xl leading-none" style={{ color: (counts[s] || 0) ? STATUS_META[s].hex : "#3a424c" }}>{counts[s] || 0}</div>
            <div className="label mt-1 !text-[8px]">{FUNNEL_LABEL[s]}</div>
          </div>
        ))}
      </div>

      {/* active work */}
      {row.active.length > 0 && (
        <div className="mb-4 space-y-1.5">
          {row.active.map((a) => (
            <div key={a.id} className="flex items-center gap-2">
              <Dot hex={STATUS_META[a.status].hex} pulse />
              <span className="text-[11px] text-fog-200 truncate flex-1">{a.subject}</span>
              <div className="w-16 h-1 bg-ink-500 rounded-full overflow-hidden">
                <div className="h-full" style={{ width: `${a.render_progress}%`, background: STATUS_META[a.status].hex }} />
              </div>
              <span className="label tabular-nums w-7 text-right">{a.render_progress}%</span>
            </div>
          ))}
        </div>
      )}

      {/* publish today + next eta */}
      <div className="mb-3">
        <div className="flex justify-between label mb-1">
          <span>published today</span>
          <span className="text-fog-200">{row.published_today}/{c.daily_publish_budget}
            {row.next_publish_eta && <span className="text-fog-400"> · next {relTime(row.next_publish_eta)}</span>}
            {!row.next_publish_eta && row.publish_hold && (
              <span className="text-amber"> · {HOLD_LABEL[row.publish_hold] ?? row.publish_hold}</span>
            )}
          </span>
        </div>
        <div className="h-1.5 bg-ink-500 rounded-full overflow-hidden">
          <div className="h-full rounded-full transition-all" style={{ width: `${pubPct}%`, background: "#56c8e6" }} />
        </div>
      </div>

      <QuotaBar spent={row.quota_spent_today} cap={row.quota_cap} />

      <div className="flex gap-3 mt-4">
        <Link to={`/board/${c.id}`} className="btn btn-ghost !py-1.5 flex-1 justify-center">Open queue</Link>
        <Link to="/channels" className="btn btn-ghost !py-1.5 flex-1 justify-center">Topics</Link>
      </div>
    </motion.div>
  );
}

export default function Dashboard() {
  const { data: rows } = useDashboard();
  const { data: runs } = useRuns();
  const { data: settings } = useSettings();
  const { data: onboarding } = useOnboarding();

  const sum = (k: Status) => rows?.reduce((a, r) => a + (r.counts[k] || 0), 0) ?? 0;
  const live = sum("published");
  const review = sum("review");
  const pipeline = sum("queued") + sum("rendering") + sum("rendered") + sum("approved") + sum("publishing");
  const ideas = sum("draft");
  const pubToday = rows?.reduce((a, r) => a + r.published_today, 0) ?? 0;
  const active = rows?.flatMap((r) => r.active.map((a) => ({ ...a, channel: r.channel.name }))) ?? [];

  return (
    <div className="p-4 md:p-8 max-w-[1500px]">
      <LlmBlockedBanner blocked={onboarding?.llm_blocked} />
      {onboarding && !onboarding.onboarding_complete && (
        <div className="mb-6 panel px-4 py-3 flex flex-wrap items-center justify-between gap-3 border-signal/30">
          <span className="text-sm text-fog-200 leading-relaxed">Finish setup: connect YouTube, add a topic, and generate your first ideas.</span>
          <Link to="/onboarding" className="btn btn-signal !py-1.5 shrink-0">Setup checklist</Link>
        </div>
      )}

      <header className="flex flex-wrap items-end justify-between gap-4 mb-8">
        <div>
          <p className="text-[11px] font-medium uppercase tracking-[0.2em] text-fog-400 mb-2">Home</p>
          <h1 className="font-display font-extrabold text-2xl sm:text-4xl text-fog-50 tracking-tight">Your channels</h1>
          <p className="text-sm text-fog-400 mt-2 max-w-xl">Live counts, what&apos;s rendering, and what needs your review.</p>
        </div>
        <div className="flex flex-wrap items-center gap-3 text-xs text-fog-300">
          {settings?.youtube_quota_reset_at && (
            <span className="flex items-center gap-1.5 panel px-3 py-1.5">
              <span className="text-fog-400">◷</span>
              <span>YouTube quota resets in</span>
              <span className="text-fog-100 tabular-nums font-medium">{relTime(settings.youtube_quota_reset_at)}</span>
            </span>
          )}
          {settings?.scheduler_paused && (
            <span className="panel px-3 py-1.5 text-amber">Publishing is paused workspace-wide</span>
          )}
        </div>
      </header>

      {/* KPI strip */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3 mb-6">
        <Kpi value={live} label="total live" accent="#c9f24e" />
        <Kpi value={review} label="awaiting review" accent={review ? "#f5a524" : undefined} />
        <Kpi value={pipeline} label="in pipeline" accent={pipeline ? "#56c8e6" : undefined} />
        <Kpi value={ideas} label="ideas" />
        <Kpi value={pubToday} label="published today" />
      </div>

      {/* NOW panel */}
      <div className="panel p-5 mb-6">
        <h2 className="text-[11px] font-medium uppercase tracking-[0.2em] text-fog-400 mb-3">Right now</h2>
        {active.length === 0 ? (
          <p className="text-sm text-fog-400">Nothing is rendering or publishing. Generate ideas from a topic when you&apos;re ready.</p>
        ) : (
          <div className="space-y-2">
            {active.map((a) => (
              <div key={a.id} className="flex items-center gap-2 sm:gap-3">
                <Dot hex={STATUS_META[a.status].hex} pulse />
                <span className="text-[11px] font-medium w-[72px] sm:w-[88px] shrink-0" style={{ color: STATUS_META[a.status].hex }}>{STATUS_META[a.status].label}</span>
                <span className="text-sm text-fog-100 truncate flex-1">{a.subject}</span>
                <span className="label text-fog-400 hidden sm:inline">{a.channel}</span>
                <div className="hidden sm:block w-28 h-1.5 bg-ink-500 rounded-full overflow-hidden">
                  <div className="h-full" style={{ width: `${a.render_progress}%`, background: STATUS_META[a.status].hex }} />
                </div>
                <span className="label tabular-nums w-8 text-right">{a.render_progress}%</span>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Channel cards */}
      {rows?.length ? (
        <div className="grid grid-cols-1 lg:grid-cols-2 2xl:grid-cols-3 gap-4 mb-8">
          {rows.map((r, i) => <ChannelCard key={r.channel.id} row={r} i={i} />)}
        </div>
      ) : (
        <Empty compact>
          <p className="text-fog-200 mb-2">No channels yet</p>
          <p>Add a channel, connect YouTube, and you&apos;ll see progress here.</p>
          <Link to="/channels" className="btn btn-signal mt-5 inline-flex">Add channel</Link>
        </Empty>
      )}

      {/* Activity */}
      <h2 className="text-[11px] font-medium uppercase tracking-[0.2em] text-fog-400 mb-3">Recent activity</h2>
      <div className="panel divide-y divide-ink-line/60">
        {!runs?.length && <div className="px-4 py-8 text-fog-400 text-sm text-center">Activity from renders and publishes will show up here.</div>}
        {runs?.slice(0, 16).map((r) => (
          <div key={r.id} className="flex items-center gap-2 sm:gap-3 px-3 sm:px-4 py-2 font-mono text-xs">
            <Dot hex={r.status === "success" ? "#c9f24e" : r.status === "error" ? "#f7768e" : "#56c8e6"} />
            <span className="text-fog-400 tabular-nums shrink-0 text-[10px] sm:text-xs w-[86px] sm:w-[112px]">{new Date(r.created_at + "Z").toLocaleString([], { month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" })}</span>
            <span className="hidden sm:inline uppercase tracking-wider text-fog-200 w-[92px] shrink-0">{r.kind}</span>
            <span className="text-fog-300 truncate flex-1">{r.detail || r.status}</span>
            {r.quota_cost > 0 && <span className="text-fog-400 shrink-0">−{r.quota_cost}u</span>}
          </div>
        ))}
      </div>
    </div>
  );
}
