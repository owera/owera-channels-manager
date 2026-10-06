"""Craft gates shared across generate / storyboard / thumbnail / publish.

Decolar lock: the first spoken sentence IS the title hook. Frame 0 and the
thumbnail must repeat that claim (or a faithful ≤8-word compression) — never a
second typographic hook or curiosity gap. The opening visual is a concrete
OBJECT on frame0 AND the thumb that echoes that same spoken phrase — not an
abstract diagram, emoji soup, or generic slide.

Spoken-title suffix (shorts): `· <series> <nn>` with series in
Copilot Credits | Agent memory | CrewAI | IA | Local | Claude Code | Shipping | Agent traps.

Series endcard (shorts, after the claim — not frame0): spoken
`Subscribe — next {series} {noun}.` (≤8 words) + chip `Subscribe · {series}`.
Optional micro `same series` only if it fits. No Follow / waitlist /
Cloud / SMY. Endcard hold ≤ ENDCARD_MAX_S (4.0s).

Pré-pattern leftovers are parked via reject. Do not mass-retitle.
"""

from __future__ import annotations

import difflib
import json
import re

from app.services.engines import theme

SERIES_LABELS = (
    "Copilot Credits",
    "Agent memory",
    "CrewAI",
    "IA",
    "Local",
    "Claude Code",
    "Shipping",
    "Agent traps",
)

# Middle-dot + one of the live series labels + integer episode. Case-insensitive
# so a PT "ia 12" still matches the IA label; labels with spaces stay literal.
SPOKEN_TITLE_RE = re.compile(
    r"·\s*(Copilot Credits|Agent memory|CrewAI|IA|Local|Claude Code|Shipping|Agent traps)\s+\d+\b",
    re.IGNORECASE,
)

TITLE_GATE_REASON = (
    "title does not match spoken series pattern "
    "(need '· Copilot Credits|Agent memory|CrewAI|IA|Local|Claude Code|Shipping|Agent traps <nn>') "
    "— pré-pattern leftovers must be parked via reject, not mass-retitled"
)

# Next-claim noun on the series endcard VO. Only these — never invent a CTA.
NEXT_CLAIM_NOUNS = ("trap", "receipt", "bill", "drop")

# Brand defaults when the title has no · series nn.
# OS = Agent memory (Credits wedge killed 2026-09-22); RR = IA.
# Unknown brand keeps the English public fallback. Patterned titles still win.
DEFAULT_SERIES = {"os": "Agent memory", "rr": "IA"}
# CMO option b (2026-10-05): RR IA series uses the PT closer / chip.
# Other series (Agent memory, Shipping, Local, Copilot Credits) stay EN.
PT_ENDCARD_SERIES = frozenset({"IA"})
DEFAULT_SERIES_FALLBACK = "Copilot Credits"
DEFAULT_NOUN = "trap"

# Craft gate: last cta/endcard visual hold. Claim stays on screen; chip is last.
# VM 2026-09-29: the endcard card lasts ≤3.9s INCLUDING its fade (RR endcards
# measured 4.05–4.08s with a 4.0s hold). Hold = 3.9 − 0.12 fade gap = 3.78s.
ENDCARD_CARD_MAX_S = 3.9
ENDCARD_MAX_S = round(ENDCARD_CARD_MAX_S - 0.12, 2)  # 3.78 — the 0.12 is BEAT_GAP_S

# Spoken series endcard. 1 line, ≤8 words. Subscribe is the YT ask — not Follow.
#   Subscribe — next {series} {noun}.
# ALLOWED only as the last spoken line / last visual slot. Mid-short (miolo)
# Subscribe CTAs are stripped — this is not a global invert of Follow/waitlist.
# EN: Subscribe — next {series} {noun}.
# PT (RR IA, CMO option b 2026-10-05): Se inscreve. Próxima armadilha de {series}.
# The PT closer is TWO sentences; endcard_vo_tail_n / strip_mid_subscribe
# treat them as one trailing unit so a mid "Se inscreve" still strips.
_ENDCARD_VO_RE = re.compile(
    r"(?:subscribe\s*[—–-]\s*next\s+.+\s+(?:trap|receipt|bill|drop)"
    r"|se\s+inscreve\.?\s+pr[oó]xima\s+armadilha\s+de\s+.+)\.?\s*$",
    re.IGNORECASE,
)
_ENDCARD_VO_PT_HEAD_RE = re.compile(r"^se\s+inscreve\.?$", re.IGNORECASE)
_ENDCARD_VO_PT_TAIL_RE = re.compile(r"^pr[oó]xima\s+armadilha\s+de\s+.+\.?$", re.IGNORECASE)

# Mute on-screen chip. YPP#5 visual exception — not a Follow button.
#   EN: Subscribe · {series}
#   PT (RR IA): Se inscreve · {series}
_ENDCARD_CHIP_RE = re.compile(
    r"^(?:subscribe|se\s+inscreve)\s*·\s+\S.*$",
    re.IGNORECASE,
)

# Subscribe / Inscreva CTAs. Never added to BANNED_RE (that would strip the
# endcard VO). Use strip_mid_subscribe / contains_subscribe_cta instead.
SUBSCRIBE_CTA_RE = re.compile(
    r"\bsubscribe\b"
    r"|\binscreva(?:-se)?"
    r"|\binscreve(?:-se)?"
    r"|\bse\s+inscreve\b",
    re.IGNORECASE,
)

# Endcard-only bans (card + VO). Broader than BANNED_RE: amanhã / owera.com /
# Cloud / "part 2 coming" / 💸 / neon. Do NOT run this on the whole script —
# "cloud GPU" in a lesson is fine; it must not appear on the endcard.
_ENDCARD_BANNED_PHRASES = (
    r"\bfollow(?:\s+tomorrow)?\b",
    r"\bsiga\b",
    r"\bsigue\b",
    r"\bamanh[ãa]\b",
    r"\bwaitlist\b",
    r"\bowera\.com\b",
    r"\bowera\.ai\b",
    r"\bowera cloud\b",
    r"\bcloud[- ]as[- ](?:a[- ])?(?:product|ready|service)\b",
    r"\bcloud is (?:live|ready)\b",
    r"\bpart\s*2\s+coming\b",
    r"\bsmy\b",
    r"\binstagram\b",
    r"\blinkedin\b",
    r"💸",
    r"\bneon\b",
)
ENDCARD_BANNED_RE = re.compile("|".join(_ENDCARD_BANNED_PHRASES), re.IGNORECASE)

# Builder/confiança shorts: ZERO follow / waitlist / Cloud-as-product / SMY /
# Instagram-LinkedIn CTAs. Matched case-insensitively against folded text.
# Subscribe on the series endcard VO is allowed (English public YT).
_BANNED_PHRASES = (
    r"\bfollow\s+for\s+more\b",
    r"\bfollow\s*→",
    r"\bfollow\s+me\b",
    r"\bsiga\s*[-–—]?\s*amanh[ãa]\b",
    r"\bsiga\s+para\s+mais\b",
    r"\bsiga\s*→",
    r"\bsiga\s+me\b",
    r"\bsiga\b",
    r"\bfollow\b",
    r"\bsigue\b",
    r"\bwaitlist\b",
    r"\bjoin the waitlist\b",
    r"\bowera cloud\b",
    r"\bcloud[- ]as[- ](?:a[- ])?(?:product|ready|service)\b",
    r"\bcloud is (?:live|ready)\b",
    r"\bsmy\b",
    r"\binstagram\b",
    r"\blinkedin\b",
    r"\bowera\.ai\b",
    r"\bcli\.owera\.ai\b",
    r"\bowera\.com\b",
)

BANNED_RE = re.compile("|".join(_BANNED_PHRASES), re.IGNORECASE)

# Idea / script / metadata prompt addenda — enforced in code, not only theme_prompt.
CRAFT_RULES_SHORT = (
    "CRAFT (enforced): first spoken sentence IS the title hook. "
    "Frame 0 and the thumbnail show a concrete object that echoes that phrase "
    "(receipt, terminal, bill, the named tool) — not a diagram, emoji soup, or generic slide. "
    "No Follow/Siga/'follow for more'/Siga-amanhã/Follow-tomorrow. No waitlist, "
    "owera.com, Owera Cloud-as-product, SMY, 'part 2 coming', Instagram or LinkedIn. "
    "Subscribe is ALLOWED only as the LAST spoken line of the series endcard "
    "('Subscribe — next {series} {noun}.') + chip 'Subscribe · {series}'. FORBIDDEN in the "
    "mid-short / body / miolo: no Subscribe text, VO, or chip before the endcard. "
    "(noun = trap|receipt|bill|drop; optional micro 'same series' only if it fits). "
    "Title suffix must be '· <series> <nn>' with series one of: "
    + " | ".join(SERIES_LABELS) + ". "
    "NEVER put a money value (a currency symbol followed by a number) in the TITLE — "
    "the publish gate rejects it. Where a money amount is spoken in the VO, keep it "
    "as numerals — NEVER spell it out in words. Credits/IA pre-approve needs "
    "spoken phrase + concrete noun + ·nn."
)

STOPWORDS = {
    "the", "a", "an", "to", "of", "in", "on", "for", "and", "or", "your", "you",
    "is", "it", "that", "this", "with", "at", "as", "be", "by", "from", "my",
    "o", "a", "os", "as", "de", "da", "do", "em", "um", "uma", "e", "que", "seu",
    "sua", "no", "na", "pra", "pro",
}


def spoken_title_ok(title: str | None) -> bool:
    return bool(SPOKEN_TITLE_RE.search(title or ""))


# Credits / IA pre-approve lock (new queue only — never mass-retitle the catalog).
_CREDITS_IA_SERIES_RE = re.compile(
    r"·\s*(Copilot Credits|IA)\s+\d+\b",
    re.IGNORECASE,
)
_SPELLED_DOLLAR_RE = re.compile(
    r"\b(?:(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)"
    r"(?:[\s-]+(?:one|two|three|four|five|six|seven|eight|nine))?|"
    r"(?:eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|"
    r"eighteen|nineteen|ten|one|two|three|four|five|six|seven|eight|nine))"
    r"\s+dollars?\b",
    re.IGNORECASE,
)
TITLE_BANNED_REASON = (
    "title carries banned YT CTA (Follow / Follow-tomorrow / Siga / "
    "Siga-amanhã / waitlist / owera.com) — new-queue Credits/IA lock"
)
TITLE_CLAIM_REASON = (
    "Credits/IA title must carry (a) a useful spoken first phrase, "
    "(b) a concrete noun (currency values are not allowed in titles), and (c) · series nn — "
    "do not mass-retitle the catalog; park leftovers via reject"
)


def dollar_numerals(text: str | None) -> list[str]:
    """Dollar amounts kept as numerals (e.g. $79). Never spelled-out words."""
    out: list[str] = []
    for m in _AMOUNT_RE.finditer(text or ""):
        raw = m.group(0) or ""
        n = (m.group(1) or m.group(2) or "").replace(",", ".")
        if not n:
            continue
        folded = theme.fold(raw)
        if "$" in raw or folded.startswith("r$"):
            out.append("$" + n)
        else:
            out.append(n)
    return out


def spelled_dollar_amount(text: str | None) -> bool:
    """True when copy expands a stake to words (e.g. 'seventy-nine dollars')."""
    return bool(_SPELLED_DOLLAR_RE.search(text or ""))


def preserves_dollar_numerals(spoken: str | None, shown: str | None) -> bool:
    """Frame0 / thumb must keep the same $N as the spoken claim — never expand."""
    amounts = dollar_numerals(spoken)
    if not amounts:
        return not spelled_dollar_amount(shown)
    if spelled_dollar_amount(shown):
        return False
    shown_l = theme.fold(shown or "")
    for a in amounts:
        token = theme.fold(a)
        bare = token.lstrip("$")
        if token in shown_l or (bare and bare in shown_l):
            continue
        return False
    return True


def _useful_spoken_phrase(title: str | None) -> bool:
    head = ((title or "").split("·", 1)[0] or "").strip()
    if len(head) < 12:
        return False
    words = [w for w in re.findall(r"[A-Za-zÀ-ÿ0-9$]+", head)
             if theme.fold(w) not in STOPWORDS]
    return len(words) >= 3


def _has_claim_stake(title: str | None) -> bool:
    """(b) concrete noun grounded in the spoken head.

    2026-09-28: a $N numeral no longer satisfies (b) on its own — currency values
    in titles are now a publish-gate reject (CURRENCY_TITLE_PATTERN), so the
    noun path is the only way to pass. The noun check is unchanged."""
    head = ((title or "").split("·", 1)[0] or "").strip()
    if spelled_dollar_amount(head):
        return False
    obj = opening_object(head)
    label = (obj.get("label") or "").strip().upper()
    return bool(label) and label != "OBJECT"


def credits_ia_title_ok(title: str | None) -> bool:
    """Pre-approve lock for Credits/IA series (new queue). Other series unchanged."""
    raw = title or ""
    if not _CREDITS_IA_SERIES_RE.search(raw):
        return True  # not a Credits/IA patterned title — leave to spoken_title_ok
    if contains_banned(raw):
        return False
    if not _useful_spoken_phrase(raw):
        return False
    if not _has_claim_stake(raw):
        return False
    return True


def title_gate_reason(title: str | None, content_format: str | None = "short") -> str | None:
    """None = allowed to leave review toward publish. Longs are exempt (no series suffix).

    New-queue only: does not rewrite / mass-retitle the catalog. Credits/IA
    titles also need a useful spoken phrase + ($N | concrete noun); Follow /
    waitlist / owera.com CTAs fail.
    """
    if (content_format or "short") == "long":
        return None
    if contains_banned(title):
        return TITLE_BANNED_REASON
    if not spoken_title_ok(title):
        return TITLE_GATE_REASON
    if not credits_ia_title_ok(title):
        return TITLE_CLAIM_REASON
    return None


def first_spoken_sentence(script: str | None) -> str:
    text = (script or "").strip()
    if not text:
        return ""
    parts = re.split(r"(?<=[.!?…])\s+", text)
    return (parts[0] if parts else text).strip()


def spoken_hook_source(title: str | None, script: str | None, subject: str | None = None) -> str:
    """The claim frame0/thumb must echo: title before '·', else first spoken sentence."""
    raw = (title or "").strip()
    if raw:
        head = raw.split("·", 1)[0].strip()
        if head:
            return head
    return first_spoken_sentence(script) or (subject or "").strip()


def compress_claim(text: str | None, max_words: int = 8) -> str:
    words = (text or "").split()
    return " ".join(words[:max_words]).strip().rstrip(".!?…,;:").strip()


# ---------------------------------------------------------------------------
# On-screen overlay copy (frame0 hook + thumbnail). Unlike ``compress_claim``
# (endcard chip: clip + rstrip punctuation), the overlay keeps the claim as
# written: sentence punctuation . , ? ! and EVERY digit, including a leading
# one ("16GB rodou…", "48 tok/s. Sem, 11."). 2026-09-28 (#1354 / #1363):
# frame0 showed only sentence 1 with its period stripped, and the thumb LLM
# flattened "Chat routed to Maya. Prod paged Lee." into one run-on line.
# ---------------------------------------------------------------------------

OVERLAY_MAX_WORDS = 12          # wrap-safe ceiling (same as the frame0 clip)
_OVERLAY_SENT_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")
_OVERLAY_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")
_OVERLAY_MARKS = ".,?!"
_OVERLAY_TAIL = ",;:—–- "


def title_head(text: str | None) -> str:
    """Spoken claim of a title/subject: the text before `` · <series> <nn>``."""
    return ((text or "").split("·", 1)[0] or "").strip()


def _overlay_norm(text: str | None) -> str:
    return " ".join(theme.fold(text or "").split())


def script_opens_with(script: str | None, head: str | None) -> bool:
    """True when the narration starts with ``head`` (case/diacritic-folded,
    whitespace-collapsed; the head's terminal punctuation is optional)."""
    h = _overlay_norm(head).rstrip(".!?… ")
    return bool(h) and _overlay_norm(script).startswith(h)


def overlay_hook_source(title: str | None = None, script: str | None = None,
                        subject: str | None = None) -> str:
    """The claim frame0 / thumb show — the whole title head, not just sentence 1.

    Head = title (else subject) before `` · Series N`` (the series episode
    number is intentionally not overlay copy). With a script: use the head
    when the narration opens on it (two-sentence heads like "Chat routed to
    Maya. Prod paged Lee." stay two sentences), else the first spoken sentence
    (unchanged Decolar rule for unpatterned subjects).
    """
    head = title_head(title) or title_head(subject)
    if script is None:
        return head
    if head and script_opens_with(script, head):
        return head
    return first_spoken_sentence(script) or head


def overlay_claim(text: str | None, max_words: int = OVERLAY_MAX_WORDS) -> str:
    """Overlay copy: the claim verbatim (punctuation + digits kept) up to
    ``max_words``. Longer text keeps whole sentences that fit; a single
    over-long sentence is word-clipped and only a dangling ``, ; : —`` is
    trimmed. Never strips . ? ! or digits."""
    s = " ".join((text or "").split())
    if not s:
        return ""
    words = s.split(" ")
    if len(words) <= max_words:
        return s.rstrip(_OVERLAY_TAIL) or s
    kept: list[str] = []
    n = 0
    for sent in _OVERLAY_SENT_SPLIT_RE.split(s):
        w = len(sent.split())
        if n + w > max_words:
            break
        kept.append(sent)
        n += w
    if kept:
        return " ".join(kept).rstrip(_OVERLAY_TAIL)
    return " ".join(words[:max_words]).rstrip(_OVERLAY_TAIL)


def overlay_numbers(text: str | None) -> list[str]:
    """Every number in the text (``32B`` → 32, ``tok/s. Sem, 11`` → 11, ``1.5x`` → 1.5)."""
    return _OVERLAY_NUMBER_RE.findall(text or "")


def _overlay_marks(text: str | None) -> dict[str, int]:
    # Decimal/thousand separators inside numbers are digits, not punctuation.
    bare = _OVERLAY_NUMBER_RE.sub("0", text or "")
    return {m: bare.count(m) for m in _OVERLAY_MARKS}


def overlay_preserves_claim(claim: str | None, shown: str | None) -> bool:
    """True when overlay copy keeps every number and every . , ? ! of the claim.

    Used to reject an LLM thumb compression that flattens sentences into a
    run-on or drops a digit (including a leading one). Case is free.
    """
    shown_nums = overlay_numbers(shown)
    pool = list(shown_nums)
    for num in overlay_numbers(claim):
        if num in pool:
            pool.remove(num)
        else:
            return False
    want, got = _overlay_marks(claim), _overlay_marks(shown)
    return all(got[m] >= want[m] for m in _OVERLAY_MARKS)


def claim_aligned(hook: str | None, spoken: str | None) -> bool:
    """True when hook is the same claim (echo/compression), not a second slogan."""
    h = theme.fold(hook or "")
    s = theme.fold(spoken or "")
    if not h or not s:
        return False
    if h == s or h in s or s.startswith(h):
        return True
    hw = {w for w in h.split() if w not in STOPWORDS}
    sw = {w for w in s.split() if w not in STOPWORDS}
    if not hw or not sw:
        return False
    return bool(hw & sw)


# Designer lock (YPP move 1): 1 object = the noun of the spoken first phrase.
# Kind picks the widget (bill UI / receipt / GPU meter / named app / terminal).
# GPU before $ so "VRAM ate $58" is a meter, never 💸. Copilot/credits/$ → bill.
_OBJECT_HINTS = (
    (r"\b(chrome|chromium)\b", "CHROME", "app"),
    (r"\bdiscord\b", "DISCORD", "app"),
    (r"\b(slack|vscode|notion|figma|whatsapp|telegram)\b", None, "app"),
    (r"\bollama\b", "OLLAMA", "terminal"),
    (r"\b(gpu|vram|rtx|egpu|e-gpu|4090|3080|placa|batch)\b", "GPU", "gpu"),
    (r"\b(terminal|cli|shell|stdout|stderr|prompt|comando|command|local)\b",
     "TERMINAL", "terminal"),
    (r"\b(config|\.env\b|yaml|toml)\b", "CONFIG", "terminal"),
    (r"(?:r\$|\$)\s*\d", "BILL", "bill"),
    (r"\b(billed|billing|bill|cobr(?:ou|ar|anca)|credits?|creditos?|copilot)\b",
     "BILL", "bill"),
    (r"\b(recibo|receipt|invoice|fatura)\b", "RECEIPT", "receipt"),
    (r"\b(api|paid|pago|produto|product)\b", "RECEIPT", "receipt"),
    (r"\b(mcp|integra(?:cao|coes)|connector|conector)\b", "MCP", "object"),
    (r"\b(rag|retriev|chunk|embed|rerank)\b", "RAG", "object"),
    (r"\b(memor(?:y|ia)|forget|amnesia|session|sessao)\b", "MEMORY", "object"),
    (r"\bcrew(?:ai)?\b", "CREW", "object"),
    (r"\bclaude\b", "CLAUDE", "terminal"),
)

_AMOUNT_RE = re.compile(
    r"(?:r\$|\$)\s*(\d+(?:[.,]\d{1,2})?)|"
    r"(\d+(?:[.,]\d{1,2})?)\s*(?:usd|credits?|creditos?)",
    re.IGNORECASE,
)
_VRAM_RE = re.compile(r"(\d+)\s*(gb|%|percent)", re.IGNORECASE)

# Hard-FAIL tokens: never the opening object (Designer + VM lock).
FORBIDDEN_OBJECT_EMOJI = ("💸", "🔥")
RAINBOW_HEX = ("#a36bff", "#ff5bb0")

# Extra folds dropped when mining a fallback noun (title grammar, not the object).
_OBJECT_STOP = STOPWORDS | {
    "about", "how", "why", "what", "when", "title", "video", "watch", "here",
    "there", "then", "than", "into", "over", "after", "before", "porque",
    "quando", "onde", "como", "para", "mais", "muito", "ainda", "voce",
    "its", "are", "was", "were", "been", "have", "has", "had", "will", "can",
    "not", "nao", "sem", "only", "just", "very",
}

_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\U00002700-\U000027BF"
    "\U00002600-\U000026FF"
    "\U0000FE00-\U0000FE0F"
    "\U0001F1E0-\U0001F1FF"
    "]+"
)


def _spoken_amount(text: str | None) -> str:
    m = _AMOUNT_RE.search(text or "")
    if not m:
        return ""
    n = (m.group(1) or m.group(2) or "").replace(",", ".")
    raw = theme.fold(m.group(0))
    if "$" in (m.group(0) or "") or raw.startswith("r$"):
        return "$" + n
    return n


def _vram_fill(text: str | None) -> tuple[str, str]:
    """(meter width %, caption) from a spoken GB/% — else a mute 70% meter."""
    m = _VRAM_RE.search(text or "")
    if not m:
        return "70%", "VRAM"
    n, unit = m.group(1), theme.fold(m.group(2))
    if unit == "gb":
        return "70%", n + " GB"
    return f"{min(100, int(n))}%", n + "%"


def opening_object(text: str | None) -> dict:
    """Concrete object for frame0 + thumb, grounded in the spoken first phrase.

    Returns label / kind / amount / prompt / app. Kind is the widget
    (bill, receipt, gpu, app, terminal, object) — not an OS/RR brand split.
    """
    raw = text or ""
    folded = theme.fold(raw)
    spec = {"label": "OBJECT", "kind": "object", "amount": "", "prompt": "",
            "app": "", "meter": "70%", "meter_caption": "VRAM"}
    for pat, label, kind in _OBJECT_HINTS:
        m = re.search(pat, folded)
        if not m:
            continue
        spec["kind"] = kind
        spec["label"] = (label or m.group(0)).upper()[:16]
        break
    else:
        words = [w for w in re.findall(r"[A-Za-zÀ-ÿ0-9$]+", raw)
                 if theme.fold(w) not in _OBJECT_STOP and len(w) >= 3]
        if words:
            long_enough = [w for w in words if len(w) >= 4]
            spec["label"] = (long_enough[0] if long_enough else words[0]).upper()[:16]
    spec["amount"] = _spoken_amount(raw)
    if spec["kind"] == "app":
        spec["app"] = spec["label"]
    if spec["kind"] == "terminal":
        spec["prompt"] = "$ ollama run" if "ollama" in folded else "$"
    if spec["kind"] == "gpu":
        spec["meter"], spec["meter_caption"] = _vram_fill(raw)
    return spec


def coerce_object(obj) -> dict:
    """Normalize a label-string / partial dict into a full opening_object spec."""
    if isinstance(obj, dict) and obj.get("kind") and obj.get("label"):
        base = opening_object(obj.get("label"))
        base.update({k: obj[k] for k in obj if obj[k] not in (None, "")})
        return base
    if isinstance(obj, dict) and obj.get("label"):
        guessed = opening_object(str(obj["label"]))
        guessed["label"] = str(obj["label"])
        return guessed
    if isinstance(obj, str) and obj.strip():
        return opening_object(obj)
    return opening_object("")


def object_echoes(label: str | None, spoken: str | None) -> bool:
    """True when chrome label is grounded in the spoken first phrase."""
    if not label or not spoken:
        return False
    derived = opening_object(spoken)["label"]
    if theme.fold(label) == theme.fold(derived):
        return True
    return claim_aligned(label, spoken)


def object_markup(obj: dict | str | None) -> str:
    """HTML widget for the opening object. Never emoji. Never covers hook type."""
    spec = coerce_object(obj)
    kind = spec["kind"]
    label = theme.esc(spec["label"])
    amount = theme.esc(spec.get("amount") or "")
    attrs = (f'class="obj obj-{theme.esc(kind)}" data-object="{label}" '
             f'data-kind="{theme.esc(kind)}"')
    if kind == "bill":
        big = amount or label
        return (f'<div {attrs}><div class="obj-head">INVOICE</div>'
                f'<div class="obj-amt">{big}</div>'
                f'<div class="obj-line"></div><div class="obj-line obj-line-s"></div></div>')
    if kind == "receipt":
        extra = f'<div class="obj-amt">{amount}</div>' if amount else ""
        return (f'<div {attrs}><div class="obj-head">RECEIPT</div>'
                f'<div class="obj-stub">POST /v1/charges</div>{extra}'
                f'<div class="obj-tear"></div></div>')
    if kind == "gpu":
        fill = theme.esc(spec.get("meter") or "70%")
        cap = theme.esc(spec.get("meter_caption") or "VRAM")
        return (f'<div {attrs}><div class="obj-chip"><i></i><i></i><i></i></div>'
                f'<div class="obj-meter"><b style="width:{fill}"></b></div>'
                f'<div class="obj-cap">{cap}</div></div>')
    if kind == "app":
        mark = theme.esc((spec.get("app") or spec["label"])[:2])
        return (f'<div {attrs}><div class="obj-icon">{mark}</div>'
                f'<div class="obj-name">{label}</div></div>')
    if kind == "terminal":
        prompt = theme.esc(spec.get("prompt") or "$")
        return (f'<div {attrs}><div class="obj-tb"><i></i><i></i><i></i></div>'
                f'<div class="obj-prompt">{prompt}</div></div>')
    return (f'<div {attrs}><div class="obj-token">{label}</div></div>')


# ---------------------------------------------------------------------------
# Contrast split-card (Designer council 2026-09-29, P0): frame0 ≡ thumb for the
# Agent memory (OS) and IA / Local (RR) series. Top card = what the chat / the
# "Com" side said, bottom card = what production / the "Sem" side did, with an
# ✕ stamp. Every value comes verbatim from the title head (punctuation and
# digits kept, nothing invented). Never applied when the operator provided a
# thumbnail (thumb_source=provided, #39). Replaces the first-word object chip
# and the O ring on that card only.
# ---------------------------------------------------------------------------
SPLIT_CARD_SERIES = frozenset({"Agent memory", "IA", "Local"})
_SPLIT_LABEL_PAIRS = (("chat", "prod"), ("com", "sem"))
_SPLIT_SENT_RE = re.compile(r"(?<=[.!?])\s+")
_SPLIT_IA_RE = re.compile(r"^(?P<top>.+?,?)\s+(?P<bot>não é engenharia[.!?]?)$", re.IGNORECASE)


def _split_first(sentence: str) -> tuple[str, str]:
    parts = sentence.split(None, 1)
    return (parts[0], parts[1]) if len(parts) == 2 else ("", sentence)


def contrast_split(title: str | None, *, provided_thumb: bool = False) -> dict | None:
    """Split-card spec for a patterned title, or None (normal hook card).

    Only titles that carry a real `` · <series> <nn>`` suffix in
    SPLIT_CARD_SERIES (no brand default). Two-sentence heads split into
    top/bottom; ``Chat … / Prod …`` and ``Com … / Sem …`` put the first word
    in the card label. A one-sentence IA head ``X, não é engenharia.`` splits
    at the verdict. ``top_label + top`` / ``bottom_label + bottom`` rebuild
    the head verbatim (whole claim on frame0 and thumb).
    """
    if provided_thumb:
        return None
    m = SPOKEN_TITLE_RE.search(title or "")
    if not m:
        return None
    series = _canonical_series(m.group(1))
    if series not in SPLIT_CARD_SERIES:
        return None
    head = " ".join(((title or "").split("·", 1)[0] or "").split())
    if not head:
        return None
    sents = [x for x in _SPLIT_SENT_RE.split(head) if x.strip()]
    spec = None
    if len(sents) == 2:
        a, b = sents
        la, ra = _split_first(a)
        lb, rb = _split_first(b)
        pair = (theme.fold(la).strip(",;:"), theme.fold(lb).strip(",;:"))
        if pair in _SPLIT_LABEL_PAIRS and ra and rb:
            spec = {"top_label": la, "top": ra, "bottom_label": lb, "bottom": rb}
        else:
            spec = {"top_label": "", "top": a, "bottom_label": "", "bottom": b}
    elif len(sents) == 1:
        mi = _SPLIT_IA_RE.match(head)
        if mi and mi.group("top").strip():
            spec = {"top_label": "", "top": mi.group("top").strip(),
                    "bottom_label": "", "bottom": mi.group("bot")}
    if not spec:
        return None
    spec["series"] = series
    spec["head"] = head
    return spec


def split_card_text(spec: dict | None) -> str:
    """The claim the split card shows, rebuilt from its parts (for tests/gates)."""
    if not spec:
        return ""
    top = " ".join(x for x in (spec.get("top_label"), spec.get("top")) if x)
    bot = " ".join(x for x in (spec.get("bottom_label"), spec.get("bottom")) if x)
    return f"{top} {bot}".strip()


# Heavy (900) sans glyph advance ≈ 0.62em incl. the -0.02em tracking; used to
# keep the LONGEST word on one line (RR 2026-09-30: "engenha/ria.",
# "escanea/do" broke mid-word at 2–3× size with overflow-wrap:anywhere).
SPLIT_CHAR_EM = 0.62
SPLIT_CAP_EM = 0.74  # capitals/digits are wider ("PARALLEL", "NVIDIA", "2048")
SPLIT_TEXT_W_FRAC = 0.76  # card inner width (stage 88% − card padding 12%)


def split_font_px(text: str | None, width: int, height: int) -> int:
    """Card type size: 2–3× the old hook, fit to the card, and small enough
    that the longest word fits one line (shrink, never break inside a word)."""
    n = max(1, len(" ".join((text or "").split())))
    avail_w = width * 0.80
    avail_h = height * 0.26
    px = (avail_w * avail_h / (0.58 * 1.05 * n)) ** 0.5
    lo, hi = int(width * 0.10), int(width * 0.205)
    px = max(lo, min(hi, px))
    ems = [sum(SPLIT_CAP_EM if (c.isupper() or c.isdigit()) else SPLIT_CHAR_EM for c in w)
           for w in re.split(r"[\s_/]+", text or "") if w]
    fit = (width * SPLIT_TEXT_W_FRAC) / max(ems or [SPLIT_CHAR_EM])
    return int(max(int(width * 0.05), min(px, fit)))


def _split_text_html(text: str) -> str:
    """Escaped card text; line breaks only between words or after "_" / "/"
    inside an identifier (<wbr>), never inside a plain word."""
    from app.services.engines.theme import esc
    return re.sub(r"([_/])", r"\1<wbr>", esc(text))


# Fit (Designer council 06/10, FINDINGS #1): 9 of 22 split cards on air ran
# past 72% (#1408 text to 88.6%) because each card picked its own size against
# 26% of the height and the two were stacked with no global fit. Now both
# outputs (thumb + frame0) lay the pair out in ONE box, 9%–72% of the frame
# height, 8% padding on the left and 11% on the right (CoS 06/10: the right
# edge stays ≤ 89% of the width, clear of the Shorts action-button column),
# and the two cards share one font size that shrinks step-wise until the
# estimated block height fits the box.
SPLIT_BOX_TOP_FRAC = 0.09
SPLIT_BOX_BOTTOM_FRAC = 0.72
SPLIT_PAD_LEFT_FRAC = 0.08       # left padding of the box (of width)
SPLIT_PAD_RIGHT_FRAC = 0.11      # right padding (of width): right edge ≤ 89% (action column)
SPLIT_GAP_FRAC = 0.025           # gap between the cards (of height)
SPLIT_CARD_PAD_X_FRAC = 0.05     # card inner padding left/right (of width)
SPLIT_CARD_PAD_T_FRAC = 0.035    # card inner padding top (of width)
SPLIT_CARD_PAD_B_FRAC = 0.04     # card inner padding bottom (of width)
SPLIT_CARD_MIN_H_FRAC = 0.24     # visual weight for short cards (of height)
SPLIT_BORDER_PX = 4
SPLIT_LABEL_FRAC = 44 / 1080     # label type size (of width; 44px at 1080w)
SPLIT_LABEL_LH = 1.2
SPLIT_LABEL_GAP_EM = 0.25
SPLIT_LINE_H = 1.04
SPLIT_SPACE_EM = 0.30
SPLIT_FONT_STEP = 0.94           # shrink factor per step
SPLIT_FONT_MIN_FRAC = 0.035      # floor (of width)


def _split_word_em(word: str) -> float:
    return sum(SPLIT_CAP_EM if (c.isupper() or c.isdigit()) else SPLIT_CHAR_EM for c in word)


def split_text_lines(text: str | None, px: float, inner_w: float) -> int:
    """Greedy word-wrap line count at ``px`` in ``inner_w`` (identifiers may
    break after "_" / "/", like the <wbr> in the markup)."""
    words = [w for w in re.split(r"\s+", text or "") if w]
    if not words:
        return 0
    lines, cur = 1, 0.0
    for w in words:
        parts = [p for p in re.split(r"(?<=[_/])", w) if p]
        for k, part in enumerate(parts):
            wpx = _split_word_em(part) * px
            sep = 0.0 if (cur == 0 or k > 0) else SPLIT_SPACE_EM * px
            if cur and cur + sep + wpx > inner_w + 1e-6:
                lines += 1
                cur = wpx
            else:
                cur += sep + wpx
    return lines


def split_box(width: int, height: int) -> dict:
    """The 9%–72% × 8%-padded box both outputs place the pair in (px)."""
    top = height * SPLIT_BOX_TOP_FRAC
    return {"top": top, "height": height * SPLIT_BOX_BOTTOM_FRAC - top,
            "left": width * SPLIT_PAD_LEFT_FRAC,
            "width": width * (1 - SPLIT_PAD_LEFT_FRAC - SPLIT_PAD_RIGHT_FRAC)}


def split_card_height(label: str | None, text: str | None, px: float,
                      width: int, height: int) -> float:
    """Estimated rendered card height (px) at type size ``px``."""
    box = split_box(width, height)
    inner_w = box["width"] - 2 * width * SPLIT_CARD_PAD_X_FRAC - 2 * SPLIT_BORDER_PX
    body = split_text_lines(text, px, inner_w) * px * SPLIT_LINE_H
    if label:
        lab = width * SPLIT_LABEL_FRAC
        body += lab * SPLIT_LABEL_LH + lab * SPLIT_LABEL_GAP_EM
    h = body + width * (SPLIT_CARD_PAD_T_FRAC + SPLIT_CARD_PAD_B_FRAC) + 2 * SPLIT_BORDER_PX
    return max(h, height * SPLIT_CARD_MIN_H_FRAC)


def split_fit(spec: dict | None, width: int, height: int) -> dict:
    """One shared type size for both cards, shrunk step-wise until the pair
    (top card + gap + bottom card) fits the 9%–72% box.

    {"px", "top_h", "bottom_h", "block_h", "box", "fits", "steps"}"""
    spec = spec or {}
    top_l, top_t = spec.get("top_label") or "", spec.get("top") or ""
    bot_l, bot_t = spec.get("bottom_label") or "", spec.get("bottom") or ""
    box = split_box(width, height)
    inner_w = box["width"] - 2 * width * SPLIT_CARD_PAD_X_FRAC - 2 * SPLIT_BORDER_PX
    # Start at the smaller of the two per-card sizes, capped so the longest
    # word of either card fits one line (shrink, never break inside a word).
    ems = [_split_word_em(p) for t in (top_t, bot_t)
           for w in re.split(r"[\s]+", t) if w for p in re.split(r"(?<=[_/])", w) if p]
    px = float(min(split_font_px(top_t, width, height), split_font_px(bot_t, width, height),
                   inner_w / max(ems or [SPLIT_CHAR_EM])))
    floor = width * SPLIT_FONT_MIN_FRAC
    gap = height * SPLIT_GAP_FRAC
    steps = 0
    while True:
        th = split_card_height(top_l, top_t, px, width, height)
        bh = split_card_height(bot_l, bot_t, px, width, height)
        block = th + gap + bh
        if block <= box["height"] + 1e-6 or px * SPLIT_FONT_STEP < floor:
            break
        px *= SPLIT_FONT_STEP
        steps += 1
    return {"px": int(px), "top_h": th, "bottom_h": bh, "block_h": block, "box": box,
            "fits": block <= box["height"] + 1e-6, "steps": steps}


def split_card_markup(spec: dict, width: int, height: int) -> str:
    """Shared frame0/thumb markup (same HTML → frame0 ≡ thumb). The container
    (thumbnail #stage / storyboard .hook[data-split]) is the 9%–72% box with
    8% left / 11% right padding; sizes are explicit px so the fit estimate holds."""
    from app.services.engines.theme import esc

    fit = split_fit(spec, width, height)
    px = fit["px"]
    lab_px = int(round(width * SPLIT_LABEL_FRAC))
    pad = (f"{int(round(width * SPLIT_CARD_PAD_T_FRAC))}px {int(round(width * SPLIT_CARD_PAD_X_FRAC))}px "
           f"{int(round(width * SPLIT_CARD_PAD_B_FRAC))}px")
    min_h = int(height * SPLIT_CARD_MIN_H_FRAC)

    def card(cls, label, text, stamp):
        lab = (f'<div class="sc-label" style="font-size:{lab_px}px">' + esc(label) + "</div>") if label else ""
        x = '<div class="sc-x">✕</div>' if stamp else ""
        return (f'<div style="min-height:{min_h}px;padding:{pad}" class="sc {cls}">' + x + lab +
                '<div class="sc-text" style="font-size:' + str(px) + 'px">' +
                _split_text_html(text) + "</div></div>")

    return (f'<div class="split" data-font="{px}" style="gap:{int(height * SPLIT_GAP_FRAC)}px">' +
            card("sc-top", spec.get("top_label") or "", spec.get("top") or "", False) +
            card("sc-bot", spec.get("bottom_label") or "", spec.get("bottom") or "", True) +
            "</div>")


SPLIT_CSS = (
    ".split{display:flex;flex-direction:column;width:100%;height:100%;"
    "box-sizing:border-box;justify-content:flex-start}"
    ".sc{position:relative;flex:0 0 auto;box-sizing:border-box;border-radius:26px;"
    "text-align:left;display:flex;flex-direction:column;justify-content:center;"
    "background:rgba(255,255,255,.06);border:4px solid var(--split-top,#9aa4b2)}"
    ".sc-bot{border-color:var(--split-bot,#e5484d);background:rgba(229,72,77,.10)}"
    ".sc-label{font-family:ui-monospace,Menlo,Consolas,monospace;font-weight:800;"
    "letter-spacing:.14em;text-transform:uppercase;font-size:44px;line-height:1.2;opacity:.78;"
    "margin-bottom:.25em}"
    ".sc-text{font-weight:900;line-height:1.04;letter-spacing:-.02em;"
    "overflow-wrap:normal;word-break:normal;hyphens:manual}"
    ".sc-x{position:absolute;right:5%;top:6%;font-size:96px;font-weight:900;line-height:1;"
    "color:var(--split-bot,#e5484d);transform:rotate(-8deg)}"
)


# Shared widget CSS — storyboard + thumbnail. Object sits ABOVE/BESIDE the hook
# type; it must not overlay line 1. No emoji, no rainbow bar.
OBJECT_CSS = (
    ".obj{display:flex;flex-direction:column;align-items:stretch;justify-content:center;"
    "gap:.28em;width:100%;box-sizing:border-box;border:3px solid var(--obj-accent,#888);"
    "background:rgba(0,0,0,.45);color:#fff;border-radius:10px;padding:.55em .7em;text-align:left}"
    ".obj-head,.obj-cap,.obj-name,.obj-stub,.obj-prompt{font-family:var(--obj-mono,ui-monospace,Menlo,Consolas,monospace);"
    "font-size:clamp(14px,2.1vw,22px);letter-spacing:.1em;opacity:.85;font-weight:700}"
    ".obj-amt{font-weight:900;font-size:clamp(42px,8vw,92px);line-height:.95;letter-spacing:-2px}"
    ".obj-line{height:6px;background:rgba(255,255,255,.22);border-radius:3px;width:88%}"
    ".obj-line-s{width:54%}"
    ".obj-tear{height:10px;margin-top:.2em;background:repeating-linear-gradient(90deg,"
    "transparent 0 8px,rgba(255,255,255,.25) 8px 10px)}"
    ".obj-chip{display:flex;gap:6px;align-items:center}"
    ".obj-chip i{display:block;width:22px;height:22px;border:2px solid var(--obj-accent,#888);"
    "background:rgba(255,255,255,.08)}"
    ".obj-meter{height:14px;background:rgba(255,255,255,.12);border-radius:7px;overflow:hidden}"
    ".obj-meter b{display:block;height:100%;background:var(--obj-accent,#888)}"
    ".obj-icon{width:72px;height:72px;border-radius:16px;border:3px solid var(--obj-accent,#888);"
    "display:flex;align-items:center;justify-content:center;font-weight:900;font-size:28px}"
    ".obj-tb{display:flex;gap:6px}"
    ".obj-tb i{display:block;width:10px;height:10px;border-radius:50%;background:var(--obj-accent,#888)}"
    ".obj-token{font-weight:900;font-size:clamp(28px,5.4vw,56px);letter-spacing:.08em}"
    ".obj-prompt{font-size:clamp(16px,2.6vw,28px)}"
)


def object_hard_fail(html: str | None, hook: str | None, spoken: str | None) -> str | None:
    """None = pass. Reason = Designer hard-FAIL (typography-only / emoji / rainbow / drift)."""
    h = html or ""
    if any(tok in h for tok in FORBIDDEN_OBJECT_EMOJI):
        return "emoji-as-object"
    if any(c in h.lower() for c in RAINBOW_HEX):
        return "neon-bar rainbow"
    if 'class="obj ' not in h or "data-kind=" not in h:
        return "typography-only"
    if spoken and hook and not claim_aligned(hook, spoken):
        return "punchline ≠ spoken first phrase"
    return None


def emoji_first(text: str | None) -> bool:
    raw = (text or "").lstrip()
    return bool(raw) and bool(_EMOJI_RE.match(raw))


def emoji_soup(text: str | None) -> bool:
    """Two+ emoji, or the whole string is emoji — generic 💸 punch, not an object."""
    raw = text or ""
    bits = _EMOJI_RE.findall(raw)
    if len(bits) >= 2:
        return True
    rest = _EMOJI_RE.sub("", raw).strip()
    return bool(bits) and not rest


def scan_banned(text: str | None) -> list[str]:
    if not text:
        return []
    return [m.group(0) for m in BANNED_RE.finditer(text)]


def contains_banned(text: str | None) -> bool:
    return bool(scan_banned(text))


def is_endcard_vo(text: str | None) -> bool:
    """True when text is the series endcard spoken closer (EN one-liner or
    the full PT two-sentence closer)."""
    return bool(_ENDCARD_VO_RE.search((text or "").strip()))


def is_endcard_vo_sentence(text: str | None) -> bool:
    """True when a single sentence is (part of) the series endcard VO —
    the EN closer, the PT head 'Se inscreve.', or the PT tail
    'Próxima armadilha de …'."""
    t = (text or "").strip()
    if not t:
        return False
    if is_endcard_vo(t):
        return True
    return bool(_ENDCARD_VO_PT_HEAD_RE.fullmatch(t) or _ENDCARD_VO_PT_TAIL_RE.fullmatch(t))


def endcard_vo_tail_n(parts) -> int:
    """How many trailing script sentences are the series endcard VO (0/1/2).
    EN = 1; PT = 2 ('Se inscreve.' + 'Próxima armadilha de …')."""
    ps = [p.strip() for p in (parts or []) if str(p or "").strip()]
    if not ps:
        return 0
    if is_endcard_vo(ps[-1]):
        return 1
    if (len(ps) >= 2 and _ENDCARD_VO_PT_HEAD_RE.fullmatch(ps[-2])
            and _ENDCARD_VO_PT_TAIL_RE.fullmatch(ps[-1])):
        return 2
    return 0


def is_endcard_chip(text: str | None) -> bool:
    """True when text is the series endcard chip (EN or PT)."""
    return bool(_ENDCARD_CHIP_RE.fullmatch((text or "").strip()))


def contains_subscribe_cta(text: str | None) -> bool:
    return bool(SUBSCRIBE_CTA_RE.search(text or ""))


def strip_subscribe_cta(text: str | None) -> str:
    """Drop subscribe-CTA sentences. Does not protect the endcard VO — use
    strip_mid_subscribe for scripts that may already carry the closer."""
    raw = (text or "").strip()
    if not raw or not contains_subscribe_cta(raw):
        return raw
    parts = re.split(r"(?<=[.!?…])\s+", raw)
    kept = [p for p in parts if p.strip() and not contains_subscribe_cta(p)]
    if kept:
        return " ".join(kept).strip()
    cleaned = SUBSCRIBE_CTA_RE.sub("", raw)
    return re.sub(r"\s{2,}", " ", cleaned).strip(" -—,;:→")


def strip_mid_subscribe(script: str | None) -> str:
    """Ban Subscribe/Inscreve in the miolo. Keep only a trailing endcard VO
    (EN one-liner or the PT two-sentence closer), if present."""
    raw = (script or "").strip()
    if not raw:
        return ""
    parts = [p.strip() for p in re.split(r"(?<=[.!?…])\s+", raw) if p.strip()]
    if not parts:
        return ""
    keep_from = len(parts) - endcard_vo_tail_n(parts)
    out: list[str] = []
    for i, p in enumerate(parts):
        if i >= keep_from:
            out.append(p)
            continue
        if contains_subscribe_cta(p):
            continue
        out.append(p)
    return " ".join(out).strip()


def mid_body_has_subscribe(script: str | None) -> bool:
    """True if Subscribe/Inscreve appears before a trailing endcard VO (or anywhere if none)."""
    raw = (script or "").strip()
    if not raw or not contains_subscribe_cta(raw):
        return False
    parts = [p.strip() for p in re.split(r"(?<=[.!?…])\s+", raw) if p.strip()]
    n = endcard_vo_tail_n(parts)
    if n:
        return any(contains_subscribe_cta(p) for p in parts[:-n])
    return True


def strip_banned(text: str | None) -> str:
    """Drop sentences that carry a banned CTA. Keeps the rest (never raises)."""
    raw = (text or "").strip()
    if not raw or not contains_banned(raw):
        return raw
    parts = re.split(r"(?<=[.!?…])\s+", raw)
    kept = [p for p in parts if p.strip() and not contains_banned(p)]
    if kept:
        return " ".join(kept).strip()
    # Whole blob was the CTA — strip the matching phrases in place.
    cleaned = BANNED_RE.sub("", raw)
    return re.sub(r"\s{2,}", " ", cleaned).strip(" -—,;:→")


def brand_of(slug: str | None, name: str | None = None, channel_id=None) -> str | None:
    """os = Owera Software B&W; rr = Rodrigo Recio burgundy. None = legacy neon.

    Delegates to ``theme.infer_brand`` so storyboard/thumbnail/render share one map:
    slug/name tokens win; live-fleet ids 1/2 are the fallback.
    """
    return theme.infer_brand(channel_id, slug, name)


# ---------------------------------------------------------------------------
# Video Maker craft gate (Shorts only) — A object 0–3s / B miolo ≤3s (hook exempt) / C spam
# ---------------------------------------------------------------------------

OBJECT_BEAT_TYPES = frozenset({"code", "command", "diagram", "compare", "stat"})
TYPOGRAPHY_ONLY_TYPES = frozenset({"hook", "statement"})
CTA_TYPES = frozenset({"cta", "endcard"})
OPENING_WINDOW_S = 3.0
# Gate B measures the *visual hold* (`dur`) of mid (miolo) beats only —
# AFTER the opening hook, BEFORE cta/endcard. Hook / beat type=hook is EXEMPT
# (Gate A already covers object 0–3s). Cue-to-cue is 0.12s longer because of
# the inter-beat fade (_GAP) — never count the fade as hold (v1258). Rodrigo
# YES via CoS 2026-09-15: Gate B HARD at 3.0s for new-queue Shorts miolo
# (closes the ~5–5.8s command-beat auto-approve hole). MUST equal
# storyboard._MID_MAX.
# VM 2026-09-29 (Rodrigo P0 b): a mid card lasts ≤3.0s INCLUDING its fade —
# card-to-card intervals measured 3.05–3.20s with a 3.0s hold + 0.12s fade
# blank. So the card (hold + BEAT_GAP_S) is capped at MID_CARD_MAX_S and the
# visual hold at MID_CARD_MAX_S − BEAT_GAP_S = 2.88s. Same for the endcard
# (ENDCARD_CARD_MAX_S 3.9 → hold 3.78s).
MID_CARD_MAX_S = 3.0

# Must equal storyboard._GAP. Duplicated so craft does not import storyboard
# (storyboard already imports craft).
BEAT_GAP_S = 0.12
# RR publish check 2026-09-30: with the real fade a 2.88s hold still measured
# 3.05–3.08s card-to-card (blank midpoint to blank midpoint). Content hold is
# now ≤2.80s so hold + fade ≤ 3.0s. MUST equal storyboard._MID_MAX.
MID_BEAT_MAX_S = 2.80                                        # visual hold (current)
MID_FADE_S = round(MID_CARD_MAX_S - MID_BEAT_MAX_S, 2)       # 0.20 measured fade
INCL_FADE_MID_BEAT_MAX_S = round(MID_CARD_MAX_S - BEAT_GAP_S, 2)  # 2.88 (card_incl_fade boards)
CTA_CARD_MAX_S = ENDCARD_CARD_MAX_S                          # 3.9 incl. fade
CTA_BEAT_MAX_S = ENDCARD_MAX_S                               # 3.78 visual hold
# creation_config["beat_timing"] marker: boards rendered by the aligner that
# caps the card INCLUDING its fade. Stored boards without it were rendered
# under the old 3.0s / 4.0s HOLD caps (CoS RR publish check 2026-09-29 rated
# their 3.05–3.20s cards WARN, not FAIL), so the review/publish gate
# re-checks them with the legacy hold caps instead of auto-rejecting the
# approved inventory. Every new render is checked strictly.
BEAT_TIMING_INCL_FADE = "card_incl_fade"
# New renders (2026-09-30): mid hold ≤2.80s. Boards stamped card_incl_fade
# (rendered 29 Sep, some already in review/approved) keep their 2.88s cap —
# same marker trick, nothing already rendered is rejected retroactively.
BEAT_TIMING_HOLD_280 = "card_hold_280"
BEAT_TIMING_CURRENT = BEAT_TIMING_HOLD_280
LEGACY_MID_BEAT_MAX_S = 3.0
LEGACY_CTA_BEAT_MAX_S = 4.0
# RR hook pace (P1 d, council 2026-09-29): on the RR channel the claim is
# ≤8 words, fully spoken by 3.0s, and the first cut lands by 2.5s (PULSE:
# RR first cut 4.12–5.83s, claim spoken 3.72–5.66s on 5/7). New RR renders
# carry creation_config["hook_pace"] = {"version": HOOK_PACE_V1, ...}; boards
# without the marker (approved inventory, OS) are never checked (same marker
# trick as beat_timing), so nothing already approved is rejected at publish.
HOOK_PACE_V1 = "rr_v1"
HOOK_PACE_BRANDS = frozenset({"rr"})
HOOK_CLAIM_MAX_WORDS = 8
HOOK_CLAIM_SPOKEN_BY_S = 3.0
HOOK_FIRST_CUT_BY_S = 2.5
STATEMENT_MAX_SHORTS = 1
LIST_MAX_PER_SHORT = 1
LIST_MAX_ITEMS = 3
LIST_STAGGER_MAX_S = 0.6
LIST_REVEAL_PAD_S = 0.8  # matches storyboard.render_list win = dur - 0.8

_BEAT_SNAP_KEYS = (
    "type", "start", "dur", "cue", "text", "sub", "object", "prop", "emoji",
    "items", "value", "unit", "label", "title", "lines", "command", "nodes",
    # On-screen copy the repeated-card check compares (term_define / compare /
    # command output / quote attribution). Older snapshots lack these keys —
    # screen_text_key then sees less copy and never over-matches (fail-open).
    "term", "definition", "left", "right", "output", "attribution",
)
# Repeated card (VM 2026-09-29: RR #1340/#1349/#1370/#1376 held one card
# ~6.2s back-to-back; #1354/#1357/#1372 replayed cards 1-3 as 4-6).
# Freezedetect misses it because the inter-beat fade blink resets the freeze.
# Neighbouring beats FAIL on normalized on-screen text equality, or
# near-equality (same digits + ratio >= REPEAT_NEAR_RATIO). A later
# non-adjacent replay of a mid card FAILs on exact normalized equality.
REPEAT_NEAR_RATIO = 0.9
# Subscribe CTA regex lives above (never in BANNED_RE). Gate C uses it via
# _subscribe_hits — legal only on the trailing cta/endcard series.
_BEATS_SCRIPT_RE = re.compile(
    r'<script type="application/json" id="storyboard-beats">(.*?)</script>',
    re.DOTALL,
)
_BEAT_DIV_RE = re.compile(
    r'class="beat ([a-z_]+)"[^>]*data-start="([0-9.]+)"[^>]*data-duration="([0-9.]+)"',
)
_HAS_WORD_RE = re.compile(r"[A-Za-z0-9À-ÿ]")


def _as_dict(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def _emoji_only(text: str | None) -> bool:
    """True when the string has no letters/digits — emoji/punct is not a Decolar object."""
    s = (text or "").strip()
    return (not s) or (_HAS_WORD_RE.search(s) is None)


def hook_object(beat: dict | None) -> str:
    """Non-empty Decolar prop on a hook. Emoji is rejected (does not count as object)."""
    if not isinstance(beat, dict):
        return ""
    raw = beat.get("object") or beat.get("prop") or ""
    if not isinstance(raw, str):
        raw = str(raw or "")
    raw = raw.strip()
    if _emoji_only(raw):
        return ""
    return raw


def snapshot_beats(beats) -> list[dict]:
    """Gate-relevant subset (JSON-safe). Drops renderer-only keys."""
    out = []
    for b in beats or []:
        if not isinstance(b, dict):
            continue
        snap = {k: b[k] for k in _BEAT_SNAP_KEYS if k in b}
        out.append(snap)
    return out


def beats_from_html(html: str | None) -> list[dict]:
    """Prefer the compose-embedded JSON snapshot; scrape data-* divs otherwise."""
    raw = html or ""
    m = _BEATS_SCRIPT_RE.search(raw)
    if m:
        try:
            data = json.loads(m.group(1).replace("<\\/", "</"))
            if isinstance(data, list):
                return [b for b in data if isinstance(b, dict)]
        except (TypeError, ValueError):
            pass
    scraped = []
    for m in _BEAT_DIV_RE.finditer(raw):
        scraped.append({
            "type": m.group(1),
            "start": float(m.group(2)),
            "dur": float(m.group(3)),
        })
    return scraped


def _cue_start(beat: dict) -> float:
    try:
        return float(beat.get("start") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _cue_span(beats: list[dict], i: int) -> float:
    """Visual hold of beat i — the quantity align_storyboard caps at _MID_MAX.

    Prefer stored ``dur`` (GSAP tween length). Fall back to next.start − start
    minus the 0.12s inter-beat fade, then to raw next−start, then to dur.
    Never count the fade as hold: that is how a capped quote failed B
    when cue-span included the 0.12s fade (v1258).
    """
    try:
        dur = max(0.0, float(beats[i].get("dur") or 0.0))
    except (TypeError, ValueError):
        dur = 0.0
    if dur > 0:
        return dur
    start = _cue_start(beats[i])
    if i + 1 < len(beats):
        nxt = beats[i + 1].get("start")
        if nxt is not None:
            try:
                span = float(nxt) - start
                return max(0.0, span - BEAT_GAP_S)
            except (TypeError, ValueError):
                pass
    return dur


def _list_items(beat: dict) -> list:
    items = beat.get("items")
    return items if isinstance(items, list) else []


def _list_stagger(beat: dict, span: float) -> float:
    n = len(_list_items(beat))
    if n <= 0:
        return 0.0
    win = max(0.0, span - LIST_REVEAL_PAD_S)
    return min(win / n, LIST_STAGGER_MAX_S)


def _endcard_series_start(board: list[dict]) -> int:
    """Index of the trailing cta/endcard series. len(board) if the last beat is not one."""
    i = len(board)
    while i > 0 and (board[i - 1].get("type") or "") in CTA_TYPES:
        i -= 1
    return i


def _visible_copy(beat: dict) -> str:
    """On-screen copy only (not cue). Subscribe on a mid card is a CTA, not a sync word."""
    parts: list[str] = []
    for k in ("text", "sub", "title", "label"):
        v = beat.get(k)
        if isinstance(v, str) and v.strip():
            parts.append(v)
    for it in _list_items(beat):
        if isinstance(it, dict):
            t = it.get("text")
            if t:
                parts.append(str(t))
        elif it:
            parts.append(str(it))
    for side in ("left", "right"):
        col = beat.get(side)
        if not isinstance(col, dict):
            continue
        if col.get("title"):
            parts.append(str(col["title"]))
        for x in col.get("items") or []:
            parts.append(str(x))
    return " ".join(parts)


def beat_screen_text(beat: dict | None) -> str:
    """Every piece of on-screen copy a beat renders (not the cue, not emoji)."""
    if not isinstance(beat, dict):
        return ""
    parts: list[str] = []
    for k in ("value", "unit", "text", "title", "label", "sub", "term",
              "definition", "command", "attribution"):
        v = beat.get(k)
        if isinstance(v, (str, int, float)) and not isinstance(v, bool):
            if str(v).strip():
                parts.append(str(v))
    for k in ("lines", "output"):
        for x in beat.get(k) or [] if isinstance(beat.get(k), list) else []:
            if str(x).strip():
                parts.append(str(x))
    for it in _list_items(beat):
        if isinstance(it, dict):
            if it.get("text"):
                parts.append(str(it["text"]))
        elif it:
            parts.append(str(it))
    for side in ("left", "right"):
        col = beat.get(side)
        if not isinstance(col, dict):
            continue
        if col.get("title"):
            parts.append(str(col["title"]))
        for x in col.get("items") or []:
            parts.append(str(x))
    for n in beat.get("nodes") or [] if isinstance(beat.get("nodes"), list) else []:
        if isinstance(n, dict) and n.get("label"):
            parts.append(str(n["label"]))
    return " ".join(parts)


def screen_text_key(beat: dict | None) -> str:
    """Normalized on-screen copy: folded case/accents, punctuation dropped."""
    return " ".join(re.findall(r"[a-z0-9]+", theme.fold(beat_screen_text(beat))))


def screen_text_near(a: str | None, b: str | None) -> bool:
    """Equal, or near-equal (same digits, SequenceMatcher >= REPEAT_NEAR_RATIO)."""
    a, b = a or "", b or ""
    if not a or not b:
        return False
    if a == b:
        return True
    if re.findall(r"\d+", a) != re.findall(r"\d+", b):
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= REPEAT_NEAR_RATIO


_REPEAT_EXEMPT_REPLAY = frozenset({"hook"}) | CTA_TYPES
# Text cards whose copy can be a fragment of a neighbour (RR 2026-09-30:
# #1382 card 13 "obedece" = the tail of card 12 "A prefill manda. O n_batch
# obedece."). Object cards (stat/compare/…) legitimately reuse a word.
FRAGMENT_TYPES = frozenset({"hook", "quote", "statement"})


def screen_text_fragment(a: str | None, b: str | None) -> bool:
    """True when one normalized card text is a contiguous word run (prefix,
    suffix or middle) of the other, and they are not equal."""
    ta, tb = (a or "").split(), (b or "").split()
    if not ta or not tb or ta == tb:
        return False
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    m = len(short)
    return any(long_[k:k + m] == short for k in range(len(long_) - m + 1))


def repeated_card_reason(board: list[dict], i: int,
                         keys: list[str] | None = None) -> str | None:
    """Why beat i repeats a card (None = it does not).

    Adjacent: beat i shows the same (or near-same) on-screen text as beat i-1
    — any type, except two trailing cta/endcard beats. Replay: a mid beat
    (not hook/cta) shows exactly the text of an earlier non-adjacent mid beat.
    Beats with no comparable copy (legacy scraped snapshots) never match.
    """
    if not (0 < i < len(board)):
        return None
    if keys is None:
        keys = [screen_text_key(b) for b in board]
    cur = keys[i]
    if not cur:
        return None
    t_cur = board[i].get("type") or "?"
    t_prev = board[i - 1].get("type") or "?"
    shown = beat_screen_text(board[i])[:60]
    if screen_text_near(keys[i - 1], cur) and not (
            t_cur in CTA_TYPES and t_prev in CTA_TYPES):
        return (f"beat[{i - 1}]→beat[{i}] ({t_prev}→{t_cur}) show the same card "
                f"{shown!r} back-to-back")
    if (t_cur in FRAGMENT_TYPES and t_prev in FRAGMENT_TYPES
            and screen_text_fragment(keys[i - 1], cur)):
        return (f"beat[{i - 1}]→beat[{i}] ({t_prev}→{t_cur}) card {shown!r} is a "
                "fragment of its neighbour (partial-text echo)")
    if t_cur in _REPEAT_EXEMPT_REPLAY:
        return None
    for j in range(0, i - 1):
        if (board[j].get("type") or "") in _REPEAT_EXEMPT_REPLAY:
            continue
        if keys[j] and keys[j] == cur:
            return (f"beat[{i}] type={t_cur} replays beat[{j}] card {shown!r}")
    return None


def repeated_card_hits(beats) -> list[str]:
    """All repeated-card findings on a board (see repeated_card_reason)."""
    board = [b for b in (beats or []) if isinstance(b, dict)]
    keys = [screen_text_key(b) for b in board]
    hits = []
    for i in range(1, len(board)):
        r = repeated_card_reason(board, i, keys)
        if r:
            hits.append(r)
    return hits


def _subscribe_hits(text: str) -> list[str]:
    return [m.group(0) for m in SUBSCRIBE_CTA_RE.finditer(text or "")]


def _echoes_narration(beat: dict) -> bool:
    """True when on-screen copy is just the cue / spoken words (no added information)."""
    cue = theme.fold(str(beat.get("cue") or ""))
    text = theme.fold(str(beat.get("text") or ""))
    if not text:
        parts = []
        for it in _list_items(beat):
            if isinstance(it, dict):
                parts.append(str(it.get("text") or ""))
            else:
                parts.append(str(it))
        text = theme.fold(" ".join(parts))
    if not text or not cue:
        return False
    tw = {w for w in text.split() if w not in STOPWORDS}
    cw = {w for w in cue.split() if w not in STOPWORDS}
    if not tw:
        return False
    return tw <= cw or text in cue or cue in text


# TTS spoken-text normalization (RR 2026-09-30): edge-tts reads "_" as
# "underline" (n_batch → "N underline batch"). Only the audio input changes;
# the displayed text (title, cards, thumb) keeps the identifier, and the
# WordBoundary words are merged back to the displayed identifier so cue
# alignment and claim_spoken_end match the printed claim.
_TTS_IDENT_RE = re.compile(r"(?<![\w])([A-Za-z0-9]+(?:_+[A-Za-z0-9]+)+)(?![\w])")


def _ident_parts(ident: str) -> list[str]:
    return [p for p in ident.split("_") if p]


# Brand pronunciation lexicon (OS 2026-09-30, #1385 Gate B): edge-tts
# en-US voices say "Owera's" as "Ora's/Aura's" (whisper small/medium on the
# #1385 master), dropping the "we" syllable. Only the audio input is
# respelled; cards/titles keep "Owera". "Oh-weh-ruh" was picked because
# faster-whisper small AND medium transcribe it as "Oweru"/"Oweru's" (3
# syllables, o-WEH-ruh) in both the bare and the possessive form, and edge-tts
# returns it as ONE WordBoundary token. URLs/handles (owera.com, @owera) are
# left untouched. Portuguese voices read "Owera" phonetically, so the lexicon
# is English-only (voice None → applied; "pt-*" → skipped).
# EN (#53): Owera → Oh-weh-ruh (English voices only; pt reads Owera fine).
# PT (CMO option b 2026-10-05): IA → I-A so pt-BR TTS does not expand to
# "Inteligência Artificial" / "trepe". Cards and titles keep the letters "IA".
# Match is case-sensitive on "IA" so the Portuguese verb "ia" is untouched.
TTS_LEXICON = {"owera": "Oh-weh-ruh"}  # EN alias kept for older imports/tests
TTS_LEXICON_EN = TTS_LEXICON
TTS_LEXICON_PT = {"IA": "I-A"}
_TTS_LEX_RE = re.compile(
    r"(?<![\w./@\-])(" + "|".join(TTS_LEXICON_EN) + r")(?=(?:['\u2019]s)?(?![\w/@\-]|\.\w))",
    re.IGNORECASE)
_TTS_LEX_PT_RE = re.compile(r"(?<![\w./@\-])(IA)(?![\w/@\-])")


def _lexicon_applies(voice: str | None) -> bool:
    """True for English voices (Owera respelling). Kept for older callers."""
    return not (voice or "").lower().startswith("pt")


def _lexicon_for(voice: str | None) -> dict:
    """Active lexicon for this voice: EN Owera, or PT IA acronym."""
    if (voice or "").lower().startswith("pt"):
        return TTS_LEXICON_PT
    return TTS_LEXICON_EN


def tts_spoken_text(text: str | None, voice: str | None = None) -> str:
    """Text for edge-tts: identifiers with underscores are spoken as words
    (n_batch → "n batch"), EN brand Owera → "Oh-weh-ruh", PT acronym IA →
    "I-A" (cards stay written "IA")."""
    def _say(m):
        ident = m.group(1)
        spoken = " ".join(_ident_parts(ident))
        return spoken.lower() if ident.isupper() else spoken
    out = _TTS_IDENT_RE.sub(_say, text or "")
    lex = _lexicon_for(voice)
    if lex is TTS_LEXICON_PT:
        out = _TTS_LEX_PT_RE.sub(lambda m: lex[m.group(1)], out)
    elif lex:
        out = _TTS_LEX_RE.sub(lambda m: lex[m.group(1).lower()], out)
    return out


def _remerge_lexicon(out: list[dict], text: str | None,
                    lex: dict | None = None, case_sensitive: bool = False) -> list[dict]:
    """Map respelled brand/acronym words back to the displayed word. edge-tts
    may return the respelling as one token ("Oh-weh-ruh's" / "I-A") or
    several ("Oh", "weh", "ruh's" / "I", "A"); consecutive tokens whose
    letters add up to the respelling (+ optional possessive) collapse into
    one word with the display text."""
    lex = TTS_LEXICON_EN if lex is None else lex
    rx = _TTS_LEX_PT_RE if case_sensitive else _TTS_LEX_RE
    k = 0
    for m in rx.finditer(text or ""):
        disp = m.group(1)
        spoken = lex[disp] if case_sensitive else lex[disp.lower()]
        tail = (text or "")[m.end():m.end() + 2]
        poss = tail if tail[:1] in ("'", "\u2019") and tail[1:2].lower() == "s" else ""
        target = _alnum_fold(spoken + poss)
        for j in range(k, len(out)):
            acc, n = "", 0
            while j + n < len(out) and len(acc) < len(target):
                acc += _alnum_fold(out[j + n].get("text") or "")
                n += 1
                if not target.startswith(acc):
                    break
            if acc == target and n:
                start = float(out[j].get("start") or 0.0)
                end = _word_end(out[j + n - 1]) or start
                out[j:j + n] = [{**out[j], "text": disp + poss, "start": start,
                                 "dur": round(max(0.0, end - start), 4)}]
                k = j + 1
                break
    return out


def remerge_tts_words(words, text: str | None, voice: str | None = None) -> list[dict]:
    """Merge the WordBoundary words of a normalized identifier back into one
    word carrying the displayed identifier ("n" + "batch" → "n_batch"), with
    the first word's start and the last word's end, and map lexicon
    respellings back to the brand word ("Oh-weh-ruh's" → "Owera's").
    Unmatched → unchanged."""
    out = [dict(w) for w in (words or []) if isinstance(w, dict)]
    idents = [m.group(1) for m in _TTS_IDENT_RE.finditer(text or "")]
    k = 0
    for ident in idents:
        parts = [_alnum_fold(p) for p in _ident_parts(ident)]
        n = len(parts)
        for j in range(k, len(out)):
            if _alnum_fold(out[j].get("text") or "") == "".join(parts):
                out[j] = {**out[j], "text": ident}  # service kept it as one word
                k = j + 1
                break
            if j > len(out) - n:
                continue
            if [_alnum_fold(out[j + x].get("text") or "") for x in range(n)] == parts:
                start = float(out[j].get("start") or 0.0)
                end = _word_end(out[j + n - 1]) or start
                out[j:j + n] = [{**out[j], "text": ident, "start": start,
                                 "dur": round(max(0.0, end - start), 4)}]
                k = j + 1
                break
    lex = _lexicon_for(voice)
    if lex is TTS_LEXICON_PT:
        out = _remerge_lexicon(out, text, lex=lex, case_sensitive=True)
    elif lex:
        out = _remerge_lexicon(out, text, lex=lex)
    return out


def _alnum_fold(text: str | None) -> str:
    return "".join(ch for ch in theme.fold(text or "") if ch.isalnum())


def claim_word_count(text: str | None) -> int:
    """Words in the on-screen/spoken claim (tokens that carry a letter/digit)."""
    return sum(1 for w in (text or "").split() if any(ch.isalnum() for ch in w))


def _word_end(w) -> float | None:
    try:
        return round(float(w.get("start") or 0.0) + float(w.get("dur") or 0.0), 3)
    except (TypeError, ValueError, AttributeError):
        return None


def claim_match(claim: str | None, words) -> str | None:
    """How ``claim_spoken_end`` measured the claim: "exact" (claim letters
    matched the TTS word boundaries), "fallback" (tokens differ: the n-th
    spoken word was used — a measurement estimate, not a craft fact) or None
    (no word timings: unmeasurable)."""
    target = _alnum_fold(claim)
    words = [w for w in (words or []) if isinstance(w, dict)]
    if not target or not words:
        return None
    acc = ""
    for w in words:
        acc += _alnum_fold(w.get("text") or w.get("word") or "")
        if not target.startswith(acc[: len(target)]):
            return "fallback"
        if len(acc) >= len(target):
            return "exact"
    return "fallback"


def next_speech_start(claim: str | None, words) -> float | None:
    """Start (s) of the first TTS word after the spoken ``claim`` (None when
    the claim is not matched exactly or nothing follows it)."""
    target = _alnum_fold(claim)
    words = [w for w in (words or []) if isinstance(w, dict)]
    if not target or not words:
        return None
    acc = ""
    for k, w in enumerate(words):
        acc += _alnum_fold(w.get("text") or w.get("word") or "")
        if not target.startswith(acc[: len(target)]):
            return None
        if len(acc) >= len(target):
            if k + 1 >= len(words):
                return None
            try:
                return round(float(words[k + 1].get("start") or 0.0), 3)
            except (TypeError, ValueError):
                return None
    return None


# Video length (P0 2026-10-03): duration was narration + 0.6s, but edge-tts
# mp3s end with ~0.9s of silence after the last word, so the endcard had to
# hold over ~1.5s of dead air and the card before it could not reach it
# (cards hold ≤2.8s) — the sync composer had no feasible tail on most real
# boards. The video now ends END_TAIL_S after the last spoken word (never
# longer than before; _mux cuts the audio to the video, so only trailing
# silence is dropped).
END_TAIL_S = 0.5
VIDEO_MIN_S = 4.0


def video_duration(narr_secs: float, words=None) -> float:
    """Composition/video length for a narration of ``narr_secs`` seconds."""
    dur = float(narr_secs) + 0.6
    ends = [e for e in (_word_end(w) for w in (words or []) if isinstance(w, dict))
            if isinstance(e, (int, float))]
    if ends:
        dur = min(dur, max(ends) + END_TAIL_S)
    return max(VIDEO_MIN_S, round(dur, 2))


# RR hook join (P0 2026-10-03, CoS decision): the pt-BR voice pauses ~0.94s
# after a full stop, so with a claim ending at ~2.7s the next sentence starts
# at ~3.65s — the first card (≤1.1s lead) cannot cut by 2.5s. When that
# happens, the claim's terminal "."/"!" is spoken as ";" (TTS input only —
# script, cards and title keep the period), which shortens the pause to
# ~0.28s. Recorded as creation_config["hook_join"]; HOOK_JOIN_ENABLED is the
# kill switch.
HOOK_JOIN_ENABLED = True
HOOK_JOIN_NEXT_BY_S = 3.6   # HOOK_FIRST_CUT_BY_S + SYNC_LEAD_MAX_S
_FIRST_SENT_RE = re.compile(r"^(\s*.+?)([.!])(\s+)(?=\S)", re.S)


def first_sentence(script: str | None) -> str:
    m = re.match(r"^\s*(.+?[.!?…])(?=\s|$)", script or "", re.S)
    return (m.group(1) if m else (script or "")).strip()


def hook_join_text(script: str | None) -> str | None:
    """TTS input with the first sentence's terminal "."/"!" spoken as ";"
    (None when the first sentence doesn't end in one or nothing follows)."""
    m = _FIRST_SENT_RE.match(script or "")
    if not m or "?" in m.group(1) or "…" in m.group(1):
        return None
    return m.group(1) + ";" + m.group(3) + (script or "")[m.end():]


def needs_hook_join(script: str | None, words, brand: str | None,
                    content_format: str | None = "short") -> bool:
    """True when an RR short's next sentence starts too late for the first cut."""
    if not HOOK_JOIN_ENABLED or (content_format or "short") == "long":
        return False
    if (brand or "") not in HOOK_PACE_BRANDS or hook_join_text(script) is None:
        return False
    nxt = next_speech_start(first_sentence(script), words)
    return nxt is not None and nxt > HOOK_JOIN_NEXT_BY_S + 1e-6


def claim_spoken_end(claim: str | None, words) -> float | None:
    """End time (s) of the last TTS word of ``claim``. Matches the folded
    letters/digits of the edge-tts word boundaries against the claim; when the
    TTS tokens differ (e.g. a number read out), falls back to the end of the
    n-th spoken word (n = claim words). None only without word timings."""
    target = _alnum_fold(claim)
    words = [w for w in (words or []) if isinstance(w, dict)]
    if not target or not words:
        return None
    acc = ""
    for w in words:
        acc += _alnum_fold(w.get("text") or w.get("word") or "")
        if not target.startswith(acc[: len(target)]):
            break
        if len(acc) >= len(target):
            return _word_end(w)
    n = claim_word_count(claim)
    return _word_end(words[min(n, len(words)) - 1]) if n else None


def hook_pace_marker(beats, words, brand: str | None,
                     content_format: str | None = "short") -> dict | None:
    """creation_config["hook_pace"] for a new render, or None (not in scope)."""
    if (content_format or "short") == "long" or (brand or "") not in HOOK_PACE_BRANDS:
        return None
    board = [b for b in (beats or []) if isinstance(b, dict)]
    if not board or (board[0].get("type") or "") != "hook":
        return None
    claim = board[0].get("text") or ""
    return {"version": HOOK_PACE_V1, "claim_words": claim_word_count(claim),
            "claim_spoken_end": claim_spoken_end(claim, words),
            # measurement provenance (CoS 2026-10-03): exact | fallback | None
            "claim_match": claim_match(claim, words)}


def hook_pace_hits(board, hook_pace: dict | None) -> list[str]:
    """Gate B hook-pace hits for a marked board (empty when unmarked/in spec)."""
    if not isinstance(hook_pace, dict) or hook_pace.get("version") != HOOK_PACE_V1:
        return []
    if not board or (board[0].get("type") or "") != "hook":
        return []
    hits = []
    n = claim_word_count(board[0].get("text") or "")
    if n > HOOK_CLAIM_MAX_WORDS:
        hits.append(f"claim {n} words (max {HOOK_CLAIM_MAX_WORDS})")
    end = hook_pace.get("claim_spoken_end")
    if isinstance(end, (int, float)) and float(end) > HOOK_CLAIM_SPOKEN_BY_S + 1e-6:
        hits.append(f"claim spoken by {float(end):.2f}s (max {HOOK_CLAIM_SPOKEN_BY_S:.1f}s)")
    if len(board) > 1:
        cut = _cue_start(board[1])
        if cut > HOOK_FIRST_CUT_BY_S + 1e-6:
            hits.append(f"first cut at {cut:.2f}s (max {HOOK_FIRST_CUT_BY_S:.1f}s)")
    return hits


# Card ↔ speech sync (CoS 2026-09-30): a card may LEAD its own speech by at
# most SYNC_LEAD_MAX_S, and must NEVER appear after its words begin. New
# renders carry creation_config["card_sync"] = {"version": SYNC_V1, "notes":
# [...]} (one note per card: start, speech_start, lead, status); Gate B fails
# a "late" or "early" note. Unmarked boards (older renders) are not checked.
SYNC_V1 = "sync_v1"
SYNC_LEAD_MAX_S = 1.1
SYNC_TOL_S = 0.05
_SYNC_TOK_RE = re.compile(r"[a-z0-9]+")


def _sync_toks(text) -> list[str]:
    return _SYNC_TOK_RE.findall(theme.fold(str(text or "")))


def _find_run(stream: list[str], needle: list[str], start: int) -> int:
    m = len(needle)
    if not m:
        return -1
    for p in range(max(0, start), len(stream) - m + 1):
        if stream[p:p + m] == needle:
            return p
    return -1


def _speech_stream(words) -> tuple[list[str], list[float]]:
    toks, starts = [], []
    for w in words or []:
        if not isinstance(w, dict):
            continue
        try:
            t0 = float(w.get("start") or 0.0)
        except (TypeError, ValueError):
            continue
        for t in _sync_toks(w.get("text") or w.get("word")):
            toks.append(t)
            starts.append(t0)
    return toks, starts


def _card_speech_matches(board, words) -> list[tuple[float | None, list[float]]]:
    """Per card: (primary speech start, every occurrence of its words at or
    after the previous card's anchor). The primary match is the historical
    sync_v1 one (first occurrence after the cursor); the alternatives let the
    notes tell a repeated phrase apart from a mistimed card."""
    toks, starts = _speech_stream(words)
    out: list[tuple[float | None, list[float]]] = []
    cursor, floor = 0, 0
    for i, b in enumerate(board):
        typ = b.get("type") or ""
        if i == 0 and typ == "hook":
            ht = _sync_toks(b.get("text") or b.get("cue"))
            if ht and toks[:len(ht)] == ht:
                cursor = len(ht)
            out.append((0.0, [0.0]))
            continue
        cands = []
        if typ in ("quote", "statement"):
            cands.append(_sync_toks(b.get("text")))
        cands.append(_sync_toks(b.get("cue")))
        pos, alts = -1, []
        for c in cands:
            p = _find_run(toks, c, floor)
            while p >= 0:
                alts.append(starts[p])
                p = _find_run(toks, c, p + 1)
        for c in cands:
            pos = _find_run(toks, c, cursor)
            if pos >= 0:
                break
        if pos < 0:
            out.append((None, sorted(set(alts))))
            continue
        out.append((starts[pos], sorted(set(alts))))
        # the next card may not claim speech that began before this one's
        # (a card continuing the same sentence is "late", never unmatched)
        cursor = floor = pos
    return out


def card_speech_starts(beats, words) -> list[float | None]:
    """Start (s) of each card's own speech: the first spoken word of its text
    (quote/statement) or cue, matched in order in the TTS words. The hook is
    the claim (0.0). None = not found in the narration (unverifiable)."""
    board = [b for b in (beats or []) if isinstance(b, dict)]
    return [None if v is None else round(v, 3)
            for v, _ in _card_speech_matches(board, words)]


def _hold(b) -> float:
    try:
        return float(b.get("dur") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def endcard_vo_content_end(words, script: str | None) -> float | None:
    """End (s) of the last spoken word BEFORE the series endcard VO. The PT
    closer is two sentences — content ends before 'Se inscreve.', not before
    'Próxima armadilha…'. None when there is no endcard VO or the words
    don't carry it."""
    sents = [x.strip() for x in re.split(r"(?<=[.!?…])\s+", (script or "").strip()) if x.strip()]
    n = endcard_vo_tail_n(sents)
    if n:
        head = sents[-n]
    else:
        head = next((x for x in reversed(sents)
                     if is_endcard_vo(x) or contains_subscribe_cta(x)), None)
    need = _sync_toks(head)
    if not need:
        return None
    toks, owner = [], []
    ws = [w for w in (words or []) if isinstance(w, dict)]
    for wi, w in enumerate(ws):
        for t in _sync_toks(w.get("text") or w.get("word")):
            toks.append(t)
            owner.append(wi)
    for p in range(len(toks) - len(need), 0, -1):
        if toks[p:p + len(need)] == need:
            return _word_end(ws[owner[p - 1]])
    return None


def card_sync_notes(beats, words, script: str | None = None) -> list[dict]:
    """Per-card sync note (index, type, start, speech_start, lead, status).
    With ``script``, the endcard also gets ``content_end`` (end of the last
    word before the Subscribe line) and status "straddle" when it cuts in
    while that content sentence is still being spoken (#1385: "Coming soon"
    22.84–23.50 under an endcard at 23.20)."""
    board = [b for b in (beats or []) if isinstance(b, dict)]
    matches = _card_speech_matches(board, words)
    c_end = endcard_vo_content_end(words, script) if script else None
    no_words = not any(isinstance(w, dict) for w in (words or []))
    notes = []
    for i, b in enumerate(board):
        if i == 0 and (b.get("type") or "") == "hook":
            continue
        st = _cue_start(b)
        sp, alts = matches[i]
        note = {"i": i, "type": b.get("type") or "?", "start": round(st, 3),
                "speech_start": None if sp is None else round(sp, 3)}
        last_cta = i == len(board) - 1 and (b.get("type") or "") in CTA_TYPES
        if sp is None:
            # measurement (CoS 2026-10-03): the card's words aren't in the
            # TTS boundaries (or there are none) — unverifiable, not a defect
            note.update(lead=None, status="unmatched", kind="measurement",
                        why="no_word_boundaries" if no_words else "words_not_in_tts")
        else:
            # lead from unrounded times (no rounding flips at the 0.05 edge)
            lead = float(sp) - st
            status = _sync_status(lead)
            if status == "late" and last_cta and _hold(b) >= CTA_BEAT_MAX_S - SYNC_TOL_S:
                # the endcard already starts as early as its cap allows
                status = "late_capped"
            if status in ("late", "early") and len(alts) > 1:
                # repeated phrase: the in-order matcher took one occurrence,
                # but the card sits on another one of its own words
                ok = [a for a in alts if _sync_status(float(a) - st) == "ok"]
                if ok:
                    note.update(measured=round(sp, 3), measured_status=status,
                                kind="measurement", why="repeated_phrase")
                    sp = min(ok, key=lambda a: abs(float(a) - st))
                    note["speech_start"] = round(sp, 3)
                    lead, status = float(sp) - st, "ok"
            note.update(lead=round(lead, 3), status=status)
            if status in ("late", "early"):
                note["kind"] = "real"
        if last_cta and c_end is not None:
            note["content_end"] = c_end
            if note["status"] in ("ok", "early", "unmatched") and st < c_end - SYNC_TOL_S:
                note["status"] = "straddle"
                note["kind"] = "real"
        notes.append(note)
    return notes


def _sync_status(lead: float) -> str:
    return ("late" if lead < -SYNC_TOL_S - 1e-9 else
            "early" if lead > SYNC_LEAD_MAX_S + SYNC_TOL_S + 1e-9 else "ok")


def card_sync_marker(beats, words, content_format: str | None = "short",
                     script: str | None = None) -> dict | None:
    """creation_config["card_sync"] for a new render (None: long / no timings)."""
    if (content_format or "short") == "long" or not words:
        return None
    return {"version": SYNC_V1, "lead_max": SYNC_LEAD_MAX_S,
            "notes": card_sync_notes(beats, words, script)}


def card_sync_hits(card_sync: dict | None) -> list[str]:
    """Gate B hits for a marked board: a card after its speech, or leading it
    by more than SYNC_LEAD_MAX_S. Unmatched cards are noted, not failed."""
    if not isinstance(card_sync, dict) or card_sync.get("version") != SYNC_V1:
        return []
    hits = []
    for n in card_sync.get("notes") or []:
        if not isinstance(n, dict):
            continue
        st, sp, lead = n.get("start"), n.get("speech_start"), n.get("lead")
        if n.get("status") == "late":
            hits.append(f"card {n.get('i')} ({n.get('type')}) at {st:.2f}s appears "
                        f"{-float(lead):.2f}s AFTER its speech ({sp:.2f}s)")
        elif n.get("status") == "early":
            hits.append(f"card {n.get('i')} ({n.get('type')}) at {st:.2f}s leads its "
                        f"speech ({sp:.2f}s) by {float(lead):.2f}s (max {SYNC_LEAD_MAX_S:.1f}s)")
        elif n.get("status") == "straddle":
            hits.append(f"endcard at {st:.2f}s cuts in before the last content "
                        f"sentence ends ({float(n.get('content_end') or 0):.2f}s)")
    return hits


# Product teasers (OS "· Shipping N", CoS/CMO 2026-09-30, #1385 Gate B):
# no invented CLI on screen. The compose prompt required a code/command beat
# on every 9:16 board, so #1385 showed "$ channels thumb cover.png" — a
# command that does not exist (v4/CMO: no API or path names on screen). New
# teaser renders carry creation_config["cli_check"] = {"version": CLI_V1,
# "hits": [...]}: every command/code card whose command, output or code lines
# are not literally in the narration. Gate B fails any hit. Scope: product
# teasers only (RR/OS explainers keep their illustrative terminal stills).
PRODUCT_TEASER_SERIES = "Shipping"
CLI_V1 = "cli_v1"
CLI_BEAT_TYPES = frozenset({"command", "code"})


def is_product_teaser(title: str | None = None, topic_name: str | None = None) -> bool:
    """Shipping series: title suffix " · Shipping N" or topic "Shipping"."""
    m = SPOKEN_TITLE_RE.search(title or "")
    if m and _canonical_series(m.group(1)) == PRODUCT_TEASER_SERIES:
        return True
    return series_from_topic(topic_name) == PRODUCT_TEASER_SERIES


def cli_lines(beat: dict | None) -> list[str]:
    """On-screen terminal/code text of a command/code card."""
    if not isinstance(beat, dict) or (beat.get("type") or "") not in CLI_BEAT_TYPES:
        return []
    if beat.get("type") == "command":
        raw = [beat.get("command")] + list(beat.get("output") or [])
    else:
        raw = list(beat.get("lines") or [])
    return [str(x).strip() for x in raw if str(x or "").strip()]


def text_in_script(text: str | None, script: str | None) -> bool:
    """True when ``text`` (letters/digits, in order) is said in ``script``."""
    need = _sync_toks(text)
    if not need:
        return True
    return f" {' '.join(need)} " in f" {' '.join(_sync_toks(script))} "


def fabricated_cli(beats, script: str | None) -> list[dict]:
    """Command/code cards showing text the narration never says."""
    out = []
    for i, b in enumerate(b for b in (beats or []) if isinstance(b, dict)):
        bad = [ln for ln in cli_lines(b) if not text_in_script(ln, script)]
        if bad:
            out.append({"i": i, "type": b.get("type"), "text": bad[:3]})
    return out


def cli_check_marker(beats, script: str | None, *, title: str | None = None,
                     topic_name: str | None = None,
                     content_format: str | None = "short") -> dict | None:
    """creation_config["cli_check"] for a new product-teaser short (else None)."""
    if (content_format or "short") == "long" or not is_product_teaser(title, topic_name):
        return None
    return {"version": CLI_V1, "hits": fabricated_cli(beats, script)}


def cli_check_hits(cli_check: dict | None) -> list[str]:
    if not isinstance(cli_check, dict) or cli_check.get("version") != CLI_V1:
        return []
    hits = []
    for h in cli_check.get("hits") or []:
        if isinstance(h, dict):
            shown = " / ".join(str(x) for x in (h.get("text") or []))
            hits.append(f"card {h.get('i')} ({h.get('type')}) shows {shown!r}, "
                        "which the narration never says")
    return hits


def _pass_fail(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def _timing_caps(legacy_timing: bool, beat_timing: str | None) -> tuple[float, float]:
    """(mid hold cap, cta hold cap) for a board's timing marker."""
    if legacy_timing:
        return LEGACY_MID_BEAT_MAX_S, LEGACY_CTA_BEAT_MAX_S
    if beat_timing == BEAT_TIMING_INCL_FADE:
        return INCL_FADE_MID_BEAT_MAX_S, CTA_BEAT_MAX_S
    return MID_BEAT_MAX_S, CTA_BEAT_MAX_S


def video_maker_gate(beats, *, content_format: str | None = "short",
                     used_fallback: bool = False, legacy_timing: bool = False,
                     hook_pace: dict | None = None,
                     beat_timing: str | None = None,
                     card_sync: dict | None = None,
                     cli_check: dict | None = None,
                     card_text: dict | None = None,
                     card_vo: dict | None = None) -> dict:
    """A+B+C PASS/FAIL for YouTube Shorts. Longs are exempt (all PASS, no reasons)."""
    checks = {"A": "PASS", "B": "PASS", "C": "PASS"}
    reasons: list[str] = []
    if (content_format or "short") == "long":
        return {"result": "PASS", "checks": checks, "reasons": reasons}

    board = [b for b in (beats or []) if isinstance(b, dict)]

    if used_fallback and not board:
        checks["A"] = "FAIL"
        checks["C"] = "FAIL"
        reasons.append(
            "[A] Object 0–3s: FAIL — kinetic-text fallback has no typed beats "
            "(typography-only; emoji is not an object). Need a code/command/diagram/"
            "compare/stat beat starting before t=3.0s, or hook.object (receipt/"
            "terminal/bill)."
        )
        reasons.append(
            "[C] Spoken list/slide spam: FAIL — fallback re-displays narration as "
            "text cards (no code/command/diagram/compare/stat). statement max 1; "
            "list forbidden-or ≤1 list / ≤3 items / ≤3.0s / stagger ≤0.6s."
        )
        return {"result": "FAIL", "checks": checks, "reasons": reasons}

    if not board:
        # Legacy row / missing snapshot — caller fail-opens via video_maker_gate_reason.
        return {"result": "PASS", "checks": checks, "reasons": reasons,
                "legacy": True}

    # --- A: object in the first 3.0s ---------------------------------------
    opening = [b for b in board if _cue_start(b) < OPENING_WINDOW_S + 1e-9]
    if not opening:
        opening = [board[0]]
    hooks = [b for b in board if (b.get("type") or "") == "hook"]
    has_object_beat = any((b.get("type") or "") in OBJECT_BEAT_TYPES for b in opening)
    has_hook_object = any(hook_object(b) for b in (hooks or opening[:1]))
    if not (has_object_beat or has_hook_object):
        kinds = [b.get("type") or "?" for b in opening]
        checks["A"] = "FAIL"
        reasons.append(
            f"[A] Object 0–3s: FAIL — first {OPENING_WINDOW_S:.1f}s is typography-only "
            f"(types={kinds}; emoji does not count). Need ≥1 beat type in "
            f"{sorted(OBJECT_BEAT_TYPES)} starting before t={OPENING_WINDOW_S:.1f}s, "
            "or a non-empty hook.object Decolar prop (receipt/terminal/bill)."
        )

    # --- B: mid card ≤3.0s / endcard ≤3.9s INCLUDING the fade; hook EXEMPT -
    # card = visual hold (dur) + the 0.12s fade gap it owns, which is what the
    # VM measures card-to-card (3.05–3.20s on a 3.0s hold, 2026-09-29).
    b_hits = []
    mid_hold, cta_hold = _timing_caps(legacy_timing, beat_timing)
    for i, b in enumerate(board):
        btype = b.get("type") or "?"
        # Opening hook is Gate A territory (object 0–3s) — not Gate B.
        if btype == "hook":
            continue
        span = _cue_span(board, i)
        is_cta = btype in CTA_TYPES
        hold_cap = cta_hold if is_cta else mid_hold
        if span > hold_cap + 1e-6:
            label = "cta/endcard" if is_cta else "mid"
            # fade as measured for this hold cap (0.20s on ≤2.80s mids, 0.12s
            # otherwise); card = hold + fade vs the card limit.
            fade = (MID_FADE_S if (not is_cta and hold_cap == MID_BEAT_MAX_S)
                    else BEAT_GAP_S)
            limit = hold_cap + fade
            b_hits.append(
                f"beat[{i}] type={btype} {label} card {span + fade:.2f}s incl. fade "
                f"(hold {span:.2f}s + {fade:.2f}s fade; limit {limit:.1f}s)"
            )
    if b_hits:
        checks["B"] = "FAIL"
        reasons.append(
            f"[B] Beats ≤{MID_CARD_MAX_S:.1f}s incl. fade: FAIL — " + "; ".join(b_hits) +
            f". Mid cards/slides must be ≤{MID_CARD_MAX_S:.1f}s including the fade "
            f"(hold ≤{mid_hold:.2f}s); cta/endcard ≤{CTA_CARD_MAX_S:.1f}s "
            f"including the fade (hold ≤{cta_hold:.2f}s) — split or add "
            "beats, never stretch a card (not a Follow-tomorrow hold)."
        )
    # Repeated card: an identical card split only by the fade blink reads as
    # one ~6s frozen visual (freezedetect misses it). Gate B territory.
    rep_hits = repeated_card_hits(board)
    if rep_hits:
        checks["B"] = "FAIL"
        reasons.append(
            "[B] Repeated card: FAIL — " + "; ".join(rep_hits) +
            ". Neighbouring beats must change the on-screen text, and a mid "
            "card must not be replayed later; a repeated spoken phrase needs "
            "a visual change (different beat type/layout/emphasis)."
        )
    # RR hook pace (marked new RR renders only; see HOOK_PACE_V1).
    pace_hits = hook_pace_hits(board, hook_pace)
    if pace_hits:
        checks["B"] = "FAIL"
        reasons.append(
            "[B] RR hook pace: FAIL — " + "; ".join(pace_hits) +
            f". RR claim ≤{HOOK_CLAIM_MAX_WORDS} words, fully spoken by "
            f"{HOOK_CLAIM_SPOKEN_BY_S:.1f}s, first cut by {HOOK_FIRST_CUT_BY_S:.1f}s "
            "(shorter title head / opening line)."
        )

    # Card ↔ speech sync (marked new renders only; see SYNC_V1).
    sync_hits = card_sync_hits(card_sync)
    if sync_hits:
        checks["B"] = "FAIL"
        reasons.append(
            "[B] Card sync: FAIL — " + "; ".join(sync_hits) +
            f". A card never appears after its own words; it may lead them by "
            f"≤{SYNC_LEAD_MAX_S:.1f}s."
        )
    # Product teaser: no invented CLI (marked new teaser renders; CLI_V1).
    cli_hits = cli_check_hits(cli_check)
    if cli_hits:
        checks["B"] = "FAIL"
        reasons.append(
            "[B] Fabricated CLI: FAIL — " + "; ".join(cli_hits) +
            ". Product teasers never show a command, CLI, file path or API "
            "output that the script does not literally contain."
        )
    # Card text rules (marked new short renders; CARD_TEXT_V1).
    text_hits = card_text_check_hits(card_text)
    if text_hits:
        checks["B"] = "FAIL"
        reasons.append(
            "[B] Card text: FAIL — " + "; ".join(text_hits) +
            ". One card per spoken sentence, quotes only of words the VO says, no "
            "invented tool output, PT-BR copy on PT cards, no back-to-back "
            "near-duplicate cards."
        )
    # Card ⊂ VO (marked new short renders; CARD_VO_V1).
    vo_hits = card_vo_check_hits(card_vo)
    if vo_hits:
        checks["B"] = "FAIL"
        reasons.append(
            "[B] Card ⊂ VO: FAIL — " + "; ".join(vo_hits) +
            ". Every text card shows one whole sentence the VO says (a long "
            "sentence may continue on the next card only with its literal next "
            "words), and each spoken sentence gets one card."
        )

    # --- C: kill spoken list/slide spam ------------------------------------
    types = [b.get("type") or "" for b in board]
    n_stmt = types.count("statement")
    list_idxs = [i for i, t in enumerate(types) if t == "list"]
    rich = {t for t in types if t in OBJECT_BEAT_TYPES}
    c_hits = []
    if n_stmt > STATEMENT_MAX_SHORTS:
        c_hits.append(
            f"{n_stmt} statement beats (max {STATEMENT_MAX_SHORTS} on Shorts; "
            "was tolerated at 2)"
        )
    if len(list_idxs) > LIST_MAX_PER_SHORT:
        c_hits.append(
            f"{len(list_idxs)} list beats (max {LIST_MAX_PER_SHORT}; prefer "
            "code/command/diagram/compare/stat)"
        )
    for i in list_idxs:
        b = board[i]
        n_items = len(_list_items(b))
        span = _cue_span(board, i)
        stagger = _list_stagger(b, span)
        if n_items > LIST_MAX_ITEMS:
            c_hits.append(
                f"list beat[{i}] has {n_items} items (max {LIST_MAX_ITEMS})"
            )
        if span > mid_hold + 1e-9:
            c_hits.append(
                f"list beat[{i}] held {span:.2f}s (max {mid_hold:.2f}s hold)"
            )
        if stagger > LIST_STAGGER_MAX_S + 1e-9:
            c_hits.append(
                f"list beat[{i}] item stagger {stagger:.2f}s "
                f"(max {LIST_STAGGER_MAX_S:.1f}s)"
            )
    echo_spam = []
    for i, b in enumerate(board):
        if (b.get("type") or "") not in ("statement", "list"):
            continue
        if _echoes_narration(b) and not rich:
            echo_spam.append(f"beat[{i}] type={b.get('type')}")
    if echo_spam:
        c_hits.append(
            "list/statement only re-displays narration with no rich type "
            f"({', '.join(echo_spam)}; need code/command/diagram/compare/stat "
            "in the middle)"
        )
    elif (n_stmt or list_idxs) and not rich:
        c_hits.append(
            "list/statement mid-board with no code/command/diagram/compare/stat "
            "(spoken-slide spam)"
        )
    # Subscribe CTA is only legal on the trailing cta/endcard series.
    end_i = _endcard_series_start(board)
    for i, b in enumerate(board[:end_i]):
        hits = _subscribe_hits(_visible_copy(b))
        if hits:
            shown = "/".join(dict.fromkeys(hits))
            c_hits.append(
                f"beat[{i}] type={b.get('type') or '?'} has Subscribe CTA "
                f"({shown!r}) — Subscribe is only allowed on the series "
                "endcard, never on a mid card"
            )
    if c_hits:
        checks["C"] = "FAIL"
        reasons.append("[C] Spoken list/slide spam: FAIL — " + "; ".join(c_hits))

    result = "FAIL" if reasons else "PASS"
    kinds = gate_check_kinds(
        board, hook_pace=hook_pace, card_sync=card_sync,
        board_hits={"beat_hold": b_hits, "repeated_card": rep_hits,
                    "fabricated_cli": cli_hits, "card_text": text_hits,
                    "card_vo": vo_hits,
                    "slide_spam": c_hits})
    out = {"result": result, "checks": checks, "reasons": reasons}
    if kinds:
        out["kinds"] = kinds
    return out


# Real fail vs measurement artifact (CoS 2026-10-03). Every Gate B/C check
# that failed — or that could not be measured — gets an entry:
#   kind="real"         a craft defect on the board/timing (still fails);
#   kind="measurement"  the measuring step, not the video: the card's words
#                       are not in the TTS word boundaries / no boundaries /
#                       a repeated phrase matched to the wrong occurrence
#                       (sync, now resolved) / claim letters not matched
#                       (hook pace estimated from the n-th word).
# ``edge`` marks a real fail within EDGE_S of its threshold (VM eyeball).
EDGE_S = 0.02


def gate_check_kinds(board, *, hook_pace: dict | None = None,
                     card_sync: dict | None = None,
                     board_hits: dict | None = None) -> list[dict]:
    out: list[dict] = []
    for check, hits in (board_hits or {}).items():
        for h in hits or []:
            out.append({"check": check, "kind": "real", "detail": h})
    if (isinstance(hook_pace, dict) and hook_pace.get("version") == HOOK_PACE_V1
            and board and (board[0].get("type") or "") == "hook"):
        n = claim_word_count(board[0].get("text") or "")
        if n > HOOK_CLAIM_MAX_WORDS:
            out.append({"check": "hook_claim_words", "kind": "real",
                        "detail": f"{n} words"})
        end, how = hook_pace.get("claim_spoken_end"), hook_pace.get("claim_match")
        if isinstance(end, (int, float)) and float(end) > HOOK_CLAIM_SPOKEN_BY_S + 1e-6:
            e = {"check": "hook_spoken_by", "detail": f"{float(end):.2f}s"}
            if how == "fallback":
                e.update(kind="measurement", why="claim_not_matched_in_tts")
            else:
                e["kind"] = "real"
                if float(end) - HOOK_CLAIM_SPOKEN_BY_S <= EDGE_S:
                    e["edge"] = True
            out.append(e)
        elif "claim_match" in hook_pace and how != "exact":
            out.append({"check": "hook_spoken_by", "kind": "measurement",
                        "why": "claim_not_matched_in_tts" if how else "no_word_boundaries",
                        "detail": "not verifiable (not failed)"})
        if len(board) > 1:
            cut = _cue_start(board[1])
            if cut > HOOK_FIRST_CUT_BY_S + 1e-6:
                e = {"check": "hook_first_cut", "kind": "real", "detail": f"{cut:.2f}s"}
                if cut - HOOK_FIRST_CUT_BY_S <= EDGE_S:
                    e["edge"] = True
                out.append(e)
    if isinstance(card_sync, dict) and card_sync.get("version") == SYNC_V1:
        for n in card_sync.get("notes") or []:
            if not isinstance(n, dict) or not n.get("kind"):
                continue
            e = {"check": "card_sync", "kind": n["kind"], "card": n.get("i"),
                 "status": n.get("status")}
            if n.get("why"):
                e["why"] = n["why"]
            lead = n.get("lead")
            if n["kind"] == "real" and isinstance(lead, (int, float)):
                over = (-SYNC_TOL_S - lead) if n.get("status") == "late" else (
                    lead - SYNC_LEAD_MAX_S - SYNC_TOL_S)
                if n.get("status") in ("late", "early") and over <= EDGE_S:
                    e["edge"] = True
            out.append(e)
    return out


def format_craft_gate_reason(gate: dict | None) -> str | None:
    if not gate or gate.get("result") != "FAIL":
        return None
    reasons = [r for r in (gate.get("reasons") or []) if r]
    if not reasons:
        return "Video Maker craft gate FAIL"
    return "Video Maker craft gate FAIL: " + " ".join(reasons)


# Measurement-fail cap (CTO council 06/10 item 2c). A check classified
# kind="measurement" is "not verifiable (not failed)", so on its own it lets the
# gate PASS. One such render behaves as before; MEASUREMENT_CAP_N consecutive
# renders of the same video with a measurement entry never PASS silently: the
# render gets creation_config.measurement_cap and the gate FAILs with a reason
# that routes it to VM review. Only the VM's Gate B PASS recorded on THAT render
# (POST /api/videos/{id}/vm-pass, bound to video_path) clears it; a render with
# no measurement entry resets the streak (Video.measurement_streak).
MEASUREMENT_CAP_N = 2
MEASUREMENT_CAP_KEY = "measurement_cap"
MEASUREMENT_CAP_REASON = (
    "[B] Measurement cap: FAIL — {n} consecutive renders with unverifiable Gate "
    "B/C checks ({checks}); not a silent PASS — route to VM review: needs the "
    "Video Maker's Gate B PASS on this render (POST /api/videos/{{id}}/vm-pass)")


def gate_measurement_kinds(gate: dict | None) -> list[dict]:
    """The kind="measurement" entries of a video_maker_gate result."""
    if not isinstance(gate, dict):
        return []
    return [k for k in (gate.get("kinds") or [])
            if isinstance(k, dict) and k.get("kind") == "measurement"]


def video_maker_gate_of(creation_config=None,
                        content_format: str | None = "short") -> dict | None:
    """The Video Maker gate for a render snapshot (same inputs as
    video_maker_gate_reason), or None when exempt (long) / no snapshot."""
    if (content_format or "short") == "long":
        return None
    cc = _as_dict(creation_config)
    beats = cc.get("beats")
    if not isinstance(beats, list):
        beats = []
    used_fallback = bool(cc.get("used_fallback"))
    if not beats and not used_fallback:
        stored = cc.get("craft_gate")
        return stored if isinstance(stored, dict) else None
    bt = cc.get("beat_timing")
    legacy = bt not in (BEAT_TIMING_INCL_FADE, BEAT_TIMING_HOLD_280)
    pace = cc.get("hook_pace") if isinstance(cc.get("hook_pace"), dict) else None
    sync = cc.get("card_sync") if isinstance(cc.get("card_sync"), dict) else None
    cli = cc.get("cli_check") if isinstance(cc.get("cli_check"), dict) else None
    ctext = cc.get("card_text") if isinstance(cc.get("card_text"), dict) else None
    return video_maker_gate(beats, content_format=content_format,
                            used_fallback=used_fallback, legacy_timing=legacy,
                            hook_pace=pace, beat_timing=bt, card_sync=sync,
                            cli_check=cli, card_text=ctext)


def next_measurement_streak(prev: int | None, creation_config=None,
                            content_format: str | None = "short") -> int:
    """Streak after a render: +1 when its gate has a measurement entry, else 0."""
    gate = video_maker_gate_of(creation_config, content_format)
    return (int(prev or 0) + 1) if gate_measurement_kinds(gate) else 0


def with_measurement_cap(creation_config, streak: int, *, video_path: str | None,
                         content_format: str | None = "short"):
    """creation_config with the cap marker set (streak ≥ N) or removed.
    A vm_pass recorded for another render never carries over to a capped one."""
    cc = dict(_as_dict(creation_config))
    if int(streak or 0) >= MEASUREMENT_CAP_N and (content_format or "short") != "long":
        checks = sorted({k.get("check") or "?" for k in gate_measurement_kinds(
            video_maker_gate_of(cc, content_format))})
        cc[MEASUREMENT_CAP_KEY] = {"streak": int(streak), "video_path": video_path,
                                   "checks": checks}
        vp = cc.get("vm_pass")
        if isinstance(vp, dict) and vp.get("video_path") != video_path:
            cc.pop("vm_pass", None)
    elif MEASUREMENT_CAP_KEY in cc:
        cc.pop(MEASUREMENT_CAP_KEY)
    else:
        return creation_config
    return json.dumps(cc)


def measurement_cap_reason(creation_config=None,
                           content_format: str | None = "short") -> str | None:
    """Gate FAIL reason while the cap is on and the VM has not passed THIS render."""
    if (content_format or "short") == "long":
        return None
    cc = _as_dict(creation_config)
    cap = cc.get(MEASUREMENT_CAP_KEY)
    if not cap:
        return None
    cap = cap if isinstance(cap, dict) else {}
    vp = cc.get("vm_pass")
    if (isinstance(vp, dict) and vp.get("result") == "PASS"
            and vp.get("video_path") and vp.get("video_path") == cap.get("video_path")):
        return None
    return "Video Maker craft gate FAIL: " + MEASUREMENT_CAP_REASON.format(
        n=cap.get("streak") or MEASUREMENT_CAP_N,
        checks=", ".join(cap.get("checks") or []) or "measurement")


def video_maker_gate_reason(creation_config=None,
                            content_format: str | None = "short") -> str | None:
    """None = allowed to leave review toward publish. Longs are exempt.
    A real gate FAIL wins; else the measurement cap (never a silent PASS)."""
    return (_video_maker_gate_reason_core(creation_config, content_format)
            or measurement_cap_reason(creation_config, content_format))


def _video_maker_gate_reason_core(creation_config=None,
                                  content_format: str | None = "short") -> str | None:
    if (content_format or "short") == "long":
        return None
    cc = _as_dict(creation_config)
    beats = cc.get("beats")
    if not isinstance(beats, list):
        beats = []
    used_fallback = bool(cc.get("used_fallback"))
    if not beats and not used_fallback:
        stored = cc.get("craft_gate")
        if isinstance(stored, dict) and stored.get("result") == "FAIL":
            return format_craft_gate_reason(stored)
        return None  # no snapshot (pré-gate inventory) — fail-open
    bt = cc.get("beat_timing")
    legacy = bt not in (BEAT_TIMING_INCL_FADE, BEAT_TIMING_HOLD_280)
    pace = cc.get("hook_pace") if isinstance(cc.get("hook_pace"), dict) else None
    sync = cc.get("card_sync") if isinstance(cc.get("card_sync"), dict) else None
    cli = cc.get("cli_check") if isinstance(cc.get("cli_check"), dict) else None
    ctext = cc.get("card_text") if isinstance(cc.get("card_text"), dict) else None
    cvo = cc.get("card_vo") if isinstance(cc.get("card_vo"), dict) else None
    gate = video_maker_gate(beats, content_format=content_format,
                            used_fallback=used_fallback, legacy_timing=legacy,
                            hook_pace=pace, beat_timing=bt, card_sync=sync,
                            cli_check=cli, card_text=ctext, card_vo=cvo)
    return format_craft_gate_reason(gate)


def review_gate_reason(title: str | None,
                       content_format: str | None = "short",
                       creation_config=None) -> str | None:
    """Title pattern then Video Maker A+B+C. First failure wins (operator-readable)."""
    return (title_gate_reason(title, content_format)
            or video_maker_gate_reason(creation_config, content_format))


# ---------------------------------------------------------------------------
# Series endcard (shorts) — Subscribe VO + chip Subscribe · {series}. Not in BANNED_RE.
# ---------------------------------------------------------------------------

def _canonical_series(raw: str) -> str:
    folded = theme.fold(raw)
    for label in SERIES_LABELS:
        if theme.fold(label) == folded:
            return label
    return raw.strip()


def series_from_topic(topic_name: str | None) -> str | None:
    """Series label named by the video's topic, or None.

    Exact label ("Shipping", "IA", "Agent traps") or a topic name that starts
    with a label as whole words ("Agent memory and state in production" →
    Agent memory, "Claude Code production workflows" → Claude Code). Never a
    substring match inside a longer name ("Engenheiro de IA no Brasil" → None).
    """
    folded = " ".join(theme.fold(topic_name or "").split())
    if not folded:
        return None
    for label in sorted(SERIES_LABELS, key=len, reverse=True):
        fl = theme.fold(label)
        if folded == fl or re.match(re.escape(fl) + r"(?![\w])", folded):
            return label
    return None


def series_of(title: str | None, brand: str | None = None,
              topic_name: str | None = None) -> str:
    """Series label for the endcard chip + VO.

    Order: title suffix `` · <series> <nn>`` → the video's real topic
    (``series_from_topic``) → brand default (OS → Agent memory; RR → IA) →
    Copilot Credits (unknown brand). Shipping #1308/#1311 (rendered 22 Sep,
    before #33 taught SPOKEN_TITLE_RE "Shipping" and before the OS brand
    default) fell through to "Copilot Credits" although their topic is
    "Shipping" — the topic now wins over any default. Never invent a third
    CTA — only swap {series}/{noun}.
    """
    m = SPOKEN_TITLE_RE.search(title or "")
    if m:
        return _canonical_series(m.group(1))
    from_topic = series_from_topic(topic_name)
    if from_topic:
        return from_topic
    return DEFAULT_SERIES.get(brand or "", DEFAULT_SERIES_FALLBACK)


def next_claim_noun(*blobs: str | None) -> str:
    """Pick trap|receipt|bill|drop from title/script; default trap."""
    blob = theme.fold(" ".join(b or "" for b in blobs))
    for noun in ("receipt", "bill", "drop", "trap"):
        if re.search(rf"\b{noun}\b", blob):
            return noun
    return DEFAULT_NOUN


# EN closer phrase per series when "{series} {noun}" reads wrong. CMO
# 2026-10-06 (#1328): Agent traps → "Subscribe — next agent trap." (the
# template gave "next Agent traps trap.").
SERIES_CTA_PHRASE = {"Agent traps": "agent trap"}


def series_cta_phrase(series: str, noun: str = DEFAULT_NOUN) -> str:
    """The "{series} {noun}" part of the EN closer, never naming the noun twice.

    Explicit SERIES_CTA_PHRASE first. Otherwise, when the series' last word is
    already a closer noun (trap|receipt|bill|drop, singular or plural), that
    word becomes the noun in the singular ("Cloud bills" → "Cloud bill");
    else "{series} {noun}" as before ("Agent memory trap").
    """
    if series in SERIES_CTA_PHRASE:
        return SERIES_CTA_PHRASE[series]
    n = noun if noun in NEXT_CLAIM_NOUNS else DEFAULT_NOUN
    words = (series or "").split()
    if len(words) >= 2:
        last = theme.fold(words[-1])
        for nn in NEXT_CLAIM_NOUNS:
            if last in (nn, nn + "s", nn + "es"):
                return " ".join(words[:-1] + [nn])
    return f"{series} {n}"


def series_endcard_vo(series: str, noun: str = DEFAULT_NOUN) -> str:
    """Spoken closer. EN: Subscribe — next {series} {noun}. (noun said once —
    see series_cta_phrase). PT (IA): Se inscreve. Próxima armadilha de {series}."""
    if series in PT_ENDCARD_SERIES:
        return f"Se inscreve. Próxima armadilha de {series}."
    n = noun if noun in NEXT_CLAIM_NOUNS else DEFAULT_NOUN
    line = f"Subscribe — next {series_cta_phrase(series, n)}."
    # Safety clip — live series labels are 1–2 words; never invent extra CTA.
    words = line.rstrip(".").split()
    if len(words) > 8:
        line = " ".join(words[:8]) + "."
    return line


def series_endcard_chip(series: str) -> str:
    """On-screen chip, one line. EN: Subscribe · {series}. PT (IA): Se
    inscreve · {series}."""
    if series in PT_ENDCARD_SERIES:
        return f"Se inscreve · {series}"
    return f"Subscribe · {series}"


def series_endcard_micro(series: str) -> str:
    """Optional second line. Only if the chip is short enough; no extra CTA."""
    chip = series_endcard_chip(series)
    # Chip grew from `· {series}` (~+10). 32 still fits Copilot Credits.
    if len(chip) <= 32:
        return "same series"
    return ""


def series_endcard(title: str | None, script: str | None = None,
                   brand: str | None = None, noun: str | None = None,
                   topic_name: str | None = None) -> dict:
    """VO + chip + optional micro for the series endcard.

    Defaults: OS / Agent memory / trap; RR / IA / trap. The topic name wins
    over the brand default when the title carries no series suffix.
    """
    series = series_of(title, brand, topic_name)
    n = noun if noun in NEXT_CLAIM_NOUNS else next_claim_noun(title, script)
    vo = series_endcard_vo(series, n)
    chip = series_endcard_chip(series)
    micro = series_endcard_micro(series)
    return {"series": series, "noun": n, "vo": vo, "chip": chip, "micro": micro}


def endcard_scan_banned(text: str | None) -> list[str]:
    if not text:
        return []
    return [m.group(0) for m in ENDCARD_BANNED_RE.finditer(text)]


def endcard_clean(card: dict) -> bool:
    """True when VO/chip/micro stay in the YPP#5 template.

    Subscribe is allowed on the VO and on the chip as `Subscribe · {series}`.
    Subscribe on the micro, or any other Subscribe chip, fails. Off-endcard
    Subscribe is a different gate (mid_body_has_subscribe / craft C).
    """
    vo = (card.get("vo") or "").strip()
    chip = (card.get("chip") or "").strip()
    micro = (card.get("micro") or "").strip()
    if endcard_scan_banned(vo) or endcard_scan_banned(chip) or endcard_scan_banned(micro):
        return False
    folded_micro = theme.fold(micro)
    if "subscribe" in folded_micro or "inscreve" in folded_micro:
        return False
    if not is_endcard_chip(chip):
        return False
    if micro and micro != "same series":
        return False
    if len(vo.split()) > 8:
        return False
    return is_endcard_vo(vo)


def ensure_series_endcard_vo(script: str | None, subject: str | None,
                             brand: str | None = None,
                             noun: str | None = None,
                             topic_name: str | None = None) -> str:
    """Pin the trailing spoken closer to the series endcard VO (EN one-liner
    or the PT two-sentence closer). Mid Subscribe/Inscreve is stripped first.
    Long-form callers should skip this."""
    card = series_endcard(subject, script, brand, noun, topic_name=topic_name)
    vo = card["vo"]
    raw = strip_mid_subscribe(script)
    if not raw:
        return vo
    parts = [p for p in re.split(r"(?<=[.!?…])\s+", raw) if p.strip()]
    n = endcard_vo_tail_n(parts)
    if n:
        head = parts[:-n]
        return (" ".join(head) + " " + vo).strip() if head else vo
    return (raw.rstrip() + " " + vo).strip()



# ---------------------------------------------------------------------------
# Provided scripts (item 8) — operator/CMO-authored VO used verbatim.
#
# A video created with a ``script`` (POST /api/videos) or given one on a draft
# (PATCH /api/videos/{id}/script) carries ``creation_config.script_source =
# "provided"``. The render loop then skips ``worker._generate_script`` (grok -p)
# and speaks that text. It is NEVER silently regenerated: a first spoken line
# that doesn't carry the title claim fails the craft gate with
# PROVIDED_SCRIPT_HOOK_REASON. The only edits are the same deterministic craft
# rules the generated path applies (series endcard pin, miolo Subscribe strip,
# banned-CTA strip) — recorded on ``creation_config.script_edits``.
# ---------------------------------------------------------------------------

SCRIPT_SOURCE_PROVIDED = "provided"
SCRIPT_SOURCE_GENERATED = "generated"

PROVIDED_SCRIPT_HOOK_REASON = (
    "provided script: first spoken line does not carry the title claim"
)


def script_source(creation_config) -> str | None:
    """``creation_config.script_source`` (provided|generated) or None (legacy)."""
    src = _as_dict(creation_config).get("script_source")
    return src if isinstance(src, str) and src else None


def is_provided_script(creation_config) -> bool:
    return script_source(creation_config) == SCRIPT_SOURCE_PROVIDED


def provided_script_hook_reason(script: str | None, *, title: str | None = None,
                                subject: str | None = None) -> str | None:
    """None when a provided script opens on the title/subject claim.

    Reuses the storyboard hook helpers: hook = title head before ``·``
    (else subject head), spoken = first sentence of the script, aligned via
    ``claim_aligned``; a ``$N`` stake in the hook must stay numerals on the
    spoken line (``preserves_dollar_numerals``). No hook available → None
    (nothing to align against; the title gate owns empty titles).
    """
    hook = spoken_hook_source(title or subject, None, None)
    if not hook:
        hook = spoken_hook_source(subject, None, None)
    if not hook:
        return None
    spoken = first_spoken_sentence(script)
    if not spoken:
        return f"{PROVIDED_SCRIPT_HOOK_REASON} (script is empty)"
    if claim_aligned(hook, spoken) and preserves_dollar_numerals(hook, spoken):
        return None
    return (f"{PROVIDED_SCRIPT_HOOK_REASON} (hook {hook[:80]!r} vs spoken "
            f"{spoken[:80]!r}) — fix it via PATCH /api/videos/{{id}}/script; "
            f"provided scripts are never regenerated")


def prepare_provided_script(script: str | None, subject: str | None, *,
                            brand: str | None = None,
                            content_format: str | None = "short"
                            ) -> tuple[str, list[str]]:
    """Provided script → the exact VO text to speak, plus the edits applied.

    Verbatim except the generated path's deterministic craft rules:
    banned CTA sentences dropped (``strip_banned``), miolo Subscribe dropped
    (``strip_mid_subscribe``), and for shorts the standard series endcard
    EN ``Subscribe — next {series} {noun}.`` or PT IA
    ``Se inscreve. Próxima armadilha de IA.`` appended when the trailing
    closer isn't already an endcard VO (an existing closer is kept as
    written — never duplicated or rewritten). Long-form skips the endcard,
    like ``worker._generate_script``.
    """
    text = (script or "").strip()
    edits: list[str] = []
    if not text:
        return "", edits
    cleaned = strip_banned(text) or text
    if cleaned != text:
        edits.append("banned_cta_stripped")
    text = cleaned
    if (content_format or "short") == "long":
        return text, edits
    body = strip_mid_subscribe(text)
    if body and body != text:
        edits.append("mid_subscribe_stripped")
        text = body
    parts = [p for p in re.split(r"(?<=[.!?…])\s+", text) if p.strip()]
    # EN one-liner OR the PT two-sentence closer already present → keep as written.
    if endcard_vo_tail_n(parts):
        return text, edits
    edits.append("endcard_appended")
    return ensure_series_endcard_vo(text, subject, brand=brand), edits

# ---------------------------------------------------------------------------
# Publish craft gate — durable craft_review + anti-nonsense (CoS 2026-09-22)
#
# Publish must NOT pick videos without an explicit craft_review=pass.
# Extends the existing title lock + Video Maker A/B/C (review_gate_reason)
# with: non-empty script, optional VO/beats on creation_config, mute/no-audio
# probe, and configurable nonsense-title patterns.
# Does not replace Gate A/B/C or PATCH /craft — those stay as-is.
# ---------------------------------------------------------------------------

CRAFT_REVIEW_PENDING = "pending"
CRAFT_REVIEW_PASS = "pass"
CRAFT_REVIEW_FAIL = "fail"

# Configurable. Replace the list (or call set_nonsense_title_patterns) to
# extend ops bans without a code change on the hot path. Matched against the
# title head before `·` when present, else the whole title (folded-ish).
# 2026-09-28 (CoS): ANY currency value anywhere in the title — `R$` or `$`
# followed by a number (R$50, R$ 22, $47, $1.5k, US$9). Title-only: the script /
# VO may still say the amount. Supersedes the narrower "billed $N" head (kept
# first so its reason text stays stable for existing callers).
CURRENCY_TITLE_PATTERN = r"(?:r\$|\$)\s*\d"

NONSENSE_TITLE_PATTERNS: list[str] = [
    # "billed $58" / "billed $58 when…" spam heads without a real claim stake
    r"^billed\s*\$\d+\b",
    # bare series-only titles (no spoken claim)
    r"^(copilot\s+credits|ia|agent\s+memory|crewai|local|claude\s+code)\s+\d+\s*$",
    CURRENCY_TITLE_PATTERN,
]

_NONSENSE_TITLE_RES: list[re.Pattern[str]] | None = None

NONSENSE_TITLE_REASON = (
    "title matches configurable nonsense/spam pattern "
    "(e.g. billed $N head or bare series nn) — park via reject"
)
EMPTY_SCRIPT_REASON = "script is empty — craft persist or re-render before publish"

# Stale render (2026-10-06, OS Agent traps #1319/#1328): PATCH /craft re-scores
# craft_review from the SAVED text, so after a text fix a video could read
# status=review + craft_review=pass while its mp4 is still the OLD render the
# VM failed. PATCH /craft on a rendered video now writes this marker into
# creation_config; every publish-gate caller (approve, skip-gate auto-approve
# in _finalize, publish loop, review_ready digest, craft_review re-score)
# refuses it until a re-render completes (_finalize drops the marker).
STALE_RENDER_KEY = "stale_render"
STALE_RENDER_REASON = (
    "render is stale: script/creation_config were edited via PATCH /craft after "
    "this mp4 was rendered — requeue and wait for the re-render before approve/publish")


def stale_render_of(creation_config) -> dict | None:
    """The stale-render marker when present (and truthy), else None."""
    rec = _as_dict(creation_config).get(STALE_RENDER_KEY)
    if not rec:
        return None
    return rec if isinstance(rec, dict) else {"marked": True}


def mark_stale_render(creation_config, *, video_path: str | None, fields) -> str:
    """creation_config JSON with the stale-render marker set."""
    from app.models import utcnow
    cc = dict(_as_dict(creation_config))
    cc[STALE_RENDER_KEY] = {"video_path": video_path, "fields": sorted(fields),
                            "at": utcnow().isoformat()}
    return json.dumps(cc)


def clear_stale_render(creation_config):
    """Drop the marker (a re-render completed). Returns the input unchanged
    when there is no marker, else JSON without it."""
    cc = _as_dict(creation_config)
    if STALE_RENDER_KEY not in cc:
        return creation_config
    cc = {k: v for k, v in cc.items() if k != STALE_RENDER_KEY}
    return json.dumps(cc)
MISSING_VO_BEATS_REASON = (
    "creation_config has no VO/beats — need beats[] (or omit creation_config for legacy)"
)
MUTE_AUDIO_REASON = "video has no audio track (mute / missing narration) — reject"
CURRENCY_TITLE_REASON = (
    "title carries a currency value (R$N / $N) — not allowed in titles; keep the "
    "amount in the script/VO and retitle, or park via reject"
)


def set_nonsense_title_patterns(patterns: list[str] | None) -> None:
    """Replace the nonsense-title regex list and clear the compiled cache."""
    global NONSENSE_TITLE_PATTERNS, _NONSENSE_TITLE_RES
    NONSENSE_TITLE_PATTERNS = list(patterns or [])
    _NONSENSE_TITLE_RES = None


def _nonsense_res() -> list[re.Pattern[str]]:
    global _NONSENSE_TITLE_RES
    if _NONSENSE_TITLE_RES is None:
        _NONSENSE_TITLE_RES = [
            re.compile(p, re.IGNORECASE) for p in NONSENSE_TITLE_PATTERNS if p
        ]
    return _NONSENSE_TITLE_RES


def _nonsense_reason_for(rx: re.Pattern[str]) -> str:
    return (CURRENCY_TITLE_REASON if rx.pattern == CURRENCY_TITLE_PATTERN
            else NONSENSE_TITLE_REASON)


_CURRENCY_TITLE_RE = re.compile(CURRENCY_TITLE_PATTERN, re.IGNORECASE)


def currency_in_text(text: str | None) -> bool:
    """True when text carries `R$`/`$` followed by a number (the title ban shape)."""
    return bool(_CURRENCY_TITLE_RE.search(text or ""))


def _title_head_for_nonsense(title: str | None) -> str:
    raw = (title or "").strip()
    if not raw:
        return ""
    # Prefer the spoken head before · series nn (same split as Credits/IA lock).
    if "·" in raw:
        return raw.split("·", 1)[0].strip()
    return raw


def nonsense_title_reason(title: str | None) -> str | None:
    """None when the title is not nonsense-spam under the configured patterns."""
    head = _title_head_for_nonsense(title)
    whole = (title or "").strip()
    if not head and not whole:
        return None  # empty title is title_gate's job
    for rx in _nonsense_res():
        if head and rx.search(head):
            return _nonsense_reason_for(rx)
        if whole and rx.search(whole):
            return _nonsense_reason_for(rx)
    return None


def script_nonempty(script: str | None) -> bool:
    return bool((script or "").strip())


def creation_config_has_vo_beats(creation_config) -> bool:
    """Optional VO/beats presence for the publish craft gate.

    - No creation_config / empty dict → True (pré-gate / legacy inventory).
    - beats non-empty list → True.
    - beats=[] or used_fallback without beats → False.
    """
    cc = _as_dict(creation_config)
    if not cc:
        return True
    beats = cc.get("beats")
    if isinstance(beats, list) and len(beats) > 0:
        return True
    if cc.get("used_fallback") and not (isinstance(beats, list) and beats):
        return False
    if "beats" in cc and isinstance(beats, list) and len(beats) == 0:
        return False
    # creation_config present without a beats key — treat as legacy snapshot.
    return True


def probe_has_audio(path: str | None) -> bool | None:
    """Audio-track probe via ffprobe.

    Returns:
      True  — file exists and has ≥1 audio stream
      False — file exists and has zero audio streams (mute)
      None  — path missing / unreadable / ffprobe failed (skip mute reject)
    """
    if not path:
        return None
    from pathlib import Path
    import subprocess

    p = Path(path)
    if not p.is_file():
        return None
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "a",
                "-show_entries", "stream=index",
                "-of", "csv=p=0",
                str(p),
            ],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    lines = [ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()]
    return bool(lines)


def publish_craft_block_reason(
    *,
    title: str | None,
    script: str | None,
    creation_config=None,
    content_format: str | None = "short",
    video_path: str | None = None,
    require_vo_beats: bool = True,
    check_audio: bool = True,
) -> str | None:
    """First publish-blocking craft reason, or None when eligible.

    Order (operator-readable): stale render (PATCH /craft after the mp4) →
    nonsense title → existing review_gate (title
    lock + Gate A/B/C) → empty script → provided-script hook alignment
    (``script_source=provided`` only) → optional VO/beats → mute audio.
    Long-form still requires script (+ audio when checkable); A/B/C stay exempt
    via review_gate_reason.
    """
    if stale_render_of(creation_config):
        return STALE_RENDER_REASON
    blocked = nonsense_title_reason(title)
    if blocked:
        return blocked
    blocked = review_gate_reason(title, content_format, creation_config)
    if blocked:
        return blocked
    if not script_nonempty(script):
        return EMPTY_SCRIPT_REASON
    if is_provided_script(creation_config):
        # Provided VO is spoken verbatim — never regenerated to fix the hook.
        blocked = provided_script_hook_reason(script, title=title)
        if blocked:
            return blocked
    if require_vo_beats and not creation_config_has_vo_beats(creation_config):
        return MISSING_VO_BEATS_REASON
    if check_audio:
        audio = probe_has_audio(video_path)
        if audio is False:
            return MUTE_AUDIO_REASON
    return None


def evaluate_craft_review(
    *,
    title: str | None,
    script: str | None,
    creation_config=None,
    content_format: str | None = "short",
    video_path: str | None = None,
    require_vo_beats: bool = True,
    check_audio: bool = True,
) -> tuple[str, str | None]:
    """Return (pass|fail, reason). Does not mutate the video row."""
    reason = publish_craft_block_reason(
        title=title,
        script=script,
        creation_config=creation_config,
        content_format=content_format,
        video_path=video_path,
        require_vo_beats=require_vo_beats,
        check_audio=check_audio,
    )
    if reason:
        return CRAFT_REVIEW_FAIL, reason
    return CRAFT_REVIEW_PASS, None


def apply_craft_review_to_video(video, *, content_format: str | None = "short",
                                require_vo_beats: bool = True,
                                check_audio: bool = True) -> tuple[str, str | None]:
    """Evaluate and write video.craft_review. Returns (status, reason)."""
    status, reason = evaluate_craft_review(
        title=video.title,
        script=video.script,
        creation_config=video.creation_config,
        content_format=content_format,
        video_path=video.video_path,
        require_vo_beats=require_vo_beats,
        check_audio=check_audio,
    )
    video.craft_review = status
    return status, reason


# Text cards must say something (Channels 2026-10-03, #1385 VM FAIL): card 4
# rendered "Your PNG Channels Manager YouTube" — a Shorts diagram demoted to
# a text card by joining its node labels (loose nouns, no verb). A
# statement/quote card needs a verb and must not be only labels/capitalized
# nouns. POS-free heuristic: a small EN/PT verb lexicon + inflection endings.
TEXT_CARD_TYPES = frozenset({"statement", "quote"})
_VERB_WORDS = frozenset("""
is are was were be been being am isn't aren't wasn't weren't it's that's
there's here's what's who's he's she's they're we're you're i'm let's
has have had having hasn't haven't hadn't do does did doing don't doesn't
didn't can can't cannot could couldn't will won't would wouldn't shall should
shouldn't must may might need needs go goes went gone get gets got make makes
made run runs ran ship ships show shows take takes took say says said use
uses work works break breaks broke fail fails cost costs pay pays save saves
read reads write writes wrote see sees saw know knows knew think thinks
thought land lands hit hits stop stops start starts keep keeps kept turn
turns become becomes became give gives gave come comes came leave leaves
left put puts set sets return returns call calls answer answers find finds
found fit fits lose loses lost win wins won cut cuts drop drops eat eats ate
burn burns hold holds held send sends sent spend spends spent build builds
built feel feels felt try tries mean means meant want wants look looks seem
seems tell tells told ask asks let lets help helps move moves pick picks
open opens close closes push pushes pull pulls ask asks skip skips wait
waits beat beats hang hangs crash crashes swap swaps load loads fill fills
stay stays sit sits grow grows rise rises fall falls fell paid threw thrown
bought brought caught taught sold told spent lent meant built burnt drove
wrote broke spoke chose froze ate began ran swam sang rang drank shrank
é são era eram foi foram ser sendo sido está estão estava estavam esteve
tem têm tinha tinham teve há havia vai vão ia foi fez faz fazem fazia pode
podem podia pôde deve devem devia precisa precisam roda rodam rodou cabe
cabem coube dá dão deu vê viu sabe sei quer querem custa custam custou
gasta gastou paga pagou para param parou trava travou cai caiu sobe subiu
volta voltou fica ficou ficam usa usou usam mostra mostrou leva levou
chega chegou passa passou entra entrou sai saiu põe pôs diz disse dizem
pensa pensam espera esperam responde respondem aguenta aguentam enche enchem
erra erram mente acerta acertam ganha ganhou perde perdeu muda mudou
vem vêm veio vieram vou extrai extraio extraem lê leem leio
""".split())
_VERB_ENDINGS_EN = ("ed", "ing")
_VERB_ENDINGS_PT = ("ou", "aram", "eram", "iram", "ava", "avam", "ando", "endo",
                    "indo", "ado", "ido", "ará", "erá", "irá", "aria", "eria", "eu", "iu")
# EN base verbs: base, +s/+es, +ed, +ing are all accepted.
_EN_VERB_BASES = frozenset("""
pay treat check trust reach repeat sign earn walk join ship show run cost save
read write see know think land hit stop start keep turn become give come leave
put set return call answer find fit lose win cut drop eat burn hold send spend
build feel try mean want look seem tell ask let help move pick open close push
pull skip wait beat hang crash swap load fill stay sit grow rise fall go get
make take say use work break fail need throw ignore miss fix test measure
count print log retry lock leak own owe charge bill bump blow eat pass ping
post block catch kill wake sleep page route rot scale swallow bite cache cap
compare decide deploy drift explain hide fire guess hire learn list match
lie die pin plan prove quit rank refuse remember render replace reply rewrite
roll serve sell share slow speed split steal store switch teach thank track
trade train trigger trip type update watch wipe wonder wrap
""".split())
# PT infinitives: 3sg/3pl present, preterite, imperfect, gerund, participle.
_PT_VERBS = """
comprar segurar nascer sobrar revisar esconder desmentir medir construir
escolher mandar contar encolher quantizar cortar caber obedecer reservar falar
cobrar criar desenhar morar deixar acender pesar continuar confiar sobreviver
assinar rodar travar pagar gastar custar usar mostrar levar chegar passar
entrar sair voltar ficar pensar esperar responder aguentar encher errar mentir
acertar ganhar perder mudar testar trocar subir cair estourar vazar quebrar
ler escrever ver saber querer poder dever precisar fazer dizer dar ter ser
estar ir vir pôr achar cumprir abrir fechar ligar desligar carregar baixar
subir rodar responder pedir parar começar terminar acabar salvar guardar
lembrar esquecer aprender ensinar explicar decidir provar cair valer render
dobrar caber engolir queimar comer vender servir ajudar matar morrer ganhar
separar perguntar existir extrair resumir ignorar rodar conferir resolver
mandar jogar converter enviar receber chamar trocar somar contar checar
""".split()


_PT_1SG_NOT_VERB = frozenset("""
custo gasto troco conto salvo baixo passo acerto ganho começo servo resumo dobro
desenho seguro continuo
""".split())


def _pt_forms(inf: str) -> set:
    if len(inf) < 3 or inf[-2:] not in ("ar", "er", "ir", "or"):
        return {inf}
    st, k = inf[:-2], inf[-2]
    if k == "a":
        f = ("a", "am", "ou", "aram", "ava", "avam", "ando", "ado", "e", "em", "ei")
    elif k == "e":
        f = ("e", "em", "eu", "eram", "ia", "iam", "endo", "ido", "a", "am")
    else:
        f = ("e", "em", "iu", "iram", "ia", "iam", "indo", "ido", "a", "am")
    out = {inf} | {st + x for x in f}
    # 1sg present for first-person narration ("Eu separo / pergunto"); short
    # stems ("do" ← dar, "lo" ← ler) and 1sg forms that are common nouns or
    # adjectives ("custo", "passo", "resumo", "seguro") are not verbs here.
    if len(inf) >= 6 and st + "o" not in _PT_1SG_NOT_VERB:
        out.add(st + "o")
    return out


_PT_VERB_FORMS = frozenset(w for inf in _PT_VERBS for w in _pt_forms(inf))
# Function words: a card with none of these and ≥3 tokens reads as a label list.
_FUNCTION_WORDS = frozenset("""
a an the of to in on at for with by from and or but not no your my our their
its this that these those every each any all only just than then as if when
o os as um uma uns umas de do da dos das no na nos nas em ao aos à às por
pelo pela com sem e ou mas não nem seu sua seus suas meu minha cada todo
toda só já que se quem mais menos muito
""".split())


_CARD_WORD_RE = re.compile(r"[A-Za-zÀ-ÿ0-9][\w'’À-ÿ\-]*")


def _looks_like_verb(tok: str) -> bool:
    t = tok.lower().replace("’", "'")
    if t in _VERB_WORDS or t in _PT_VERB_FORMS:
        return True
    for suf in ("", "s", "es", "ed", "d", "ing"):
        if suf and not t.endswith(suf):
            continue
        stem = t[: len(t) - len(suf)] if suf else t
        if stem in _EN_VERB_BASES or (suf == "ing" and stem + "e" in _EN_VERB_BASES):
            return True
        if suf in ("ed", "ing") and len(stem) > 2 and stem[-1] == stem[-2] and stem[:-1] in _EN_VERB_BASES:
            return True
        if suf == "es" and stem.endswith("i") and stem[:-1] + "y" in _EN_VERB_BASES:
            return True
    if t.endswith("ied") and t[:-3] + "y" in _EN_VERB_BASES:
        return True
    if len(t) >= 6 and t.endswith(_VERB_ENDINGS_EN):
        return True
    return len(t) >= 5 and t.endswith(_VERB_ENDINGS_PT)


def text_card_reason(text: str | None, labels=None) -> str | None:
    """Why a statement/quote card text is not a sentence, or None when fine:
    no verb, or nothing but node labels / capitalized nouns."""
    toks = _CARD_WORD_RE.findall(str(text or ""))
    if not toks:
        return "empty card"
    if not any(_looks_like_verb(t) for t in toks):
        return f"no verb in card text {text!r}"
    lab = {theme.fold(x).strip() for x in (labels or []) if str(x or "").strip()}
    if lab:
        rest = theme.fold(str(text or ""))
        for x in sorted(lab, key=len, reverse=True):
            rest = rest.replace(x, " ")
        if not re.search(r"[a-z0-9]", rest):
            return f"card text {text!r} is only node labels"
    if len(toks) >= 3 and not any(t.lower() in _FUNCTION_WORDS for t in toks):
        lower_verb = any(_looks_like_verb(t) and not t[:1].isupper() for t in toks)
        capsish = sum(1 for t in toks if t[:1].isupper() or any(c.isdigit() for c in t))
        if not lower_verb and capsish * 2 >= len(toks):
            return f"card text {text!r} is a list of nouns/labels"
    if len(toks) > 1 and all(t[:1].isupper() or t[:1].isdigit() for t in toks):
        if not any(t.lower() in _VERB_WORDS for t in toks):
            return f"card text {text!r} is only capitalized nouns"
    return None


def text_card_hits(beats) -> list[str]:
    """Every statement/quote card that fails text_card_reason."""
    out = []
    for i, b in enumerate(beats or []):
        if isinstance(b, dict) and (b.get("type") or "") in TEXT_CARD_TYPES:
            r = text_card_reason(b.get("text"))
            if r:
                out.append(f"beat[{i}] {b.get('type')}: {r}")
    return out


# Card text rules (VM Gate B content review of RR #1423, 2026-10-03). The
# card copy failed although every card was readable:
#   1. echo_card       — two cards for one spoken sentence (#3 re-quoted the
#                        sentence card #2 already carried). One card per
#                        sentence; a second one only as a verbatim
#                        continuation (the sentence's later words, sharing
#                        < CARD_CONT_OVERLAP_MAX of its content). The
#                        composer keeps one LLM card per sentence (a second
#                        only on a sentence longer than one card hold) and
#                        the sync DP adds continuation clauses only where
#                        the hold cap / first cut needs them.
#   2. unspoken_quote  — text in quote marks that the VO never says (#10
#                        “Se erra o valor, não leu”).
#   3. invented_output — RR terminal/tool-output cards may only show output
#                        the script says (#1 "[gráfico ausente]": pdftotext
#                        never prints that). Extends the #53 teaser rule.
#   4. foreign_term    — RR PT-BR cards: "T3" not "Q3", no English word the
#                        narration does not say ("Split de página").
#   5. near_duplicate  — consecutive cards with near-identical copy (#8
#                        repeated #7), by content-word overlap.
# The composer enforces all five (storyboard._enforce_card_text_rules and
# the sync DP); new short renders carry creation_config["card_text"] and
# Gate B fails any hit.
CARD_TEXT_V1 = "card_text_v1"
CARD_NEAR_DUP = 0.6          # content-stem containment that reads as "the same card"
CARD_CONT_OVERLAP_MAX = 0.3  # a continuation card shares < this with its sentence's card
CARD_CONT_MIN_SPAN_S = round(MID_BEAT_MAX_S + BEAT_GAP_S, 2)  # 2.92: one card can't hold it
_CARD_RULE_SKIP = frozenset({"hook"}) | CTA_TYPES
_FUNC_FOLD = frozenset(theme.fold(w) for w in _FUNCTION_WORDS) | frozenset(STOPWORDS)
_QUOTED_RE = re.compile(r"[“\"«„]([^”\"»“„]{2,}?)[”\"»“]")
_QUOTE_CHARS_RE = re.compile(r"[“”\"«»„]")
_PLACEHOLDER_RE = re.compile(r"^\s*[\[<(][A-Za-zÀ-ÿ][A-Za-zÀ-ÿ \-]+[\]>)]\s*$")
_QUARTER_RE = re.compile(r"\bQ([1-4])\b")
# Output dumps shown as `code` (receipt / log / console) are simulated output.
OUTPUT_CODE_LANGS = frozenset({"text", "txt", "log", "output", "console", "terminal",
                               "plain", "receipt", "bill", "stdout"})
# Real CLI binaries an RR terminal card may invoke (the OUTPUT still has to
# be in the script). Any other binary must itself be said in the narration.
REAL_CLI_TOOLS = frozenset("""
pdftotext pdftoppm pdfimages pdfinfo python python3 pip pip3 uv uvx ollama nvidia-smi
nvtop nvcc git curl wget docker podman llama-cli llama-server llama-bench llama-run
vllm huggingface-cli hf ls cat grep rg head tail jq ps top htop free df du kill make
npm npx node pnpm yarn bun cargo go gcc ffmpeg ffprobe tesseract kubectl ssh scp
rsync tar unzip echo export sed awk sqlite3 psql redis-cli time watch lms mlx_lm
sudo env which wc sort uniq tee xargs
""".split())
# English words that must not appear on a PT-BR RR card unless the narration
# says them (technical loanwords the VO uses — prompt, token, cache — pass
# because they are in the script).
_EN_CARD_WORDS = (frozenset(_EN_VERB_BASES) | frozenset("""
page pages chart charts value values sentinel output input fallback quarter revenue
missing route routes text texts image images vision model models summary legend
axis curve bar bars split splits only just with without from into after before
""".split())) - frozenset("""
render cache log post set ping pin come list track test type plan
""".split())


def _card_stem(tok: str) -> str:
    t = tok
    if len(t) > 4 and t.endswith("s"):
        t = t[:-1]
    return t[:5]


def card_stems(text: str | None) -> set:
    """Content-word stems of a card's copy (folded, function words dropped)."""
    toks = re.findall(r"[a-z0-9]+", theme.fold(str(text or "")))
    return {_card_stem(t) for t in toks
            if (len(t) >= 3 or t.isdigit()) and t not in _FUNC_FOLD}


def card_overlap(a: dict | None, b: dict | None) -> float:
    """Share of the smaller card's content stems that the other card repeats."""
    sa, sb = card_stems(beat_screen_text(a)), card_stems(beat_screen_text(b))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / min(len(sa), len(sb))


def cards_near_duplicate(a: dict | None, b: dict | None) -> bool:
    return card_overlap(a, b) >= CARD_NEAR_DUP - 1e-9


def is_pt_text(text: str | None) -> bool:
    """Rough language test: more PT than EN function words."""
    toks = re.findall(r"[a-zà-ÿ]+", str(text or "").lower())
    pt = sum(1 for t in toks if t in {"o", "os", "um", "uma", "de", "do", "da", "dos",
                                      "das", "não", "que", "é", "com", "pra", "pro",
                                      "você", "eu", "ele", "se", "no", "na", "em", "vai"})
    en = sum(1 for t in toks if t in {"the", "of", "to", "and", "is", "you", "it",
                                      "your", "with", "that", "this", "on", "for"})
    return pt > en


def _script_stems(script: str | None) -> set:
    return {_card_stem(t) for t in _sync_toks(script)}


def unspoken_quotes(beat: dict | None, script: str | None) -> list[str]:
    """Rule 2: quoted copy the narration never says verbatim. A quote card
    renders a quote mark (unless ``plain``), so its whole text counts."""
    if not isinstance(beat, dict) or not script:
        return []
    out = []
    if (beat.get("type") or "") == "quote" and not beat.get("plain"):
        t = str(beat.get("text") or "")
        if t.strip() and not text_in_script(_QUOTE_CHARS_RE.sub(" ", t), script):
            out.append(t)
    for m in _QUOTED_RE.finditer(beat_screen_text(beat)):
        q = m.group(1)
        if not text_in_script(q, script) and q not in out:
            out.append(q)
    return out


def _cli_binary(cmd: str | None) -> str:
    toks = str(cmd or "").strip().split()
    while toks and (toks[0] in ("sudo", "env", "time") or re.match(r"^[A-Z_][A-Z0-9_]*=", toks[0])):
        toks = toks[1:]
    return toks[0].rsplit("/", 1)[-1] if toks else ""


def is_placeholder_line(line: str | None) -> bool:
    """"[gráfico ausente]", "<missing>", "(sem saída)": a narrator's note
    dressed up as tool output — no real tool prints it."""
    return bool(_PLACEHOLDER_RE.match(str(line or "")))


def invented_output(beat: dict | None, script: str | None) -> list[str]:
    """Rule 3 (RR): terminal/tool-output lines the script does not say.
    command: the command line must be a real tool (or said in the script);
    every output line must be said in the script. code: an output dump
    (lang text/log/console/receipt…) follows the output rule; a code snippet
    keeps its illustrative lines but never a placeholder line."""
    if not isinstance(beat, dict):
        return []
    typ = beat.get("type") or ""
    bad: list[str] = []
    if typ == "command":
        cmd = str(beat.get("command") or "").strip()
        if cmd and not text_in_script(cmd, script):
            binary = _cli_binary(cmd)
            if binary.lower() not in REAL_CLI_TOOLS and not text_in_script(binary, script):
                bad.append(cmd)
        for o in beat.get("output") or []:
            o = str(o or "").strip()
            if o and (is_placeholder_line(o) or not text_in_script(o, script)):
                bad.append(o)
    elif typ == "code":
        dump = str(beat.get("lang") or "").strip().lower() in OUTPUT_CODE_LANGS
        for ln in beat.get("lines") or []:
            ln = str(ln or "").strip()
            if ln and (is_placeholder_line(ln) or (dump and not text_in_script(ln, script))):
                bad.append(ln)
    return bad


_QUARTER_CTX_RE = re.compile(r"\b(?:trimestr\w*|receita|faturamento|lucro|balanco|"
                             r"relatorio|vendas|fiscal)\b")
_QUANT_CTX_RE = re.compile(r"\b(?:quantiz\w*|\d+\s*-?\s*bits?|bits?|gguf|gptq|awq|exl2|"
                           r"q\d_k\w*)\b")


def quarter_labels_apply(script: str | None) -> bool:
    """Q1–Q4 on a PT card reads as a business quarter (→ T1–T4) only when the
    narration is about quarters/revenue, is not about quantization ("Q4" =
    4-bit) and does not itself say "Q1–Q4"."""
    f = theme.fold(str(script or ""))
    return (bool(_QUARTER_CTX_RE.search(f)) and not _QUANT_CTX_RE.search(f)
            and not _QUARTER_RE.search(str(script or "")))


def foreign_terms(beat: dict | None, script: str | None) -> list[str]:
    """Rule 4 (RR, PT-BR narration): "Q1–Q4" (PT is T1–T4) and English words
    on screen that the narration never says. Code/command text is exempt
    (identifiers keep their language) except the quarter label in output."""
    if not isinstance(beat, dict):
        return []
    typ = beat.get("type") or ""
    quarters = quarter_labels_apply(script)
    if typ == "command":
        copy = " ".join(str(o) for o in beat.get("output") or [])
        return [f"Q{m.group(1)}" for m in _QUARTER_RE.finditer(copy)] if quarters else []
    if typ == "code":
        return []
    copy = beat_screen_text(beat)
    out = [f"Q{m.group(1)}" for m in _QUARTER_RE.finditer(copy)] if quarters else []
    spoken = set(_sync_toks(script))
    for tok in re.findall(r"[A-Za-z]+", copy):
        t = tok.lower()
        if t in _EN_CARD_WORDS and t not in spoken and tok not in out:
            out.append(tok)
    return out


def unspoken_headline_terms(beat: dict | None, script: str | None) -> list[str]:
    """Generator rule 4 (RR PT-BR): the headline of a statement / term_define
    card uses the narration's own words. Content words (≥4 letters, no
    digits, not an acronym) whose stem the script never says — coined jargon
    like "Valor-sentinela" or "Split de página"."""
    if not isinstance(beat, dict):
        return []
    typ = beat.get("type") or ""
    head = beat.get("text") if typ == "statement" else beat.get("term") if typ == "term_define" else None
    if not head:
        return []
    stems = _script_stems(script)
    out = []
    for tok in re.findall(r"[A-Za-zÀ-ÿ0-9]+", str(head)):
        f = theme.fold(tok)
        if (len(f) < 4 or any(c.isdigit() for c in f) or (tok.isupper() and len(tok) <= 6)
                or f in _FUNC_FOLD):
            continue
        if _card_stem(f) not in stems:
            out.append(tok)
    return out


def _sentence_token_index(script: str | None) -> tuple[list[str], list[int], list[str]]:
    sents = [x.strip() for x in _OVERLAY_SENT_SPLIT_RE.split((script or "").strip()) if x.strip()]
    toks, sid = [], []
    for k, s in enumerate(sents):
        for t in _sync_toks(s):
            toks.append(t)
            sid.append(k)
    return toks, sid, sents


def card_anchors(board, script: str | None) -> list[tuple[int | None, int, int]]:
    """Per card: (script sentence, token position, shown-token length). The
    text of a quote/statement (else its cue) is matched in order in the
    script; shown length is the matched text length (0 for a cue match).
    (None, -1, 0) = hook / cta / unmatched."""
    toks, sid, _ = _sentence_token_index(script)
    out: list[tuple[int | None, int, int]] = []
    cursor = 0
    for i, b in enumerate(board or []):
        typ = (b.get("type") or "") if isinstance(b, dict) else ""
        if i == 0 and typ == "hook":
            ht = _sync_toks(b.get("text") or b.get("cue"))
            if ht and toks[:len(ht)] == ht:
                cursor = len(ht)
            out.append((None, -1, 0))
            continue
        if typ in CTA_TYPES or not isinstance(b, dict):
            out.append((None, -1, 0))
            continue
        cands = [(_sync_toks(b.get("text")), True)] if typ in ("quote", "statement") else []
        cands.append((_sync_toks(b.get("cue")), False))
        pos, shown = -1, 0
        for c, is_text in cands:
            pos = _find_run(toks, c, cursor)
            if pos >= 0:
                shown = len(c) if is_text else 0
                break
        if pos < 0:
            out.append((None, -1, 0))
            continue
        out.append((sid[pos], pos, shown))
        cursor = pos
    return out


def card_sentence_ids(board, script: str | None) -> list[int | None]:
    """Script sentence each card belongs to (None = hook/cta/unmatched)."""
    return [a[0] for a in card_anchors(board, script)]


def sentence_spans(script: str | None, words) -> dict[int, float]:
    """Spoken span (s) per script sentence from TTS word timings (empty when
    the words cannot be aligned)."""
    from app.services.engines import storyboard
    spans: dict[int, list[float]] = {}
    for w in storyboard.annotate_sentences(words, script):
        if "_s" not in w:
            continue
        try:
            t0 = float(w.get("start") or 0.0)
            t1 = t0 + float(w.get("dur") or 0.0)
        except (TypeError, ValueError):
            continue
        lo_hi = spans.setdefault(w["_s"], [t0, t1])
        lo_hi[0], lo_hi[1] = min(lo_hi[0], t0), max(lo_hi[1], t1)
    return {k: round(v[1] - v[0], 3) for k, v in spans.items()}


def sentence_card_cap(span: float | None) -> int:
    """Cards one sentence may carry: 1, plus one verbatim continuation per
    further card-length of speech."""
    if span is None or span <= CARD_CONT_MIN_SPAN_S + 1e-9:
        return 1
    return 1 + int(span // CARD_CONT_MIN_SPAN_S)


def is_continuation_card(beat: dict | None, prev: dict | None, script: str | None, *,
                         pos: int | None = None, prev_pos: int | None = None,
                         prev_len: int = 0) -> bool:
    """A further card on the sentence ``prev`` (the sentence's previous card)
    already carries. A text card must be verbatim narration that starts
    after ``prev``'s own words (its later words, nothing re-said — #1423 #3
    re-quoted the sentence from its first word); a rich card must anchor
    later and not repeat ``prev`` (overlap < CARD_NEAR_DUP)."""
    if not isinstance(beat, dict):
        return False
    if pos is not None and prev_pos is not None and pos < prev_pos + max(1, prev_len):
        return False
    typ = beat.get("type") or ""
    if typ in ("quote", "statement"):
        return text_in_script(_QUOTE_CHARS_RE.sub(" ", str(beat.get("text") or "")), script)
    return card_overlap(beat, prev) < CARD_NEAR_DUP - 1e-9


def card_text_hits(beats, script: str | None, words=None, *, rr: bool = False,
                   pt: bool | None = None) -> list[dict]:
    """Every card-text rule violation on a board: [{"i","check","detail"}]."""
    board = [b for b in (beats or []) if isinstance(b, dict)]
    out: list[dict] = []
    if not board or not script:
        return out
    if pt is None:
        pt = is_pt_text(script)
    anchors = card_anchors(board, script)
    by_sent: dict[int, list[int]] = {}
    prev_mid = None
    for i, b in enumerate(board):
        typ = b.get("type") or ""
        if i == 0 or typ in _CARD_RULE_SKIP:
            prev_mid = None if typ in CTA_TYPES else prev_mid
            continue
        shown = beat_screen_text(b)[:60]
        s, pos, _ = anchors[i]
        if s is not None:
            group = by_sent.setdefault(s, [])
            if group:
                k = group[-1]
                if not is_continuation_card(b, board[k], script, pos=pos,
                                            prev_pos=anchors[k][1], prev_len=anchors[k][2]):
                    out.append({"i": i, "check": "echo_card",
                                "detail": f"card {i} {shown!r} is a second card for the sentence "
                                          f"card {group[0]} already carries"})
            group.append(i)
        for q in unspoken_quotes(b, script):
            out.append({"i": i, "check": "unspoken_quote",
                        "detail": f"card {i} quotes {q!r}, which the narration never says"})
        if rr:
            for ln in invented_output(b, script):
                out.append({"i": i, "check": "invented_output",
                            "detail": f"card {i} ({typ}) shows {ln!r}, output the script never says"})
            if pt:
                for t in foreign_terms(b, script):
                    out.append({"i": i, "check": "foreign_term",
                                "detail": f"card {i} shows {t!r} on a PT-BR card"
                                          + (" (use T1–T4)" if _QUARTER_RE.match(t) else "")})
        if prev_mid is not None and cards_near_duplicate(board[prev_mid], b):
            out.append({"i": i, "check": "near_duplicate",
                        "detail": f"card {i} {shown!r} repeats card {prev_mid} "
                                  f"({card_overlap(board[prev_mid], b):.0%} shared words)"})
        prev_mid = i
    return out


def card_text_marker(beats, script: str | None, words=None, *, brand: str | None = None,
                     content_format: str | None = "short") -> dict | None:
    """creation_config["card_text"] for a new short render (else None)."""
    if (content_format or "short") == "long":
        return None
    rr = (brand or "") in HOOK_PACE_BRANDS
    return {"version": CARD_TEXT_V1, "rr": rr,
            "hits": card_text_hits(beats, script, words, rr=rr)}


def card_text_check_hits(card_text: dict | None) -> list[str]:
    if not isinstance(card_text, dict) or card_text.get("version") != CARD_TEXT_V1:
        return []
    return [f"{h.get('check')}: {h.get('detail')}" for h in card_text.get("hits") or []
            if isinstance(h, dict)]


# Card ⊂ VO (Video Maker P0, council 06/10). The VM reads every card and checks
# it against the VO transcript: a text card must show one whole spoken
# sentence, and each spoken sentence gets one card (continuation: see below). Fragment quotes
# ("sends, and deploys."), half-sentence cards, cards joining two sentences,
# a second card on the same sentence and card text the VO never says were the
# bulk of the week's Gate B FAILs (#1328, #1401, #1402, #1423).
# There is no OCR inside the pipeline: the check reads the rendered card
# source text (beat text/cue, the same copy the HTML draws) against the VO
# script, which edge-tts speaks verbatim (the TTS word boundaries come from
# that script). Punctuation and case are normalised (_sync_toks).
# New short renders carry creation_config["card_vo"]; older renders are not
# re-judged (same pattern as CARD_TEXT_V1).
CARD_VO_V1 = "card_vo_v1"
# Continuation (CoS 06/10): a long sentence may run over several cards, but a
# further TEXT card only counts when it shows the literal NEXT words of the
# same sentence — it starts exactly where the previous text card on that
# sentence stopped (the #70 rule, made strict here). Repeating earlier words
# (echo), skipping words (non-contiguous fragment), a loose tail that does not
# start the sentence, a chain that stops before the sentence ends and an
# unspoken quote still FAIL. A further RICH (object) card on a sentence keeps
# the #70 rule (anchors later, not a near-duplicate). False = the VM's strict
# one-card-per-sentence reading.
CARD_VO_ALLOW_CONTINUATION = True
CARD_VO_GATED = frozenset({"card_not_in_vo", "card_fragment", "card_spans_sentences",
                           "sentence_two_cards", "echo_card"})


def _spoken_sentence_ids(sents: list[str]) -> list[int]:
    return [k for k, s in enumerate(sents)
            if not (is_endcard_vo(s) or contains_subscribe_cta(s))]


def card_vo_hits(beats, script: str | None, *,
                 allow_continuation: bool | None = None) -> list[dict]:
    """Card ⊂ VO violations: [{"i","check","detail"}] (i=None for a sentence).

    Text cards (statement/quote) are matched in the VO after normalising
    punctuation and case:
      card_not_in_vo        the card's words are not spoken in that order;
      card_spans_sentences  the words run across two or more sentences;
      card_fragment         not a whole sentence: a loose part that does not
                            start the sentence, a chain of cards that stops
                            before the sentence ends, or (continuation) words
                            skipped after the previous card;
      echo_card             (continuation) repeats words the previous card on
                            the sentence already showed.
    Every card (object cards via their cue, the hook = sentence 0) is mapped
    to a spoken sentence:
      sentence_two_cards    a sentence carries a further card that is not a
                            valid continuation;
      sentence_no_card      a spoken sentence has no card (informational).
    The endcard/Subscribe sentence and cta/endcard cards are skipped."""
    allow = CARD_VO_ALLOW_CONTINUATION if allow_continuation is None else allow_continuation
    board = [b for b in (beats or []) if isinstance(b, dict)]
    out: list[dict] = []
    if not board or not script:
        return out
    toks, sid, sents = _sentence_token_index(script)
    stoks = [_sync_toks(s) for s in sents]
    s_start: dict[int, int] = {}
    s_end: dict[int, int] = {}
    for p, k in enumerate(sid):
        s_start.setdefault(k, p)
        s_end[k] = p + 1
    spoken = _spoken_sentence_ids(sents)
    anchors = card_anchors(board, script)
    cards_on: dict[int, list[dict]] = {}   # sentence → [{"i","text","pos","end","chain"}]
    cursor = 0
    prev_entry: dict | None = None

    def hit(i, check, detail):
        out.append({"i": i, "check": check, "detail": detail})

    for i, b in enumerate(board):
        typ = b.get("type") or ""
        if typ in CTA_TYPES:
            continue
        shown = str(b.get("text") or "")[:60]
        if i == 0 and typ == "hook":
            ht = _sync_toks(b.get("text") or b.get("cue"))
            e = {"i": 0, "text": False, "pos": 0, "end": 0, "chain": False, "hook": True}
            if ht and toks[:len(ht)] == ht and len(set(sid[:len(ht)])) == 1:
                e.update(text=True, end=len(ht), chain=True)
                cursor = len(ht)
            cards_on.setdefault(0, []).append(e)
            prev_entry = e
            continue
        if typ in TEXT_CARD_TYPES:
            ct = _sync_toks(b.get("text"))
            pos = -1
            # The literal next words of the previous text card come first.
            if (allow and ct and prev_entry and prev_entry["text"]
                    and toks[prev_entry["end"]:prev_entry["end"] + len(ct)] == ct):
                pos = prev_entry["end"]
            if pos < 0:
                pos = _find_run(toks, ct, cursor)
            if pos < 0:
                pos = _find_run(toks, ct, 0)
            if not ct or pos < 0:
                hit(i, "card_not_in_vo", f"card {i} {shown!r} is not a sentence the VO says")
                s = anchors[i][0]
                e = {"i": i, "text": False, "pos": -1, "end": -1, "chain": False}
                if s is not None:
                    cards_on.setdefault(s, []).append(e)
                prev_entry = e
                continue
            cursor = pos
            end = pos + len(ct)
            ss = sorted(set(sid[pos:end]))
            if len(ss) > 1:
                hit(i, "card_spans_sentences", f"card {i} {shown!r} runs across {len(ss)} spoken sentences")
                e = {"i": i, "text": False, "pos": pos, "end": end, "chain": False}
                for k in ss:  # a spanning card sits on every sentence it covers
                    cards_on.setdefault(k, []).append(e)
                prev_entry = e
                continue
            k = ss[0]
            on = cards_on.setdefault(k, [])
            prev = on[-1] if on else None
            e = {"i": i, "text": True, "pos": pos, "end": end, "chain": False}
            whole = sents[k][:80]
            if not allow:
                if ct != stoks[k]:
                    hit(i, "card_fragment", f"card {i} {shown!r} is part of the sentence {whole!r}, "
                                            "not the whole sentence")
            elif prev is None:
                if pos != s_start[k]:
                    hit(i, "card_fragment", f"card {i} {shown!r} is a loose part of the sentence "
                                            f"{whole!r} (does not start it)")
                else:
                    e["chain"] = True
            elif prev["text"]:
                if pos == prev["end"]:
                    e["chain"] = prev["chain"]  # literal next words: a continuation
                    prev["continued"] = True
                elif pos < prev["end"]:
                    hit(i, "echo_card", f"card {i} {shown!r} repeats words card {prev['i']} already "
                                        f"shows of the sentence {whole!r}")
                else:
                    hit(i, "card_fragment", f"card {i} {shown!r} skips words after card {prev['i']} "
                                            f"in the sentence {whole!r} (not the next words)")
            else:
                if pos == s_start[k]:
                    hit(i, "sentence_two_cards", f"card {i} {shown!r} re-cards the sentence {whole!r} "
                                                 f"card {prev['i']} already carries")
                else:
                    hit(i, "card_fragment", f"card {i} {shown!r} is a loose part of the sentence "
                                            f"{whole!r} (does not start it)")
            on.append(e)
            prev_entry = e
            continue
        # Rich / object card: anchored by its cue.
        s, apos, _ = anchors[i]
        e = {"i": i, "text": False, "pos": apos, "end": apos, "chain": False}
        if s is not None:
            on = cards_on.setdefault(s, [])
            prev = on[-1] if on else None
            if allow and prev is not None:
                plen = (prev["end"] - prev["pos"]) if prev["text"] else 0
                if not is_continuation_card(b, board[prev["i"]], script, pos=apos,
                                            prev_pos=prev["pos"], prev_len=plen):
                    hit(i, "sentence_two_cards", f"cards {prev['i']}, {i} both sit on the sentence "
                                                 f"{sents[s][:80]!r} (not a continuation)")
            on.append(e)
        prev_entry = e
    for k in spoken:
        on = cards_on.get(k, [])
        if not on:
            hit(None, "sentence_no_card", f"the sentence {sents[k][:80]!r} has no card")
            continue
        if not allow:
            if len(on) > 1:
                hit(on[1]["i"], "sentence_two_cards",
                    f"cards {', '.join(str(e['i']) for e in on)} all sit on the sentence "
                    f"{sents[k][:80]!r} (one card per sentence)")
            continue
        # A chain of text cards must reach the end of its sentence (the hook
        # is exempt: its claim may be the head of sentence 0).
        for e in on:
            if (e["text"] and e["chain"] and not e.get("continued") and not e.get("hook")
                    and e["end"] != s_end[k]):
                hit(e["i"], "card_fragment",
                    f"card {e['i']} stops before the sentence {sents[k][:80]!r} ends "
                    "(no card shows its next words)")
    return out


def card_vo_marker(beats, script: str | None,
                   content_format: str | None = "short") -> dict | None:
    """creation_config["card_vo"] for a new short render (else None)."""
    if (content_format or "short") == "long":
        return None
    return {"version": CARD_VO_V1, "source": "card_source_text+vo_script",
            "continuation": CARD_VO_ALLOW_CONTINUATION,
            "hits": card_vo_hits(beats, script)}


def card_vo_check_hits(card_vo: dict | None) -> list[str]:
    """Gated Card ⊂ VO hits of a CARD_VO_V1 marker (sentence_no_card is a note)."""
    if not isinstance(card_vo, dict) or card_vo.get("version") != CARD_VO_V1:
        return []
    return [f"{h.get('check')}: {h.get('detail')}" for h in card_vo.get("hits") or []
            if isinstance(h, dict) and h.get("check") in CARD_VO_GATED]


# RR sentence pace (CoS 2026-10-03): one card per spoken sentence holds when a
# sentence is spoken within one card hold + its lead (2.8s + 1.1s ≈ 3.9s);
# continuation cards stay the exception. Soft target only — the script
# generator asks once for a rewrite and the render logs/records the measure;
# it is never a gate check.
SENTENCE_SPOKEN_MAX_S = round(MID_BEAT_MAX_S + SYNC_LEAD_MAX_S, 2)  # 3.9
SPOKEN_WORDS_PER_S = 3.2  # edge-tts pt-BR/en ~3.1–3.5 w/s (#1423); low = conservative
SENTENCE_MAX_WORDS = int(SENTENCE_SPOKEN_MAX_S * SPOKEN_WORDS_PER_S)  # 12


def long_spoken_sentences(script: str | None, words=None,
                          max_s: float = SENTENCE_SPOKEN_MAX_S) -> list[dict]:
    """Script sentences spoken longer than ``max_s``: [{"i","text","secs","how"}].
    ``secs`` is measured from TTS word timings when given (how="tts"), else
    estimated at SPOKEN_WORDS_PER_S (how="estimate"). The endcard VO is skipped."""
    sents = [x.strip() for x in _OVERLAY_SENT_SPLIT_RE.split((script or "").strip()) if x.strip()]
    spans = sentence_spans(script, words) if words else {}
    out = []
    for i, sent in enumerate(sents):
        if is_endcard_vo(sent) or contains_subscribe_cta(sent):
            continue
        if i in spans:
            secs, how = spans[i], "tts"
        else:
            secs, how = round(len(_sync_toks(sent)) / SPOKEN_WORDS_PER_S, 2), "estimate"
        if secs > max_s + 1e-9:
            out.append({"i": i, "text": sent, "secs": secs, "how": how})
    return out


def sentence_pace_marker(script: str | None, words, brand: str | None,
                         content_format: str | None = "short") -> dict | None:
    """creation_config["sentence_pace"] for RR shorts (informational, no gate)."""
    if (content_format or "short") == "long" or (brand or "") not in HOOK_PACE_BRANDS:
        return None
    over = long_spoken_sentences(script, words)
    return {"max_s": SENTENCE_SPOKEN_MAX_S, "over": [{k: h[k] for k in ("i", "secs", "how")} for h in over]}
