"""Generator slips surfaced in the craft report (CoS 08/10 15:27).

Run: PYTHONPATH=. .venv/bin/python tests/verify_generator_slips.py

On a literal_cards topic (OS named tool, 47) compose repairs what the
generator invented before timing (#88): a generated evidence card not in the
on-screen allowlist → plain text card (or dropped when its cue is not spoken),
an unsaid attribution / stat removed, a repeated version dropped. Every repair
is a "generator slip", logged and shown to the VM:
  * storyboard helpers record one slip per repair (kind, type, cue, detail);
  * compose embeds them in index.html (#generator-slips) on flagged topics
    only; craft.generator_slips_from_html reads them back;
  * worker._creation_config → creation_config["generator_slips"] = {count,
    slips} and craft_gate["generator_slips"] = N + ["generator_slip_list"];
    video_maker_gate_of (recompute) keeps them; informational only — the gate
    result / checks / reasons are unchanged;
  * other topics / fallback renders carry no slip record.
"""
import json
import sys

from app.config import settings
from app.services import craft
from app.services.engines import storyboard, worker

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


SCRIPT = ("Cursor timed out on French Windows. Every Agent request failed right away with "
          "Agent Execution Timed Out. The real error was in the dev tools: a PowerShell "
          "parser error, because a quote broke on non-English Windows. The timeout message "
          "pointed at the wrong place. The fix: update Cursor to 3.20.21 or higher. Cursor "
          "support said so on the forum, and the reporter confirmed it works. Agent times "
          "out instantly? Check the logs before the network. Subscribe — next agent trap.")
L_FQ = "    + FullyQualifiedErrorId : UnexpectedToken"
ALLOW = [{"kind": "log", "text": L_FQ, "source": "https://forum.cursor.com/raw/171409/1"},
         {"kind": "quote", "text": "It should be fixed there!", "source": "https://forum.cursor.com/raw/171409/14"},
         {"kind": "stat", "text": "100% Agent requests failed", "source": "https://forum.cursor.com/raw/171409/1"}]
ALLOWED = ["hook", "statement", "stat", "quote", "code", "command", "cta"]


def board():
    return [
        {"type": "hook", "cue": "Cursor timed out on French Windows",
         "text": "Cursor timed out on French Windows."},
        {"type": "stat", "cue": "Every Agent request failed", "value": "100%", "unit": "",
         "label": "Agent requests failed"},
        {"type": "command", "cue": "The real error was in", "prompt": ">",
         "command": "CategoryInfo: ParserError",
         "output": ["FullyQualifiedErrorId: MissingTerminator", '"exécution"']},
        {"type": "code", "cue": "pointed at the wrong place", "lang": "log", "lines": [L_FQ]},
        {"type": "stat", "cue": "update Cursor to 3.20.21", "value": "3.20.21", "unit": "+",
         "label": "minimum build"},
        {"type": "quote", "cue": "Cursor support said so", "text": "Fixed in Cursor 3.20.21",
         "attribution": "Cursor forum"},
        {"type": "quote", "cue": "Check the logs", "text": "It should be fixed there!",
         "attribution": "Colin, Cursor staff"},
        {"type": "cta", "cue": "Subscribe next agent trap", "text": "Subscribe · Agent traps"},
    ]


print("helpers record one slip per repair")
slips = []
b = board()
storyboard._apply_literal_cards(b, SCRIPT, ALLOW, None, ALLOWED, slips)
kinds = [s["kind"] for s in slips]
ok({"cli_not_in_allowlist", "stat_not_in_script", "stat_not_in_allowlist",
    "quote_not_in_allowlist", "attribution_dropped"} <= set(kinds), f"slip kinds {kinds}")
cli = [s for s in slips if s["kind"] == "cli_not_in_allowlist"][0]
ok(cli["type"] == "command" and cli["cue"] == "The real error was in"
   and "FullyQualifiedErrorId: MissingTerminator" in cli["detail"] and "→ plain text card" in cli["detail"],
   "the #1449 invented terminal card is a slip: type, cue, invented lines, outcome")
ok(any(s["kind"] == "attribution_dropped" and s["detail"] == ["Colin, Cursor staff"] for s in slips),
   "a dropped attribution is a slip with the attribution text")
ok(not any(s["cue"] == "pointed at the wrong place" for s in slips), "an allowlisted card is not a slip")
vb = [{"type": "hook", "cue": "Cursor timed out", "text": "x"},
      {"type": "stat", "cue": "update Cursor to", "value": "3.20.21", "label": "fix"},
      {"type": "statement", "cue": "Cursor support said so", "text": "Fixed in 3.20.21"},
      {"type": "cta", "cue": "Subscribe", "text": "s"}]
vs = []
storyboard._drop_repeated_versions(vb, vs)
ok([s["kind"] for s in vs] == ["repeated_version_dropped"] and vs[0]["detail"] == ["3.20.21"],
   "a dropped repeated version is a slip")
storyboard._apply_literal_cards(board(), SCRIPT, ALLOW, None, ALLOWED)
ok(True, "helpers still work without a slip list")

print("compose embeds slips on flagged topics only")
words, t = [], 0.1
for w in SCRIPT.split():
    words.append({"text": w.strip(".,!?:"), "start": round(t, 3), "dur": 0.3})
    t += 0.36


def llm(user, system=None, max_tokens=None):
    return json.dumps({"beats": board()})


kw = dict(subject="Cursor timed out on French Windows. · Agent traps 9", script=SCRIPT, words=words,
          duration=round(t + 0.6, 2), resolution="portrait", width=1080, height=1920,
          content_format="short", language="English", llm=llm, brand="os", topic_name="Agent traps",
          on_screen_allow=ALLOW, allowed_types=settings.composition_beat_types)
html = storyboard.compose(topic_id=47, **kw)
gs = craft.generator_slips_from_html(html)
ok(isinstance(gs, list) and len(gs) >= 5, f"topic 47: {len(gs or [])} slips embedded in index.html")
ok(any(s["kind"] == "cli_not_in_allowlist" for s in gs), "…including the invented terminal card")
html1 = storyboard.compose(topic_id=1, **kw)
# OS whole-sentence cards (CoS 09/10): an OS board records its fragment
# repairs as slips too — but never a literal_cards slip off topic 47.
_LIT_KINDS = {"cli_not_in_allowlist", "stat_not_in_allowlist", "quote_not_in_allowlist",
              "stat_not_in_script", "attribution_dropped", "repeated_version_dropped"}
gs1 = craft.generator_slips_from_html(html1) or []
ok(not any(s["kind"] in _LIT_KINDS for s in gs1), "other topics: no literal_cards slip")
ok(all(s["kind"].startswith(("fragment_", "whole_cards_")) for s in gs1),
   f"other OS topics: only whole-card repair slips ({[s['kind'] for s in gs1]})")
html2 = storyboard.compose(topic_id=1, **dict(kw, brand="owera"))
ok(craft.generator_slips_from_html(html2) is None, "brand outside rr/os, other topic: no slip record")
ok(craft.generator_slips_from_html("<html>") is None
   and craft.generator_slips_from_html('<script type="application/json" id="generator-slips">{bad</script>') is None,
   "fallback / malformed HTML → None")

print("creation_config + craft_gate report")
params = {"content_format": "short", "topic_id": 47, "topic_name": "Agent traps",
          "on_screen_allow": ALLOW, "brand": "os"}
cc = worker._creation_config(kw["subject"], params, html, SCRIPT, kw["duration"], "portrait", None,
                             False, words=words, brand="os")
ok(cc.get("generator_slips", {}).get("count") == len(gs) and cc["generator_slips"]["slips"] == gs,
   f"creation_config['generator_slips'] = {{count: {len(gs)}, slips}}")
g = cc["craft_gate"]
ok(g.get("generator_slips") == len(gs) and g.get("generator_slip_list") == gs,
   "craft_gate carries generator_slips: N + generator_slip_list (the VM's report)")
plain = craft.video_maker_gate(cc["beats"], content_format="short",
                               beat_timing=craft.BEAT_TIMING_CURRENT, hook_pace=cc.get("hook_pace"),
                               card_sync=cc.get("card_sync"), cli_check=cc.get("cli_check"),
                               card_text=cc.get("card_text"), card_vo=cc.get("card_vo"))
ok(g["result"] == plain["result"] and g["checks"] == plain["checks"] and g["reasons"] == plain["reasons"],
   "slips are informational: gate result / checks / reasons unchanged")
rg = craft.video_maker_gate_of(cc, "short")
ok(rg.get("generator_slips") == len(gs), "video_maker_gate_of (recompute) keeps generator_slips")
cc1 = worker._creation_config(kw["subject"], dict(params, topic_id=1), html1, SCRIPT, kw["duration"],
                              "portrait", None, False, words=words, brand="os")
ok(all(s["kind"].startswith(("fragment_", "whole_cards_"))
       for s in (cc1.get("generator_slips") or {}).get("slips") or []),
   "other OS topics: generator_slips only ever carry whole-card repairs")
cc2 = worker._creation_config(kw["subject"], dict(params, topic_id=1), html2, SCRIPT, kw["duration"],
                              "portrait", None, False, words=words, brand="owera")
ok("generator_slips" not in cc2 and "generator_slips" not in cc2["craft_gate"],
   "other brand + topic: no generator_slips keys")
ccf = worker._creation_config(kw["subject"], params, html, SCRIPT, kw["duration"], "portrait", None,
                              True, words=words, brand="os")
ok("generator_slips" not in ccf, "fallback render: no slip record")
big = craft.attach_generator_slips({"result": "PASS"}, [{"kind": "x"}] * 100)
ok(big["generator_slips"] == 100 and len(big["generator_slip_list"]) == craft.GEN_SLIPS_LIST_MAX,
   "count is exact; the list is capped")
ok(craft.attach_generator_slips({"result": "PASS"}, None) == {"result": "PASS"}, "no record → gate untouched")

print()
print(f"ALL {_checks} CHECKS PASSED")
