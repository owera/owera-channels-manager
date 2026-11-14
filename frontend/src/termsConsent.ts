/** S2 — the single Terms + Privacy checkbox (LegalOps 14/11, "Terms of Use" wording).
 *
 * Verbatim LegalOps sentences (same strings as app/services/terms_consent.py):
 * the user accepts the Terms of Use and has read the Privacy Notice. Links
 * sit on the document names. The SPA sends the sha256 of the exact sentence
 * shown + its locale; the server stores it as terms_accepted_version.
 *
 * PT gate: the PT sentence links to https://owera.com/terms/pt/, not live yet.
 * The flag comes from the server (MANAGER_TERMS_PT_LIVE → GET /api/onboarding/wizard
 * `terms.pt_live`); while it is false (or unknown) a PT-locale user gets NO Terms
 * box and NO AI-data box in the wizard, only TERMS_PT_SOON (CTO / LegalOps 14/11),
 * whose last clause switches the assistant to English (then the EN box, locale en).
 * The server accepts only its configured shas (MANAGER_TERMS_VERSION_SHA_EN / _PT)
 * plus the FIX4 "Terms of Service" shas for earlier acceptances.
 */
import { L6Locale } from "./l6ConnectAiLine";

export const TERMS_TEXT_EN = "I accept the Owera Terms of Use and have read the Privacy Notice.";
export const TERMS_TEXT_PT = "Aceito os Termos de Uso da Owera e li o Aviso de Privacidade.";
export const TERMS_SHA_EN = "f586e08debd5496c1bcf1aee80436e2e4477cbcf38522870217dc5328411fefa";
export const TERMS_SHA_PT = "646e87d7f4dca1e836c54527c0f9fe81123df869215703040693efcbbfde92ca";

/** Shown INSTEAD of the Terms + AI-data boxes to PT users while /terms/pt/ isn't live (exact text). */
export const TERMS_PT_SOON_BEFORE =
  "Os termos em português estarão disponíveis em breve. Até lá, a ativação da IA não está disponível em português. Se preferir, você pode ";
export const TERMS_PT_SOON_SWITCH = "mudar o idioma do assistente para inglês";
export const TERMS_PT_SOON = `${TERMS_PT_SOON_BEFORE}${TERMS_PT_SOON_SWITCH}.`;

export type TermsPart = { text: string; href?: string };
export type TermsCopy = { text: string; sha: string; locale: "en" | "pt"; lang?: "pt"; parts: TermsPart[] };

const EN: TermsCopy = {
  text: TERMS_TEXT_EN, sha: TERMS_SHA_EN, locale: "en",
  parts: [
    { text: "I accept the Owera " },
    { text: "Terms of Use", href: "https://owera.com/terms/" },
    { text: " and have read the " },
    { text: "Privacy Notice", href: "https://owera.com/privacy/" },
    { text: "." },
  ],
};
const PT: TermsCopy = {
  text: TERMS_TEXT_PT, sha: TERMS_SHA_PT, locale: "pt", lang: "pt",
  parts: [
    { text: "Aceito os " },
    { text: "Termos de Uso", href: "https://owera.com/terms/pt/" },
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

/** PT locale while the PT terms aren't live: hide both boxes, show TERMS_PT_SOON. */
export function termsPtBlocked(nav: L6Locale | undefined, ptLive: boolean): boolean {
  const fromLanguage = (nav?.language ?? "").trim();
  const fromList = (nav?.languages && nav.languages.length > 0 ? nav.languages[0] : "") || "";
  return (fromLanguage || fromList).toLowerCase().startsWith("pt") && ptLive !== true;
}
