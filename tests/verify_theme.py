"""Dependency-free regression checks for app/services/engines/theme.py (backlog #7).

This project has no pytest; run directly:
    PYTHONPATH=. .venv/bin/python tests/verify_theme.py

``theme`` is the leaf visual-token module (zero internal deps) every HyperFrames
path imports: worker HTML, storyboard beat renderers, and thumbnail cards. A
diverged palette or a broken ``esc``/``fold`` is a silent brand mismatch or an
HTML-injection / cue-miss. Previously only a handful of resolve/fold/esc
smoke pins lived inside verify_storyboard; the zero-is-missing gate, wrap
arithmetic, full resolve-dict shape, and consumer wiring had no dedicated suite.

Covers, dependency-free (no network, no HyperFrames, no ffmpeg):
  - module contracts: PALETTE (8 exact pairs), BG_VARIANTS (5 named looks
    in order), MONO_STACK / _SANS_STACK, resolve-dict key set + constants
  - _subject_hash: SHA-1 of subject-or-empty (None ≡ ""), independent of
    fold (case-sensitive)
  - resolve palette: topic_id keys accent/bg_deep; 0/None/""/"0"/non-int
    fall back to subject-hash (discriminates ``if tid is None``); wrap at
    len(PALETTE); negative ids use Python modulo
  - resolve bg_variant: ALWAYS keyed by subject, even when topic_id is set
    (two same-topic videos still look distinct)
  - esc: & first so ``<`` is ``&lt;`` not ``&amp;lt;``; quotes NOT escaped;
    None/int stringify; already-escaped input is escaped again
  - fold: NFKD + strip combining + lower; PT/ES diacritics; ß is NOT
    casefolded to ss; empty/None → ""
  - consumer wiring: worker._esc IS theme.esc, thumbnail._THUMB_PALETTE IS
    theme.PALETTE, thumbnail.resolve IS theme.resolve (zero-is-missing
    gate is shared, not a private % 8 index), storyboard.theme IS the
    theme module

Every non-trivial behavior is mutation-verified (hand-built semantic mutants
run from an isolated copy with bytecode caching disabled). Exits non-zero on
the first failed assertion.
"""
from __future__ import annotations

import hashlib
import sys

from app.services import thumbnail
from app.services.engines import storyboard, theme, worker

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


def sha1_int(s: str) -> int:
    """Independent SHA-1 so a mutant swapping hashlib.md5 / hash() dies."""
    return int(hashlib.sha1((s or "").encode()).hexdigest(), 16)


# Subjects whose SHA-1 % 8 / % 5 are known and DISTINCT from the values a
# broken gate would produce. "hello" → pal 5 / var 1 (dots). "cache" → pal 0
# and is reserved as the VACUOUS case (never use it to discriminate tid=0
# vs palette[0]). "prod" → pal 1 (vacuous against topic_id=1).
HELLO = "hello"
# Independent pin of the hex so a SHA-1→other-digest mutant fails even if
# resolve's own % happens to land on the same slot.
_HELLO_SHA1 = 0xAAF4C61DDCC5E8A2DABEDE0F3B482CD9AEA9434D
_EMPTY_SHA1 = 0xDA39A3EE5E6B4B0D3255BFEF95601890AFD80709


# ---------------------------------------------------------------------------
# Module contracts
# ---------------------------------------------------------------------------
print("module contracts: PALETTE, BG_VARIANTS, stacks, resolve-dict shape")

ok(theme.PALETTE == [
    ("#5b8cff", "#1b2a6b"),   # blue
    ("#00c9a7", "#0b2e22"),   # teal
    ("#ff6b35", "#2e1208"),   # orange
    ("#9b5fe0", "#1a0b2e"),   # purple
    ("#ff3b5c", "#2e0b12"),   # red
    ("#2ec4b6", "#0b2228"),   # cyan
    ("#ff85a1", "#2e0b1a"),   # pink
    ("#f9c74f", "#2e2208"),   # gold
], "PALETTE is the eight (accent, bg_deep) pairs in this brand order")
ok(len(theme.PALETTE) == 8, "PALETTE has exactly 8 pairs (wrap modulus)")
ok(all(len(p) == 2 and p[0].startswith("#") and p[1].startswith("#")
       for p in theme.PALETTE),
   "every PALETTE entry is a (#accent, #bg_deep) pair")
# Uniqueness: a swapped/duplicated pair would still have length 8.
ok(len({p[0] for p in theme.PALETTE}) == 8,
   "every PALETTE accent is unique (a swapped pair cannot hide)")

ok(theme.BG_VARIANTS == ("bloom", "dots", "scan", "gradient", "overlay"),
   "BG_VARIANTS is the five named looks in this order")
ok(len(theme.BG_VARIANTS) == 5, "BG_VARIANTS has exactly 5 entries")
ok(len(set(theme.BG_VARIANTS)) == 5, "BG_VARIANTS names are unique")

ok(theme.MONO_STACK == (
    "ui-monospace,'SF Mono',Menlo,Consolas,'Liberation Mono',monospace"
), "MONO_STACK is the Chromium-safe monospace fallback list")
ok(theme._SANS_STACK == "-apple-system,Segoe UI,Helvetica,Arial,sans-serif",
   "_SANS_STACK is the system-ui sans fallback list")

KEYS = {"accent", "bg_deep", "bg_base", "fg", "fg_dim", "mono", "sans", "bg_variant"}
th = theme.resolve(1, HELLO)
ok(set(th) == KEYS, f"resolve dict keys are exactly {sorted(KEYS)}")
ok(th["bg_base"] == "#0b0b16", "bg_base is the fixed near-black canvas")
ok(th["fg"] == "#ffffff", "fg is white")
ok(th["fg_dim"] == "#c9d2ff", "fg_dim is the lavender secondary")
ok(th["mono"] == theme.MONO_STACK, "resolve.mono is MONO_STACK (not a copy drift)")
ok(th["sans"] == theme._SANS_STACK, "resolve.sans is _SANS_STACK")


# ---------------------------------------------------------------------------
# _subject_hash
# ---------------------------------------------------------------------------
print("_subject_hash: SHA-1 of subject-or-empty, case-sensitive")

ok(theme._subject_hash(HELLO) == _HELLO_SHA1,
   "hello hashes to the known SHA-1 integer (not md5 / builtin hash)")
ok(theme._subject_hash("") == _EMPTY_SHA1,
   "empty subject is SHA-1 of b''")
try:
    none_hash = theme._subject_hash(None)
except Exception as e:  # AttributeError if `subject or ""` is dropped
    ok(False, f"None subject hashes as empty (subject or '') (raised {type(e).__name__})")
    none_hash = None  # unreachable
else:
    ok(none_hash == _EMPTY_SHA1,
       "None subject hashes as empty (subject or '')")
ok(none_hash == theme._subject_hash(""),
   "None ≡ '' for the hash (same slot, not a random default)")
ok(theme._subject_hash(HELLO) == sha1_int(HELLO),
   "_subject_hash matches an independent hashlib.sha1")
ok(theme._subject_hash("Hello") != theme._subject_hash("hello"),
   "hash is case-sensitive (fold is the caller's job, not the hash's)")
ok(theme._subject_hash("Produção") != theme._subject_hash("producao"),
   "hash does not strip diacritics (fold is a separate helper)")


# ---------------------------------------------------------------------------
# resolve — palette keyed by topic_id
# ---------------------------------------------------------------------------
print("resolve: topic_id keys accent/bg_deep; 0/missing → subject-hash")

# topic_id=1 + hello (pal 5) — a always-subject-hash mutant lands on cyan,
# not teal. "prod" hashes to pal 1 and would be VACUOUS here.
ok(_HELLO_SHA1 % 8 == 5, "precondition: hello lands on palette[5], not [1]")
ok(theme.resolve(1, HELLO)["accent"] == "#00c9a7",
   "topic_id=1 → palette[1] teal (not hello's cyan subject-hash)")
ok(theme.resolve(1, HELLO)["bg_deep"] == "#0b2e22",
   "topic_id=1 → palette[1] bg_deep rides with the accent")
ok(theme.resolve(1, HELLO)["accent"] != theme.PALETTE[5][0],
   "topic_id=1 does NOT use hello's subject-hash slot (discriminates always-hash)")

# Same topic, different subjects share accent (the thumbnail-match contract).
ok(theme.resolve(1, "alpha")["accent"] == theme.resolve(1, "omega")["accent"]
   == "#00c9a7",
   "same topic_id → same accent regardless of subject")

# Numeric string accepted via int().
ok(theme.resolve("1", HELLO)["accent"] == "#00c9a7",
   "numeric-string topic_id='1' is accepted (int() then palette index)")

# Wrap: 8 → [0], 9 → [1]. Subject is hello (pal 5) so a dropped-wrap
# IndexError-or-hash mutant cannot accidentally land on blue/teal.
# These run BEFORE the topic_id='12' pin so a no-`%` mutant dies on the
# named wrap-8 check (IndexError) rather than an earlier uncaught raise.
try:
    wrap8 = theme.resolve(8, HELLO)["accent"]
except Exception as e:  # IndexError if `% len(PALETTE)` is dropped
    ok(False, f"topic_id=8 wraps to palette[0] blue (not hello's cyan) (raised {type(e).__name__})")
    wrap8 = None
else:
    ok(wrap8 == "#5b8cff",
       "topic_id=8 wraps to palette[0] blue (not hello's cyan)")
ok(theme.resolve(9, HELLO)["accent"] == "#00c9a7",
   "topic_id=9 wraps to palette[1] teal")
ok(theme.resolve("12", HELLO)["accent"] == theme.PALETTE[12 % 8][0],
   "topic_id='12' wraps (12 % 8 == 4)")
ok(theme.resolve(7, HELLO)["accent"] == "#f9c74f",
   "topic_id=7 → palette[7] gold (last slot, no wrap)")

# Negative ids: Python (-1) % 8 == 7. A mutant that rejects / abs() /
# treats negatives as missing would land on hello's cyan, not gold.
ok((-1) % 8 == 7, "precondition: Python modulo of -1 is 7")
ok(theme.resolve(-1, HELLO)["accent"] == "#f9c74f",
   "topic_id=-1 → palette[7] via Python modulo (not subject-hash, not reject)")

# topic_id=0 / None / "" / "0" / non-int → subject-hash. hello is pal 5,
# so a mutant treating 0 as palette[0] (thumbnail's own indexing) dies.
ok(theme.resolve(0, HELLO)["accent"] == "#2ec4b6",
   "topic_id=0 is missing (subject-hash cyan), NOT palette[0] blue")
ok(theme.resolve(0, HELLO)["accent"] != theme.PALETTE[0][0],
   "precondition+pin: hello does not land on palette[0] (kills if-tid-is-None)")
# Else-branch must pin the PAIR, not just accent — a mutant that hashes
# accent but leaves bg_deep at palette[0] survived the first cut.
ok(theme.resolve(0, HELLO)["bg_deep"] == "#0b2228",
   "hash-path bg_deep rides with hello's cyan, not palette[0] deep")
ok(theme.resolve(0, HELLO)["bg_deep"] != theme.PALETTE[0][1],
   "precondition: hello's deep is not palette[0]'s deep (kills constant-deep)")
ok(theme.resolve(None, HELLO)["accent"] == "#2ec4b6",
   "topic_id=None → subject-hash (same slot as 0)")
ok(theme.resolve("", HELLO)["accent"] == "#2ec4b6",
   "topic_id='' is falsy → subject-hash (int() is never called)")
ok(theme.resolve("0", HELLO)["accent"] == "#2ec4b6",
   "topic_id='0' int()s to 0, then the if-tid gate treats it as missing")
try:
    nope = theme.resolve("nope", HELLO)["accent"]
except Exception as e:  # ValueError if the except clause drops ValueError
    ok(False,
       f"non-int topic_id falls back to subject-hash (ValueError → tid=0) "
       f"(raised {type(e).__name__})")
    nope = None
else:
    ok(nope == "#2ec4b6",
       "non-int topic_id falls back to subject-hash (ValueError → tid=0)")
try:
    one_point_five = theme.resolve("1.5", HELLO)["accent"]
except Exception as e:
    ok(False,
       f"topic_id='1.5' is ValueError (int() rejects dots) → subject-hash, not pal[1] "
       f"(raised {type(e).__name__})")
    one_point_five = None
else:
    ok(one_point_five == "#2ec4b6",
       "topic_id='1.5' is ValueError (int() rejects dots) → subject-hash, not pal[1]")
try:
    list_tid = theme.resolve(["1"], HELLO)["accent"]
except Exception as e:  # TypeError if the except clause drops TypeError
    ok(False,
       f"topic_id=['1'] is TypeError (int() rejects lists) → subject-hash, not pal[1] "
       f"(raised {type(e).__name__})")
    list_tid = None
else:
    ok(list_tid == "#2ec4b6",
       "topic_id=['1'] is TypeError (int() rejects lists) → subject-hash, not pal[1]")
ok(theme.resolve(False, HELLO)["accent"] == "#2ec4b6",
   "topic_id=False is falsy → subject-hash (not palette[0])")

# Empty/None subject with missing topic_id: empty hashes to pal 1, not 0.
ok(_EMPTY_SHA1 % 8 == 1, "precondition: empty subject lands on palette[1]")
ok(theme.resolve(None, "")["accent"] == "#00c9a7",
   "missing topic + empty subject → palette[empty-hash] teal, not blue")
ok(theme.resolve(None, None)["accent"] == "#00c9a7",
   "missing topic + None subject ≡ empty subject")

# resolve must hash the subject as given — a `.lower()` / fold() inside
# resolve would make HELLO (pal 7) collapse to hello (pal 5).
ok(sha1_int("HELLO") % 8 == 7,
   "precondition: HELLO lands on palette[7], not hello's [5]")
ok(theme.resolve(0, "HELLO")["accent"] == "#f9c74f",
   "resolve does not lower/fold the subject before hashing (HELLO ≠ hello)")
ok(theme.resolve(0, "HELLO")["accent"] != theme.resolve(0, HELLO)["accent"],
   "HELLO and hello resolve to different accents on the hash path")


# ---------------------------------------------------------------------------
# resolve — bg_variant ALWAYS from subject
# ---------------------------------------------------------------------------
print("resolve: bg_variant keyed by subject (same-topic videos still differ)")

# topic_id=1 is VACUOUS against a topic-keyed variant (1 % 5 == 1 == hello).
# The discriminator is topic 7 (7 % 5 == 2 → 'scan') plus the missing-topic
# pins below (a `if tid else bloom` mutant survives every truthy-tid check).
ok(theme.resolve(1, HELLO)["bg_variant"] == "dots",
   "hello → bg_variant 'dots' (SHA-1 % 5 == 1) under topic_id=1")
ok(theme.resolve(7, HELLO)["bg_variant"] == "dots",
   "same subject, different topic_id → SAME bg_variant (not re-keyed by topic)")
ok(theme.resolve(0, HELLO)["bg_variant"] == "dots",
   "missing topic still keys bg_variant by subject (not a bloom default)")
ok(theme.resolve(None, "")["bg_variant"] == "bloom",
   "missing topic + empty subject → bloom (empty SHA-1 % 5 == 0)")
ok(theme.resolve(1, HELLO)["accent"] != theme.resolve(7, HELLO)["accent"],
   "precondition: topics 1 and 7 really differ in accent (so the shared "
   "variant is not 'everything equal')")

# Two subjects under the same topic: accents match, variants differ.
# "hello" → dots (1); "alpha" → overlay (4).
ok(theme.resolve(1, "alpha")["bg_variant"] == "overlay",
   "alpha → bg_variant 'overlay' (SHA-1 % 5 == 4)")
ok(theme.resolve(1, HELLO)["bg_variant"] != theme.resolve(1, "alpha")["bg_variant"],
   "same topic_id, different subjects → different bg_variant")
ok(theme.resolve(1, HELLO)["accent"] == theme.resolve(1, "alpha")["accent"],
   "same topic_id, different subjects → same accent (variant is the only differ)")

# Every named look is reachable (a truncated BG_VARIANTS would miss one).
# Subjects picked so each % 5 slot is hit: x=0 bloom, hello=1 dots,
# omega=2 scan, cache=3 gradient, alpha=4 overlay.
by_slot = {
    "x": "bloom",
    HELLO: "dots",
    "omega": "scan",
    "cache": "gradient",
    "alpha": "overlay",
}
for subj, expected in by_slot.items():
    got = theme.resolve(1, subj)["bg_variant"]
    ok(got == expected, f"subject {subj!r} → bg_variant {expected!r} (got {got!r})")

ok({theme.resolve(1, s)["bg_variant"] for s in by_slot} == set(theme.BG_VARIANTS),
   "the five fixture subjects cover every BG_VARIANTS entry")

# Deterministic: same args → identical dict (no rng, no mutation of tables).
a = theme.resolve(3, "foo")
b = theme.resolve(3, "foo")
ok(a == b, "resolve is deterministic (same args → same dict)")
ok(theme.PALETTE[0] == ("#5b8cff", "#1b2a6b"),
   "resolve does not mutate PALETTE[0]")
ok(theme.BG_VARIANTS[0] == "bloom",
   "resolve does not mutate BG_VARIANTS[0]")


# ---------------------------------------------------------------------------
# esc
# ---------------------------------------------------------------------------
print("esc: & first, quotes untouched, stringify None/int")

# The &-first order is the whole game: escaping < first turns "<" into
# "&lt;" and then the later & pass double-escapes it to "&amp;lt;".
ok(theme.esc("<") == "&lt;",
   "leading < is &lt; (not &amp;lt; — proves & is replaced FIRST)")
ok(theme.esc("&") == "&amp;", "bare & is &amp;")
ok(theme.esc(">") == "&gt;", "bare > is &gt;")
ok(theme.esc("<a&b>") == "&lt;a&amp;b&gt;",
   "<a&b> encodes to &lt;a&amp;b&gt; (amp first, then brackets)")
ok(theme.esc("&lt;") == "&amp;lt;",
   "already-escaped input is escaped again (callers pass raw text)")
ok(theme.esc('"') == '"',
   "double quotes are NOT escaped (text-node helper, not attribute)")
ok(theme.esc("'") == "'",
   "single quotes are NOT escaped")
ok(theme.esc("") == "", "empty string is empty")
try:
    esc_none = theme.esc(None)
except Exception as e:  # AttributeError if str() is dropped
    ok(False, f"None stringifies via str() (never raises) (raised {type(e).__name__})")
    esc_none = None
else:
    ok(esc_none == "None", "None stringifies via str() (never raises)")
try:
    esc_int = theme.esc(42)
except Exception as e:
    ok(False, f"int stringifies via str() (raised {type(e).__name__})")
    esc_int = None
else:
    ok(esc_int == "42", "int stringifies via str()")
ok(theme.esc("ok") == "ok", "plain text is unchanged")
# Ampersand in the middle of a tag-like string, no brackets.
ok(theme.esc("a&b") == "a&amp;b", "mid-string & is escaped without inventing brackets")


# ---------------------------------------------------------------------------
# fold
# ---------------------------------------------------------------------------
print("fold: NFKD + strip combining + lower; ß is not casefold")

ok(theme.fold("Produção") == "producao",
   "fold strips PT cedilla+tilde (cue 'produção' matches spoken 'producao')")
ok(theme.fold("inferência") == "inferencia",
   "fold strips PT circumflex")
ok(theme.fold("INFERÊNCIA") == "inferencia",
   "fold lowercases AFTER stripping (uppercase PT still matches)")
ok(theme.fold("São Paulo") == "sao paulo",
   "fold strips tilde and keeps the space (not a tokeniser)")
ok(theme.fold("niño") == "nino", "fold strips ES eñe")
ok(theme.fold("Café") == "cafe", "fold strips é")
ok(theme.fold("e\u0301") == "e",
   "NFKD then strip combining: decomposed e+acute becomes 'e'")
ok(theme.fold("HELLO") == "hello", "plain ASCII is lowercased")
ok(theme.fold("") == "", "fold of empty is empty")
try:
    fold_none = theme.fold(None)
except Exception as e:  # TypeError if `s or ""` is dropped
    ok(False, f"fold of None is empty (never raises) (raised {type(e).__name__})")
    fold_none = None
else:
    ok(fold_none == "", "fold of None is empty (never raises)")
# casefold() would map ß → ss. The helper is .lower() after NFKD, so ß stays.
ok(theme.fold("ß") == "ß",
   "sharp s is NOT casefolded to 'ss' (lower, not casefold)")
ok(theme.fold("ß") != "ss",
   "discriminates a casefold() mutant (ß → ss would silently pass a == ß pin "
   "if someone also lowercased the expected value)")
ok(theme.fold("producao") == "producao",
   "already-folded input is a no-op (idempotent on ASCII)")


# ---------------------------------------------------------------------------
# Consumer wiring — a private copy in worker/thumbnail/storyboard is how
# the palette previously drifted. Identity (`is`) for real aliases;
# value-equal for worker._ACCENTS, which is still a derived list.
# ---------------------------------------------------------------------------
print("consumer wiring: worker / thumbnail / storyboard share this module")

ok(worker._esc is theme.esc,
   "worker._esc IS theme.esc (single HTML-escape, no private copy)")
ok(thumbnail._THUMB_PALETTE is theme.PALETTE,
   "thumbnail._THUMB_PALETTE IS theme.PALETTE (single brand list)")
ok(thumbnail.resolve is theme.resolve,
   "thumbnail.resolve IS theme.resolve (shared zero-is-missing gate, not a private % 8 index)")
ok(worker._ACCENTS == [p[0] for p in theme.PALETTE],
   "worker._ACCENTS is the PALETTE accents in the same order")
ok(storyboard.theme is theme,
   "storyboard.theme IS the theme module (compose calls theme.resolve)")
ok(storyboard.theme.resolve is theme.resolve,
   "storyboard.theme.resolve IS theme.resolve (not a wrapper / copy)")
ok(storyboard.theme.esc is theme.esc,
   "storyboard beat renderers call the same esc object")
ok(storyboard.theme.fold is theme.fold,
   "storyboard cue-match calls the same fold object")


print()
print(f"ALL {_checks} CHECKS PASSED")
