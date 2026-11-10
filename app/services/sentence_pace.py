"""Per-channel spoken-sentence pace for generated short scripts (PO CM cut, RR).

A configured channel/brand gets a per-sentence spoken-duration *target* (prompt
guidance) and a hard *cap* (deterministic post-pass) — RR default 2.2s / 2.5s.
Brands with no entry (OS, other tenants) keep the generator exactly as before.

Config: ``settings.sentence_pace`` (env MANAGER_SENTENCE_PACE, JSON object)
maps a key to ``{"target_s": float, "cap_s": float}``. Lookup order (first hit
wins): ``"channel:<id>"``, the channel slug, the brand. Default
``{"rr": {"target_s": 2.2, "cap_s": 2.5}}``.

Estimator: spoken first-word → last-word span of one sentence, calibrated on
real edge-tts pt-BR-AntonioNeural (rate -8%) word timings — #1455, #1464,
#1443 and 9 more RR-style scripts, 87 sentences: fit MAE 0.135s (max 0.47s),
leave-one-script-out MAE 0.143s (p90 0.31s); the old words/3.2 rule had MAE
0.354s (max 1.26s). Numbers are spelled out (pt) and acronyms (IA, JSON, API…)
are counted as spoken letters/syllables, so they weigh more than a normal word.

Post-pass (``enforce``), per sentence estimated over the cap:
  1. shorten — drop conservative filler words (``FILLER_WORDS``) only;
  2. split — at ; : , or a conjunction, only into two halves that are each
     a clause (≥2 words, a verb-like token), the second never starting with
     "E", no echo (the second half repeats no content word of the first);
     the split conjunction "e" is the only other word that may be dropped.
  3. otherwise the sentence is left as is and reported ("unsplittable").
The hook (sentence 0: title = hook = frame0 = thumb) and the series closer
("Se inscreve. Próxima armadilha de IA.") are never touched. Whether a half is
a *grammatically* complete sentence is not decidable here — the checkable
parts are enforced, the rest is in the prompt guidance.
"""
from __future__ import annotations

import re
import unicodedata

# --- estimator (pt-BR-AntonioNeural, rate -8%) ------------------------------
EST_SYL_S = 0.109      # per spoken syllable
EST_WORD_S = 0.065     # per token
EST_NUMBER_S = 0.211   # extra per number token (spelled out as well)
EST_PUNCT_S = 0.417    # per internal , ; : (TTS pause)
EST_BASE_S = 0.41      # intercept
INTER_SENTENCE_PAUSE_S = 0.94   # measured PT pause after a full stop (#1455)

# spoken syllables of common acronyms (pt-BR voice)
ACRONYM_SYL = {"IA": 2, "JSON": 2, "API": 3, "VRAM": 2, "GPU": 3, "SSD": 3, "RAM": 1,
               "CLI": 3, "PDF": 3, "SQL": 3, "LLM": 3, "RAG": 1, "URL": 3, "CPU": 3}

_U = ["zero", "um", "dois", "três", "quatro", "cinco", "seis", "sete", "oito", "nove", "dez",
      "onze", "doze", "treze", "catorze", "quinze", "dezesseis", "dezessete", "dezoito", "dezenove"]
_T = ["", "", "vinte", "trinta", "quarenta", "cinquenta", "sessenta", "setenta", "oitenta", "noventa"]
_H = ["", "cento", "duzentos", "trezentos", "quatrocentos", "quinhentos", "seiscentos",
      "setecentos", "oitocentos", "novecentos"]
_TOK_RE = re.compile(r"\d+|[^\W\d_]+", re.UNICODE)
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")


def _spell_pt(n: int) -> str:
    if n < 20:
        return _U[n]
    if n < 100:
        return _T[n // 10] + ("" if n % 10 == 0 else " e " + _U[n % 10])
    if n == 100:
        return "cem"
    if n < 1000:
        return _H[n // 100] + ("" if n % 100 == 0 else " e " + _spell_pt(n % 100))
    if n < 1_000_000:
        k, r = divmod(n, 1000)
        return ("mil" if k == 1 else _spell_pt(k) + " mil") + ("" if r == 0 else " e " + _spell_pt(r))
    return " ".join(_U[int(d)] for d in str(n))


def _fold(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", (s or "").lower())
                   if unicodedata.category(c) != "Mn")


def _syl(word: str) -> int:
    return max(1, len(re.findall(r"[aeiouy]+", _fold(word))))


def _tok_syl(tok: str) -> int:
    if tok.isdigit():
        return sum(_syl(w) for w in _spell_pt(int(tok)).split())
    if tok in ACRONYM_SYL:
        return ACRONYM_SYL[tok]
    if tok.isupper() and len(tok) <= 4:
        return len(tok)          # spelled letter by letter
    return _syl(tok)


def estimate_spoken_s(sentence: str | None) -> float:
    """Estimated first→last word span (s) of one sentence (see module doc)."""
    s = sentence or ""
    toks = _TOK_RE.findall(s)
    if not toks:
        return 0.0
    syl = sum(_tok_syl(t) for t in toks)
    nums = sum(1 for t in toks if t.isdigit())
    punct = len(re.findall(r"[,;:]", s))
    return round(EST_BASE_S + EST_SYL_S * syl + EST_WORD_S * len(toks)
                 + EST_NUMBER_S * nums + EST_PUNCT_S * punct, 3)


# --- config -------------------------------------------------------------------
def pace_for(params: dict | None) -> dict | None:
    """{"target_s", "cap_s", "key"} for this render's channel, or None (unchanged
    generator). Shorts only."""
    p = params or {}
    if (p.get("content_format") or "short") == "long":
        return None
    try:
        from app.config import settings
        cfg = getattr(settings, "sentence_pace", None) or {}
    except Exception:  # pragma: no cover
        cfg = {}
    if not isinstance(cfg, dict):
        return None
    keys = []
    if p.get("channel_id") is not None:
        keys.append(f"channel:{p.get('channel_id')}")
    for k in (p.get("channel_slug"), p.get("brand")):
        if k:
            keys.append(str(k))
    for k in keys:
        v = cfg.get(k)
        if isinstance(v, dict):
            try:
                target, cap = float(v["target_s"]), float(v["cap_s"])
            except (KeyError, TypeError, ValueError):
                continue
            if target > 0 and cap >= target:
                return {"target_s": target, "cap_s": cap, "key": k}
    return None


def target_words(target_s: float) -> int:
    """Rough word budget for the prompt (≈ 3.2 spoken words/s)."""
    return max(4, int(round(target_s * 3.2)))


def prompt_guidance(pace: dict) -> str:
    t, c = pace["target_s"], pace["cap_s"]
    return (f" SENTENCE PACE (hard rule): every spoken sentence after the hook takes about "
            f"{t:.1f}s to say (~{target_words(t)} words) and NEVER more than {c:.1f}s "
            f"(~{target_words(c)} words; numbers, 'IA' and 'JSON' count as long words). "
            "Shorten first: cut filler words. Split only when the sentence really holds two "
            "ideas, and then into two COMPLETE sentences, each with its own subject and verb — "
            "never a fragment, never a sentence starting with 'E', and the second must not "
            "repeat the first one's verb or noun. Never change the opening hook sentence or "
            "the closing subscribe line (endcard VO).")


# --- deterministic post-pass ----------------------------------------------------
# Conservative pt-BR filler (intensifier / hedge adverbs). Never articles,
# negations, numbers or nouns — dropping those changes meaning or grammar.
FILLER_WORDS = frozenset({
    "realmente", "simplesmente", "basicamente", "literalmente", "totalmente",
    "exatamente", "praticamente", "certamente", "claramente", "justamente",
    "obviamente", "absolutamente", "extremamente", "bastante", "muito", "super",
    "tipo", "aí", "então,",
})
# a conjunction the split consumes (the second half never starts with "E")
SPLIT_DROP_WORDS = frozenset({"e"})
_CONJ = ("mas", "porque", "então", "e")
_STOP = frozenset(_fold(w) for w in (
    "o a os as um uma uns umas de do da dos das em no na nos nas por pelo pela pra pro "
    "para com sem que se e ou mas porque então quando como eu você ele ela eles elas "
    "isso isto esse essa este esta aquele aquela meu minha seu sua nao não é já só "
    "mais menos cada todo toda tudo lá aqui ainda também depois antes muito").split())
_VERB_HINT = frozenset(_fold(w) for w in (
    "é são está estão era foi foram fica ficam tem têm vai vão vem vêm traz some sai "
    "cabe lê leio volta voltam faz fazem diz pede peço pergunta pergunto abre abro "
    "confere confiro trato serve roda rodo corrijo decide entra sobra pesa custa "
    "soma estoura chama chamou devolve escolhe escolhendo precisa precisava mede "
    "erra acerta aprovo aprovei deixa deixo uso usa salva salvo mando manda vê vejo dá dou "
    "corta resume lembra reenvia fecho abro confere inventa mistura cria troca explica "
    "perde gasta tenta coloca repete melhora calcula escreve bate acha percebe sabe "
    "esquece lembro funciona quebra trava responde").split())
_VERB_SUFFIX_RE = re.compile(r"(ar|er|ir|ou|am|em|ei|eu|iu|ava|avam|ia|iam|ando|endo|indo|aram|eram|iram)$")


def _words(s: str) -> list[str]:
    return [w for w in re.findall(r"[^\W_]+(?:[-'][^\W_]+)*", s or "", re.UNICODE)]


def _is_protected(i: int, sent: str) -> bool:
    if i == 0:
        return True     # hook: title = hook = frame0 = thumb
    from app.services import craft
    return craft.is_endcard_vo_sentence(sent) or craft.contains_subscribe_cta(sent)


# a half may not open with a subordinator / gerund (it would be a fragment:
# "Quando não cabe mais." / "Puxando a resposta.") nor with "E"
_BAD_START = frozenset(_fold(w) for w in (
    "e quando se que enquanto embora caso porque pois como onde cujo cuja "
    "até após durante sem").split())
_GERUND_RE = re.compile(r"(ando|endo|indo)$")


def _verb_like(raw: str) -> bool:
    low = raw.lower()
    if low == "é":
        return True
    f = _fold(raw)
    if f == "e":                      # the conjunction, not the verb "é"
        return False
    return f in _VERB_HINT or (len(f) >= 4 and bool(_VERB_SUFFIX_RE.search(f))
                               and not _GERUND_RE.search(f))


def looks_clausal(s: str) -> bool:
    """Checkable part of "complete sentence": ≥2 words, a finite-verb-like
    token, and no gerund / "E" opening; a subordinator opening ("Quando …")
    only with its main clause after a comma ("Quando não cabe, corta.")."""
    ws = _words(s)
    if len(ws) < 2 or (len(ws) == 2 and not _verb_like(ws[1])):
        return False      # "Ninguém percebe." is a clause; "Confiro tudo"-style stubs need a verb
    first = _fold(ws[0])
    if first == "e" or (_GERUND_RE.search(first) and first not in _STOP
                        and first not in _BAD_START):
        return False
    if first in _BAD_START:
        head, sep, main = s.partition(",")
        if not sep or len(_words(main)) < 2:
            return False
        return True
    return any(_verb_like(w) for w in ws)


def content_words(s: str) -> set[str]:
    """Folded content words (≥4 letters, not a stop word), prefix-stemmed."""
    out = set()
    for w in _words(s):
        f = _fold(w)
        if len(f) >= 4 and f not in _STOP and not f.isdigit():
            out.add(f[:5])
    return out


def is_echo(first: str, second: str) -> bool:
    return bool(content_words(first) & content_words(second))


def _finish(s: str) -> str:
    s = s.strip().strip(",;:").strip()
    if not s:
        return s
    s = s[0].upper() + s[1:]
    return s if s[-1] in ".!?…" else s + "."


def _drop_filler(sent: str) -> tuple[str, list[str]]:
    dropped, out = [], []
    for tok in sent.split():
        core = _fold(tok.strip(".,;:!?…"))
        if core in FILLER_WORDS and not tok.strip(".,;:!?…").isupper():
            dropped.append(tok.strip(".,;:!?…"))
            # keep trailing punctuation on the previous token
            tail = tok[len(tok.rstrip(".,;:!?…")):]
            if tail and out:
                out[-1] = out[-1] + tail
            continue
        out.append(tok)
    return " ".join(out), dropped


def _candidates(sent: str):
    body = sent.rstrip(".!?…")
    for m in re.finditer(r"[;:]\s+", body):
        yield body[:m.start()], body[m.end():], None
    enumeration = body.count(",") >= 2 or ":" in body
    for m in re.finditer(r",\s+", body):
        right = body[m.end():]
        rw = right.split()
        if enumeration and not (rw and _fold(rw[0]) in _CONJ and "," not in right):
            continue   # never cut a list ("valor, prazo, multa, e …")
        if rw and _fold(rw[0]) in _CONJ:
            if _fold(rw[0]) == "e":
                yield body[:m.start()], " ".join(rw[1:]), rw[0]
            else:
                yield body[:m.start()], right, None
        else:
            yield body[:m.start()], right, None
    for m in re.finditer(r"\s+(e|mas|porque|então)\s+", body, re.IGNORECASE):
        left, right = body[:m.start()], body[m.end():]
        if left.endswith(","):
            continue
        conj = m.group(1)
        if _fold(conj) == "e":
            yield left, right, conj
        else:
            yield left, conj + " " + right, None


def _best_split(sent: str, cap: float):
    best = None
    for left, right, dropped in _candidates(sent):
        a, b = _finish(left), _finish(right)
        if not a or not b or b.split()[0].lower() == "e":
            continue
        if not (looks_clausal(a) and looks_clausal(b)) or is_echo(a, b):
            continue
        if _fold(_words(b)[0]) in _BAD_START:   # second half never opens subordinate
            continue
        ea, eb = estimate_spoken_s(a), estimate_spoken_s(b)
        score = (max(ea, eb) > cap + 1e-9, max(ea, eb), abs(ea - eb))
        if best is None or score < best[0]:
            best = (score, a, b, dropped)
    return best


def enforce(script: str | None, pace: dict) -> tuple[str, dict]:
    """Shorten / split every non-protected sentence estimated over ``cap_s``.

    Returns (script, report). report = {"target_s", "cap_s", "changes": [...],
    "unsplittable": [...], "splits": n, "dropped": [...]}."""
    cap = float(pace["cap_s"])
    text = (script or "").strip()
    sents = [x.strip() for x in _SENT_SPLIT_RE.split(text) if x.strip()]
    rep = {"target_s": float(pace["target_s"]), "cap_s": cap, "changes": [],
           "unsplittable": [], "splits": 0, "dropped": []}
    out: list[str] = []
    for i, sent in enumerate(sents):
        if _is_protected(i, sent) or estimate_spoken_s(sent) <= cap + 1e-9:
            out.append(sent)
            continue
        before = estimate_spoken_s(sent)
        short, dropped = _drop_filler(sent)
        if dropped:
            rep["dropped"] += dropped
        if estimate_spoken_s(short) <= cap + 1e-9:
            out.append(short)
            rep["changes"].append({"i": i, "kind": "shorten", "from": sent, "to": [short],
                                   "est_before": before, "est_after": [estimate_spoken_s(short)]})
            continue
        pieces = _split_rec(short, cap, rep, depth=2)
        for piece in pieces:
            if estimate_spoken_s(piece) > cap + 1e-9:
                rep["unsplittable"].append({"i": i, "text": piece, "est": estimate_spoken_s(piece)})
        if len(pieces) > 1:
            rep["changes"].append({"i": i, "kind": "split", "from": sent, "to": pieces,
                                   "est_before": before,
                                   "est_after": [estimate_spoken_s(p) for p in pieces]})
        out.extend(pieces)
    return " ".join(out), rep


def _split_rec(sent: str, cap: float, rep: dict, depth: int) -> list[str]:
    if depth <= 0 or estimate_spoken_s(sent) <= cap + 1e-9:
        return [sent]
    best = _best_split(sent, cap)
    if best is None:
        return [sent]
    _, a, b, dropped = best
    rep["splits"] += 1
    if dropped:
        rep["dropped"].append(dropped)
    return _split_rec(a, cap, rep, depth - 1) + _split_rec(b, cap, rep, depth - 1)


def over_cap(script: str | None, cap: float) -> list[dict]:
    """Non-protected sentences estimated over ``cap`` (for the rewrite ask / tests)."""
    sents = [x.strip() for x in _SENT_SPLIT_RE.split((script or "").strip()) if x.strip()]
    return [{"i": i, "text": s, "est": estimate_spoken_s(s)} for i, s in enumerate(sents)
            if not _is_protected(i, s) and estimate_spoken_s(s) > cap + 1e-9]


def estimated_total_s(script: str | None) -> float:
    """Estimated narration length: sentence spans + the PT full-stop pause."""
    sents = [x.strip() for x in _SENT_SPLIT_RE.split((script or "").strip()) if x.strip()]
    return round(sum(estimate_spoken_s(s) for s in sents)
                 + INTER_SENTENCE_PAUSE_S * max(0, len(sents) - 1), 2)
