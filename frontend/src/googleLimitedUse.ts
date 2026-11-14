/** Google Limited Use consent checkbox (LegalOps, 14/11): verbatim EN/PT text.
 *
 * Shown in the wizard's consent step and on the ack-only page (/ai-data), with a
 * privacy-policy link next to it (new tab). The SPA sends back the sha256 of the
 * EXACT string it displayed plus its locale; the server only accepts the shas of
 * these two strings (app/services/google_limited_use.py) and asks again when the
 * text changes. Locale: same rule as the L6 line (PT when the browser locale
 * starts with "pt", else EN). tests/verify_google_limited_use.py pins both sides.
 */
import { L6Locale } from "./l6ConnectAiLine";

export const GOOGLE_LIMITED_USE_TEXT_EN =
  "I agree that Channels Manager sends my videos' titles, descriptions, tags and channel stats to an AI provider in the US, only to create my own drafts.";

export const GOOGLE_LIMITED_USE_TEXT_PT =
  "Concordo que o Channels Manager envie os títulos, descrições e tags dos meus vídeos e as estatísticas do canal a um provedor de IA nos EUA, só para criar os meus próprios rascunhos.";

/** sha256 hex of the strings above (UTF-8). */
export const GOOGLE_LIMITED_USE_SHA_EN = "df70f5975cabedaf674a0997e766fab1d2f002ac973e8cfa75f3a3dc6e52ec00";
export const GOOGLE_LIMITED_USE_SHA_PT = "a333b8d42296f7232ab8440ff03e9e12a8511500fdbdb441fe98b562bd854013";

export const GOOGLE_LIMITED_USE_LINK_EN = "Privacy policy";
export const GOOGLE_LIMITED_USE_LINK_PT = "Política de privacidade";
/** PO CM 14/11: the link opens the Google section of the privacy page, per locale
 * (the #cm-google anchors ship with the Mon 16 privacy deploy). */
export const GOOGLE_LIMITED_USE_HREF_EN = "https://owera.com/privacy/#cm-google";
export const GOOGLE_LIMITED_USE_HREF_PT = "https://owera.com/privacy/pt/#cm-google";

export type GoogleLimitedUseCopy = {
  text: string; sha: string; locale: "en" | "pt"; lang?: "pt"; linkLabel: string; href: string;
};

export function googleLimitedUse(nav?: L6Locale): GoogleLimitedUseCopy {
  const fromLanguage = (nav?.language ?? "").trim();
  const fromList = (nav?.languages && nav.languages.length > 0 ? nav.languages[0] : "") || "";
  const pt = (fromLanguage || fromList).toLowerCase().startsWith("pt");
  return pt
    ? { text: GOOGLE_LIMITED_USE_TEXT_PT, sha: GOOGLE_LIMITED_USE_SHA_PT, locale: "pt", lang: "pt",
        linkLabel: GOOGLE_LIMITED_USE_LINK_PT, href: GOOGLE_LIMITED_USE_HREF_PT }
    : { text: GOOGLE_LIMITED_USE_TEXT_EN, sha: GOOGLE_LIMITED_USE_SHA_EN, locale: "en",
        linkLabel: GOOGLE_LIMITED_USE_LINK_EN, href: GOOGLE_LIMITED_USE_HREF_EN };
}
