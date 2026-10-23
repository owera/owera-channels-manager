"""Card literal multi-linha (code/log/error/command): as linhas juntadas com um
espaço precisam ser BYTE-EXATAS a uma entrada inteira da allowlist (#1453 c5).

    PYTHONPATH=. .venv/bin/python tests/verify_multiline_literal.py
"""
import sys

from app.services import craft

_checks = 0


def ok(cond, msg):
    global _checks
    _checks += 1
    if not cond:
        print("FAIL:", msg)
        sys.exit(1)
    print("  ok:", msg)


# Dados reais do OS #1453 (fix-overrides-2026-10-21.json / fix-script-2026-10-19.json).
ERR = ("pydantic.errors.PydanticForbiddenQualifier: The annotation 'NotRequired[int]' "
       "contains the 'typing.NotRequired' type qualifier, which is invalid in the "
       "context it is defined.")
CMD = "uv run --python 3.10 --frozen --all-extras --dev python repro.py"
ALLOW = [
    {"kind": "error", "text": ERR,
     "source": "https://github.com/modelcontextprotocol/python-sdk/issues/3227"},
    {"kind": "command", "text": CMD,
     "source": "https://github.com/modelcontextprotocol/python-sdk/issues/3227"},
    {"kind": "quote", "text": "Python three eleven and up were fine.", "source": "vo"},
    {"kind": "quote", "text": "Registration works.", "source": "vo"},
]
SCRIPT = ("One type hint broke MCP tools. Python three ten. The tool returned a TypedDict. "
          "One key was NotRequired. It failed at registration, before it ever ran. "
          "Pydantic called the annotation invalid. Python three eleven and up were fine. "
          "Update the MCP Python SDK. Registration works. Optional keys are left out. "
          "They are not sent as null. Exact version in the description. Old Python in prod? "
          "Test registration there. Subscribe \u2014 next agent trap.")
LINES = [
    "pydantic.errors.PydanticForbiddenQualifier: The",
    "annotation 'NotRequired[int]' contains the",
    "'typing.NotRequired' type qualifier, which is",
    "invalid in the context it is defined.",
]
HOOK = {"type": "hook", "text": "One type hint broke MCP tools."}


def code(lines):
    return {"type": "code", "cue": "Pydantic called the annotation invalid",
            "lang": "", "lines": list(lines), "_lit": True}


def all_hits(card):
    """(literal_hits, fabricated_cli, card_text_not_literal) do card."""
    lit = craft.literal_hits([card], SCRIPT, ALLOW)
    fab = craft.fabricated_cli([card], SCRIPT, ALLOW)
    txt = [h for h in craft.os_card_hits([HOOK, card], SCRIPT, ALLOW)
           if h["check"] == "card_text_not_literal"]
    return lit, fab, txt


def passes(card):
    lit, fab, txt = all_hits(card)
    return not lit and not fab and not txt and craft.card_is_literal(card, SCRIPT, ALLOW)


def fails_all(card):
    lit, fab, txt = all_hits(card)
    return bool(lit) and bool(fab) and bool(txt) and not craft.card_is_literal(card, SCRIPT, ALLOW)


print("#1453 c5: erro pydantic em 4 linhas")
ok(" ".join(LINES) == ERR, "dado real: ' '.join(linhas) é byte-idêntico à entrada")
ok(passes(code(LINES)), "c5 real passa literal_hits, fabricated_cli e card_text_not_literal")
ok(craft.cli_card_whole_allowlisted(code(LINES), ALLOW), "helper reconhece o card inteiro")
ok(len(ALLOW) == 4 and sum(1 for e in ALLOW if e["kind"] == "error") == 1,
   "allowlist continua com uma entrada por card")
ok(passes(code([ERR])), "linha única igual à entrada continua passando")
_marker = craft.cli_check_marker([HOOK, code(LINES)], SCRIPT, topic_id=47, sources=ALLOW)
ok(_marker is None or not _marker.get("hits"), "cli_check_marker do board real sem hits")

print("negativos")
ok(fails_all(code(LINES[:1])), "só a 1ª linha (fragmento) falha")
ok(fails_all(code(LINES[:3])), "3 de 4 linhas (prefixo) falha")
ok(fails_all(code(LINES[1:])), "sem a 1ª linha (sufixo) falha")
_one = list(LINES)
_one[3] = "invalid in the context it is defined!"
ok(fails_all(code(_one)), "um caractere diferente falha")
_case = list(LINES)
_case[0] = _case[0].replace("The", "the")
ok(fails_all(code(_case)), "diferença só de caixa falha (sem fold)")
ok(fails_all(code([" / ".join(LINES)])), "join com ' / ' falha")
ok(fails_all(code([ln + " /" for ln in LINES[:3]] + LINES[3:])), "separador ' / ' entre linhas falha")
ok(fails_all(code(["pydantic.errors.PydanticForbiddenQualifier:", "The annotation",
                   "'NotRequired[int]' contains the"])),
   "fragmentos que não formam a entrada inteira falham")
ok(not craft.cli_card_whole_allowlisted(code(LINES), None), "sem allowlist não há atalho")
ok(fails_all(code(LINES + ["extra line"])), "entrada inteira + linha extra falha")

print("VM PR #138: sem fold, sem corte, kinds, strip")
ok(fails_all(code(["".join(LINES)])), "junção '' numa linha só ('…Theannotation…') falha")
ok(fails_all(code(["pydantic.errors.PydanticForbiddenQualifier: The annotation\u2026"])),
   "corte com '…' falha")
ok(fails_all(code(["pydantic.errors.PydanticForbiddenQualifier: The annotation..."])),
   "corte com '...' falha")
ok(fails_all(code([LINES[0] + " "] + LINES[1:])), "espaço no fim de uma linha falha")
ok(fails_all(code([" " + LINES[0]] + LINES[1:])), "espaço no começo de uma linha falha")
ok(fails_all(code(LINES[:2] + [""] + LINES[2:])), "linha vazia inserida falha")
ok(fails_all(code([CMD])), "card code não bate com entrada command (kinds)")
_cmd_as_out = {"type": "command", "prompt": "$", "command": "uv run", "output": CMD.split(" ")[2:]}
ok(craft.literal_hits([_cmd_as_out], SCRIPT, ALLOW), "command+output juntos não batem com entrada única")
_err_cmd = {"type": "command", "prompt": "$", "command": ERR, "output": []}
ok(craft.literal_hits([_err_cmd], SCRIPT, ALLOW), "command card não bate com entrada error (kinds)")
_cmd_out = {"type": "command", "prompt": "$", "command": CMD, "output": LINES}
ok(not craft.literal_hits([_cmd_out], SCRIPT, ALLOW), "command + output (erro inteiro em linhas) passa")
ok(craft.source_literal("".join(LINES), ALLOW, craft.CLI_ALLOW_KINDS),
   "source_literal (não-CLI) mantém o fold de sempre")

print("entrada multi-linha: run contíguo e em ordem de UMA entrada")
ML = "Traceback (most recent call last):\n  File \"repro.py\", line 3, in <module>\n    run()\nValueError: boom"
ML2 = "line A\nline B"
ALLOW_ML = ALLOW + [{"kind": "log", "text": ML, "source": "x"}, {"kind": "error", "text": ML2, "source": "x"}]
_ml = ML.splitlines()


def lit_ok(lines, allow=ALLOW_ML):
    c = code(lines)
    return (not craft.literal_hits([c], SCRIPT, allow) and craft.card_is_literal(c, SCRIPT, allow)
            and not craft.fabricated_cli([c], SCRIPT, allow))


ok(lit_ok(_ml), "todas as linhas da entrada multi-linha passam")
ok(lit_ok(_ml[1:3]), "run contíguo (com indentação crua) passa")
ok(lit_ok(_ml[3:]), "uma linha inteira de entrada multi-linha passa")
ok(not lit_ok([_ml[1], _ml[0]]), "linhas fora de ordem falham")
ok(not lit_ok([_ml[0], _ml[2]]), "linha pulada falha")
ok(not lit_ok([_ml[3], "line A"]), "misturar entradas falha")
ok(not lit_ok([_ml[1].strip()]), "linha sem a indentação crua falha")
ok(not lit_ok([_ml[0], ""]), "linha vazia falha")
ok(not lit_ok(["ValueError: bo\u2026"]), "corte com '…' de linha multi-linha falha")
ok(not lit_ok([CMD]), "linhas de entrada command não valem para card code")
for _k, _ln in enumerate(LINES):
    ok(not lit_ok([_ln]), f"linha quebrada {_k + 1} do c5 sozinha falha (entrada de uma linha)")
ok(not lit_ok([LINES[0], ERR]), "linha + entrada inteira (mistura) falha")

print("command")
_cmd = {"type": "command", "prompt": "$", "command": CMD, "output": [], "_lit": True}
ok(passes(_cmd), "command card byte-exato passa")
_cmd2 = dict(_cmd, command=CMD.replace("3.10", "3.11"))
ok(craft.literal_hits([_cmd2], SCRIPT, ALLOW), "command com um caractere diferente falha")

print("exibição do hit")
_h = craft.cli_check_hits({"version": craft.CLI_V1,
                           "hits": [{"i": 5, "type": "code", "text": LINES[:2]}]})
ok(_h and " / " not in _h[0] and " ".join(LINES[:2]) in _h[0], "hit mostra linhas juntadas com espaço")

print(f"\nALL {_checks} CHECKS PASSED")
