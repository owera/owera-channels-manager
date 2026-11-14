/** S2 ack-only page (/ai-data): the Google Limited Use consent alone.
 *
 * AuthGate sends an external workspace here before the first LLM use when the
 * release flag is ON and it has no current consent (never ticked, withdrawn, or
 * ticked for a text that changed since), including workspaces that accepted
 * the terms outside the wizard. Internal allowlisted workspaces are exempt.
 * "Not now" leaves the app usable (connect, view); only drafts stay paused.
 */
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAiData, useAiDataMut } from "../api";
import { googleLimitedUse } from "../googleLimitedUse";
import { GoogleLimitedUseBox } from "../GoogleLimitedUseBox";
import { l6ConnectAiLineProps } from "../l6ConnectAiLine";
import { LlmBlockedBanner } from "../ui";
import { wizardT as t } from "../wizardI18n";

export const AI_DATA_DISMISS_KEY = "cm_ai_data_not_now";

export default function AiData() {
  const { data } = useAiData();
  const m = useAiDataMut();
  const nav = useNavigate();
  const [ai, setAi] = useState(false);
  const loc = typeof navigator !== "undefined" ? navigator : undefined;
  const glu = googleLimitedUse(loc);
  const l6 = l6ConnectAiLineProps(loc);
  const done = data?.valid === true;
  return (
    <div className="p-4 md:p-8 max-w-3xl" data-ai-data-page="">
      <LlmBlockedBanner blocked={data?.llm_blocked} />
      <div className="label mb-2">{t("wizard.page.eyebrow")}</div>
      <section className="panel p-6 md:p-8">
        <h1 className="font-display font-extrabold text-2xl text-fog-50 tracking-tight mb-2">{t("ai_data.title")}</h1>
        <p className="text-sm text-fog-300 max-w-xl leading-relaxed">
          {data?.stale ? t("ai_data.stale") : t("ai_data.help")}
        </p>
        {done ? (
          <div className="mt-5">
            <p className="text-sm text-fog-100">{t("ai_data.done")}</p>
            <Link to="/" className="btn btn-signal mt-4">{t("wizard.first_draft.cta_overview")}</Link>
          </div>
        ) : (
          <form className="max-w-xl mt-4" onSubmit={(e) => {
            e.preventDefault();
            m.ack.mutate({ ai_data: ai, text_sha: glu.sha, locale: glu.locale });
          }}>
            <div className="rounded-md border border-signal/25 bg-signal/[0.04] px-5 py-4">
              <p id="l6-ai-data-page-line" lang={l6.lang} data-l6-consent-line=""
                className="text-sm text-fog-100 leading-relaxed">{l6.text}</p>
              <GoogleLimitedUseBox copy={glu} checked={ai} onChange={setAi} describedBy="l6-ai-data-page-line" />
            </div>
            <div className="mt-5 flex flex-wrap items-center gap-3">
              <button className="btn btn-signal" disabled={!ai || m.ack.isPending}>
                {m.ack.isPending ? t("wizard.consents.cta_pending") : t("ai_data.cta")}
              </button>
              <button type="button" className="btn btn-ghost" onClick={() => {
                try { sessionStorage.setItem(AI_DATA_DISMISS_KEY, "1"); } catch { /* private mode */ }
                nav("/", { replace: true });
              }}>{t("ai_data.not_now")}</button>
            </div>
            <p className="mt-3 text-xs text-fog-400 max-w-xl">{t("ai_data.not_now_help")}</p>
            {m.ack.error && <div role="alert" className="mt-3 text-xs font-mono text-[#f7768e]">{String(m.ack.error.message)}</div>}
          </form>
        )}
      </section>
    </div>
  );
}

/** Settings control: status + withdraw (cuts the LLM at the next call). */
/** PO CM 14/11: 1-step confirmation (CMO v2 dialog copy, Withdraw / Keep) for a
 * manual withdrawal. Withdraw cuts the LLM for the WHOLE workspace at once (even
 * with channels still connected); only a new ack on /ai-data brings it back. */
export function WithdrawAiConsentDialog({ onDone }: { onDone: () => void }) {
  const m = useAiDataMut();
  return (
    <div role="alertdialog" aria-labelledby="glu-withdraw-title" aria-describedby="glu-withdraw-help"
      className="panel p-4 max-w-sm border border-ink-line" data-glu-withdraw-dialog="">
      <div id="glu-withdraw-title" className="text-sm text-fog-50 font-medium">{t("settings.glu.dialog_title")}</div>
      <p id="glu-withdraw-help" className="mt-2 text-xs text-fog-300 leading-relaxed">{t("settings.glu.dialog_help")}</p>
      <div className="mt-3 flex flex-wrap gap-2">
        <button type="button" className="btn btn-signal text-xs" disabled={m.withdraw.isPending}
          data-glu-withdraw-confirm=""
          onClick={() => m.withdraw.mutate(undefined, { onSuccess: onDone })}>
          {t("settings.glu.dialog_withdraw")}</button>
        <button type="button" className="btn btn-ghost text-xs" data-glu-withdraw-keep=""
          onClick={onDone}>{t("settings.glu.dialog_keep")}</button>
      </div>
    </div>
  );
}

/** Always-visible account-footer link (PO CM 14/11), independent of Settings:
 * shown whenever this workspace has a current ack, also before the first draft. */
export function WithdrawAiConsentLink({ className = "" }: { className?: string }) {
  const { data } = useAiData();
  const [asking, setAsking] = useState(false);
  if (!data || data.exempt || !data.valid) return null;
  return (
    <div className={className} data-glu-withdraw-link="">
      {asking ? <WithdrawAiConsentDialog onDone={() => setAsking(false)} /> : (
        <button type="button" className="text-[11px] text-fog-400 hover:text-fog-100 underline"
          data-glu-withdraw-open="footer" onClick={() => setAsking(true)}>
          {t("glu.footer.withdraw")}</button>
      )}
    </div>
  );
}

export function GoogleLimitedUseSettings() {
  const { data } = useAiData();
  const [asking, setAsking] = useState(false);
  if (!data) return null;
  return (
    <div className="panel p-6 mt-6" data-google-limited-use-settings="">
      <div className="text-sm text-fog-100 mb-1">{t("settings.glu.title")}</div>
      <div className="label mb-4">
        {data.exempt ? t("settings.glu.exempt")
          : data.valid ? t("settings.glu.on", { date: (data.acked_at ?? "").slice(0, 10) })
          : t("settings.glu.off")}
      </div>
      {data.valid && !data.exempt && (
        <>
          {asking ? <WithdrawAiConsentDialog onDone={() => setAsking(false)} /> : (
            <button className="btn btn-ghost" data-glu-withdraw-open="settings"
              onClick={() => setAsking(true)}>{t("settings.glu.withdraw")}</button>
          )}
          <p className="mt-3 text-xs text-fog-400 max-w-xl leading-relaxed">{t("settings.glu.withdraw_help")}</p>
        </>
      )}
      {!data.valid && !data.exempt && (
        <Link to="/ai-data" className="btn btn-signal">{t("settings.glu.give")}</Link>
      )}
    </div>
  );
}
