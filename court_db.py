#!/usr/bin/env python3
"""Збірка SQLite (+FTS5) із CSV-архіву ЄДРСР (відкриті дані ДСА). Лише stdlib.

Архів містить ЛИШЕ метадані рішень (тексту немає). Імпорт ідемпотентний:
нові doc_id додаються, наявні пропускаються (INSERT OR IGNORE).
"""

import csv
import os
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

csv.field_size_limit(10_000_000)

SCHEMA = """
CREATE TABLE IF NOT EXISTS regions(region_code TEXT PRIMARY KEY, name TEXT);
CREATE TABLE IF NOT EXISTS instances(instance_code TEXT PRIMARY KEY, name TEXT);
CREATE TABLE IF NOT EXISTS justice_kinds(justice_kind TEXT PRIMARY KEY, name TEXT);
CREATE TABLE IF NOT EXISTS judgment_forms(judgment_code TEXT PRIMARY KEY, name TEXT);
CREATE TABLE IF NOT EXISTS categories(category_code TEXT PRIMARY KEY, name TEXT);
CREATE TABLE IF NOT EXISTS courts(
  court_code TEXT PRIMARY KEY, name TEXT, instance_code TEXT, region_code TEXT);
CREATE TABLE IF NOT EXISTS decisions(
  doc_id INTEGER PRIMARY KEY,
  court_code TEXT, judgment_code TEXT, justice_kind TEXT, category_code TEXT,
  cause_num TEXT, adjudication_date TEXT, receipt_date TEXT, judge TEXT,
  doc_url TEXT, status INTEGER, date_publ TEXT,
  cause_norm TEXT);
CREATE INDEX IF NOT EXISTS ix_dec_court_date ON decisions(court_code, adjudication_date);
CREATE INDEX IF NOT EXISTS ix_dec_date ON decisions(adjudication_date);
CREATE INDEX IF NOT EXISTS ix_dec_kind_date ON decisions(justice_kind, adjudication_date);
CREATE INDEX IF NOT EXISTS ix_dec_cause ON decisions(cause_num);
CREATE VIRTUAL TABLE IF NOT EXISTS decisions_fts USING fts5(
  cause_num, court, category, judge,
  content='', detail=column, tokenize='unicode61 remove_diacritics 2');
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""

REF_FILES = {
    "regions": ("regions.csv", "regions", ("region_code", "name")),
    "instances": ("instances.csv", "instances", ("instance_code", "name")),
    "justice_kinds": ("justice_kinds.csv", "justice_kinds", ("justice_kind", "name")),
    "judgment_forms": ("judgment_forms.csv", "judgment_forms", ("judgment_code", "name")),
    "categories": ("cause_categories.csv", "categories", ("category_code", "name")),
    "courts": ("courts.csv", "courts", ("court_code", "name", "instance_code", "region_code")),
}


# Індекси, що створюються після появи колонки cause_norm (див. ensure_schema).
EXTRA_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_dec_cause_norm ON decisions(cause_norm)",
    # для court_category_stats: покриваючі індекси (групування без читання таблиці)
    "CREATE INDEX IF NOT EXISTS ix_dec_cat_date ON decisions(category_code, adjudication_date)",
    "CREATE INDEX IF NOT EXISTS ix_dec_court_cat ON decisions(court_code, category_code)",
)

_DROP = dict.fromkeys(map(ord, "\"'`«»„“”‘’‚‹›′″ʼ"), None)
_DASH = dict.fromkeys(map(ord, "‐‑‒–—−"), "-")
_LATIN = str.maketrans("ABCEHIKMOPTXY", "АВСЕНІКМОРТХУ")  # латинські двійники -> кирилиця


def norm_cause(value):
    """Нормалізація номера справи: без пробілів і лапок, РЕГІСТР верхній, роздільники
    '\\' '|' '⁄' -> '/', тире -> '-', латинські двійники літер -> кирилиця,
    без «№» і без зайвої пунктуації по краях. Однаково застосовується до даних і до запиту."""
    s = (value or "").replace("№", "")
    s = unicodedata.normalize("NFKC", s).translate(_DROP).translate(_DASH)
    s = s.replace("\\", "/").replace("|", "/").replace("⁄", "/").replace("∕", "/")
    s = re.sub(r"\s+", "", s).strip(",.;:").upper()
    return s.translate(_LATIN)


def ensure_schema(con, batch=50000):
    """Міграція наявної бази: колонка cause_norm (+ заповнення) та додаткові індекси.
    Ідемпотентна; на новій базі нічого не робить, крім створення індексів."""
    cols = [r[1] for r in con.execute("PRAGMA table_info(decisions)")]
    if "cause_norm" not in cols:
        con.execute("ALTER TABLE decisions ADD COLUMN cause_norm TEXT")
        con.commit()
    while True:
        rows = con.execute(
            "SELECT doc_id, cause_num FROM decisions WHERE cause_norm IS NULL LIMIT ?", (batch,)).fetchall()
        if not rows:
            break
        con.executemany("UPDATE decisions SET cause_norm=? WHERE doc_id=?",
                        [(norm_cause(c), i) for i, c in rows])
        con.commit()
    for sql in EXTRA_INDEXES:
        con.execute(sql)
    con.commit()


def connect(db_path):
    con = sqlite3.connect(str(db_path))
    # WAL за замовчуванням; COURT_JOURNAL_MODE=DELETE потрібен, коли базу читає сервіс
    # з read-only файлової системи (WAL створює -shm поруч із базою).
    mode = os.environ.get("COURT_JOURNAL_MODE", "WAL").upper()
    if mode not in ("WAL", "DELETE"):
        raise ValueError("COURT_JOURNAL_MODE: WAL або DELETE")
    con.execute(f"PRAGMA journal_mode={mode}")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA)
    ensure_schema(con)
    return con


def read_tsv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        yield from csv.DictReader(f, delimiter="\t", quotechar='"')


def day(value):
    """'2026-01-01 00:00:00+02' -> '2026-01-01'."""
    v = (value or "").strip()
    return v[:10] if len(v) >= 10 else None


def load_refs(con, csv_dir):
    csv_dir = Path(csv_dir)
    for _, (fname, table, cols) in REF_FILES.items():
        path = csv_dir / fname
        if not path.is_file():
            continue
        rows = [tuple((r.get(c) or "").strip() for c in cols) for r in read_tsv(path)]
        ph = ",".join("?" * len(cols))
        con.executemany(f"INSERT OR REPLACE INTO {table}({','.join(cols)}) VALUES({ph})", rows)
    con.commit()


def import_documents(con, csv_dir, since=None, since_publ=None, batch=50000, log=sys.stderr):
    """Додає нові рішення. since — лише adjudication_date >= since; since_publ — лише date_publ >= since_publ (обидва YYYY-MM-DD).
    Повертає (прочитано, додано)."""
    csv_dir = Path(csv_dir)
    court_name = dict(con.execute("SELECT court_code, name FROM courts"))
    cat_name = dict(con.execute("SELECT category_code, name FROM categories"))
    seen = added = 0
    rows, fts = [], []
    before = con.total_changes

    def flush():
        nonlocal rows, fts, added
        if not rows:
            return
        cur = con.cursor()
        for r, f in zip(rows, fts):
            cur.execute(
                "INSERT OR IGNORE INTO decisions(doc_id,court_code,judgment_code,justice_kind,"
                "category_code,cause_num,adjudication_date,receipt_date,judge,doc_url,status,"
                "date_publ,cause_norm) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", r)
            if cur.rowcount == 1:
                cur.execute(
                    "INSERT INTO decisions_fts(rowid,cause_num,court,category,judge) "
                    "VALUES(?,?,?,?,?)", (r[0],) + f)
                added += 1
        con.commit()
        rows, fts = [], []

    for r in read_tsv(csv_dir / "documents.csv"):
        seen += 1
        adj = day(r.get("adjudication_date"))
        if since and (adj is None or adj < since):
            continue
        if since_publ and (day(r.get("date_publ")) or "") < since_publ:
            continue
        try:
            doc_id = int(r["doc_id"])
        except (KeyError, ValueError):
            continue
        cc = (r.get("court_code") or "").strip()
        cat = (r.get("category_code") or "").strip()
        cause = (r.get("cause_num") or "").strip()
        judge = (r.get("judge") or "").strip()
        status = r.get("status")
        rows.append((
            doc_id, cc, (r.get("judgment_code") or "").strip(),
            (r.get("justice_kind") or "").strip(), cat, cause, adj,
            day(r.get("receipt_date")), judge, (r.get("doc_url") or "").strip() or None,
            int(status) if (status or "").strip().isdigit() else None,
            day(r.get("date_publ")), norm_cause(cause),
        ))
        fts.append((cause.replace("/", " "), court_name.get(cc, ""),
                    cat_name.get(cat, ""), judge))
        if len(rows) >= batch:
            flush()
            print(f"  прочитано {seen}, додано {added}", file=log, flush=True)
    flush()
    return seen, added


def refresh_stats(con):
    """Кешує stats() у meta (повний підрахунок на мільйонах рядків повільний)."""
    import json
    import court_tools
    con.commit()
    db = con.execute("PRAGMA database_list").fetchone()[2]
    set_meta(con, "stats_cache", json.dumps(court_tools.stats(db, use_cache=False), ensure_ascii=False))


def set_meta(con, key, value):
    con.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, str(value)))
    con.commit()


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Імпорт CSV-архіву ЄДРСР у SQLite")
    p.add_argument("csv_dir")
    p.add_argument("db")
    p.add_argument("--since", help="YYYY-MM-DD: лише рішення з цієї дати ухвалення")
    a = p.parse_args()
    con = connect(a.db)
    load_refs(con, a.csv_dir)
    seen, added = import_documents(con, a.csv_dir, a.since)
    set_meta(con, "last_import_rows", f"{seen}/{added}")
    refresh_stats(con)
    print(f"прочитано {seen}, додано {added}")
