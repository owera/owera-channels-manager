import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useMut, useVideo } from "../api";
import { StatusChip } from "../ui";

export default function Review() {
  const { videoId } = useParams();
  const id = Number(videoId);
  const nav = useNavigate();
  const { data: t } = useVideo(id);
  const m = useMut();

  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [tags, setTags] = useState("");
  const [privacy, setPrivacy] = useState("public");
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");

  useEffect(() => {
    if (t) {
      setTitle(t.title || t.subject);
      setDescription(t.description || "");
      setTags(t.tags_json ? JSON.parse(t.tags_json).join(", ") : "");
      setPrivacy(t.privacy || "public");
    }
  }, [t?.id]);

  if (!t) return <div className="p-8 font-mono text-fog-400">loading…</div>;

  const tagList = tags.split(",").map((s) => s.trim()).filter(Boolean);
  const editable = t.status === "review" || t.status === "rendered";
  const saveBody = () => ({ title, description, tags: tagList, privacy });

  return (
    <div className="p-8 max-w-[1200px]">
      <header className="flex items-center gap-4 mb-6">
        <button className="btn btn-ghost" onClick={() => nav(-1)}>← back</button>
        <div className="label">// review</div>
        <StatusChip status={t.status} />
      </header>

      <div className="grid grid-cols-[400px_1fr] gap-8">
        <div>
          <div className="panel overflow-hidden bg-black">
            {t.video_path ? (
              <video src={`/api/videos/${t.id}/video`} controls className="w-full aspect-[9/16] bg-black"
                poster={t.thumb_path ? `/api/videos/${t.id}/thumb` : undefined} />
            ) : (
              <div className="aspect-[9/16] grid place-items-center text-fog-400 font-mono text-xs">no video yet</div>
            )}
          </div>
          <div className="label mt-3">subject</div>
          <div className="text-sm text-fog-200 mt-1">{t.subject}</div>
          {t.yt_video_id && (
            <a href={`https://youtube.com/watch?v=${t.yt_video_id}`} target="_blank" className="btn btn-ghost w-full mt-4 justify-center">open on youtube ↗</a>
          )}
        </div>

        <div>
          <div className="label mb-1.5">title <span className="text-fog-400">({title.length}/100)</span></div>
          <input className="input mb-4" maxLength={100} value={title} onChange={(e) => setTitle(e.target.value)} disabled={!editable} />

          <div className="label mb-1.5">description</div>
          <textarea className="input h-40 mb-4 leading-relaxed" value={description} onChange={(e) => setDescription(e.target.value)} disabled={!editable} />

          <div className="label mb-1.5">tags <span className="text-fog-400">(comma separated)</span></div>
          <input className="input mb-4" value={tags} onChange={(e) => setTags(e.target.value)} disabled={!editable} />

          <div className="w-40 mb-6">
            <div className="label mb-1.5">privacy</div>
            <select className="input" value={privacy} onChange={(e) => setPrivacy(e.target.value)} disabled={!editable}>
              {["public", "unlisted", "private"].map((p) => <option key={p}>{p}</option>)}
            </select>
          </div>

          {editable ? (
            <>
              <div className="flex gap-3">
                <button className="btn btn-signal flex-1 justify-center" disabled={m.approveVideo.isPending}
                  onClick={() => m.approveVideo.mutate({ id: t.id, body: saveBody() }, { onSuccess: () => nav(-1) })}>
                  ✓ approve &amp; publish
                </button>
                <button className="btn" onClick={() => m.updateVideo.mutate({ id: t.id, body: saveBody() })}>save draft</button>
                <button className="btn" onClick={() => m.regenMeta.mutate(t.id)} disabled={m.regenMeta.isPending}>
                  {m.regenMeta.isPending ? "…" : "↻ metadata"}
                </button>
              </div>
              <button className="btn btn-ghost mt-3 text-[#f7768e] hover:border-[#f7768e]/40" onClick={() => setRejecting((v) => !v)}>reject</button>
              {rejecting && (
                <div className="mt-3 flex gap-2">
                  <input className="input" placeholder="reason (optional)" value={reason} onChange={(e) => setReason(e.target.value)} />
                  <button className="btn" onClick={() => m.rejectVideo.mutate({ id: t.id, reason }, { onSuccess: () => nav(-1) })}>confirm</button>
                </div>
              )}
            </>
          ) : (
            <div className="panel px-4 py-3 font-mono text-xs text-fog-300">
              This video is <span style={{ color: "#fff" }}>{t.status}</span> — metadata is read-only. It publishes into its topic's playlist.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
