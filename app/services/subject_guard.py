"""Pre-produce subject guard: keep broken idea subjects out of the render queue.

2026-09-27 (RR, ch2): generate_ideas stripped the leading count/stake off some
subjects ("regenerações no mesmo bug saem R$22…", "camadas na GPU, o resto no
CPU…", "B em…", "GB rodou…"). _auto_produce queued them anyway, each one used a
daily render slot, and the morning pass then rejected them. The strip itself was
fixed in 445aae4. This guard is the belt-and-braces check at the one place a
draft turns into render spend.

Conservative by design: it only flags the *shape* a stripped number leaves
behind at the very start of the subject. It does not try to prove that a number
or stake exists anywhere, because plenty of valid subjects have none (OS:
"Chat targeted staging. Prod wrote production.").

Flagged (returns a reason):
  - first word all-lowercase letters, e.g. "regenerações …", "camadas …",
    "mil tokens …", "ligada no mês …". Allowed: known lowercase literals
    (`node_modules`, `npm`, `git`, …), code-ish tokens containing `_ . / - @ :`
    or a digit, and camelCase brands with an uppercase letter or digit after
    the first letter (`eGPU`, `vLLM`, `iPhone`).
  - a bare unit / currency / multiplier token with no number: "B em …",
    "GB rodou …", "% das …", "R$ por mês …", "x mais rápido …".
  - leading punctuation left over from a dropped number: ", …", ". …", ": …".
  - (2026-09-28, CoS item 6) any currency value anywhere: `R$`/`$` followed by
    a number ("sai R$50", "cobrou $47"). The title is built from the subject,
    and the publish craft gate now rejects currency in titles.
"""

import re

# Lowercase literals that are legitimately lowercase at the start of a subject
# (CLI tools, paths, identifiers). Extend here instead of loosening the rule.
LOWERCASE_ALLOWLIST: frozenset[str] = frozenset({
    "node_modules", "npm", "npx", "pnpm", "yarn", "pip", "pipx", "uv", "git",
    "gh", "curl", "wget", "grep", "sed", "awk", "sudo", "ssh", "scp", "rsync",
    "kubectl", "helm", "docker", "make", "cargo", "go", "bun", "deno", "brew",
    "apt", "ollama", "llama.cpp", "vllm", "venv", "localhost", "cron", "bash",
    "zsh", "vim", "nvim", "tmux", "jq", "rm", "ls", "cd", "chmod", "env",
    "stdout", "stderr", "main", "master", "prod", "dev", "localStorage",
})

# Bare unit tokens: valid only when glued to (or preceded by) a number, so a
# subject that STARTS with one lost its number. Case-sensitive on purpose:
# "B" / "GB" are units; "X" (the brand) and "M4" are not flagged.
BARE_UNIT_TOKENS: frozenset[str] = frozenset({
    "B", "KB", "MB", "GB", "TB", "PB", "K", "k", "M", "ms", "s", "h", "min",
    "x", "%", "GB/s", "MB/s", "tok/s", "tokens",
})

# Currency markers with no amount attached ("R$ por mês", "$ no terminal").
_CURRENCY_ONLY = re.compile(r"^(R\$|US\$|\$|€|£)[\s,.;:]*$")
_LEADING_PUNCT = re.compile(r"^[,.;:%)\]}/\\]")
_CODEISH = re.compile(r"[_./@:\-0-9]")

HOLD_PREFIX = "subject guard:"


_QUOTES = "\"'“”‘’«»"


def _first_token(subject: str) -> str:
    head = subject.strip()
    # Opening quotes/brackets are fine ("'X' …", "(Local) …") — look past them.
    head = head.lstrip(_QUOTES + "([{¿¡ ")
    return head.split(None, 1)[0].rstrip(_QUOTES + ")]}") if head else ""


def subject_guard_reason(subject: str | None) -> str | None:
    """None when the subject is safe to produce; else a short operator-readable reason."""
    raw = (subject or "").strip()
    if not raw:
        return f"{HOLD_PREFIX} empty subject"
    # Item 6 (2026-09-28): a currency value anywhere in the subject becomes a
    # currency value in the title (the title is the subject + series suffix),
    # which the publish craft gate now rejects. Hold it here instead of
    # spending a render slot on a video that can never publish.
    from app.services.craft import currency_in_text
    if currency_in_text(raw):
        return (f"{HOLD_PREFIX} currency value (R$N / $N) in subject — titles may "
                f"not carry currency; retitle with a spoken claim + concrete noun")
    if _LEADING_PUNCT.match(raw):
        return (f"{HOLD_PREFIX} starts with {raw[0]!r} — leading number/stake "
                f"probably stripped")
    tok = _first_token(raw)
    if not tok:
        return f"{HOLD_PREFIX} empty subject"
    bare = tok.rstrip(",.;:!?")
    if bare in BARE_UNIT_TOKENS or tok in BARE_UNIT_TOKENS:
        return (f"{HOLD_PREFIX} starts with bare unit {bare!r} — leading "
                f"number/stake stripped")
    if _CURRENCY_ONLY.match(tok):
        return (f"{HOLD_PREFIX} starts with currency {bare!r} and no amount — "
                f"leading number/stake stripped")
    first = tok[0]
    if first.isalpha() and first.islower():
        if bare in LOWERCASE_ALLOWLIST or bare.lower() in LOWERCASE_ALLOWLIST:
            return None
        if _CODEISH.search(bare):
            return None           # node_modules, package.json, gpt-4o, k8s …
        if any(c.isupper() for c in bare[1:]):
            return None           # camelCase brands: eGPU, vLLM, iPhone
        return (f"{HOLD_PREFIX} starts with lowercase fragment {bare!r} — "
                f"leading number/stake probably stripped")
    return None
