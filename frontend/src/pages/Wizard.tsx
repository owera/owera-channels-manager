/** S2 guided onboarding: one wizard from first login to the first draft.
 *
 * create channel → Connect YouTube (#215 consent note + L6 line) → niche + 3 topics
 * → default budgets → Terms / Privacy + AI-data acknowledgement → ideas → first
 * draft queued at top render priority. Every step posts to /api/onboarding/wizard/*
 * and the server derives where the user is, so a reload resumes the same step.
 *
 * Copy: the wizard's English UI text and the topic presets are neutral
 * placeholders — copy pending CMO review. The only bilingual text is the L6 line
 * (verbatim EN/PT from l6ConnectAiLine.ts, LegalOps sha 845c22005853).
 */
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useFeatures, useMut, useWizard, useWizardMut, WizardPreset, WizardState } from "../api";
import {
  L6_CONNECT_AI_LINE_SHA, l6ConnectAiLineId, l6ConnectAiLineProps,
} from "../l6ConnectAiLine";
import { ConnectConsentNote, Dot, LlmBlockedBanner, POLICY_LINKS } from "../ui";

const STEPS: { id: string; label: string; covers: string[] }[] = [
  { id: "channel", label: "Channel", covers: ["channel"] },
  { id: "youtube", label: "Connect YouTube", covers: ["youtube"] },
  { id: "topics", label: "Niche & topics", covers: ["topics"] },
  { id: "budgets", label: "Daily budgets", covers: ["budgets"] },
  { id: "consents", label: "Terms & AI data", covers: ["consents"] },
  { id: "first_draft", label: "First draft", covers: ["ideas", "first_draft"] },
];

function errText(e: unknown): string {
  const raw = e instanceof Error ? e.message : String(e ?? "");
  try {
    const d = JSON.parse(raw);
    if (d && typeof d === "object" && typeof d.message === "string") return d.message;
  } catch { /* plain text */ }
  return raw;
}

function navLocale() {
  return typeof navigator !== "undefined" ? navigator : undefined;
}

function Stepper({ state }: { state: WizardState }) {
  const done = new Map(state.steps.map((s) => [s.id, s.done]));
  const cur = state.current_step;
  return (
    <ol className="flex flex-wrap gap-x-5 gap-y-2 mb-8" aria-label="Setup steps">
      {STEPS.map((s, i) => {
        const isDone = s.covers.every((c) => done.get(c as any));
        const isCur = !!cur && s.covers.includes(cur);
        return (
          <li key={s.id} data-step={s.id} aria-current={isCur ? "step" : undefined}
            className={`flex items-center gap-2 text-xs font-mono uppercase tracking-wider ${
              isCur ? "text-signal" : isDone ? "text-fog-200" : "text-fog-500"}`}>
            <Dot hex={isDone ? "#c9f24e" : isCur ? "#c9f24e" : "#6c7681"} pulse={isCur} />
            <span className="tabular-nums">{String(i + 1).padStart(2, "0")}</span> {s.label}
          </li>
        );
      })}
    </ol>
  );
}

function Panel({ title, children, step }: { title: string; children: React.ReactNode; step: string }) {
  return (
    <section className="panel p-6 md:p-8" data-wizard-step={step}>
      <h2 className="font-display font-extrabold text-2xl text-fog-50 tracking-tight mb-2">{title}</h2>
      {children}
    </section>
  );
}

function ErrorRow({ error }: { error: unknown }) {
  if (!error) return null;
  return <div role="alert" className="mt-3 text-xs font-mono text-[#f7768e]">{errText(error)}</div>;
}

function ChannelStep() {
  const m = useWizardMut();
  const [name, setName] = useState("");
  return (
    <Panel title="Name your channel" step="channel">
      <p className="text-sm text-fog-300 mb-5 max-w-xl leading-relaxed">
        This is the channel Channels Manager writes drafts for. You can rename it later.
      </p>
      <form onSubmit={(e) => { e.preventDefault(); m.channel.mutate({ name }); }} className="max-w-md">
        <label className="block mb-4">
          <div className="label mb-1.5">Channel name</div>
          <input className="input w-full" value={name} maxLength={80} autoFocus
            onChange={(e) => setName(e.target.value)} placeholder="My channel" />
        </label>
        <button className="btn btn-signal" disabled={!name.trim() || m.channel.isPending}>
          {m.channel.isPending ? "creating…" : "Create channel"}
        </button>
        <ErrorRow error={m.channel.error} />
      </form>
    </Panel>
  );
}

function YoutubeStep({ state }: { state: WizardState }) {
  const ch = state.channel!;
  const { data: features } = useFeatures();
  const platformOauth = features?.google_web_oauth === true;
  const m = useMut();
  const fileRef = useRef<HTMLInputElement>(null);
  const [connecting, setConnecting] = useState(false);
  useWizard(true, connecting);   // poll while the Google tab is open
  const start = platformOauth ? m.platformOauthStart : m.oauthStart;
  // L6 AI-consent line (LegalOps, sha 845c22005853) before the button that opens Google OAuth.
  const l6 = l6ConnectAiLineProps(navLocale());
  const l6Id = l6ConnectAiLineId(ch.id);
  return (
    <Panel title="Connect YouTube" step="youtube">
      <p className="text-sm text-fog-300 max-w-xl leading-relaxed">
        Sign in with Google so Channels Manager can upload videos you approve, create playlists, and read
        basic channel stats.
        {platformOauth ? "" : " Upload your Google Cloud client secret first, then connect."}
      </p>
      <p id={l6Id} lang={l6.lang} data-l6-consent-line=""
        className="text-sm text-fog-200 mt-3 max-w-xl leading-relaxed">
        {l6.text}
      </p>
      {ch.oauth_status !== "disconnected" && ch.oauth_status !== "connected" && (
        <div className="mt-3 text-sm text-amber">Google sign-in did not finish. Try again.</div>
      )}
      <div className="mt-5 flex flex-wrap gap-2">
        {!platformOauth && (
          <>
            <input ref={fileRef} type="file" accept="application/json" className="hidden"
              onChange={(e) => e.target.files?.[0] && m.uploadSecret.mutate({ id: ch.id, file: e.target.files[0] })} />
            <button className="btn btn-ghost" onClick={() => fileRef.current?.click()}>
              {m.uploadSecret.isPending ? "uploading…" : "upload client_secret.json"}
            </button>
          </>
        )}
        <button className="btn btn-signal" aria-describedby={l6Id} data-google-oauth=""
          disabled={start.isPending || connecting}
          onClick={async () => {
            try {
              const { auth_url } = await start.mutateAsync(ch.id);
              window.open(auth_url, "_blank", "noopener");
              setConnecting(true);
            } catch { /* error row below */ }
          }}>
          {connecting ? "waiting for consent…" : start.isPending ? "starting…" : "Connect YouTube"}
        </button>
      </div>
      <ConnectConsentNote className="mt-3 max-w-xl" />
      {connecting && (
        <p className="mt-3 text-sm text-fog-300">
          A Google sign-in tab opened. Approve access there; this page moves on when you&apos;re done.
        </p>
      )}
      <ErrorRow error={start.error} />
    </Panel>
  );
}

function TopicsStep({ state }: { state: WizardState }) {
  const ch = state.channel!;
  const m = useWizardMut();
  const n = state.presets.topic_count;
  const [preset, setPreset] = useState<string | null>(null);
  const [niche, setNiche] = useState(ch.niche ?? "");
  const [topics, setTopics] = useState<{ name: string; theme_prompt: string | null }[]>(
    () => Array.from({ length: n }, (_, i) => state.topics[i] ?? { name: "", theme_prompt: null }));
  const pick = (p: WizardPreset) => {
    setPreset(p.id);
    setNiche(p.niche);
    setTopics(p.topics.slice(0, n).map((t) => ({ name: t.name, theme_prompt: t.theme_prompt })));
  };
  const ready = niche.trim() && topics.every((t) => t.name.trim());
  return (
    <Panel title="Pick a niche and 3 topics" step="topics">
      <p className="text-sm text-fog-300 mb-5 max-w-xl leading-relaxed">
        Start from a suggestion or type your own. Drafts are written for this niche, one topic at a time.
      </p>
      <div className="flex flex-wrap gap-2 mb-6" role="group" aria-label="Suggestions">
        {state.presets.niches.map((p) => (
          <button key={p.id} type="button" aria-pressed={preset === p.id}
            className={`btn text-xs ${preset === p.id ? "border-signal text-signal bg-signal/10" : "btn-ghost"}`} onClick={() => pick(p)}>
            {p.label}
          </button>
        ))}
      </div>
      <form className="max-w-xl" onSubmit={(e) => {
        e.preventDefault();
        m.topics.mutate({ channel_id: ch.id, niche, preset_id: preset, topics });
      }}>
        <label className="block mb-4">
          <div className="label mb-1.5">Niche</div>
          <input className="input w-full" value={niche} maxLength={200}
            onChange={(e) => setNiche(e.target.value)} placeholder="What your channel is about" />
        </label>
        {topics.map((t, i) => (
          <label key={i} className="block mb-3">
            <div className="label mb-1.5">Topic {i + 1}</div>
            <input className="input w-full" value={t.name} maxLength={80}
              onChange={(e) => setTopics(topics.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))} />
          </label>
        ))}
        <button className="btn btn-signal mt-2" disabled={!ready || m.topics.isPending}>
          {m.topics.isPending ? "saving…" : "Save topics"}
        </button>
        <ErrorRow error={m.topics.error} />
      </form>
    </Panel>
  );
}

function BudgetsStep({ state }: { state: WizardState }) {
  const ch = state.channel!;
  const m = useWizardMut();
  const [render, setRender] = useState(String(ch.daily_render_budget || state.default_budgets.daily_render_budget));
  const [publish, setPublish] = useState(String(ch.daily_publish_budget || state.default_budgets.daily_publish_budget));
  return (
    <Panel title="Daily budgets" step="budgets">
      <p className="text-sm text-fog-300 mb-5 max-w-xl leading-relaxed">
        How many drafts Channels Manager may render per day, and how many approved videos it may publish per
        day. The defaults work for most channels; you can change them later in Channels.
      </p>
      <form className="max-w-md grid grid-cols-2 gap-4" onSubmit={(e) => {
        e.preventDefault();
        m.budgets.mutate({ channel_id: ch.id, daily_render_budget: Number(render),
                           daily_publish_budget: Number(publish) });
      }}>
        <label className="block">
          <div className="label mb-1.5">Drafts per day</div>
          <input className="input w-full" type="number" min={1} max={50} value={render}
            onChange={(e) => setRender(e.target.value)} />
        </label>
        <label className="block">
          <div className="label mb-1.5">Publishes per day</div>
          <input className="input w-full" type="number" min={1} max={50} value={publish}
            onChange={(e) => setPublish(e.target.value)} />
        </label>
        <div className="col-span-2">
          <button className="btn btn-signal" disabled={m.budgets.isPending}>
            {m.budgets.isPending ? "saving…" : "Use these budgets"}
          </button>
          <ErrorRow error={m.budgets.error} />
        </div>
      </form>
    </Panel>
  );
}

function ConsentsStep() {
  const m = useWizardMut();
  const [terms, setTerms] = useState(false);
  const [privacy, setPrivacy] = useState(false);
  const [ai, setAi] = useState(false);
  const l6 = l6ConnectAiLineProps(navLocale());
  const box = "mt-0.5 h-4 w-4 accent-[#c9f24e] shrink-0";
  return (
    <Panel title="Terms and AI data" step="consents">
      <form className="max-w-xl space-y-4 mt-4" onSubmit={(e) => {
        e.preventDefault();
        m.consents.mutate({ terms, privacy, ai_data: ai, ai_data_line_version: L6_CONNECT_AI_LINE_SHA });
      }}>
        <label className="flex gap-3 text-sm text-fog-200">
          <input type="checkbox" className={box} checked={terms} onChange={(e) => setTerms(e.target.checked)} />
          <span>I accept the Owera{" "}
            <a href={POLICY_LINKS.terms} target="_blank" rel="noopener noreferrer" className="underline">Terms of Service</a>.
          </span>
        </label>
        <label className="flex gap-3 text-sm text-fog-200">
          <input type="checkbox" className={box} checked={privacy} onChange={(e) => setPrivacy(e.target.checked)} />
          <span>I accept the Owera{" "}
            <a href={POLICY_LINKS.privacy} target="_blank" rel="noopener noreferrer" className="underline">Privacy Policy</a>.
          </span>
        </label>
        <div className="rounded-md border border-signal/25 bg-signal/[0.04] px-5 py-4">
          <div className="label mb-2">AI data</div>
          <p id="l6-ai-data-ack-line" lang={l6.lang} data-l6-consent-line=""
            className="text-sm text-fog-100 leading-relaxed">{l6.text}</p>
          <label className="flex gap-3 text-sm text-fog-200 mt-3">
            <input type="checkbox" className={box} checked={ai} aria-describedby="l6-ai-data-ack-line"
              onChange={(e) => setAi(e.target.checked)} />
            <span>I understand how my channel data is used to write my drafts.</span>
          </label>
        </div>
        <button className="btn btn-signal" disabled={!(terms && privacy && ai) || m.consents.isPending}>
          {m.consents.isPending ? "saving…" : "Continue"}
        </button>
        <ErrorRow error={m.consents.error} />
      </form>
    </Panel>
  );
}

function FirstDraftStep({ state }: { state: WizardState }) {
  const ch = state.channel!;
  const m = useWizardMut();
  const fired = useRef(false);
  const fd = state.first_draft;
  const blocked = !!state.llm_blocked;
  useWizard(true, !!fd && !state.first_draft_at);   // follow the render
  useEffect(() => {
    // Auto-start once when the user reaches this step (no button to find).
    if (!fd && !fired.current && !blocked) {
      fired.current = true;
      m.firstDraft.mutate({ channel_id: ch.id });
    }
  }, [fd, blocked]);   // eslint-disable-line react-hooks/exhaustive-deps
  if (fd) {
    const rendered = !!state.first_draft_at;
    return (
      <Panel title={rendered ? "Your first draft is ready" : "Your first draft is on its way"} step="first_draft">
        <p className="text-sm text-fog-300 max-w-xl leading-relaxed">
          {rendered
            ? "It is waiting in your review queue. Nothing is published until you approve it."
            : "It is first in line to render and appears in your review queue as soon as it is ready. Nothing is published until you approve it."}
        </p>
        <div className="panel mt-5 px-4 py-3 flex items-center gap-3 max-w-xl">
          <Dot hex="#c9f24e" pulse={!rendered} />
          <span className="text-fog-100 text-sm">{fd.subject}</span>
          <span className="ml-auto font-mono text-[11px] uppercase text-fog-400">{fd.status}</span>
        </div>
        <div className="mt-6 flex gap-3">
          <Link to={`/board/${ch.id}`} className="btn btn-signal">Open review queue</Link>
          <Link to="/" className="btn btn-ghost">Overview</Link>
        </div>
      </Panel>
    );
  }
  return (
    <Panel title="Writing your first ideas" step="first_draft">
      <p className="text-sm text-fog-300 max-w-xl leading-relaxed">
        {m.firstDraft.isPending
          ? "Generating ideas for your first topic and queueing the best one as your first draft…"
          : "We could not start your first draft yet."}
      </p>
      {!m.firstDraft.isPending && !blocked && <ErrorRow error={m.firstDraft.error} />}
      {!m.firstDraft.isPending && (
        <button className="btn btn-signal mt-5" onClick={() => m.firstDraft.mutate({ channel_id: ch.id })}>
          Try again
        </button>
      )}
    </Panel>
  );
}

export default function Wizard() {
  const { data: state, isLoading, error } = useWizard();
  if (isLoading) return <div className="p-8 label">loading setup…</div>;
  if (!state) return <div className="p-8 text-sm text-fog-300">Could not load setup. {errText(error)}</div>;
  const step = state.current_step ?? "first_draft";
  return (
    <div className="p-4 md:p-8 max-w-3xl" data-wizard="">
      {/* S8: a guard refusal is never silent ("Drafts paused: finish setup step …"). */}
      <LlmBlockedBanner blocked={state.llm_blocked} />
      <div className="label mb-2">// getting started</div>
      <h1 className="font-display font-extrabold text-3xl text-fog-50 mb-6">Set up your first draft</h1>
      <Stepper state={state} />
      {step === "channel" && <ChannelStep />}
      {step === "youtube" && <YoutubeStep state={state} />}
      {step === "topics" && <TopicsStep state={state} />}
      {step === "budgets" && <BudgetsStep state={state} />}
      {step === "consents" && <ConsentsStep />}
      {(step === "ideas" || step === "first_draft") && <FirstDraftStep state={state} />}
    </div>
  );
}
