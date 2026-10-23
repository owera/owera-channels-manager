"""Lista (read-only) vídeos cujo storyboard gravado tem linhas CLI com o
cut mark "…"/"..." — boards que o gate estrito do PR #138 passa a segurar.

No claw0, contra o DB de prod (usa a DATABASE_URL já configurada do app; só SELECT):
    PYTHONPATH=. .venv/bin/python scripts/list_cut_mark_boards.py
Também lê exports JSON (API /videos, live-*.json, listas ou {"items": [...]}):
    PYTHONPATH=. .venv/bin/python scripts/list_cut_mark_boards.py --json a.json b.json
Opções: --sqlite caminho.db (abre em modo ro), --all (inclui publicados/rejeitados).
Não escreve nada.
"""
import argparse
import json
import sqlite3
import sys

CLI_TYPES = {"code", "command"}
MARKS = ("\u2026", "...")
SKIP_STATUS = {"published", "rejected"}


def _load(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return v


def cut_lines(cfg):
    cfg = _load(cfg)
    if not isinstance(cfg, dict):
        return []
    out = []
    for i, b in enumerate(cfg.get("beats") or []):
        if not isinstance(b, dict) or b.get("type") not in CLI_TYPES:
            continue
        raw = list(b.get("lines") or []) if b.get("type") == "code" else \
            [b.get("command")] + list(b.get("output") or [])
        for ln in raw:
            if isinstance(ln, str) and ln.rstrip().endswith(MARKS):
                out.append((i, b.get("type"), ln))
    return out


def report(rows, include_all):
    n = 0
    for r in rows:
        st = str(r.get("status") or "").lower().split(".")[-1]
        if not include_all and st in SKIP_STATUS:
            continue
        hits = cut_lines(r.get("creation_config"))
        if not hits:
            continue
        n += 1
        print(f"video {r.get('id')} status={st} topic={r.get('topic_id')} "
              f"held={r.get('held')} title={str(r.get('title') or '')[:60]!r}")
        for i, typ, ln in hits:
            print(f"    card {i} ({typ}): {ln!r}")
    print(f"{n} video(s) with cut-mark CLI lines")
    return n


def rows_from_json(paths):
    for p in paths:
        try:
            d = json.load(open(p))
        except (OSError, ValueError) as e:
            print(f"skip {p}: {e}", file=sys.stderr)
            continue
        items = d if isinstance(d, list) else (d.get("items") or d.get("videos") or [d]) \
            if isinstance(d, dict) else []
        for x in items:
            if isinstance(x, dict):
                yield x


def rows_from_sqlite(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cols = {r[1] for r in con.execute("PRAGMA table_info(video)")}
    if "creation_config" not in cols:
        print(f"{path}: table video has no creation_config", file=sys.stderr)
        return
    want = [c for c in ("id", "status", "topic_id", "held", "title", "creation_config") if c in cols]
    for r in con.execute(f"SELECT {', '.join(want)} FROM video WHERE creation_config IS NOT NULL"):
        yield dict(r)


def rows_from_app_db():
    from sqlalchemy import text
    from app.db import engine
    with engine.connect() as c:
        res = c.execute(text("SELECT id, status, topic_id, held, title, creation_config "
                             "FROM video WHERE creation_config IS NOT NULL"))
        for r in res.mappings():
            yield dict(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", nargs="*")
    ap.add_argument("--sqlite")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    if a.json:
        rows = rows_from_json(a.json)
    elif a.sqlite:
        rows = rows_from_sqlite(a.sqlite)
    else:
        rows = rows_from_app_db()
    report(rows, a.all)


if __name__ == "__main__":
    main()
