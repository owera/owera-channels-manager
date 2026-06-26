import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  useChannels, useMut, useProfiles, useTopics, type Channel, type Topic,
} from "../api";
import { Dot, Empty, Field, Modal, SectionLabel, Toggle } from "../ui";
import ChannelYoutube from "./ChannelYoutube";

const OAUTH_HEX: Record<string, string> = {
  connected: "#c9f24e", expired: "#f5a524", error: "#f7768e", disconnected: "#6c7681",
};

// ---------- Topics (content themes) ----------
function TopicCard({ topic, channel }: { topic: Topic; channel: Channel }) {
  const m = useMut();
  const [count, setCount] = useState(8);
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(topic.name);
  const [prompt, setPrompt] = useState(topic.theme_prompt ?? "");
  const [format, setFormat] = useState<"short" | "long">(topic.content_format);
  const openEdit = () => {
    setName(topic.name);
    setPrompt(topic.theme_prompt ?? "");
    setFormat(topic.content_format);
    setEditing(true);
  };
  const c: Record<string, number> = topic.video_counts || {};
  const drafts = c.draft || 0;
  const live = c.published || 0;
  return (
    <div className="panel p-4">
      <div className="flex items-start justify-between">
        <div>
          <div className="flex items-center gap-2">
            <span className="font-display font-bold text-fog-50 text-base">{topic.name}</span>
            <span className="font-mono text-[9px] uppercase tracking-wider px-1.5 py-0.5 rounded border"
              style={topic.content_format === "long"
                ? { color: "#a78bfa", borderColor: "#a78bfa55" }
                : { color: "#6c7681", borderColor: "#6c768155" }}>
              {topic.content_format === "long" ? "long-form" : "shorts"}
            </span>
          </div>
          {topic.theme_prompt && <div className="text-[12px] text-fog-300 mt-0.5 line-clamp-2">{topic.theme_prompt}</div>}
        </div>
        <div className="flex items-center gap-2.5">
          <button className="label hover:text-signal" onClick={openEdit}>edit</button>
          <button className="label hover:text-[#f7768e]" onClick={() => m.deleteTopic.mutate(topic.id)}>del</button>
        </div>
      </div>

      <div className="flex items-center gap-3 mt-3 font-mono text-[10px] uppercase tracking-wider">
        {topic.playlist_title ? (
          <a href={`https://youtube.com/playlist?list=${topic.playlist_yt_id}`} target="_blank" className="text-signal hover:underline">▸ {topic.playlist_title}</a>
        ) : (
          <span className="text-fog-400">playlist · auto-created on first produce</span>
        )}
        <span className="text-fog-400">{topic.video_total} videos</span>
        {drafts > 0 && <span className="text-fog-300">{drafts} ideas</span>}
        {live > 0 && <span className="text-signal">{live} live</span>}
      </div>

      <div className="flex items-center gap-2 mt-4">
        <input type="number" className="input !w-16 !py-1.5 text-center" value={count} min={1} max={20}
          onChange={(e) => setCount(Number(e.target.value))} />
        <button className="btn btn-signal !py-1.5 flex-1 justify-center" disabled={m.generateVideos.isPending}
          onClick={() => m.generateVideos.mutate({ id: topic.id, count })}>
          {m.generateVideos.isPending ? "generating…" : "generate ideas"}
        </button>
        <Link to={`/board/${channel.id}?topic=${topic.id}`} className="btn !py-1.5">view queue →</Link>
      </div>

      <Modal open={editing} onClose={() => setEditing(false)} title="Edit topic">
        <Field label="topic name">
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label="theme guidance (optional)" hint="steers the kinds of video ideas generated">
          <textarea className="input h-20" value={prompt} onChange={(e) => setPrompt(e.target.value)} />
        </Field>
        <Field label="format" hint="applies to new ideas and not-yet-rendered videos; already-rendered videos keep their aspect">
          <div className="flex gap-2">
            {(["short", "long"] as const).map((f) => (
              <button key={f} type="button"
                className={`btn flex-1 justify-center ${format === f ? "btn-signal" : "btn-ghost"}`}
                onClick={() => setFormat(f)}>
                {f === "short" ? "Shorts" : "Long-form"}
              </button>
            ))}
          </div>
        </Field>
        <button className="btn btn-signal w-full" disabled={!name || m.updateTopic.isPending}
          onClick={() => m.updateTopic.mutate(
            { id: topic.id, body: { name, theme_prompt: prompt || null, content_format: format } },
            { onSuccess: () => setEditing(false) })}>
          {m.updateTopic.isPending ? "saving…" : "save changes"}
        </button>
      </Modal>
    </div>
  );
}

function ContentTopics({ channel }: { channel: Channel }) {
  const { data: topics } = useTopics(channel.id);
  const m = useMut();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [format, setFormat] = useState<"short" | "long">("short");
  return (
    <div className="mt-6">
      <div className="flex items-center justify-between mb-3">
        <SectionLabel>// content topics</SectionLabel>
        <button className="btn btn-signal !py-1.5" onClick={() => setOpen(true)}>+ add topic</button>
      </div>
      <p className="text-[12px] text-fog-400 mb-4 max-w-2xl">
        Topics are content themes. Each owns a YouTube playlist; the manager generates video ideas
        from a topic's theme, and every video produced under it publishes into that playlist.
      </p>

      {!topics?.length ? (
        <Empty>No topics yet. Add one (e.g. “RAG”, “AI Agents”) to start generating videos.</Empty>
      ) : (
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-3">
          {topics.map((t) => <TopicCard key={t.id} topic={t} channel={channel} />)}
        </div>
      )}

      <Modal open={open} onClose={() => setOpen(false)} title="Add content topic">
        <Field label="topic name" hint="becomes the playlist name too">
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="RAG" />
        </Field>
        <Field label="theme guidance (optional)" hint="steers the kinds of video ideas generated">
          <textarea className="input h-20" value={prompt} onChange={(e) => setPrompt(e.target.value)}
            placeholder="Retrieval-augmented generation: chunking, embeddings, rerankers, eval, pitfalls." />
        </Field>
        <Field label="format" hint="shorts are vertical 9:16; long-form is 16:9 with a longer, structured script">
          <div className="flex gap-2">
            {(["short", "long"] as const).map((f) => (
              <button key={f} type="button"
                className={`btn flex-1 justify-center ${format === f ? "btn-signal" : "btn-ghost"}`}
                onClick={() => setFormat(f)}>
                {f === "short" ? "Shorts" : "Long-form"}
              </button>
            ))}
          </div>
        </Field>
        <div className="text-[12px] text-fog-400 mb-3 font-mono">a playlist is created automatically when the topic's first video is produced</div>
        <button className="btn btn-signal w-full" disabled={!name || m.createTopic.isPending}
          onClick={() => m.createTopic.mutate(
            { channel_id: channel.id, name, theme_prompt: prompt || null, content_format: format },
            { onSuccess: () => { setOpen(false); setName(""); setPrompt(""); setFormat("short"); } })}>
          {m.createTopic.isPending ? "creating…" : "create topic"}
        </button>
      </Modal>
    </div>
  );
}

// ---------- Channel detail ----------
function ChannelDetail({ channel }: { channel: Channel }) {
  const m = useMut();
  const { data: profiles } = useProfiles(channel.id);
  const fileRef = useRef<HTMLInputElement>(null);
  const hexes = OAUTH_HEX[channel.oauth_status] || "#6c7681";
  const [connecting, setConnecting] = useState(false);

  useEffect(() => {
    if (!connecting) return;
    if (channel.oauth_status === "connected") { setConnecting(false); return; }
    const t = setInterval(() => m.oauthStatus.mutate(channel.id), 2500);
    const stop = setTimeout(() => setConnecting(false), 120000);
    return () => { clearInterval(t); clearTimeout(stop); };
  }, [connecting, channel.oauth_status, channel.id]);

  return (
    <div className="panel p-6">
      <div className="flex items-start justify-between">
        <div>
          <h2 className="font-display font-extrabold text-2xl text-fog-50">{channel.yt_channel_title || channel.name}</h2>
          <div className="label mt-1">
            {channel.yt_channel_id
              ? <span className="font-mono normal-case text-fog-400">{channel.yt_channel_id}</span>
              : "not connected"}
          </div>
        </div>
        <div className="flex items-center gap-2 font-mono text-xs uppercase tracking-wider" style={{ color: hexes }}>
          <Dot hex={hexes} pulse={channel.oauth_status === "connected"} />
          {channel.oauth_status}
        </div>
      </div>

      {channel.oauth_error && <div className="mt-2 text-xs font-mono text-amber">{channel.oauth_error}</div>}

      <div className="mt-5 flex flex-wrap gap-2">
        <input ref={fileRef} type="file" accept="application/json" className="hidden"
          onChange={(e) => e.target.files?.[0] && m.uploadSecret.mutate({ id: channel.id, file: e.target.files[0] })} />
        <button className="btn btn-ghost" onClick={() => fileRef.current?.click()}>
          {m.uploadSecret.isPending ? "uploading…" : "upload client_secret.json"}
        </button>
        <button className="btn btn-signal" disabled={m.oauthStart.isPending || connecting}
          onClick={async () => { try { const { auth_url } = await m.oauthStart.mutateAsync(channel.id); window.open(auth_url, "_blank", "noopener"); setConnecting(true); } catch {} }}>
          {connecting ? "waiting for consent…" : m.oauthStart.isPending ? "starting…" : channel.oauth_status === "connected" ? "reconnect" : "connect oauth"}
        </button>
        {channel.oauth_status === "connected" && (
          <button className="btn btn-ghost" onClick={() => m.disconnectChannel.mutate(channel.id)}>disconnect</button>
        )}
      </div>
      {connecting && <div className="mt-2 text-xs font-mono text-fog-300">A Google consent tab opened — approve there, then this updates automatically.</div>}
      {m.oauthStart.isError && <div className="mt-2 text-xs font-mono text-[#f7768e]">{(m.oauthStart.error as Error).message}</div>}

      <div className="mt-6 grid grid-cols-2 gap-x-6 gap-y-4">
        <div>
          <div className="label mb-1.5">default privacy</div>
          <select className="input" value={channel.default_privacy}
            onChange={(e) => m.updateChannel.mutate({ id: channel.id, body: { default_privacy: e.target.value } })}>
            {["public", "unlisted", "private"].map((p) => <option key={p}>{p}</option>)}
          </select>
        </div>
        <div>
          <div className="label mb-1.5">default render profile</div>
          <select className="input" value={channel.default_render_profile_id ?? ""}
            onChange={(e) => m.updateChannel.mutate({ id: channel.id, body: { default_render_profile_id: e.target.value ? Number(e.target.value) : null } })}>
            <option value="">— none —</option>
            {profiles?.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
        </div>
        <div>
          <div className="label mb-1.5">daily render budget</div>
          <input type="number" className="input" defaultValue={channel.daily_render_budget}
            onBlur={(e) => m.updateChannel.mutate({ id: channel.id, body: { daily_render_budget: Number(e.target.value) } })} />
        </div>
        <div>
          <div className="label mb-1.5">daily publish budget</div>
          <input type="number" className="input" defaultValue={channel.daily_publish_budget}
            onBlur={(e) => m.updateChannel.mutate({ id: channel.id, body: { daily_publish_budget: Number(e.target.value) } })} />
        </div>
      </div>

      <div className="mt-5 flex items-center gap-6">
        <div className="flex items-center gap-3">
          <Toggle on={channel.default_skip_gate} onChange={(v) => m.updateChannel.mutate({ id: channel.id, body: { default_skip_gate: v } })} />
          <div><div className="text-sm text-fog-100">Skip approval gate</div><div className="label">auto-publish after render</div></div>
        </div>
        <div className="flex items-center gap-3">
          <Toggle on={channel.paused} onChange={(v) => m.updateChannel.mutate({ id: channel.id, body: { paused: v } })} />
          <div><div className="text-sm text-fog-100">Pause channel</div><div className="label">halt render + publish</div></div>
        </div>
      </div>

      <ContentTopics channel={channel} />

      <ChannelYoutube channel={channel} />
    </div>
  );
}

export default function Channels() {
  const { data: channels } = useChannels();
  const m = useMut();
  const [sel, setSel] = useState<number | null>(null);
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const selected = channels?.find((c) => c.id === sel) || channels?.[0];

  return (
    <div className="p-4 md:p-8 max-w-[1400px]">
      <header className="flex items-end justify-between mb-6 md:mb-8">
        <div><div className="label mb-2">// channels</div>
          <h1 className="font-display font-extrabold text-2xl sm:text-4xl text-fog-50 tracking-tight">Channels</h1></div>
        <button className="btn btn-signal" onClick={() => setOpen(true)}>+ add channel</button>
      </header>

      {!channels?.length ? (
        <Empty>No channels yet. Click <span className="text-signal">+ add channel</span> to begin.</Empty>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-[260px_1fr] gap-4 md:gap-6">
          <div className="space-y-1.5">
            {channels.map((c) => (
              <button key={c.id} onClick={() => setSel(c.id)}
                className={`w-full text-left px-4 py-3 rounded border transition-colors ${
                  selected?.id === c.id ? "border-signal/50 bg-signal/5" : "border-ink-line bg-ink-800/40 hover:border-fog-300/40"}`}>
                <div className="flex items-center gap-2"><Dot hex={OAUTH_HEX[c.oauth_status]} />
                  <span className="text-fog-50 font-medium">{c.name}</span></div>
                <div className="label mt-1 pl-[14px]">{c.oauth_status}</div>
              </button>
            ))}
          </div>
          {selected && <ChannelDetail key={selected.id} channel={selected} />}
        </div>
      )}

      <Modal open={open} onClose={() => setOpen(false)} title="Add channel">
        <Field label="channel name" hint="Use a separate Google Cloud project + client_secret.json per channel (independent quota).">
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="AI Engineering Shorts" />
        </Field>
        <button className="btn btn-signal w-full" disabled={!name || m.createChannel.isPending}
          onClick={() => m.createChannel.mutate({ name, slug: name }, { onSuccess: () => { setOpen(false); setName(""); } })}>
          create channel
        </button>
      </Modal>
    </div>
  );
}
