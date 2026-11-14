/** S2 wizard + banner strings, EN / PT-BR.
 *
 * CMO copy: CM-WIZARD-COPY.md v2 (sha a8b4aa958efd; v1 df45bd814374), keys as CMO
 * suggested, including "ai_data.*", "settings.glu.*", "status.*" and "nav.*".
 * "glu.footer.withdraw" is PO CM's wording (14/11). Buyer-visible copy says
 * "AI data"; "Google Limited Use" stays in code and logs only. Server banners (llm_guard.BLOCK_USER_TEXT) are
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
  "wizard.ideas.help": { en: "Creating one idea for each of your 3 topics and lining up one as your first draft.", pt: "Criando uma ideia para cada um dos seus 3 tópicos e colocando uma delas como seu primeiro rascunho." },
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
  "ai_data.help": { en: "Review and agree to the note below so Channels Manager can write drafts.", pt: "Leia e concorde com o aviso abaixo para o Channels Manager escrever rascunhos." },
  "ai_data.stale": { en: "These terms have changed since you last agreed. Please read them and agree again.", pt: "Estes termos mudaram desde a última vez que você concordou. Leia de novo e concorde outra vez." },
  "ai_data.not_now": { en: "Not now", pt: "Agora não" },
  "ai_data.not_now_help": { en: "You can still connect channels and look around. Drafts stay paused until you agree.", pt: "Você ainda pode conectar canais e navegar. Os rascunhos ficam pausados até você concordar." },
  "ai_data.done": { en: "Thanks. Drafts are no longer paused.", pt: "Obrigado. Os rascunhos não estão mais pausados." },
  // CMO copy v2 (sha a8b4aa958efd): status / nav / AI-data dialog; glu.footer.withdraw = PO CM 14/11.
  "ai_data.cta": { en: "Continue", pt: "Continuar" },
  "settings.glu.dialog_title": { en: "Withdraw AI data agreement?", pt: "Retirar o acordo de dados de IA?" },
  "settings.glu.dialog_help": { en: "Drafts will pause until you agree again. Nothing already published on YouTube changes.", pt: "Os rascunhos ficam pausados até você concordar de novo. Nada que já foi publicado no YouTube muda." },
  "settings.glu.dialog_withdraw": { en: "Withdraw", pt: "Retirar" },
  "settings.glu.dialog_keep": { en: "Keep", pt: "Manter" },
  "glu.footer.withdraw": { en: "Withdraw AI consent", pt: "Retirar consentimento de IA" },
  "status.draft": { en: "Ideas", pt: "Ideias" },
  "status.queued": { en: "Queued", pt: "Na fila" },
  "status.rendering": { en: "Rendering", pt: "Renderizando" },
  // CMO v2 fallback: "Renderizando" clips in the 70px dashboard funnel cell (PT screenshot), so that cell uses "Em render".
  "status.rendering.short": { en: "Rendering", pt: "Em render" },
  "status.rendered": { en: "Rendered", pt: "Renderizado" },
  "status.review": { en: "Review", pt: "Revisão" },
  "status.approved": { en: "Approved", pt: "Aprovado" },
  "status.publishing": { en: "Publishing", pt: "Publicando" },
  "status.published": { en: "Live", pt: "No ar" },
  "status.failed": { en: "Failed", pt: "Falhou" },
  "status.rejected": { en: "Rejected", pt: "Recusado" },
  "status.held": { en: "On hold", pt: "Em espera" },
  "status.paused": { en: "Paused", pt: "Pausado" },
  "status.oauth.connected": { en: "Connected", pt: "Conectado" },
  "status.oauth.expired": { en: "Sign-in expired", pt: "Acesso expirado" },
  "status.oauth.error": { en: "Connection error", pt: "Erro de conexão" },
  "status.oauth.disconnected": { en: "Not connected", pt: "Não conectado" },
  "status.trend.researched": { en: "Researched", pt: "Pesquisado" },
  "status.trend.watching": { en: "Watching", pt: "Em observação" },
  "status.trend.adopted": { en: "Adopted", pt: "Adotado" },
  "status.trend.rejected": { en: "Rejected", pt: "Recusado" },
  "nav.tagline": { en: "YouTube channel studio", pt: "Estúdio de canais YouTube" },
  "nav.overview": { en: "Overview", pt: "Visão geral" },
  "nav.overview.short": { en: "Home", pt: "Início" },
  "nav.review_queue": { en: "Review queue", pt: "Fila de revisão" },
  "nav.review_queue.short": { en: "Queue", pt: "Fila" },
  "nav.channels": { en: "Channels", pt: "Canais" },
  "nav.channels.short": { en: "Channels", pt: "Canais" },
  "nav.onboarding": { en: "Onboarding", pt: "Início" },
  "nav.onboarding.short": { en: "Start", pt: "Começar" },
  "nav.trends": { en: "Trends", pt: "Tendências" },
  "nav.trends.short": { en: "Trends", pt: "Tendências" },
  "nav.profiles": { en: "Render Profiles", pt: "Perfis de render" },
  "nav.profiles.short": { en: "Profiles", pt: "Perfis" },
  "nav.settings": { en: "Settings", pt: "Configurações" },
  "nav.settings.short": { en: "Config", pt: "Ajustes" },
  "nav.sign_out": { en: "Sign out", pt: "Sair" },
  "settings.glu.title": { en: "AI data", pt: "Dados de IA" },
  "settings.glu.on": { en: "Agreed on {date}.", pt: "De acordo desde {date}." },
  "settings.glu.off": { en: "Not agreed. Drafts are paused.", pt: "Ainda não há acordo. Os rascunhos estão pausados." },
  "settings.glu.exempt": { en: "Not required here.", pt: "Não é necessário aqui." },
  "settings.glu.withdraw": { en: "Withdraw", pt: "Retirar" },
  "settings.glu.withdraw_help": { en: "Drafts pause until you agree again. Nothing already published on YouTube changes.", pt: "Os rascunhos ficam pausados até você concordar de novo. Nada que já foi publicado no YouTube muda." },
  "settings.glu.give": { en: "Review and agree", pt: "Ler e concordar" },
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
