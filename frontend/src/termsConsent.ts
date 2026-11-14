/** S2 — the single Terms + Privacy checkbox (LegalOps FIX4 via CMO, sha 6a389cd4d636, 14/11).
 *
 * Verbatim LegalOps sentences (same strings as app/services/terms_consent.py):
 * the user accepts the Terms of Service and has read the Privacy Notice. Links
 * sit on the document names. The SPA sends the sha256 of the exact sentence
 * shown + its locale; the server stores it as terms_accepted_version.
 *
 * PT gate: the PT sentence links to https://owera.com/terms/pt/, not live yet.
 * The flag comes from the server (MANAGER_TERMS_PT_LIVE → GET /api/onboarding/wizard
 * `terms.pt_live`); while it is false (or unknown) the PT locale shows the EN
 * sentence + EN links. The server accepts only its configured shas
 * (MANAGER_TERMS_VERSION_SHA_EN / _PT).
 */
import { L6Locale } from "./l6ConnectAiLine";

export const TERMS_TEXT_EN = "I accept the Owera Terms of Service and have read the Privacy Notice.";
export const TERMS_TEXT_PT = "Aceito os Termos de Serviço da Owera e li o Aviso de Privacidade.";
export const TERMS_SHA_EN = "91a6c69ec169f1877ba16cbc00508880116bebf9965c01cdf0646ca3bbf1fea3";
export const TERMS_SHA_PT = "16d920da6551837b890154f05de232fd48d7cfee409496184d2d294de7a81a19";

export type TermsPart = { text: string; href?: string };
export type TermsCopy = { text: string; sha: string; locale: "en" | "pt"; lang?: "pt"; parts: TermsPart[] };

const EN: TermsCopy = {
  text: TERMS_TEXT_EN, sha: TERMS_SHA_EN, locale: "en",
  parts: [
    { text: "I accept the Owera " },
    { text: "Terms of Service", href: "https://owera.com/terms/" },
    { text: " and have read the " },
    { text: "Privacy Notice", href: "https://owera.com/privacy/" },
    { text: "." },
  ],
};
const PT: TermsCopy = {
  text: TERMS_TEXT_PT, sha: TERMS_SHA_PT, locale: "pt", lang: "pt",
  parts: [
    { text: "Aceito os " },
    { text: "Termos de Serviço", href: "https://owera.com/terms/pt/" },
    { text: " da Owera e li o " },
    { text: "Aviso de Privacidade", href: "https://owera.com/privacy/pt/" },
    { text: "." },
  ],
};

/** The sentence a browser locale sees (PT only once the server says /terms/pt/ is live). */
export function termsConsent(nav: L6Locale | undefined, ptLive: boolean): TermsCopy {
  const fromLanguage = (nav?.language ?? "").trim();
  const fromList = (nav?.languages && nav.languages.length > 0 ? nav.languages[0] : "") || "";
  const pt = (fromLanguage || fromList).toLowerCase().startsWith("pt");
  return pt && ptLive === true ? PT : EN;
}
