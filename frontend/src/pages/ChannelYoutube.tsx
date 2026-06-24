import { useEffect, useState } from "react";
import {
  useChannelYoutube, useMetrics, useMut, useSubscribers, useSubscriptions,
  useMonetization, type Channel, type ChannelBranding, type MonetizationMetric,
  type MonetizationTier,
} from "../api";
import { Empty, Field, SectionLabel } from "../ui";

const fmt = (n: number) => n.toLocaleString();

const SIGNAL = "#c9f24e";
const ICE    = "#56c8e6";

function MonoBar({ pct, achieved }: { pct: number; achieved: boolean }) {
  return (
    <div className="h-1.5 rounded-full overflow-hidden" style={{ background: "rgba(255,255,255,0.08)" }}>
      <div
        className="h-full rounded-full transition-all duration-500"
        style={{ width: `${Math.min(pct, 100)}%`, background: achieved ? SIGNAL : ICE }}
      />
    </div>
  );
}

function TierRow({ label, metric, fmtVal }: {
  label: string;
  metric: MonetizationMetric;
  fmtVal: (n: number) => string;
}) {
  return (
    <div className="mb-3">
      <div className="flex items-center justify-between mb-1">
        <span className="label">{label}</span>
        {metric.achieved ? (
          <span className="font-mono text-[11px]" style={{ color: SIGNAL }}>✓ met</span>
        ) : (
          <span className="font-mono text-[11px] text-fog-400">{fmtVal(metric.needed)} left</span>
        )}
      </div>
      <MonoBar pct={metric.pct} achieved={metric.achieved} />
      <div className="label mt-0.5 text-fog-500">
        {fmtVal(metric.current)} / {fmtVal(metric.current + metric.needed)}
      </div>
    </div>
  );
}

function TierCard({ title, tier }: { title: string; tier: MonetizationTier }) {
  const fmtH = (n: number) => `${n.toLocaleString()}h`;
  return (
    <div className="panel p-4 flex-1">
      <div className="flex items-center justify-between mb-3">
        <div className="label">{title}</div>
        {tier.tier_achieved && (
          <span className="font-mono text-[10px] px-2 py-0.5 rounded-sm"
            style={{ color: SIGNAL, background: `${SIGNAL}20`, border: `1px solid ${SIGNAL}40` }}>
            UNLOCKED
          </span>
        )}
      </div>
      <TierRow label="subscribers" metric={tier.subscribers} fmtVal={fmt} />
      <TierRow label="watch hours" metric={tier.watch_hours} fmtVal={fmtH} />
      <TierRow label="shorts views" metric={tier.shorts_views} fmtVal={fmt} />
    </div>
  );
}

function MonetizationWidget({ channel }: { channel: Channel }) {
  const { data } = useMonetization(channel.id);
  return (
    <div className="mt-6">
      <SectionLabel>// monetization milestones</SectionLabel>
      {!data ? (
        <div className="label text-fog-400 mt-2">
          No snapshot data yet — metrics load after the first analytics run.
        </div>
      ) : (
        <div className="flex gap-3 mt-3">
          <TierCard title="lower tier — fan funding" tier={data.lower_tier} />
          <TierCard title="full tier — ad revenue"   tier={data.full_tier} />
        </div>
      )}
    </div>
  );
}

// Tiny inline trend line from a numeric series.
function Sparkline({ points }: { points: number[] }) {
  if (points.length < 2) return null;
  const w = 120, h = 26;
  const min = Math.min(...points), max = Math.max(...points), span = max - min || 1;
  const d = points
    .map((p, i) => {
      const x = (i / (points.length - 1)) * w;
      const y = h - ((p - min) / span) * h;
      return `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  return (
    <svg width={w} height={h}>
      <path d={d} fill="none" stroke="#c9f24e" strokeWidth={1.5} strokeLinejoin="round" />
    </svg>
  );
}

function Stat({ label, value, spark, delta }: {
  label: string; value: number; spark: number[]; delta?: number;
}) {
  return (
    <div className="panel p-4 flex-1">
      <div className="label mb-1">{label}</div>
      <div className="font-display font-extrabold text-2xl text-fog-50 tabular-nums">{fmt(value)}</div>
      <div className="flex items-center justify-between mt-1.5 h-[26px]">
        {spark.length > 1 ? <Sparkline points={spark} /> : <span className="label text-fog-500">no history yet</span>}
        {delta != null && delta !== 0 && (
          <span className={`font-mono text-[11px] ${delta > 0 ? "text-signal" : "text-[#f7768e]"}`}>
            {delta > 0 ? "+" : ""}{fmt(delta)}
          </span>
        )}
      </div>
    </div>
  );
}

function Metrics({ channel }: { channel: Channel }) {
  const { data: yt } = useChannelYoutube(channel.id);
  const { data: metrics } = useMetrics(channel.id);
  const m = useMut();
  const hist = metrics?.history ?? [];
  const subs = hist.map((p) => p.subscriber_count);
  const stats = yt?.statistics;
  const subDelta = subs.length > 1 ? subs[subs.length - 1] - subs[0] : 0;
  return (
    <div className="mt-6">
      <div className="flex items-center justify-between mb-3">
        <SectionLabel>// channel metrics</SectionLabel>
        <button className="btn !py-1.5" disabled={m.refreshMetrics.isPending}
          onClick={() => m.refreshMetrics.mutate(channel.id)}>
          {m.refreshMetrics.isPending ? "refreshing…" : "↻ refresh"}
        </button>
      </div>
      {!stats ? (
        <Empty>Couldn't load channel statistics.</Empty>
      ) : (
        <>
          <div className="flex gap-3">
            <Stat label="subscribers" value={stats.subscriber_count} spark={subs} delta={subDelta} />
            <Stat label="total views" value={stats.view_count} spark={hist.map((p) => p.view_count)} />
            <Stat label="videos" value={stats.video_count} spark={hist.map((p) => p.video_count)} />
          </div>
          <div className="label mt-2">
            {hist.length > 1
              ? `tracking ${hist.length} snapshots since ${new Date(hist[0].captured_at + "Z").toLocaleDateString()}`
              : "a daily snapshot is recorded automatically — refresh to add one now"}
          </div>
        </>
      )}
    </div>
  );
}

function Branding({ channel }: { channel: Channel }) {
  const { data: yt } = useChannelYoutube(channel.id);
  const m = useMut();
  const [form, setForm] = useState<ChannelBranding | null>(null);
  useEffect(() => { if (yt?.branding && form === null) setForm(yt.branding); }, [yt]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!form) return null;
  const set = (k: keyof ChannelBranding, v: string) => setForm({ ...form, [k]: v });
  return (
    <div className="mt-8">
      <SectionLabel>// youtube branding</SectionLabel>
      <p className="text-[12px] text-fog-400 mb-4 mt-1">Edits your channel's public details on YouTube.</p>
      <div className="grid grid-cols-2 gap-x-6 gap-y-4">
        <Field label="channel title">
          <input className="input" value={form.title ?? ""} onChange={(e) => set("title", e.target.value)} />
        </Field>
        <Field label="keywords" hint="space-separated; wrap multi-word in quotes">
          <input className="input" value={form.keywords ?? ""} onChange={(e) => set("keywords", e.target.value)} />
        </Field>
        <div className="col-span-2">
          <Field label="description">
            <textarea className="input h-28" value={form.description ?? ""}
              placeholder="No description set on YouTube — add one and save to publish it."
              onChange={(e) => set("description", e.target.value)} />
          </Field>
        </div>
        <Field label="country" hint="ISO code, e.g. US">
          <input className="input" value={form.country ?? ""} maxLength={2} placeholder="—"
            onChange={(e) => set("country", e.target.value.toUpperCase())} />
        </Field>
        <Field label="default language" hint="e.g. en">
          <input className="input" value={form.default_language ?? ""} placeholder="—"
            onChange={(e) => set("default_language", e.target.value)} />
        </Field>
      </div>
      <div className="flex items-center gap-3 mt-4">
        <button className="btn btn-signal" disabled={m.updateBranding.isPending}
          onClick={() => m.updateBranding.mutate({ id: channel.id, body: form })}>
          {m.updateBranding.isPending ? "saving…" : "save branding"}
        </button>
        {m.updateBranding.isSuccess && <span className="label text-signal">saved ✓</span>}
        {m.updateBranding.isError && <span className="label text-[#f7768e]">{(m.updateBranding.error as Error).message}</span>}
      </div>
    </div>
  );
}

function SubRow({ thumbnail, title, href, onRemove }: {
  thumbnail?: string | null; title: string; href?: string; onRemove?: () => void;
}) {
  return (
    <div className="flex items-center gap-2.5 panel p-2">
      {thumbnail
        ? <img src={thumbnail} className="w-7 h-7 rounded-full shrink-0" alt="" />
        : <span className="w-7 h-7 rounded-full bg-ink-500 shrink-0" />}
      {href
        ? <a href={href} target="_blank" className="text-sm text-fog-100 truncate flex-1 hover:underline">{title}</a>
        : <span className="text-sm text-fog-100 truncate flex-1">{title}</span>}
      {onRemove && <button className="label hover:text-[#f7768e]" onClick={onRemove}>unsub</button>}
    </div>
  );
}

function Subscriptions({ channel }: { channel: Channel }) {
  const { data: following } = useSubscriptions(channel.id);
  const { data: subscribers } = useSubscribers(channel.id);
  const m = useMut();
  const [ref, setRef] = useState("");
  const add = () => {
    if (ref.trim()) m.subscribe.mutate({ id: channel.id, channel: ref.trim() }, { onSuccess: () => setRef("") });
  };
  return (
    <div className="mt-8">
      <SectionLabel>// subscriptions</SectionLabel>
      <div className="grid grid-cols-2 gap-6 mt-3">
        <div>
          <div className="label mb-2">following ({following?.length ?? 0})</div>
          <div className="flex gap-2 mb-2">
            <input className="input" placeholder="channel URL, @handle, or ID" value={ref}
              onChange={(e) => setRef(e.target.value)} onKeyDown={(e) => e.key === "Enter" && add()} />
            <button className="btn btn-signal !py-1.5 shrink-0" disabled={!ref || m.subscribe.isPending} onClick={add}>
              {m.subscribe.isPending ? "…" : "subscribe"}
            </button>
          </div>
          {m.subscribe.isError && <div className="label text-[#f7768e] mb-2">{(m.subscribe.error as Error).message}</div>}
          {!following?.length ? <Empty>Not following any channels.</Empty> : (
            <div className="space-y-1.5 max-h-72 overflow-y-auto pr-1">
              {following.map((s) => (
                <SubRow key={s.sub_id} thumbnail={s.thumbnail} title={s.title}
                  href={`https://youtube.com/channel/${s.channel_id}`}
                  onRemove={() => m.unsubscribe.mutate({ id: channel.id, subId: s.sub_id })} />
              ))}
            </div>
          )}
        </div>
        <div>
          <div className="label mb-2">recent subscribers ({subscribers?.length ?? 0})</div>
          {!subscribers?.length ? <Empty>No visible subscribers.</Empty> : (
            <div className="space-y-1.5 max-h-72 overflow-y-auto pr-1">
              {subscribers.map((s, i) => (
                <SubRow key={s.channel_id || i} thumbnail={s.thumbnail} title={s.title}
                  href={s.channel_id ? `https://youtube.com/channel/${s.channel_id}` : undefined} />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default function ChannelYoutube({ channel }: { channel: Channel }) {
  if (channel.oauth_status !== "connected") {
    return (
      <div className="mt-6">
        <SectionLabel>// youtube management</SectionLabel>
        <div className="mt-2">
          <Empty>Connect this channel to manage its branding, metrics, and subscriptions.</Empty>
        </div>
      </div>
    );
  }
  return (
    <>
      <MonetizationWidget channel={channel} />
      <Metrics channel={channel} />
      <Branding channel={channel} />
      <Subscriptions channel={channel} />
    </>
  );
}
