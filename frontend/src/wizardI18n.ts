/** S2 wizard + banner strings, EN / PT-BR.
 *
 * CMO copy: CM-WIZARD-COPY.md (sha df45bd814374), keys as CMO suggested. The
 * "ai_data.*" and "settings.glu.*" keys are Channels Dev placeholders (no CMO copy
 * yet; listed in the PR for CMO). Server banners (llm_guard.BLOCK_USER_TEXT) are
 * localized server-side; the SPA passes ?locale= on /api/onboarding/*.
 * Locale: same rule as the L6 line (PT when the browser locale starts with "pt").
 * The Google Limited Use checkbox text is NOT here (googleLimitedUse.ts, LegalOps).
 */
import { L6Locale } from "./l6ConnectAiLine";

export type UiLocale = "en" | "pt";

export const WIZARD_STRINGS: Record<string, { en: string; pt: string }> = {
  "wizard.channel.title": { en: "Name your channel", pt: "Dê um nome ao seu canal" },
  "wizard.channel.help": { en: "This is the YouTube channel Channels Manager writes drafts for. You can rename it later.", pt: "Este é o canal do YouTube para o qual o Channels Manager escreve rascunhos. Você pode mudar o nome depois." },
  "wizard.youtube.title": { en: "Connect YouTube", pt: "Conectar o YouTube" },
  "wizard.youtube.help": { en: "Sign in with Google so Channels Manager can upload videos you approve, create playlists, and read basic channel stats.", pt: "Entre com o Google para o Channels Manager enviar os vídeos que você aprovar, criar playlists e ler estatísticas básicas do canal." },
  "wizard.topics.title": { en: "Pick a niche and 3 topics", pt: "Escolha um nicho e 3 tópicos" },
  "wizard.topics.help": { en: "Start from a suggestion or type your own. Drafts are written for this niche, one topic at a time.", pt: "Comece por uma sugestão ou escreva a sua. Os rascunhos são feitos para este nicho, um tópico de cada vez." },
  "wizard.budgets.title": { en: "Daily budgets", pt: "Limites diários" },
  "wizard.budgets.help": { en: "How many drafts may be prepared each day, and how many approved videos may be published each day. You can change this later.", pt: "Quantos rascunhos podem ser preparados por dia, e quantos vídeos aprovados podem ser publicados por dia. Você pode alterar depois." },
  "wizard.consents.title": { en: "Terms and AI data", pt: "Termos e dados de IA" },
  "wizard.consents.help": { en: "Review and accept the items below to continue.", pt: "Leia e aceite os itens abaixo para continuar." },
  "wizard.ideas.title": { en: "Writing your first ideas", pt: "Escrevendo suas primeiras ideias" },
  "wizard.ideas.help": { en: "Creating ideas for your first topic and lining up one as your first draft.", pt: "Criando ideias para o seu primeiro tópico e colocando uma delas como seu primeiro rascunho." },
  "wizard.ideas.error": { en: "We could not start your first draft yet.", pt: "Ainda não foi possível começar o seu primeiro rascunho." },
  "wizard.first_draft.queued_title": { en: "Your first draft is on its way", pt: "Seu primeiro rascunho está a caminho" },
  "wizard.first_draft.queued_help": { en: "It is first in line, and it will appear in your review queue when it is ready. Nothing is published until you approve it.", pt: "Ele é o primeiro da fila e aparece na sua fila de revisão quando estiver pronto. Nada é publicado até você aprovar." },
  "wizard.first_draft.ready_title": { en: "Your first draft is ready", pt: "Seu primeiro rascunho está pronto" },
  "wizard.first_draft.ready_help": { en: "It is waiting in your review queue. Nothing is published until you approve it.", pt: "Ele está esperando na sua fila de revisão. Nada é publicado até você aprovar." },
  "banner.drafts_paused": { en: "Drafts paused: finish setup step \"{step}\".", pt: "Rascunhos pausados: termine a etapa \"{step}\"." },
  "banner.step.ai_data": { en: "AI data", pt: "Dados de IA" },
  "banner.step.terms_privacy": { en: "Terms and privacy", pt: "Termos e privacidade" },
  "banner.step.payment": { en: "Payment", pt: "Pagamento" },
  "banner.monthly_draft_limit": { en: "Monthly draft limit reached. You can still review and publish drafts already in your queue. New drafts start again next month.", pt: "Limite mensal de rascunhos atingido. Você ainda pode revisar e publicar os que já estão na fila. Novos rascunhos voltam no próximo mês." },
  "wizard.page.eyebrow": { en: "// getting started", pt: "// primeiros passos" },
  "wizard.page.title": { en: "Set up your first draft", pt: "Prepare seu primeiro rascunho" },
  "wizard.stepper.channel": { en: "Channel", pt: "Canal" },
  "wizard.stepper.youtube": { en: "Connect YouTube", pt: "Conectar YouTube" },
  "wizard.stepper.topics": { en: "Niche & topics", pt: "Nicho e tópicos" },
  "wizard.stepper.budgets": { en: "Daily budgets", pt: "Limites diários" },
  "wizard.stepper.consents": { en: "Terms & AI data", pt: "Termos e dados de IA" },
  "wizard.stepper.first_draft": { en: "First draft", pt: "Primeiro rascunho" },
  "wizard.loading": { en: "loading setup…", pt: "carregando a configuração…" },
  "wizard.load_error": { en: "Could not load setup.", pt: "Não foi possível carregar a configuração." },
  "wizard.channel.field_name": { en: "Channel name", pt: "Nome do canal" },
  "wizard.channel.placeholder": { en: "My channel", pt: "Meu canal" },
  "wizard.channel.cta": { en: "Create channel", pt: "Criar canal" },
  "wizard.channel.cta_pending": { en: "creating…", pt: "criando…" },
  "wizard.youtube.cta": { en: "Connect YouTube", pt: "Conectar o YouTube" },
  "wizard.youtube.cta_pending": { en: "starting…", pt: "iniciando…" },
  "wizard.youtube.cta_waiting": { en: "waiting for consent…", pt: "aguardando sua autorização…" },
  "wizard.youtube.retry": { en: "Google sign-in did not finish. Try again.", pt: "O acesso com o Google não foi concluído. Tente de novo." },
  "wizard.youtube.waiting_help": { en: "A Google sign-in tab opened. Approve access there; this page moves on when you're done.", pt: "Uma aba do Google foi aberta. Autorize o acesso lá; esta página segue quando você terminar." },
  "wizard.topics.suggestions_aria": { en: "Suggestions", pt: "Sugestões" },
  "wizard.topics.field_niche": { en: "Niche", pt: "Nicho" },
  "wizard.topics.niche_placeholder": { en: "What your channel is about", pt: "Sobre o que é o seu canal" },
  "wizard.topics.field_topic": { en: "Topic {n}", pt: "Tópico {n}" },
  "wizard.topics.cta": { en: "Save topics", pt: "Salvar tópicos" },
  "wizard.topics.cta_pending": { en: "saving…", pt: "salvando…" },
  "wizard.budgets.field_render": { en: "Drafts per day", pt: "Rascunhos por dia" },
  "wizard.budgets.field_publish": { en: "Publishes per day", pt: "Publicações por dia" },
  "wizard.budgets.cta": { en: "Use these budgets", pt: "Usar estes limites" },
  "wizard.budgets.cta_pending": { en: "saving…", pt: "salvando…" },
  "wizard.consents.cta": { en: "Continue", pt: "Continuar" },
  "wizard.consents.cta_pending": { en: "saving…", pt: "salvando…" },
  "wizard.ideas.retry": { en: "Try again", pt: "Tentar de novo" },
  "wizard.first_draft.cta_queue": { en: "Open review queue", pt: "Abrir fila de revisão" },
  "wizard.first_draft.cta_overview": { en: "Overview", pt: "Visão geral" },
  "ai_data.title": { en: "AI data", pt: "Dados de IA" },
  "ai_data.help": { en: "Before Channels Manager writes drafts for this workspace, review and accept the item below.", pt: "Antes de o Channels Manager escrever rascunhos para este workspace, leia e aceite o item abaixo." },
  "ai_data.stale": { en: "This text changed since you last accepted it. Please review it again.", pt: "Este texto mudou desde a última vez que você aceitou. Leia de novo, por favor." },
  "ai_data.not_now": { en: "Not now", pt: "Agora não" },
  "ai_data.not_now_help": { en: "You can still connect channels and look around; drafts stay paused until you accept.", pt: "Você ainda pode conectar canais e navegar; os rascunhos ficam pausados até você aceitar." },
  "ai_data.done": { en: "Thanks. Drafts can start now.", pt: "Obrigado. Os rascunhos já podem começar." },
  "settings.glu.title": { en: "AI data (Google Limited Use consent)", pt: "Dados de IA (consentimento Google Limited Use)" },
  "settings.glu.on": { en: "Accepted on {date}.", pt: "Aceito em {date}." },
  "settings.glu.off": { en: "Not accepted. Drafts are paused.", pt: "Não aceito. Os rascunhos estão pausados." },
  "settings.glu.exempt": { en: "Internal workspace: not required.", pt: "Workspace interno: não é necessário." },
  "settings.glu.withdraw": { en: "Withdraw consent", pt: "Retirar consentimento" },
  "settings.glu.withdraw_help": { en: "Stops new drafts right away. To delete stored YouTube data, disconnect the channel or request deletion in Channels.", pt: "Interrompe novos rascunhos na hora. Para apagar os dados do YouTube guardados, desconecte o canal ou peça a exclusão em Canais." },
  "settings.glu.give": { en: "Review and accept", pt: "Ler e aceitar" },
};

export function uiLocale(nav?: L6Locale): UiLocale {
  const n = nav ?? (typeof navigator !== "undefined" ? navigator : undefined);
  const fromLanguage = (n?.language ?? "").trim();
  const fromList = (n?.languages && n.languages.length > 0 ? n.languages[0] : "") || "";
  return (fromLanguage || fromList).toLowerCase().startsWith("pt") ? "pt" : "en";
}

/** Wizard string for the browser locale; {name} placeholders filled from vars. */
export function wizardT(key: string, vars?: Record<string, string | number>, loc?: UiLocale): string {
  const e = WIZARD_STRINGS[key];
  let s = e ? e[loc ?? uiLocale()] : key;
  for (const [k, v] of Object.entries(vars ?? {})) s = s.split(`{${k}}`).join(String(v));
  return s;
}
