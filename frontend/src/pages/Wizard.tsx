/** S2 guided onboarding: one wizard from first login to the first draft.
 *
 * create channel → Connect YouTube (#215 consent note + L6 line) → niche + 3 topics
 * → default budgets → Terms / Privacy + Google Limited Use consent → ideas (1 per
 * topic) → first draft queued at top render priority. Every step posts to
 * /api/onboarding/wizard/* and the server derives where the user is, so a reload
 * resumes the same step.
 *
 * Copy: CMO CM-WIZARD-COPY.md (sha df45bd814374) via wizardI18n.ts, EN / PT by the
 * L6 locale rule. Verbatim LegalOps texts: the L6 line (l6ConnectAiLine.ts, sha
 * 845c22005853) and the Google Limited Use checkbox (googleLimitedUse.ts).
 */
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useFeatures, useMut, useWizard, useWizardMut, WizardPreset, WizardState } from "../api";
import { l6ConnectAiLineId, l6ConnectAiLineProps } from "../l6ConnectAiLine";
import { googleLimitedUse } from "../googleLimitedUse";
import { GoogleLimitedUseBox } from "../GoogleLimitedUseBox";
import { ConnectConsentNote, Dot, LlmBlockedBanner } from "../ui";
import { wizardT as t } from "../wizardI18n";
import { statusLabel } from "../status";
import { termsConsent } from "../termsConsent";

const STEPS: { id: string; covers: string[] }[] = [
  { id: "channel", covers: ["channel"] },
  { id: "youtube", covers: ["youtube"] },
  { id: "topics", covers: ["topics"] },
  { id: "budgets", covers: ["budgets"] },
  { id: "consents", covers: ["consents"] },
  { id: "first_draft", covers: ["ideas", "first_draft"] },
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
            <span className="tabular-nums">{String(i + 1).padStart(2, "0")}</span> {t(`wizard.stepper.${s.id}`)}
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
    <Panel title={t("wizard.channel.title")} step="channel">
      <p className="text-sm text-fog-300 mb-5 max-w-xl leading-relaxed">{t("wizard.channel.help")}</p>
      <form onSubmit={(e) => { e.preventDefault(); m.channel.mutate({ name }); }} className="max-w-md">
        <label className="block mb-4">
          <div className="label mb-1.5">{t("wizard.channel.field_name")}</div>
          <input className="input w-full" value={name} maxLength={80} autoFocus
            onChange={(e) => setName(e.target.value)} placeholder={t("wizard.channel.placeholder")} />
        </label>
        <button className="btn btn-signal" disabled={!name.trim() || m.channel.isPending}>
          {m.channel.isPending ? t("wizard.channel.cta_pending") : t("wizard.channel.cta")}
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
    <Panel title={t("wizard.youtube.title")} step="youtube">
      <p className="text-sm text-fog-300 max-w-xl leading-relaxed">
        {t("wizard.youtube.help")}
        {platformOauth ? "" : " Upload your Google Cloud client secret first, then connect."}
      </p>
      <p id={l6Id} lang={l6.lang} data-l6-consent-line=""
        className="text-sm text-fog-200 mt-3 max-w-xl leading-relaxed">
        {l6.text}
      </p>
      {ch.oauth_status !== "disconnected" && ch.oauth_status !== "connected" && (
        <div className="mt-3 text-sm text-amber">{t("wizard.youtube.retry")}</div>
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
          {connecting ? t("wizard.youtube.cta_waiting") : start.isPending ? t("wizard.youtube.cta_pending") : t("wizard.youtube.cta")}
        </button>
      </div>
      <ConnectConsentNote className="mt-3 max-w-xl" />
      {connecting && (
        <p className="mt-3 text-sm text-fog-300">{t("wizard.youtube.waiting_help")}</p>
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
    setTopics(p.topics.slice(0, n).map((x) => ({ name: x.name, theme_prompt: x.theme_prompt })));
  };
  const ready = niche.trim() && topics.every((x) => x.name.trim());
  return (
    <Panel title={t("wizard.topics.title")} step="topics">
      <p className="text-sm text-fog-300 mb-5 max-w-xl leading-relaxed">{t("wizard.topics.help")}</p>
      <div className="flex flex-wrap gap-2 mb-6" role="group" aria-label={t("wizard.topics.suggestions_aria")}>
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
          <div className="label mb-1.5">{t("wizard.topics.field_niche")}</div>
          <input className="input w-full" value={niche} maxLength={200}
            onChange={(e) => setNiche(e.target.value)} placeholder={t("wizard.topics.niche_placeholder")} />
        </label>
        {topics.map((tp, i) => (
          <label key={i} className="block mb-3">
            <div className="label mb-1.5">{t("wizard.topics.field_topic", { n: i + 1 })}</div>
            <input className="input w-full" value={tp.name} maxLength={80}
              onChange={(e) => setTopics(topics.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))} />
          </label>
        ))}
        <button className="btn btn-signal mt-2" disabled={!ready || m.topics.isPending}>
          {m.topics.isPending ? t("wizard.topics.cta_pending") : t("wizard.topics.cta")}
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
    <Panel title={t("wizard.budgets.title")} step="budgets">
      <p className="text-sm text-fog-300 mb-5 max-w-xl leading-relaxed">{t("wizard.budgets.help")}</p>
      <form className="max-w-md grid grid-cols-2 gap-4" onSubmit={(e) => {
        e.preventDefault();
        m.budgets.mutate({ channel_id: ch.id, daily_render_budget: Number(render),
                           daily_publish_budget: Number(publish) });
      }}>
        <label className="block">
          <div className="label mb-1.5">{t("wizard.budgets.field_render")}</div>
          <input className="input w-full" type="number" min={1} max={50} value={render}
            onChange={(e) => setRender(e.target.value)} />
        </label>
        <label className="block">
          <div className="label mb-1.5">{t("wizard.budgets.field_publish")}</div>
          <input className="input w-full" type="number" min={1} max={50} value={publish}
            onChange={(e) => setPublish(e.target.value)} />
        </label>
        <div className="col-span-2">
          <button className="btn btn-signal" disabled={m.budgets.isPending}>
            {m.budgets.isPending ? t("wizard.budgets.cta_pending") : t("wizard.budgets.cta")}
          </button>
          <ErrorRow error={m.budgets.error} />
        </div>
      </form>
    </Panel>
  );
}

function ConsentsStep({ state }: { state: WizardState }) {
  const m = useWizardMut();
  const [terms, setTerms] = useState(false);
  const [ai, setAi] = useState(false);
  const l6 = l6ConnectAiLineProps(navLocale());
  const glu = googleLimitedUse(navLocale());
  // Single Terms + Privacy box (LegalOps FIX 14/11). PT gate from the server (MANAGER_TERMS_PT_LIVE).
  const tc = termsConsent(navLocale(), state.terms?.pt_live === true);
  const box = "mt-0.5 h-4 w-4 accent-[#c9f24e] shrink-0";
  return (
    <Panel title={t("wizard.consents.title")} step="consents">
      <p className="text-sm text-fog-300 max-w-xl leading-relaxed">{t("wizard.consents.help")}</p>
      <form className="max-w-xl space-y-4 mt-4" onSubmit={(e) => {
        e.preventDefault();
        // The sha + locale of the exact sentences displayed (Terms box and Google Limited Use box).
        m.consents.mutate({ terms, terms_text_sha: tc.sha, terms_locale: tc.locale,
          ai_data: ai, ai_data_text_sha: glu.sha, ai_data_locale: glu.locale });
      }}>
        <label className="flex gap-3 text-sm text-fog-200" data-terms-consent="">
          <input type="checkbox" className={box} checked={terms} data-terms-sha={tc.sha}
            onChange={(e) => setTerms(e.target.checked)} />
          <span lang={tc.lang} data-terms-text={tc.text}>
            {tc.parts.map((p, i) => p.href
              ? <a key={i} href={p.href} target="_blank" rel="noopener noreferrer" className="underline"
                  data-terms-link={p.href}>{p.text}</a>
              : <span key={i}>{p.text}</span>)}
          </span>
        </label>
        <div className="rounded-md border border-signal/25 bg-signal/[0.04] px-5 py-4">
          <div className="label mb-2">{t("banner.step.ai_data")}</div>
          <p id="l6-ai-data-ack-line" lang={l6.lang} data-l6-consent-line=""
            className="text-sm text-fog-100 leading-relaxed">{l6.text}</p>
          <GoogleLimitedUseBox copy={glu} checked={ai} onChange={setAi} describedBy="l6-ai-data-ack-line" />
        </div>
        <button className="btn btn-signal" disabled={!(terms && ai) || m.consents.isPending}>
          {m.consents.isPending ? t("wizard.consents.cta_pending") : t("wizard.consents.cta")}
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
      <Panel title={rendered ? t("wizard.first_draft.ready_title") : t("wizard.first_draft.queued_title")}
        step="first_draft">
        <p className="text-sm text-fog-300 max-w-xl leading-relaxed">
          {rendered ? t("wizard.first_draft.ready_help") : t("wizard.first_draft.queued_help")}
        </p>
        <div className="panel mt-5 px-4 py-3 flex items-center gap-3 max-w-xl">
          <Dot hex="#c9f24e" pulse={!rendered} />
          <span className="text-fog-100 text-sm">{fd.subject}</span>
          <span className="ml-auto font-mono text-[11px] uppercase text-fog-400" data-fd-status={fd.status}>{statusLabel(fd.status)}</span>
        </div>
        <div className="mt-6 flex gap-3">
          <Link to={`/board/${ch.id}`} className="btn btn-signal">{t("wizard.first_draft.cta_queue")}</Link>
          <Link to="/" className="btn btn-ghost">{t("wizard.first_draft.cta_overview")}</Link>
        </div>
      </Panel>
    );
  }
  return (
    <Panel title={t("wizard.ideas.title")} step="first_draft">
      <p className="text-sm text-fog-300 max-w-xl leading-relaxed">
        {m.firstDraft.isPending || (!fd && !fired.current && !blocked)
          ? t("wizard.ideas.help") : t("wizard.ideas.error")}
      </p>
      {!m.firstDraft.isPending && !blocked && <ErrorRow error={m.firstDraft.error} />}
      {!m.firstDraft.isPending && (
        <button className="btn btn-signal mt-5" onClick={() => m.firstDraft.mutate({ channel_id: ch.id })}>
          {t("wizard.ideas.retry")}
        </button>
      )}
    </Panel>
  );
}

export default function Wizard() {
  const { data: state, isLoading, error } = useWizard();
  if (isLoading) return <div className="p-8 label">{t("wizard.loading")}</div>;
  if (!state) return <div className="p-8 text-sm text-fog-300">{t("wizard.load_error")} {errText(error)}</div>;
  const step = state.current_step ?? "first_draft";
  return (
    <div className="p-4 md:p-8 max-w-3xl" data-wizard="">
      {/* S8: a guard refusal is never silent ("Drafts paused: finish setup step …"). */}
      <LlmBlockedBanner blocked={state.llm_blocked} />
      <div className="label mb-2">{t("wizard.page.eyebrow")}</div>
      <h1 className="font-display font-extrabold text-3xl text-fog-50 mb-6">{t("wizard.page.title")}</h1>
      <Stepper state={state} />
      {step === "channel" && <ChannelStep />}
      {step === "youtube" && <YoutubeStep state={state} />}
      {step === "topics" && <TopicsStep state={state} />}
      {step === "budgets" && <BudgetsStep state={state} />}
      {step === "consents" && <ConsentsStep state={state} />}
      {(step === "ideas" || step === "first_draft") && <FirstDraftStep state={state} />}
    </div>
  );
}
