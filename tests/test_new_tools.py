"""Автотести search_by_case_number і court_category_stats.
Частина А — на КОПІЇ data/sample.db (реальні метадані, у репозиторії ≈3 тис. рядків без імен суддів; копія мігрується в тимчасовій теці,
оригінал не змінюється). Частина Б — на синтетичних даних (на підмінах) для точних очікувань.
Запуск: python3 tests/test_new_tools.py"""
import io
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import court_db
import court_tools as ct
import update_daily
import test_core as tc

SAMPLE = ROOT / "data" / "sample.db"


@unittest.skipUnless(SAMPLE.is_file(), "немає data/sample.db")
class OnSample(unittest.TestCase):
    """sample.db — реальні дані, не підміни."""

    @classmethod
    def setUpClass(cls):
        cls.t = tempfile.TemporaryDirectory()
        cls.db = Path(cls.t.name) / "s.db"
        src = sqlite3.connect(f"file:{SAMPLE}?mode=ro", uri=True)
        dst = sqlite3.connect(cls.db)
        src.backup(dst)
        src.close()
        dst.close()
        con = court_db.connect(cls.db)  # міграція: cause_norm + індекси
        cls.top, cls.n = con.execute(
            "SELECT cause_num, COUNT(*) n FROM decisions WHERE cause_num<>'' AND cause_num NOT GLOB '*[^0-9/]*' "
            "GROUP BY 1 ORDER BY n DESC LIMIT 1").fetchone()
        con.close()
        c2 = sqlite3.connect(cls.db)
        cls.n_rows = c2.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        c2.close()

    @classmethod
    def tearDownClass(cls):
        cls.t.cleanup()

    def test_migration(self):
        con = sqlite3.connect(self.db)
        idx = {r[1] for r in con.execute("PRAGMA index_list(decisions)")}
        self.assertTrue({"ix_dec_cause_norm", "ix_dec_cat_date", "ix_dec_court_cat"} <= idx)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM decisions WHERE cause_norm IS NULL").fetchone()[0], 0)
        con.close()
        con = court_db.connect(self.db)  # повторна міграція — без змін
        con.close()

    def test_exact_all_and_limit(self):
        r = ct.search_by_case_number(self.db, self.top, limit=200)
        self.assertEqual(r["count"], min(self.n, 200))
        self.assertTrue(all(d["cause_num"] == self.top for d in r["decisions"]))
        self.assertEqual(r["mode"], "exact")
        one = ct.search_by_case_number(self.db, self.top, limit=1)
        self.assertEqual((one["count"], one["truncated"]), (1, self.n > 1))
        self.assertEqual(ct.search_by_case_number(self.db, self.top)["limit"], 50)

    def test_result_fields(self):
        d = ct.search_by_case_number(self.db, self.top, limit=1)["decisions"][0]
        for k in ("doc_id", "cause_num", "court", "court_code", "adjudication_date", "justice_kind",
                  "judgment_form", "category", "category_code", "doc_url", "date_publ"):
            self.assertIn(k, d)
        self.assertIn("judge", d)   # COURT_EXPOSE_JUDGE=1 за замовчуванням (рішення власника)

    def test_normalization(self):
        base = ct.search_by_case_number(self.db, self.top, limit=200)["decisions"]
        for variant in (f"  {self.top}  ", self.top.replace("/", " / "), self.top.replace("/", "\\"),
                        self.top.replace("/", "|"), f"«{self.top}»", f'"{self.top}"', f"№ {self.top}"):
            r = ct.search_by_case_number(self.db, variant, limit=200)
            self.assertEqual([d["doc_id"] for d in r["decisions"]], [d["doc_id"] for d in base], variant)

    def test_prefix(self):
        pre = self.top.split("/")[0] + "/"
        r = ct.search_by_case_number(self.db, pre, prefix=True, limit=200)
        self.assertGreater(r["count"], 0)
        self.assertTrue(all(d["cause_num"].startswith(pre) for d in r["decisions"]))
        # точний режим з тим самим текстом не знаходить нічого
        self.assertEqual(ct.search_by_case_number(self.db, pre)["count"], 0)

    def test_empty_and_bad(self):
        self.assertEqual(ct.search_by_case_number(self.db, "000/000/00")["count"], 0)
        for kw in (dict(case_number=""), dict(case_number="   «»  "), dict(case_number=None),
                   dict(case_number=123), dict(case_number="x" * 500),
                   dict(case_number="1/1/1", limit=0), dict(case_number="1/1/1", limit=201),
                   dict(case_number="1/1/1", limit="5"), dict(case_number="1/1/1", limit=True),
                   dict(case_number="9", prefix=True), dict(case_number="1/1/1", prefix="yes")):
            with self.assertRaises(ValueError, msg=str(kw)):
                ct.search_by_case_number(self.db, **kw)

    def test_sql_injection_is_inert(self):
        r = ct.search_by_case_number(self.db, "1' OR '1'='1", limit=5)
        self.assertEqual(r["count"], 0)
        self.assertEqual(sqlite3.connect(self.db).execute("SELECT COUNT(*) FROM decisions").fetchone()[0] > 0, True)

    def test_stats_both_sorted(self):
        r = ct.court_category_stats(self.db, limit=200)
        ns = [g["count"] for g in r["groups"]]
        self.assertEqual(ns, sorted(ns, reverse=True))
        self.assertEqual(r["limit"], 200)
        self.assertEqual(r["group_by"], "court+category")
        total = sqlite3.connect(self.db).execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        self.assertEqual(r["total_matched"], total)

    def test_stats_group_modes_consistent(self):
        total = ct.court_category_stats(self.db, group_by="court", limit=200)["total_matched"]
        by_court = ct.court_category_stats(self.db, group_by="court", limit=200)
        self.assertNotIn("category", by_court["groups"][0])
        by_cat = ct.court_category_stats(self.db, group_by="category", limit=200)
        self.assertNotIn("court", by_cat["groups"][0])
        self.assertEqual(by_court["total_matched"], by_cat["total_matched"], total)

    def test_stats_filters(self):
        all_c = ct.court_category_stats(self.db, group_by="court", limit=1)
        code = all_c["groups"][0]["court_code"]
        by_code = ct.court_category_stats(self.db, court=code, group_by="category", limit=200)
        self.assertEqual(by_code["total_matched"], all_c["groups"][0]["count"])
        part = all_c["groups"][0]["court"][:6].upper()  # частина назви, інший регістр
        by_name = ct.court_category_stats(self.db, court=part, group_by="court", limit=200)
        self.assertGreaterEqual(by_name["count"], 1)
        self.assertIn(code, [g["court_code"] for g in by_name["groups"]])
        r = ct.court_category_stats(self.db, date_from="2099-01-01")
        self.assertEqual((r["count"], r["total_matched"]), (0, 0))
        r = ct.court_category_stats(self.db, court="немає такого суду ЯЯЯ")
        self.assertEqual(r["count"], 0)

    def test_stats_bad(self):
        for kw in (dict(group_by="judge"), dict(group_by=5), dict(limit=0), dict(limit=201),
                   dict(date_from="01.02.2026"), dict(date_to="2026-13"), dict(court=5),
                   dict(date_from="2026-05-01", date_to="2026-04-01"), dict(court="x" * 300)):
            with self.assertRaises(ValueError, msg=str(kw)):
                ct.court_category_stats(self.db, **kw)

    def test_stats_deadline_guard(self):
        # Запобіжник перевіряється кожні 100 000 кроків SQLite; на малій вибірці запит завершується раніше.
        if self.n_rows < 100_000:
            self.skipTest("вибірка замала для спрацювання запобіжника (перевірено на повній базі)")
        old = ct.STATS_DEADLINE
        ct.STATS_DEADLINE = 0.000001  # примусове спрацювання запобіжника
        try:
            with self.assertRaises(ValueError) as cm:
                ct.court_category_stats(self.db)
            self.assertIn("важкий", str(cm.exception))
        finally:
            ct.STATS_DEADLINE = old

    def test_readonly(self):
        con = ct.open_ro(self.db)
        with self.assertRaises(Exception):
            con.execute("UPDATE decisions SET cause_norm=''")
        con.close()


class Synthetic(unittest.TestCase):
    """На підмінах (синтетичні CSV з test_core): точні очікування."""

    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.db = Path(self.t.name) / "t.db"
        rows = list(tc.R2) + [
            (4, "0101", "2", "1", "10", "910 / 5 / 26-а", "2026-03-01 00:00:00+02", "", "Т", "http://x/4", 1, "2026-03-02 00:00:00+02"),
            (5, "0102", "3", "2", "", "910/5/26-А", "2026-03-05 00:00:00+02", "", "Т", "", 1, "2026-03-06 00:00:00+02")]
        con = court_db.connect(self.db)
        d = tc.mkdir(self.t.name, rows)
        court_db.load_refs(con, d)
        court_db.import_documents(con, d, log=io.StringIO())
        con.close()

    def tearDown(self):
        self.t.cleanup()

    def test_case_variants_same_case(self):
        for q in ("910/5/26-А", "910 / 5 / 26 - а", "910\\5\\26-A", "«910/5/26-а»"):
            r = ct.search_by_case_number(self.db, q)
            self.assertEqual([d["doc_id"] for d in r["decisions"]], [5, 4], q)  # нові спочатку

    def test_prefix_synthetic(self):
        r = ct.search_by_case_number(self.db, "910/", prefix=True)
        self.assertEqual({d["doc_id"] for d in r["decisions"]}, {4, 5})
        self.assertEqual(ct.search_by_case_number(self.db, "100/1", prefix=True)["count"], 1)

    def test_stats_exact_numbers(self):
        r = ct.court_category_stats(self.db)
        got = {(g["court_code"], g["category_code"]): g["count"] for g in r["groups"]}
        self.assertEqual(got, {("0101", "10"): 3, ("0102", "11"): 1, ("0102", ""): 1})
        self.assertEqual(r["groups"][0]["count"], 3)
        r = ct.court_category_stats(self.db, category="борг", group_by="court")
        self.assertEqual([(g["court_code"], g["count"]) for g in r["groups"]], [("0101", 3)])
        r = ct.court_category_stats(self.db, date_from="2026-03-01", group_by="category")
        self.assertEqual(r["total_matched"], 2)
        self.assertIsNone([g for g in r["groups"] if g["category_code"] == ""][0]["category"])
        self.assertEqual(ct.court_category_stats(self.db, limit=1)["truncated"], True)

    def test_update_keeps_index_and_fills_norm(self):
        z = tc.mkzip(self.t.name, tc.R2 + [(9, "0101", "2", "1", "10", " 777 / 7 / 26 ", "2026-03-09 00:00:00+02", "", "Т", "", 1, "2026-03-10 00:00:00+02")], "u.zip")
        con = court_db.connect(self.db)
        n = update_daily.update_year(con, 2026, self.t.name, zip_path=z, log=lambda *_: None)
        self.assertEqual(n, 1)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM decisions WHERE cause_norm IS NULL").fetchone()[0], 0)
        idx = {r[1] for r in con.execute("PRAGMA index_list(decisions)")}
        self.assertTrue({"ix_dec_cause_norm", "ix_dec_court_cat", "ix_dec_cat_date"} <= idx)
        con.close()
        self.assertEqual(ct.search_by_case_number(self.db, "777/7/26")["count"], 1)

    def test_migration_of_old_schema(self):
        old = Path(self.t.name) / "old.db"
        c = sqlite3.connect(old)
        c.executescript("CREATE TABLE decisions(doc_id INTEGER PRIMARY KEY, court_code TEXT, judgment_code TEXT,"
                        "justice_kind TEXT, category_code TEXT, cause_num TEXT, adjudication_date TEXT,"
                        "receipt_date TEXT, judge TEXT, doc_url TEXT, status INTEGER, date_publ TEXT);"
                        "INSERT INTO decisions(doc_id,cause_num) VALUES(1,' 12 / 3 / 26 ');")
        c.commit(); c.close()
        con = court_db.connect(old)
        self.assertEqual(con.execute("SELECT cause_norm FROM decisions").fetchone()[0], "12/3/26")
        con.close()


if __name__ == "__main__":
    unittest.main()
