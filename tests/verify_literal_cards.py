"""Literal cards on topic_flags.LITERAL_CARDS topics (VM FAIL P0 #1449 08/10).

Run: PYTHONPATH=. .venv/bin/python tests/verify_literal_cards.py

OS #1449 "Cursor timed out on French Windows · Agent traps 9" showed an
invented PowerShell error (`MissingTerminator`, "exécution" — the forum log
says `UnexpectedToken`), a misquote "“Fixed in Cursor 3.20.21” — Cursor forum"
and "3.20.21" on three cards in a row. Rodrigo 08/10 15:12: the storyboard
gets an explicit list of allowed on-screen texts (log, error, command, quote,
stat) and that list is the ONLY source for those cards. Fixtures: the real
Cursor forum thread 171409 (tests/fixtures/os1449/t171409.{md,json}, raw posts
raw_171409_{1,9}.md) and the os-1449 SCRIPT_PROPOSED.md allowlist. Pins:
  * flag: topic 47 by default, shared topic_flags structure, env override;
  * allowlist: JSON list (canonical) and the SCRIPT_PROPOSED.md section both
    parse; preflight validation against the fetched thread; matching is whole
    allowlisted lines (terminal padding / typographic quotes folded), case-
    sensitive, kind-scoped (a quote never backs a log card);
  * prompt: rule 2b (required snippet) off, literal rule + ON-SCREEN ALLOWLIST
    in; the code/command re-prompt is skipped; other topics unchanged;
  * board: a terminal/log card with any non-allowlisted line (invented, a cut
    of a line, or narration words) → plain text card; an allowlisted one kept;
    stat not allowlisted or not said → plain text card; non-allowlisted quote
    → plain, no attribution; unsaid attribution removed; provided on_screen
    cards verbatim at their cue and never rewritten (violations → gate);
  * consecutive version: a version on the previous card → the later card goes;
  * gate: cli_check (literal=True) hits for not-in-allowlist / quote /
    attribution / stat → Gate B FAIL (render blocked); clean board → none;
  * first cut ≤2.5s pulled for flagged topics (RR hook pace path).
"""
import html as _html
import inspect
import json
import os
import re
import sys
from pathlib import Path

from app.config import Settings, settings
from app.services import craft, topic_flags
from app.services.engines import storyboard

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


FIX = Path(__file__).parent / "fixtures" / "os1449"
URL = ("https://forum.cursor.com/t/agent-fails-with-error-extension-host-timeout-on-non-english-"
       "windows-agent-exec-powershell-sid-lookup-has-a-quoting-bug/171409")
MD = (FIX / "t171409.md").read_text()
_posts = json.loads((FIX / "t171409.json").read_text())["post_stream"]["posts"]
JSON_TEXT = "\n".join(_html.unescape(re.sub(r"<[^>]+>", "", p.get("cooked") or "")) for p in _posts)
RAW = [(FIX / f"raw_171409_{n}.md").read_text() for n in (1, 9)]
ALLOW_MD = (FIX / "SCRIPT_PROPOSED.md").read_text()
U1, U9, U14 = (URL.replace("/t/agent-fails-with-error-extension-host-timeout-on-non-english-windows-"
                          "agent-exec-powershell-sid-lookup-has-a-quoting-bug/171409",
                          f"/raw/171409/{n}") for n in (1, 9, 14))
L_CAT = "    + CategoryInfo          : ParserError: (:) [], ParentContainsErrorRecordException"
L_FQ = "    + FullyQualifiedErrorId : UnexpectedToken"
L_JETON = "Jeton inattendu «S-1-5-18» dans l'expression ou l'instruction."
# os-1449 allowlist (JSON form) + a quote (#14, staff) + a stat fact.
SRC = craft.on_screen_allow(ALLOW_MD) + [
    {"kind": "quote", "text": "It should be fixed there!", "source": U14},
    {"kind": "stat", "text": "100% Agent requests failed", "source": U1},
]

print("flag")
ok(Settings().topic_flags.get("literal_cards") == [47]
   and topic_flags.DEFAULTS[topic_flags.LITERAL_CARDS] == (47,),
   "literal_cards default = [47] (same per-topic structure as vm_pass_required)")
ok(topic_flags.has(topic_flags.LITERAL_CARDS, 47) and not topic_flags.has(topic_flags.LITERAL_CARDS, 1),
   "topic 47 flagged, topic 1 not")
_orig = settings.topic_flags
settings.topic_flags = {"vm_pass_required": [47]}
ok(topic_flags.has(topic_flags.LITERAL_CARDS, 47), "overriding vm_pass_required keeps literal_cards' default")
settings.topic_flags = _orig

print("allowlist (os-1449 SCRIPT_PROPOSED.md format + JSON)")
md_allow = craft.on_screen_allow(ALLOW_MD)
ok([e["text"] for e in md_allow] == [
    "ERR [Extension Host:agent-exec] Au caractère Ligne:1 : 118", L_JETON, L_CAT, L_FQ,
    '{"error":"ERROR_EXTENSION_HOST_TIMEOUT","details":{"title":"Agent Execution Timed Out", …',
    "ConnectError: [deadline_exceeded] Agent Execution Timed Out"],
   f"SCRIPT_PROPOSED.md allowlist → the 6 literal rows, in order ({len(md_allow)})")
ok(md_allow[0]["kind"] == "error" and md_allow[2]["kind"] == "log"
   and md_allow[0]["source"] == U1 and md_allow[-1]["source"] == U9,
   "kind + source URL kept per row")
ok(not any("Translate" in e["text"] or "Unexpected token" in e["text"] for e in md_allow),
   "'Removed' row and the forbidden English translation are not allowlisted")
ok(craft.on_screen_allow([{"kind": "log", "text": " "}, {"kind": "screenshot", "text": "x"},
                          "bare line", {"kind": "Quote", "text": "q", "url": U14}, 7])
   == [{"kind": "log", "text": "bare line", "source": ""},
       {"kind": "quote", "text": "q", "source": U14}],
   "JSON list: unknown kind / empty text ignored; bare string = log line; url alias")
ok(craft.literal_sources is craft.on_screen_allow, "literal sources ARE the allowlist")
print("allowlist preflight vs the fetched thread")
bad_raw = craft.allowlist_unsourced(md_allow, RAW)
ok([e["text"] for e in bad_raw] == ["ConnectError: [deadline_exceeded] Agent Execution Timed Out"],
   "raw posts back every row but ConnectError (the raw escapes [ ] for Markdown — VM note)")
bad_md = craft.allowlist_unsourced(md_allow, [MD, JSON_TEXT])
ok(L_CAT in [e["text"] for e in bad_md],
   "rendered Discourse text corrupts '(:) [' → the CategoryInfo row is flagged (why the VM uses raw)")
ok(craft.allowlist_unsourced([{"kind": "error", "text": "FullyQualifiedErrorId : MissingTerminator"}],
                             RAW + [MD]),
   "an invented allowlist row (#1449 MissingTerminator) is flagged")
print("line match")
ok(craft.source_literal("+ FullyQualifiedErrorId : UnexpectedToken", SRC, craft.CLI_ALLOW_KINDS),
   "allowlisted log line matches (terminal padding ignored)")
ok(craft.source_literal(L_JETON.replace("'", "’"), SRC, craft.CLI_ALLOW_KINDS),
   "typographic apostrophe matches; non-ASCII literal")
ok(not craft.source_literal("CategoryInfo : ParserError", SRC, craft.CLI_ALLOW_KINDS),
   "a cut of an allowlisted line (no ellipsis) does not match")
ok(craft.source_literal("ConnectError: [deadline_exceeded] Agent…", SRC, craft.CLI_ALLOW_KINDS),
   "an explicit '…' cut (≥12 chars) of an allowlisted line matches")
ok(not craft.source_literal("FullyQualifiedErrorId: MissingTerminator", SRC),
   "invented 'MissingTerminator' does not match")
ok(not craft.source_literal("+ fullyqualifiederrorid : unexpectedtoken", SRC), "case-sensitive")
ok(craft.source_literal("“It should be fixed there!”", SRC, {"quote"})
   and not craft.source_literal("It should be fixed there!", SRC, craft.CLI_ALLOW_KINDS),
   "allowlisted quote matches as a quote only (kind-scoped)")
ok(not craft.source_literal("Fixed in Cursor 3.20.21", SRC, {"quote"}),
   "the #1449 misquote 'Fixed in Cursor 3.20.21' does not match")
ok(craft.stat_allowed("100%", SRC) and not craft.stat_allowed("10", SRC)
   and not craft.stat_allowed("3.20.21", SRC), "stat value: whole token of an allowlisted stat")
ok(not craft.source_literal(L_FQ, []), "empty allowlist → nothing matches")

SCRIPT = ("Cursor timed out on French Windows. Every Agent request failed right away with "
          "Agent Execution Timed Out. The real error was in the dev tools: a PowerShell "
          "parser error, because a quote broke on non-English Windows. The timeout message "
          "pointed at the wrong place. The fix: update Cursor to 3.20.21 or higher. Cursor "
          "support said so on the forum, and the reporter confirmed it works. Agent times "
          "out instantly? Check the logs before the network. Subscribe — next agent trap.")

print("prompt")
sp = storyboard._system_prompt(["hook", "statement", "stat", "quote", "code", "command", "cta"],
                               literal=True)
ok("2b. MUST include" not in sp and "LITERAL EVIDENCE ONLY" in sp and "TERMINAL OUTPUT" in sp,
   "flagged: rule 2b (realistic snippet) off; literal rule + no-invented-output rule on")
sp0 = storyboard._system_prompt(["hook", "statement", "stat", "quote", "code", "command", "cta"])
ok("2b. MUST include" in sp0 and "LITERAL EVIDENCE ONLY" not in sp0, "other topics: prompt unchanged")
up = storyboard._user_prompt("Cursor timed out on French Windows.", SCRIPT, "short",
                             literal=True, sources=SRC)
ok("ON-SCREEN ALLOWLIST" in up and "[error] ERR [Extension Host:agent-exec]" in up
   and "[quote] It should be fixed there!" in up and "[stat] 100% Agent requests failed" in up,
   "user prompt carries the allowlist, one row per entry with its kind")
ok(len(up) < len(SCRIPT) + 6000, "allowlist is capped in the prompt")
ok("ON-SCREEN ALLOWLIST: empty" in storyboard._user_prompt("t", SCRIPT, "short", literal=True),
   "no allowlist → the prompt forbids evidence cards")


def board():
    return [
        {"type": "hook", "cue": "Cursor timed out on French Windows",
         "text": "Cursor timed out on French Windows."},
        {"type": "stat", "cue": "Every Agent request failed", "value": "100%",
         "unit": "", "label": "Agent requests failed"},
        {"type": "command", "cue": "The real error was in", "prompt": ">",
         "command": "CategoryInfo: ParserError",
         "output": ["FullyQualifiedErrorId: MissingTerminator", '"exécution"']},
        {"type": "code", "cue": "pointed at the wrong place", "lang": "log",
         "lines": [L_CAT, L_FQ], "highlight": [1]},
        {"type": "stat", "cue": "update Cursor to 3.20.21", "value": "3.20.21", "unit": "+",
         "label": "minimum build"},
        {"type": "quote", "cue": "Cursor support said so", "text": "Fixed in Cursor 3.20.21",
         "attribution": "Cursor forum"},
        {"type": "cta", "cue": "Subscribe next agent trap", "text": "Subscribe · Agent traps"},
    ]


print("board rules (#1449 cards)")
ALLOWED = ["hook", "statement", "stat", "quote", "code", "command", "cta"]
b = board()
storyboard._apply_literal_cards(b, SCRIPT, SRC, None, ALLOWED)
shown = json.dumps(b, ensure_ascii=False)
ok("MissingTerminator" not in shown and "exécution" not in shown,
   "terminal card with invented output lines is gone (#1449 card #4)")
pl = [x for x in b if x.get("cue") == "The real error was in"]
ok(len(pl) == 1 and pl[0]["type"] == "quote" and pl[0]["plain"] and pl[0]["attribution"] == ""
   and craft.text_in_script(pl[0]["text"], SCRIPT),
   "…replaced by a plain text card with the spoken words")
ok(any(x["type"] == "code" and x["lines"] == [L_CAT, L_FQ] for x in b),
   "log card whose lines are allowlisted is kept")
ok(not any(x["type"] == "stat" for x in b),
   "stat '100%' (allowlisted, never said) and '3.20.21' (said, not allowlisted) → no stat card")
ok(any(x.get("cue") == "update Cursor to 3.20.21" and x.get("plain") for x in b),
   "…the said one becomes a plain text card")
q = [x for x in b if x["type"] == "quote" and x.get("cue") == "Cursor support said so"]
ok(len(q) == 1 and q[0]["plain"] and q[0]["attribution"] == ""
   and "Fixed in Cursor" not in q[0]["text"] and craft.text_in_script(q[0]["text"], SCRIPT),
   "misquote → plain narration card, no attribution (#1449 card #9)")
ok(b[0]["type"] == "hook" and b[-1]["type"] == "cta", "hook and endcard untouched")
bn = [{"type": "hook", "cue": "Cursor timed out", "text": "x"},
      {"type": "command", "cue": "Check the logs before", "command": "Check the logs before the network"},
      {"type": "cta", "cue": "Subscribe", "text": "s"}]
storyboard._apply_literal_cards(bn, SCRIPT, SRC, None, ALLOWED)
ok(bn[1]["type"] == "quote" and bn[1]["plain"],
   "narration words on a terminal card are not allowlisted → plain text card (list is the ONLY source)")
S2 = SCRIPT.replace("failed right away", "failed, 100% of them, right away")
bs = [{"type": "hook", "cue": "Cursor timed out", "text": "x"},
      {"type": "stat", "cue": "Every Agent request failed", "value": "100%", "label": "requests failed"},
      {"type": "cta", "cue": "Subscribe", "text": "s"}]
storyboard._apply_literal_cards(bs, S2, SRC, None, ALLOWED)
ok(bs[1]["type"] == "stat" and bs[1]["value"] == "100%", "stat allowlisted AND said → kept")
b2 = [{"type": "hook", "cue": "Cursor timed out", "text": "Cursor timed out on French Windows."},
      {"type": "quote", "cue": "Check the logs", "text": "It should be fixed there!",
       "attribution": "Colin, Cursor staff"},
      {"type": "cta", "cue": "Subscribe", "text": "Subscribe · Agent traps"}]
storyboard._apply_literal_cards(b2, SCRIPT, SRC, None, ALLOWED)
ok(not b2[1].get("plain") and b2[1]["text"] == "It should be fixed there!"
   and b2[1]["attribution"] == "", "allowlisted quote keeps its quote mark; unsaid attribution removed")
b3 = [{"type": "hook", "cue": "Cursor timed out", "text": "x"},
      {"type": "quote", "cue": "Cursor support said so", "text": "It should be fixed there!",
       "attribution": "Cursor support"},
      {"type": "quote", "cue": "Check the logs", "text": "Check the logs before the network",
       "attribution": "Cursor support"},
      {"type": "cta", "cue": "Subscribe", "text": "s"}]
storyboard._apply_literal_cards(b3, SCRIPT, SRC, None, ALLOWED)
ok(b3[1]["attribution"] == "Cursor support", "attribution kept when the script says it")
ok(b3[2]["plain"] and b3[2]["attribution"] == "" and b3[2]["text"] == "Check the logs before the network",
   "spoken but not allowlisted quote → plain (words kept, no quote mark, no attribution)")

print("provided on_screen cards (verbatim, never rewritten)")
ON = [
    {"type": "code", "cue": "The real error was in the dev tools", "lang": "log",
     "lines": [L_CAT, L_FQ, L_JETON], "highlight": [1]},
    {"type": "command", "cue": "words not in the narration", "command": "x", "output": []},
    {"type": "code", "cue": "Check the logs before", "lang": "log",
     "lines": ["FullyQualifiedErrorId : MissingTerminator"]},
    {"type": "quote", "cue": "the reporter confirmed it works", "text": "It should be fixed there!",
     "attribution": "Cursor forum"},
]
b = board()
b.insert(3, {"type": "statement", "cue": "a quote broke on non-English", "text": "Quote broke"})
storyboard._apply_literal_cards(b, SCRIPT, SRC, ON, ALLOWED)
lit = [x for x in b if x.get("_lit")]
ok(len(lit) == 3 and lit[0]["lines"][1] == L_FQ,
   "on_screen cards in the board verbatim; uncued card skipped")
ok(lit[0]["lines"][0].endswith("\u2026") and len(lit[0]["lines"][0]) == 60
   and craft.source_literal(lit[0]["lines"][0], SRC, craft.CLI_ALLOW_KINDS),
   "a line longer than the card width is cut with an explicit '…' (still an allowlisted cut)")
ok(storyboard._coerce_beat({"type": "code", "cue": "c", "lines": [L_CAT]}, {"code"})["lines"][0]
   == L_CAT[:60].rstrip(), "other topics: line clip unchanged (no mark)")
ok(any(x.get("lines") == ["FullyQualifiedErrorId : MissingTerminator"] for x in lit)
   and any(x.get("attribution") == "Cursor forum" for x in lit),
   "a provided card is never rewritten — not even an invalid one (the gate blocks it)")
mo = craft.cli_check_marker(b, SCRIPT, topic_id=47, sources=SRC)
cko = {h["check"] for h in mo["hits"]}
ok({"not_in_allowlist", "attribution_not_in_script"} <= cko
   and craft.video_maker_gate(b, content_format="short", cli_check=mo)["checks"]["B"] == "FAIL",
   f"…invalid provided cards → cli_check hits → Gate B FAIL ({sorted(cko)})")
ok(not any(x.get("cue") == "a quote broke on non-English" for x in b)
   and not any(x.get("cue") == "The real error was in" for x in b),
   "generated cards anchored in the provided card's sentence give way")
pos = [storyboard._cue_token_pos(storyboard._tok(SCRIPT), x) for x in b[1:-1]]
ok(pos == sorted(pos), "board stays in cue order after the merge")
_e = [dict(x) for x in b]
storyboard._enforce_card_text_rules(_e, SCRIPT)
ok(any(x.get("_lit") and x["lines"] == lit[0]["lines"] for x in _e),
   "card text rules never rewrite a provided literal card")
bx = board()
storyboard._apply_literal_cards(bx, SCRIPT, SRC, "terminal card with the PowerShell log", ALLOWED)
ok(not any(x.get("_lit") for x in bx), "scripts.json-style descriptive on_screen string → ignored safely")

print("consecutive version")
vb = [{"type": "hook", "cue": "Cursor timed out", "text": "Cursor timed out on French Windows."},
      {"type": "stat", "cue": "update Cursor to", "value": "3.20.21", "unit": "+", "label": "fix"},
      {"type": "statement", "cue": "Cursor support said so", "text": "Fixed in 3.20.21"},
      {"type": "statement", "cue": "the reporter confirmed it works", "text": "Confirmed"},
      {"type": "statement", "cue": "Check the logs", "text": "works on v3.20.21"},
      {"type": "statement", "cue": "before the network", "text": "CrewAI 1.15.19 then 1.2.13"},
      {"type": "cta", "cue": "Subscribe", "text": "Subscribe · Agent traps"}]
storyboard._drop_repeated_versions(vb)
txt = [craft.beat_screen_text(x) for x in vb]
ok(not any("Fixed in 3.20.21" in t for t in txt), "same version on the next card → the later card goes")
ok(any("works on v3.20.21" in t for t in txt), "a version again after a version-free card is fine")
ok(any("1.15.19" in t for t in txt), "a different version on the next card is fine")
ok(craft.version_tokens("3.20.21+ / v3.20.21 / 2.5x / $3.20 / Python 3.10") == {"3.20.21"},
   "version tokens: dotted 3-part / v-prefixed; decimals and prices are not versions")

print("gate (cli_check, literal=True)")
bad = board()
bad.insert(5, {"type": "statement", "cue": "Cursor support said", "text": "3.20.21 fixed it"})
m = craft.cli_check_marker(bad, SCRIPT, title="Cursor timed out on French Windows. · Agent traps 9",
                           topic_name="Agent traps", topic_id=47, sources=SRC)
checks = {h["check"] for h in m["hits"]}
bad.insert(2, {"type": "stat", "cue": "Every Agent request failed", "value": "Agent", "label": "x"})
m = craft.cli_check_marker(bad, SCRIPT, title="Cursor timed out on French Windows. · Agent traps 9",
                           topic_name="Agent traps", topic_id=47, sources=SRC)
checks = {h["check"] for h in m["hits"]}
ok(m["literal"] and m["allow"] == len(SRC)
   and {"not_in_allowlist", "quote_not_in_allowlist", "attribution_not_in_script",
        "stat_not_in_script", "stat_not_in_allowlist", "repeated_version"} <= checks,
   f"unfiltered #1449-like board → hits {sorted(checks)}")
gate = craft.video_maker_gate(bad, content_format="short", cli_check=m)
ok(gate["checks"]["B"] == "FAIL" and "on-screen allowlist" in " ".join(gate["reasons"]),
   "Gate B FAIL (render blocked) names the allowlist rule")
good = board()
storyboard._apply_literal_cards(good, SCRIPT, SRC, None, ALLOWED)
storyboard._drop_repeated_versions(good)
m2 = craft.cli_check_marker(good, SCRIPT, topic_id=47, sources=SRC)
ok(m2["hits"] == [], f"filtered board → no hits ({m2['hits']})")
ok(craft.cli_check_marker(bad, SCRIPT, title="Other · Agent memory 3", topic_id=1) is None,
   "non-flagged, non-teaser topic: no cli_check (unchanged)")
ok(craft.cli_check_marker(bad, SCRIPT, topic_id=47, content_format="long") is None, "longs exempt")

print("compose (stubbed llm)")
calls = []


def llm(user, system=None, max_tokens=None):
    calls.append((user, system))
    return json.dumps({"beats": board()})


words = []
t = 0.1
for w in SCRIPT.split():
    words.append({"text": w.strip(".,!?:"), "start": round(t, 3), "dur": 0.3})
    t += 0.36
html = storyboard.compose(subject="Cursor timed out on French Windows. · Agent traps 9",
                          script=SCRIPT, words=words, duration=round(t + 0.6, 2),
                          resolution="portrait", width=1080, height=1920, topic_id=47,
                          content_format="short", language="English", llm=llm, brand="os",
                          topic_name="Agent traps", on_screen_allow=SRC, on_screen=ON[:1],
                          allowed_types=settings.composition_beat_types)
ok(html, "compose returns a board")
ok(not any("Your draft had no code or command beat" in u for u, _ in calls),
   "code/command re-prompt skipped on a flagged topic")
ok("LITERAL EVIDENCE ONLY" in calls[0][1] and "ON-SCREEN ALLOWLIST" in calls[0][0],
   "the model saw the literal rule and the allowlist")
fb = craft.beats_from_html(html)
fs = json.dumps(fb, ensure_ascii=False)
ok("MissingTerminator" not in fs and "Cursor forum" not in fs and "100%" not in fs,
   "rendered board: no invented log, no fake attribution, no unsaid stat")
ok(all(x.get("plain") or craft.source_literal(x.get("text"), SRC, {"quote"})
       for x in fb if x.get("type") == "quote"),
   "every rendered quote card is allowlisted or plain (timing fillers too)")
ok(not [x for x in fb if x.get("type") in craft.CLI_BEAT_TYPES
        and not all(craft.source_literal(ln, SRC, craft.CLI_ALLOW_KINDS) for ln in craft.cli_lines(x))],
   "every rendered terminal/log card is allowlisted line by line")
ok(any(x.get("type") == "code" and x.get("lines", [None, None])[1:] == [L_FQ, L_JETON[:59] + "\u2026"]
       for x in fb),
   "the provided on_screen log card is on the rendered board verbatim")
mk = craft.cli_check_marker(fb, SCRIPT, topic_id=47, sources=SRC)
ok(not [h for h in mk["hits"] if h["check"] != "repeated_version"],
   f"rendered board has no literal hits ({mk['hits']})")
first_cut = min((float(x.get("start") or 0) for x in fb[1:]), default=0)
ok(first_cut <= craft.HOOK_FIRST_CUT_BY_S + 1e-6,
   f"first cut by {craft.HOOK_FIRST_CUT_BY_S}s on a flagged OS topic ({first_cut:.2f}s)")
src_c = inspect.getsource(storyboard._compose_board)
ok("HOOK_PACE_BRANDS or literal" in src_c, "_pull_first_cut gated by brand RR OR the literal_cards flag")
calls.clear()
storyboard.compose(subject="Cursor timed out on French Windows. · Agent traps 9",
                   script=SCRIPT, words=words, duration=round(t + 0.6, 2),
                   resolution="portrait", width=1080, height=1920, topic_id=1,
                   content_format="short", language="English", llm=llm, brand="os",
                   topic_name="Agent traps", on_screen_allow=SRC)
ok("LITERAL EVIDENCE ONLY" not in calls[0][1] and "ON-SCREEN ALLOWLIST" not in calls[0][0],
   "non-flagged topic: compose prompt unchanged even if an allowlist is passed")

print("worker wiring")
from app.services.engines import worker  # noqa: E402
ok("on_screen_allow=params.get(\"on_screen_allow\")" in inspect.getsource(worker)
   and "sources=params.get(\"on_screen_allow\")" in inspect.getsource(worker)
   and "on_screen=params.get(\"on_screen\")" in inspect.getsource(worker),
   "worker passes overrides on_screen_allow/on_screen to compose, cli_check and card_text")
from app.services.engines import mpt as _mpt  # noqa: E402
ok('"on_screen_allow", "on_screen"' in inspect.getsource(_mpt), "MPT never receives on_screen_allow/on_screen")

print()
print(f"ALL {_checks} CHECKS PASSED")
