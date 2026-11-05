#!/usr/bin/env python3
"""Будує data/sample.db — малу вибірку з повної бази для тестів і демо.

Персональні дані прибрано: поле `judge` порожнє (і в таблиці, і в FTS).
Вибірка: до 40 рішень для кількох найчастіших номерів справ (щоб тестувати
пошук по справі) + випадкові рядки. Довідники (суди, категорії…) копіюються повністю.

Запуск: python3 scripts/build_sample.py ПОВНА_БАЗА.db data/sample.db [--rows 3000] [--seed 1]
"""
import argparse
import random
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import court_db  # noqa: E402
import court_tools  # noqa: E402

REF = ("regions", "instances", "justice_kinds", "judgment_forms", "categories", "courts")
COLS = ("doc_id,court_code,judgment_code,justice_kind,category_code,cause_num,"
        "adjudication_date,receipt_date,doc_url,status,date_publ,cause_norm")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--rows", type=int, default=3000)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    if Path(a.dst).exists():
        sys.exit("dst вже існує")
    src = sqlite3.connect(f"file:{a.src}?mode=ro", uri=True)
    ids = set()
    top = src.execute("SELECT cause_norm FROM decisions WHERE cause_norm<>'' AND cause_norm NOT GLOB '*[^0-9/]*' "
                      "GROUP BY 1 ORDER BY COUNT(*) DESC LIMIT 5").fetchall()
    for (c,) in top:
        ids.update(r[0] for r in src.execute("SELECT doc_id FROM decisions WHERE cause_norm=? LIMIT 40", (c,)))
    rnd = random.Random(a.seed)
    all_ids = [r[0] for r in src.execute("SELECT doc_id FROM decisions")]
    ids.update(rnd.sample(all_ids, min(a.rows, len(all_ids))))

    con = court_db.connect(a.dst)
    for t in REF:
        rows = src.execute(f"SELECT * FROM {t}").fetchall()
        if rows:
            con.executemany(f"INSERT OR REPLACE INTO {t} VALUES({','.join('?' * len(rows[0]))})", rows)
    court = dict(con.execute("SELECT court_code,name FROM courts"))
    cat = dict(con.execute("SELECT category_code,name FROM categories"))
    n = 0
    for chunk in range(0, len(ids), 500):
        part = sorted(ids)[chunk:chunk + 500]
        q = f"SELECT {COLS} FROM decisions WHERE doc_id IN ({','.join('?' * len(part))})"
        for r in src.execute(q, part):
            con.execute(f"INSERT INTO decisions({COLS},judge) VALUES({','.join('?' * 12)},'')", r)
            con.execute("INSERT INTO decisions_fts(rowid,cause_num,court,category,judge) VALUES(?,?,?,?,'')",
                        (r[0], (r[5] or "").replace("/", " "), court.get(r[1], ""), cat.get(r[4], "")))
            n += 1
    con.commit()
    court_db.set_meta(con, "sample", f"{n} рядків; judge прибрано")
    court_db.refresh_stats(con)
    con.execute("PRAGMA journal_mode=DELETE")
    con.execute("VACUUM")
    con.close()
    print("рядків:", n)


if __name__ == "__main__":
    main()
