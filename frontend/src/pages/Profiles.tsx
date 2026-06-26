import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  useChannels, useMut, useParamsOptions, useProfiles, type ParamsOptions, type RenderProfile,
} from "../api";
import { Empty, Field, Modal, SectionLabel } from "../ui";

const SELECT_SOURCE: Record<string, keyof ParamsOptions> = {
  video_aspect: "video_aspect",
  video_concat_mode: "video_concat_mode",
  video_transition_mode: "video_transition_mode",
  video_source: "video_source",
  subtitle_position: "subtitle_position",
  bgm_type: "bgm_type",
};

// Sensible defaults pre-filled into a new profile (mirror the proven channel settings).
const STARTER_DEFAULT: Record<string, any> = {
  video_aspect: "9:16", video_language: "en-US", video_source: "pexels",
  voice_name: "en-US-AndrewNeural-Male", paragraph_number: 2,
  subtitle_enabled: true, subtitle_position: "bottom",
  font_size: 60, text_fore_color: "#FFFFFF", stroke_color: "#000000", stroke_width: 1.5,
  text_background_color: false, bgm_type: "random", bgm_volume: 0.2,
};

const PRESETS: { name: string; hint: string; params: Record<string, any> }[] = [
  { name: "Default Shorts", hint: "clean 9:16, white captions", params: { ...STARTER_DEFAULT } },
  { name: "Bold Yellow", hint: "big centered captions", params: { ...STARTER_DEFAULT, font_size: 84, text_fore_color: "#FFE600", stroke_color: "#111111", stroke_width: 3.5, subtitle_position: "center" } },
  { name: "Clean Minimal", hint: "thin stroke, no box", params: { ...STARTER_DEFAULT, font_size: 56, stroke_width: 1, text_background_color: false } },
  { name: "Boxed Caption", hint: "rounded background bar", params: { ...STARTER_DEFAULT, font_size: 58, text_background_color: true, rounded_subtitle_background: true } },
  { name: "Landscape 16:9", hint: "widescreen", params: { ...STARTER_DEFAULT, video_aspect: "16:9", font_size: 48 } },
];

// Display fallbacks (VideoParams schema defaults) so the preview always looks real.
const DISPLAY_DEFAULTS = {
  video_aspect: "9:16", font_size: 60, text_fore_color: "#FFFFFF",
  stroke_color: "#000000", stroke_width: 1.5, subtitle_position: "bottom",
  custom_position: 70, text_background_color: true, rounded_subtitle_background: false,
  font_name: "",
};

const fontFamily = (name: string) => "f_" + name.replace(/[^a-z0-9]/gi, "_");

// ---- language / voice helpers ----
const localeOf = (voice?: string | null) => (voice ? voice.split("-").slice(0, 2).join("-") : "");

function langLabel(loc: string): string {
  try {
    const [l, r] = loc.split("-");
    const dn = new (Intl as any).DisplayNames(["en"], { type: "language" });
    const rn = r ? new (Intl as any).DisplayNames(["en"], { type: "region" }) : null;
    const ln = dn.of(l) || l;
    return rn ? `${ln} (${rn.of(r) || r})` : ln;
  } catch { return loc; }
}

function deriveLocales(voices: string[]): { code: string; label: string }[] {
  const set = new Set(voices.map(localeOf).filter(Boolean));
  return [...set].sort().map((code) => ({ code, label: langLabel(code) }));
}

function defaultVoiceForLocale(loc: string, voices: string[]): string | null {
  const inLoc = voices.filter((v) => localeOf(v) === loc);
  return inLoc.find((v) => v.endsWith("-Male")) || inLoc[0] || null;
}

// Combined language + filtered-voice picker (replaces the flat 331-voice dropdown).
function LanguageVoice({ params, opts, set }: any) {
  const locales = useMemo(() => deriveLocales(opts.voices), [opts.voices]);
  const cur = params.video_language || localeOf(params.voice_name) || "";
  const voicesForLocale = cur ? opts.voices.filter((v: string) => localeOf(v) === cur) : opts.voices;
  return (
    <div className="grid grid-cols-2 gap-x-4 gap-y-3">
      <div>
        <div className="label mb-1">language</div>
        <select className="input" value={cur} onChange={(e) => {
          const loc = e.target.value;
          set("video_language", loc || null);
          if (loc) set("voice_name", defaultVoiceForLocale(loc, opts.voices));
        }}>
          <option value="">— inherit —</option>
          {locales.map((l) => <option key={l.code} value={l.code}>{l.label} · {l.code}</option>)}
        </select>
      </div>
      <div>
        <div className="label mb-1">voice {cur && <span className="text-fog-400">({voicesForLocale.length})</span>}</div>
        <select className="input" value={params.voice_name ?? ""} onChange={(e) => set("voice_name", e.target.value || null)}>
          <option value="">— inherit —</option>
          {voicesForLocale.map((v: string) => <option key={v} value={v}>{v}</option>)}
        </select>
      </div>
    </div>
  );
}

// Inject @font-face for each available font once, so the preview uses the real glyphs.
function useFontFaces(fonts: string[] = []) {
  useEffect(() => {
    if (!fonts.length) return;
    const id = "profile-fontfaces";
    let el = document.getElementById(id) as HTMLStyleElement | null;
    if (!el) { el = document.createElement("style"); el.id = id; document.head.appendChild(el); }
    el.textContent = fonts.map((f) =>
      `@font-face{font-family:'${fontFamily(f)}';src:url('/api/params/font/${encodeURIComponent(f)}');font-display:swap;}`
    ).join("\n");
  }, [fonts.join("|")]);
}

const RES: Record<string, [number, number]> = { "9:16": [1080, 1920], "16:9": [1920, 1080], "1:1": [1080, 1080] };

function Preview({ params }: { params: Record<string, any> }) {
  const g = (k: string) => (params[k] ?? (DISPLAY_DEFAULTS as any)[k]);
  const aspect = g("video_aspect") || "9:16";
  const [vw, vh] = RES[aspect] || RES["9:16"];
  // Fit within a box.
  const maxW = 270, maxH = 460;
  const ratio = vw / vh;
  let dispH = maxH, dispW = maxH * ratio;
  if (dispW > maxW) { dispW = maxW; dispH = maxW / ratio; }
  const scale = dispW / vw;

  const enabled = params.subtitle_enabled ?? true;
  const pos = g("subtitle_position");
  const cp = g("custom_position");
  const bg = g("text_background_color");
  const rounded = g("rounded_subtitle_background");
  const fname = params.font_name;

  const posStyle: React.CSSProperties =
    pos === "top" ? { top: "7%" } :
    pos === "center" ? { top: "50%", transform: "translateY(-50%)" } :
    pos === "custom" ? { top: `${cp}%`, transform: "translateY(-50%)" } :
    { bottom: "8%" };

  const bgColor = bg === true ? "rgba(0,0,0,0.55)" : typeof bg === "string" && bg ? bg : "transparent";

  return (
    <div className="shrink-0">
      <div className="relative overflow-hidden rounded-md border border-ink-line mx-auto"
        style={{
          width: dispW, height: dispH,
          background: "radial-gradient(120% 80% at 50% 0%, #2a3340, #0c0f13 70%), linear-gradient(160deg,#1a2230,#0a0d11)",
        }}>
        {/* faux footage shimmer */}
        <div className="absolute inset-0 opacity-[0.12]"
          style={{ backgroundImage: "repeating-linear-gradient(135deg,#fff 0 1px,transparent 1px 9px)" }} />
        {enabled && (
          <div className="absolute left-2 right-2 text-center" style={posStyle}>
            <span style={{
              display: "inline-block",
              fontFamily: fname ? `'${fontFamily(fname)}', system-ui, sans-serif` : "system-ui, sans-serif",
              fontWeight: 700,
              fontSize: g("font_size") * scale,
              lineHeight: 1.18,
              color: g("text_fore_color"),
              WebkitTextStroke: `${g("stroke_width") * scale}px ${g("stroke_color")}`,
              paintOrder: "stroke fill",
              background: bgColor,
              borderRadius: rounded ? 6 : 0,
              padding: bgColor !== "transparent" ? `${2 * scale * 4}px ${4 * scale * 4}px` : 0,
            } as React.CSSProperties}>
              What is RAG, and why does it matter?
            </span>
          </div>
        )}
        <div className="absolute top-2 left-2 font-mono text-[9px] uppercase tracking-wider text-fog-400/70">{aspect}</div>
      </div>
      <div className="text-[10px] text-fog-400 font-mono text-center mt-2">live preview · approximate</div>
    </div>
  );
}

function Control({ field, kind, value, opts, onChange }: any) {
  const def = opts.defaults[field];
  const ph = def !== undefined && def !== null ? `default: ${def}` : "inherit";
  if (kind === "select") {
    const list = (opts[SELECT_SOURCE[field]] || []) as any[];
    return (
      <select className="input" value={value ?? ""} onChange={(e) => onChange(e.target.value || null)}>
        <option value="">— inherit —</option>
        {list.map((o) => <option key={String(o)} value={o === null ? "" : o}>{o === null ? "none" : o}</option>)}
      </select>
    );
  }
  if (kind === "voice" || kind === "font" || kind === "bgm") {
    const list = kind === "voice" ? opts.voices : kind === "font" ? opts.fonts : opts.bgm_files;
    return (
      <select className="input" value={value ?? ""} onChange={(e) => onChange(e.target.value || null)}>
        <option value="">— inherit —</option>
        {list.map((v: string) => <option key={v} value={v}>{v}</option>)}
      </select>
    );
  }
  if (kind === "bool") return (
    <select className="input" value={value === undefined || value === null ? "" : String(value)}
      onChange={(e) => onChange(e.target.value === "" ? null : e.target.value === "true")}>
      <option value="">— inherit —</option><option value="true">on</option><option value="false">off</option>
    </select>
  );
  if (kind === "color") return (
    <div className="flex gap-2 items-center">
      <input type="color" className="h-9 w-10 bg-ink-900 border border-ink-line rounded" value={value || def || "#ffffff"} onChange={(e) => onChange(e.target.value)} />
      <input className="input" value={value ?? ""} placeholder={ph} onChange={(e) => onChange(e.target.value || null)} />
    </div>
  );
  if (kind === "textarea") return (
    <textarea className="input h-20" value={value ?? ""} placeholder={ph} onChange={(e) => onChange(e.target.value || null)} />
  );
  const numeric = kind === "int" || kind === "float";
  return (
    <input className="input" type={numeric ? "number" : "text"} step={kind === "float" ? "0.1" : "1"}
      value={value ?? ""} placeholder={ph}
      onChange={(e) => { const v = e.target.value; onChange(v === "" ? null : numeric ? Number(v) : v); }} />
  );
}

const GROUPS: { title: string; fields: string[] }[] = [
  { title: "format", fields: ["video_aspect", "video_source", "video_concat_mode", "video_transition_mode", "video_clip_duration"] },
  { title: "subtitles", fields: ["subtitle_enabled", "subtitle_position", "custom_position", "font_name", "font_size", "text_fore_color", "stroke_color", "stroke_width"] },
  // language + voice are rendered by <LanguageVoice/>, not the generic grid.
  { title: "audio", fields: ["voice_rate", "voice_volume", "bgm_type", "bgm_file", "bgm_volume"] },
  { title: "script", fields: ["paragraph_number", "video_script_prompt", "custom_system_prompt"] },
];

// HyperFrames ignores MPT-only controls (stock source, burned-in captions, fonts). Only
// aspect, music, and script length carry over; voice is rendered by <LanguageVoice/>.
const HF_GROUPS: { title: string; fields: string[] }[] = [
  { title: "format", fields: ["video_aspect"] },
  { title: "audio", fields: ["bgm_type", "bgm_file", "bgm_volume"] },
  { title: "script", fields: ["paragraph_number"] },
];

function ProfileEditor({ profile, onClose }: { profile: Partial<RenderProfile>; onClose: () => void }) {
  const { data: opts } = useParamsOptions();
  const { data: channels } = useChannels();
  const m = useMut();
  useFontFaces(opts?.fonts);
  const [name, setName] = useState(profile.name || "");
  const [engine, setEngine] = useState<string>(profile.engine || "mpt");
  const [channelId, setChannelId] = useState<string>(profile.channel_id ? String(profile.channel_id) : "");
  const [params, setParams] = useState<Record<string, any>>(
    profile.id ? JSON.parse(profile.params_json || "{}") : { ...STARTER_DEFAULT }
  );

  if (!opts) return null;
  const isHF = engine === "hyperframes";
  const set = (k: string, v: any) =>
    setParams((p) => { const n = { ...p }; if (v === null || v === "") delete n[k]; else n[k] = v; return n; });

  const save = () => {
    const body = { name, engine, channel_id: channelId ? Number(channelId) : null, params };
    if (profile.id) m.updateProfile.mutate({ id: profile.id, body: { name, engine, params } }, { onSuccess: onClose });
    else m.createProfile.mutate(body, { onSuccess: onClose });
  };

  return (
    <Modal open onClose={onClose} title={profile.id ? "Edit render profile" : "New render profile"} wide>
      <div className="grid grid-cols-3 gap-4">
        <Field label="name"><input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="9:16 Shorts — Andrew" /></Field>
        <Field label="engine">
          <select className="input" value={engine} onChange={(e) => setEngine(e.target.value)}>
            <option value="mpt">MoneyPrinterTurbo (stock + captions)</option>
            <option value="hyperframes">HyperFrames (LLM motion graphics)</option>
          </select>
        </Field>
        <Field label="scope">
          <select className="input" value={channelId} onChange={(e) => setChannelId(e.target.value)} disabled={!!profile.id}>
            <option value="">shared (all channels)</option>
            {channels?.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
          </select>
        </Field>
      </div>

      {!profile.id && !isHF && (
        <div className="mb-4">
          <div className="label mb-2">start from a preset</div>
          <div className="flex flex-wrap gap-2">
            {PRESETS.map((p) => (
              <button key={p.name} className="btn btn-ghost !py-1.5 flex-col !items-start" title={p.hint}
                onClick={() => setParams({ ...p.params })}>
                <span className="text-fog-50 normal-case">{p.name}</span>
              </button>
            ))}
          </div>
        </div>
      )}

      <div className={isHF ? "" : "grid grid-cols-[270px_1fr] gap-6"}>
        {!isHF && <Preview params={params} />}
        <div className="max-h-[52vh] overflow-y-auto pr-1">
          {isHF && (
            <div className="mb-4 rounded border border-fog-700 bg-fog-900/40 p-3 text-sm text-fog-300">
              HyperFrames renders animated HTML motion graphics. The visuals are generated by
              an LLM from the video subject — there's no stock footage or burned-in captions.
              The voice below drives the muxed narration; aspect &amp; music still apply.
            </div>
          )}
          <div className="mb-4">
            <SectionLabel>// language &amp; voice</SectionLabel>
            <LanguageVoice params={params} opts={opts} set={set} />
          </div>
          {(isHF ? HF_GROUPS : GROUPS).map((grp) => {
            const fields = grp.fields.filter((f) => opts.fields[f]);
            if (!fields.length) return null;
            return (
              <div key={grp.title} className="mb-4">
                <SectionLabel>// {grp.title}</SectionLabel>
                <div className="grid grid-cols-2 gap-x-4 gap-y-3">
                  {fields.map((field) => (
                    <div key={field} className={["video_script_prompt", "custom_system_prompt"].includes(field) ? "col-span-2" : ""}>
                      <div className="label mb-1">{field.replace(/_/g, " ")}</div>
                      <Control field={field} kind={opts.fields[field]} value={params[field]} opts={opts} onChange={(v: any) => set(field, v)} />
                    </div>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      <button className="btn btn-signal w-full mt-4" disabled={!name} onClick={save}>
        {profile.id ? "save profile" : "create profile"}
      </button>
    </Modal>
  );
}

// Clone a base profile across many languages at once.
function LanguagePackModal({ onClose }: { onClose: () => void }) {
  const { data: opts } = useParamsOptions();
  const { data: profiles } = useProfiles();
  const m = useMut();
  const locales = useMemo(() => (opts ? deriveLocales(opts.voices) : []), [opts?.voices]);
  const [baseId, setBaseId] = useState("");
  const [filter, setFilter] = useState("");
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);

  if (!opts) return null;
  const baseProfile = profiles?.find((p) => String(p.id) === baseId)
    || profiles?.find((p) => p.name === "Default" && p.channel_id === null);
  const baseParams = baseProfile ? JSON.parse(baseProfile.params_json || "{}") : { ...STARTER_DEFAULT };
  const baseName = baseProfile?.name || "Default";

  const shown = locales.filter((l) =>
    !filter || l.label.toLowerCase().includes(filter.toLowerCase()) || l.code.toLowerCase().includes(filter.toLowerCase()));
  const toggle = (code: string) =>
    setSel((s) => { const n = new Set(s); n.has(code) ? n.delete(code) : n.add(code); return n; });

  const create = async () => {
    setBusy(true);
    for (const loc of sel) {
      await m.createProfile.mutateAsync({
        name: `${baseName} · ${langLabel(loc)}`,
        channel_id: baseProfile?.channel_id ?? null,
        params: { ...baseParams, video_language: loc, voice_name: defaultVoiceForLocale(loc, opts.voices) },
      });
    }
    setBusy(false);
    onClose();
  };

  return (
    <Modal open onClose={onClose} title="Create a language pack" wide>
      <p className="text-[13px] text-fog-300 mb-4">
        Pick a base profile and the languages you want — one profile is created per language, each with
        that language set and a matching voice auto-selected.
      </p>
      <div className="grid grid-cols-2 gap-4 mb-4">
        <Field label="base profile">
          <select className="input" value={baseId} onChange={(e) => setBaseId(e.target.value)}>
            {profiles?.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
        </Field>
        <Field label={`languages selected: ${sel.size}`}>
          <input className="input" placeholder="filter languages…" value={filter} onChange={(e) => setFilter(e.target.value)} />
        </Field>
      </div>
      <div className="max-h-[40vh] overflow-y-auto grid grid-cols-2 gap-1 border border-ink-line rounded p-2 mb-4">
        {shown.map((l) => (
          <label key={l.code} className="flex items-center gap-2 px-2 py-1.5 rounded hover:bg-ink-800 cursor-pointer text-sm">
            <input type="checkbox" checked={sel.has(l.code)} onChange={() => toggle(l.code)} />
            <span className="text-fog-100">{l.label}</span>
            <span className="font-mono text-[10px] text-fog-400 ml-auto">{l.code}</span>
          </label>
        ))}
      </div>
      <button className="btn btn-signal w-full" disabled={!sel.size || busy} onClick={create}>
        {busy ? "creating…" : `create ${sel.size} profile${sel.size === 1 ? "" : "s"}`}
      </button>
    </Modal>
  );
}

export default function Profiles() {
  const { data: profiles } = useProfiles();
  const { data: channels } = useChannels();
  const m = useMut();
  const [editing, setEditing] = useState<Partial<RenderProfile> | null>(null);
  const [pack, setPack] = useState(false);
  const [sp, setSp] = useSearchParams();
  useEffect(() => { if (sp.has("new")) { setEditing({}); setSp({}, { replace: true }); } }, []);
  const channelName = (id: number | null) => id ? channels?.find((c) => c.id === id)?.name : "shared";

  return (
    <div className="p-4 md:p-8 max-w-[1100px]">
      <header className="flex flex-wrap items-end justify-between gap-4 mb-6 md:mb-8">
        <div>
          <div className="label mb-2">// render profiles</div>
          <h1 className="font-display font-extrabold text-2xl sm:text-4xl text-fog-50 tracking-tight">Render Profiles</h1>
          <p className="text-fog-300 text-sm mt-2 max-w-xl">Reusable presets over the full generation surface — voice, aspect, subtitle styling, music, transitions, script length. Assign per channel or per topic.</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button className="btn" onClick={() => setPack(true)}>+ language pack</button>
          <button className="btn btn-signal" onClick={() => setEditing({})}>+ new profile</button>
        </div>
      </header>

      {!profiles?.length ? (
        <Empty>No profiles yet — the engine defaults apply. Create one to customize the look.</Empty>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          {profiles.map((p) => {
            const params = JSON.parse(p.params_json || "{}");
            return (
              <div key={p.id} className="panel p-5 card-hover">
                <div className="flex items-start justify-between">
                  <div>
                    <div className="font-display font-bold text-fog-50 text-lg">{p.name}</div>
                    <div className="label mt-1">{channelName(p.channel_id)} · {p.engine === "hyperframes" ? "HyperFrames" : "MPT"} · {Object.keys(params).length} settings</div>
                  </div>
                  <div className="flex gap-2">
                    <button className="label hover:text-signal" onClick={() => setEditing(p)}>edit</button>
                    <button className="label hover:text-[#f7768e]" onClick={() => m.deleteProfile.mutate(p.id)}>del</button>
                  </div>
                </div>
                <div className="flex flex-wrap gap-1.5 mt-4">
                  {Object.entries(params).slice(0, 8).map(([k, v]) => (
                    <span key={k} className="font-mono text-[10px] px-2 py-0.5 rounded-sm bg-ink-800 border border-ink-line text-fog-300">
                      {k.replace(/_/g, " ")}: <span className="text-fog-100">{String(v)}</span>
                    </span>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {editing && <ProfileEditor profile={editing} onClose={() => setEditing(null)} />}
      {pack && <LanguagePackModal onClose={() => setPack(false)} />}
    </div>
  );
}
