/** L6 AI-consent line (LegalOps FINAL, sha 845c22005853).
 *
 * The only i18n in the SPA: PT when the browser locale starts with "pt",
 * otherwise EN. Both strings are verbatim from CM-CONNECT-AI-LINE.md.
 */
/** Version of CM-CONNECT-AI-LINE.md these strings come from. S2: sent back with the
 * onboarding AI-data acknowledgement (Workspace.ai_data_ack_version). */
export const L6_CONNECT_AI_LINE_SHA = "845c22005853";

export const L6_CONNECT_AI_LINE_EN =
  "To write your drafts, Channels Manager sends your videos' titles, descriptions, tags and channel stats to an AI provider in the US, only to create your own drafts. Nothing is published until you approve it.";

export const L6_CONNECT_AI_LINE_PT =
  "Para escrever seus rascunhos, o Channels Manager envia os títulos, descrições e tags dos seus vídeos e as estatísticas do canal a um provedor de IA nos EUA, só para criar os seus próprios rascunhos. Nada é publicado sem a sua aprovação.";

export type L6Locale = {
  language?: string;
  languages?: readonly string[];
} | null | undefined;

/** PT if navigator.language (else languages[0]) starts with "pt"; EN otherwise. */
export function l6ConnectAiLine(nav?: L6Locale): string {
  const fromLanguage = (nav?.language ?? "").trim();
  const fromList = (nav?.languages && nav.languages.length > 0 ? nav.languages[0] : "") || "";
  const lang = (fromLanguage || fromList).toLowerCase();
  return lang.startsWith("pt") ? L6_CONNECT_AI_LINE_PT : L6_CONNECT_AI_LINE_EN;
}

/** Text plus lang for the paragraph: lang="pt" only when the PT text shows. */
export function l6ConnectAiLineProps(nav?: L6Locale): { text: string; lang?: "pt" } {
  const text = l6ConnectAiLine(nav);
  return text === L6_CONNECT_AI_LINE_PT ? { text, lang: "pt" } : { text };
}

/** id of the L6 paragraph for one channel's OAuth button (aria-describedby target). */
export function l6ConnectAiLineId(channelId: number | string): string {
  return `l6-connect-ai-line-${channelId}`;
}
