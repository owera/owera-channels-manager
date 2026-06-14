import { useEffect, useState } from "react";
import { useNavigate, useParams, useSearchParams, Link } from "react-router-dom";
import { motion } from "framer-motion";
import {
  useChannels, useMut, useProfiles, usePublishPlan, useQueuePlan, useTopics, useVideos,
  type QueueReason, type Video,
} from "../api";
import { BOARD_COLUMNS, STATUS_META, TERMINAL_COLUMNS } from "../status";
import { Empty, Field, Modal, StatusChip } from "../ui";

function relTime(iso: string): string {
  const diff = new Date(iso).getTime() - Date.now();
  if (diff <= 0) return "any moment";
  const m = Math.round(diff / 60000);
  if (m < 60) return `~${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `~${h}h ${m % 60}m`;
  const d = Math.floor(h / 24);
  return `~${d}d ${h % 24}h`;
}

function VideoCard({ v, onOpen, eta, qinfo, paused }: { v: Video; onOpen: (v: Video) => void; eta?: string; qinfo?: QueueReason; paused?: boolean }) {
  const m = useMut();
  const progressing = v.status === "rendering" || v.status === "publishing";
  return (
    <motion.div layout initial={{ opacity: 0, scale: 0.97 }} animate={{ opacity: 1, scale: 1 }}
      className="panel p-3 cursor-pointer card-hover" onClick={() => onOpen(v)}>
      <div className="text-sm text-fog-50 leading-snug line-clamp-3">{v.title || v.subject}</div>

      {progressing && (
        <div className="mt-2.5">
          <div className="h-1 bg-ink-500 rounded-full overflow-hidden">
            <div className={`h-full transition-all ${v.status === "publishing" ? "bg-signal" : "bg-ice"}`}
              style={{ width: `${v.render_progress}%` }} />
          </div>
          <div className="label mt-1 tabular-nums">
            {v.status === "publishing" ? `uploading ${v.render_progress}%` : `${v.render_progress}%`}
          </div>
        </div>
      )}

      {v.status === "approved" && (
        <div className="mt-2 flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-wider"
          style={{ color: paused ? "#f5a524" : STATUS_META.approved.hex }}>
          <span>◷</span>
          {paused ? "publishes when unpaused" : eta ? `publishes in ${relTime(eta)}` : "queued to publish"}
        </div>
      )}

      {v.status === "queued" && qinfo && (
        <div className="mt-2 flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-wider"
          style={{ color: qinfo.reason.includes("paused") ? "#f5a524" : STATUS_META.queued.hex }}>
          <span>◷</span>
          {qinfo.reason}{qinfo.eta ? ` · in ${relTime(qinfo.eta)}` : ""}
        </div>
      )}

      {v.error && <div className="mt-2 text-[11px] font-mono text-[#f7768e] line-clamp-2">{v.error}</div>}

      <div className="flex items-center gap-2 mt-3">
        {v.status === "review" && <span className="label" style={{ color: STATUS_META.review.hex }}>tap to review</span>}
        {v.added_to_playlist && <span className="label text-fog-400">in playlist</span>}
        {v.yt_video_id && (
          <a onClick={(e) => e.stopPropagation()} href={`https://youtube.com/watch?v=${v.yt_video_id}`} target="_blank"
            className="label text-signal hover:underline">watch ↗</a>
        )}
        <div className="ml-auto flex gap-1.5">
          {v.status === "draft" && (
            <button className="label text-signal hover:text-white" onClick={(e) => { e.stopPropagation(); m.produceVideo.mutate(v.id); }}>produce →</button>
          )}
          {(v.status === "failed" || v.status === "rejected") && (
            <button className="label hover:text-signal" onClick={(e) => { e.stopPropagation(); m.retryVideo.mutate(v.id); }}>retry</button>
          )}
        </div>
      </div>
    </motion.div>
  );
}

function Column({ status, videos, onOpen, onProduceAll, plan, queuePlan, paused }: any) {
  const meta = STATUS_META[status as keyof typeof STATUS_META];
  const items = videos.filter((v: Video) => v.status === status);
  return (
    <div className="w-[300px] shrink-0 flex flex-col">
      <div className="flex items-center justify-between px-1 mb-3">
        <div className="flex items-center gap-2">
          <span className="w-2 h-2 rounded-sm" style={{ background: meta.hex }} />
          <span className="font-mono text-[11px] uppercase tracking-[0.15em] text-fog-200">{meta.label}</span>
        </div>
        <div className="flex items-center gap-2">
          {status === "draft" && items.length > 0 && (
            <button className="label text-signal hover:text-white" onClick={onProduceAll}>produce all</button>
          )}
          <span className="font-mono text-xs tabular-nums text-fog-400">{items.length}</span>
        </div>
      </div>
      <div className="flex-1 space-y-2.5 min-h-[60px] rounded-md p-1 overflow-y-auto">
        {items.map((v: Video) => <VideoCard key={v.id} v={v} onOpen={onOpen} eta={plan?.[v.id]} qinfo={queuePlan?.[v.id]} paused={paused} />)}
      </div>
    </div>
  );
}

function VideoModal({ video, channelId, onClose }: { video: Video; channelId: number; onClose: () => void }) {
  const m = useMut();
  const nav = useNavigate();
  const { data: profiles } = useProfiles(channelId);
  const [subject, setSubject] = useState(video.subject);
  const [profileId, setProfileId] = useState(video.render_profile_id ? String(video.render_profile_id) : "");
  const [skipGate, setSkipGate] = useState<string>(video.skip_gate === null ? "" : String(video.skip_gate));
  const editable = ["draft", "queued", "failed", "rejected"].includes(video.status);

  const save = () => m.updateVideo.mutate({
    id: video.id,
    body: { subject, render_profile_id: profileId ? Number(profileId) : null,
            skip_gate: skipGate === "" ? null : skipGate === "true" },
  }, { onSuccess: onClose });

  return (
    <Modal open onClose={onClose} title="Manage video">
      <div className="mb-4"><StatusChip status={video.status} /></div>
      <Field label="subject">
        <textarea className="input h-20" value={subject} onChange={(e) => setSubject(e.target.value)} disabled={!editable} />
      </Field>
      <div className="grid grid-cols-2 gap-4">
        <Field label="render profile">
          <select className="input" value={profileId} onChange={(e) => setProfileId(e.target.value)}>
            <option value="">topic / channel default</option>
            {profiles?.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
        </Field>
        <Field label="approval gate">
          <select className="input" value={skipGate} onChange={(e) => setSkipGate(e.target.value)}>
            <option value="">inherit channel default</option>
            <option value="false">require review</option>
            <option value="true">auto-approve</option>
          </select>
        </Field>
      </div>
      <div className="flex flex-wrap gap-2 mt-4">
        <button className="btn btn-signal" onClick={save} disabled={m.updateVideo.isPending}>save</button>
        {video.status === "draft" && (
          <button className="btn" onClick={() => m.produceVideo.mutate(video.id, { onSuccess: onClose })}>produce →</button>
        )}
        {(video.status === "review" || video.status === "rendered" || video.status === "published") && (
          <button className="btn" onClick={() => nav(`/review/${video.id}`)}>open review →</button>
        )}
        {(video.status === "failed" || video.status === "rejected") && (
          <button className="btn" onClick={() => m.requeueVideo.mutate(video.id, { onSuccess: onClose })}>requeue</button>
        )}
        <button className="btn btn-ghost ml-auto text-[#f7768e] hover:border-[#f7768e]/40"
          onClick={() => m.deleteVideo.mutate(video.id, { onSuccess: onClose })}>delete</button>
      </div>
    </Modal>
  );
}

export default function Board() {
  const { channelId } = useParams();
  const nav = useNavigate();
  const [sp, setSp] = useSearchParams();
  const { data: channels } = useChannels();
  const active = Number(channelId) || channels?.[0]?.id || 0;
  const { data: topics } = useTopics(active);
  const { data: videos } = useVideos(active);
  const { data: plan } = usePublishPlan(active);
  const { data: queuePlan } = useQueuePlan(active);
  const m = useMut();
  const [editing, setEditing] = useState<Video | null>(null);

  const topicFilter = sp.get("topic") ? Number(sp.get("topic")) : null;

  useEffect(() => {
    if (!channelId && channels?.length) nav(`/board/${channels[0].id}`, { replace: true });
  }, [channelId, channels, nav]);

  const channel = channels?.find((c) => c.id === active);
  const shown = (videos || []).filter((v) => !topicFilter || v.topic_id === topicFilter);
  const editingLive = editing ? shown.find((v) => v.id === editing.id) ?? editing : null;

  if (!channels?.length) return <div className="p-8"><Empty>Add a channel first.</Empty></div>;

  const produceAll = () => {
    const ids = shown.filter((v) => v.status === "draft").map((v) => v.id);
    if (ids.length) m.produceBulk.mutate({ channel_id: active, ordered_ids: ids });
  };

  return (
    <div className="p-8 h-full flex flex-col">
      <header className="flex items-end justify-between mb-5">
        <div>
          <div className="label mb-2">// video queue</div>
          <div className="flex items-center gap-3">
            <select className="bg-transparent font-display font-extrabold text-4xl text-fog-50 tracking-tight focus:outline-none cursor-pointer"
              value={active} onChange={(e) => nav(`/board/${e.target.value}`)}>
              {channels.map((c) => <option key={c.id} value={c.id} className="bg-ink-700 text-base font-sans">{c.name}</option>)}
            </select>
            <span className="text-fog-400 font-display text-4xl">▾</span>
          </div>
        </div>
        <div className="flex items-center gap-3">
          <select className="input !w-auto" value={topicFilter ?? ""}
            onChange={(e) => { e.target.value ? setSp({ topic: e.target.value }) : setSp({}); }}>
            <option value="">all topics</option>
            {topics?.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
          </select>
          <Link to="/channels" className="btn">+ topics &amp; ideas</Link>
        </div>
      </header>

      {channel?.paused ? (
        <div className="panel px-4 py-2 mb-4 font-mono text-xs text-amber">
          ⏸ channel paused — nothing renders or publishes. Unpause in <Link to="/channels" className="text-signal underline">Channels</Link>.
        </div>
      ) : (
        <div className="panel px-4 py-2 mb-4 font-mono text-xs text-fog-300">
          ▶ active — produced videos render automatically; {channel?.default_skip_gate ? "auto-approved" : "held in Review for approval"} before publishing.
        </div>
      )}

      {!videos?.length ? (
        <Empty>No videos yet. Go to <Link to="/channels" className="text-signal">Channels → a topic → generate ideas</Link>, then produce them here.</Empty>
      ) : (
        <div className="flex-1 overflow-x-auto overflow-y-hidden">
          <div className="flex gap-4 h-full pb-4">
            {BOARD_COLUMNS.map((s) => <Column key={s} status={s} videos={shown} onOpen={setEditing} onProduceAll={produceAll} plan={plan} queuePlan={queuePlan} paused={channel?.paused} />)}
            {TERMINAL_COLUMNS.map((s) => shown.some((v) => v.status === s) ?
              <Column key={s} status={s} videos={shown} onOpen={setEditing} onProduceAll={produceAll} plan={plan} queuePlan={queuePlan} paused={channel?.paused} /> : null)}
          </div>
        </div>
      )}

      {editingLive && <VideoModal video={editingLive} channelId={active} onClose={() => setEditing(null)} />}
    </div>
  );
}
