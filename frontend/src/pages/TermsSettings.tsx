/** Settings: the single Terms + Privacy box again when the stored acceptance must be
 * re-accepted (CTO 14/11, `terms_reaccept_required`: the latest consent record names
 * no document sha). Same sentence, links and PT gate as the wizard consents step;
 * accepting writes a new record with the document shas now in force. Hidden otherwise. */
import { useState } from "react";
import { useTerms, useTermsMut } from "../api";
import { termsConsent, termsPtBlocked, TERMS_PT_SOON_BEFORE, TERMS_PT_SOON_SWITCH } from "../termsConsent";
import { effectiveNav, setLocaleOverride, wizardT as t } from "../wizardI18n";

export function TermsReacceptSettings() {
  const { data } = useTerms();
  const m = useTermsMut();
  const [terms, setTerms] = useState(false);
  if (!data || !data.reaccept_required) return null;
  const nav = effectiveNav();
  const ptLive = data.pt_live === true;
  const tc = termsConsent(nav, ptLive);
  return (
    <div className="panel p-6 mt-6" data-terms-reaccept="">
      <div className="text-sm text-fog-100 mb-3">{t("banner.step.terms_privacy")}</div>
      {termsPtBlocked(nav, ptLive) ? (
        <p className="text-sm text-fog-200 max-w-xl leading-relaxed" lang="pt" data-terms-pt-soon="">
          {TERMS_PT_SOON_BEFORE}
          <button type="button" className="underline text-signal" data-switch-locale="en"
            onClick={() => { setLocaleOverride("en"); window.location.reload(); }}>
            {TERMS_PT_SOON_SWITCH}
          </button>.
        </p>
      ) : (
        <form className="max-w-xl space-y-4" onSubmit={(e) => {
          e.preventDefault();
          m.mutate({ terms, terms_text_sha: tc.sha, terms_locale: tc.locale });
        }}>
          <label className="flex gap-3 text-sm text-fog-200" data-terms-consent="">
            <input type="checkbox" className="mt-0.5 h-4 w-4 accent-[#c9f24e] shrink-0" checked={terms}
              data-terms-sha={tc.sha} onChange={(e) => setTerms(e.target.checked)} />
            <span lang={tc.lang} data-terms-text={tc.text}>
              {tc.parts.map((p, i) => p.href
                ? <a key={i} href={p.href} target="_blank" rel="noopener noreferrer" className="underline"
                    data-terms-link={p.href}>{p.text}</a>
                : <span key={i}>{p.text}</span>)}
            </span>
          </label>
          <button className="btn btn-signal" disabled={!terms || m.isPending}>
            {m.isPending ? t("wizard.consents.cta_pending") : t("wizard.consents.cta")}
          </button>
          {m.error && <div className="text-xs text-red-300">{String((m.error as Error).message ?? m.error)}</div>}
        </form>
      )}
    </div>
  );
}
