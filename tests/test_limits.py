"""Тести виправлень рецензії Codex (частина 1): ПІБ суддів не віддаються (C-M2), межі й обмеження
одночасності (C-M1), перевірка входу й єдиний формат помилок (C-L1). Синтетичні дані, без мережі.
Запуск: python tests/test_limits.py"""
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import court_db
import court_tools as ct

JUDGE = "Зубренко Ґеорґій Прокопович"
FMT = re.compile(r"^\[[a-z_]+\] .+")


def make_db(path, rows=3, long_names=False, extra_meta=()):
    """Синтетична база: у judge — впізнаване ім'я, також у FTS-індексі (як у справжній базі)."""
    con = court_db.connect(path)
    cname = "Суд " + ("к" * 5000 if long_names else "Київський")
    kname = "Категорія " + ("м" * 5000 if long_names else "боргу")
    con.execute("INSERT INTO courts VALUES('0101',?,'1','01')", (cname,))
    con.execute("INSERT INTO categories VALUES('10',?)", (kname,))
    con.execute("INSERT INTO justice_kinds VALUES('1','Цивільне')")
    con.execute("INSERT INTO judgment_forms VALUES('2','Ухвала')")
    con.execute("INSERT INTO regions VALUES('01','Київ')")
    con.execute("INSERT INTO instances VALUES('1','Перша')")
    for i in range(1, rows + 1):
        cause = "100/1/26" if long_names else f"{100 + i}/1/26"
        url = "http://x/" + ("u" * 5000 if long_names else str(i))
        con.execute("INSERT INTO decisions(doc_id,court_code,judgment_code,justice_kind,category_code,cause_num,"
                    "adjudication_date,receipt_date,judge,doc_url,status,date_publ,cause_norm) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (i, "0101", "2", "1", "10", cause, "2026-01-05", "2026-01-01", JUDGE, url, 1,
                     "2026-01-06", court_db.norm_cause(cause)))
        con.execute("INSERT INTO decisions_fts(rowid,cause_num,court,category,judge) VALUES(?,?,?,?,?)",
                    (i, cause, cname, kname, JUDGE))
    for k, v in extra_meta:
        court_db.set_meta(con, k, v)
    con.commit()
    con.close()


class Base(unittest.TestCase):
    long_names = False
    rows = 3

    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.db = Path(self.t.name) / "t.db"
        make_db(self.db, self.rows, self.long_names,
                extra_meta=[("last_update", "2026-11-05"), ("secret_future_key", "СЕКРЕТ"), ("judge_note", JUDGE)])

    def tearDown(self):
        self.t.cleanup()

    def all_tool_outputs(self):
        db = self.db
        return [
            ct.search_decisions(db, query="боргу", limit=50),
            ct.search_decisions(db, court="Київ", limit=50),
            ct.get_decision(db, 1), ct.get_decision(db, "2"), ct.get_decision(db, 999),
            ct.list_courts(db), ct.stats(db), ct.stats(db, use_cache=False),
            ct.search_by_case_number(db, "101/1/26"), ct.search_by_case_number(db, "10", prefix=True),
            ct.court_category_stats(db), ct.court_category_stats(db, group_by="court"),
        ]


def keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from keys(v)


class EnvMixin:
    def set_mode(self, value):
        old = os.environ.get("COURT_EXPOSE_JUDGE")
        if value is None:
            os.environ.pop("COURT_EXPOSE_JUDGE", None)
        else:
            os.environ["COURT_EXPOSE_JUDGE"] = value
        self.addCleanup(lambda: os.environ.pop("COURT_EXPOSE_JUDGE", None) if old is None
                        else os.environ.__setitem__("COURT_EXPOSE_JUDGE", old))


def tool_dicts(outs):
    for o in outs:
        for lst in ("decisions", "courts", "groups"):
            if isinstance(o.get(lst), list):
                yield from o[lst]
        if "doc_id" in o and o.get("found"):
            yield o


class JudgeExposed(EnvMixin, Base):
    """Рішення власника: за замовчуванням (COURT_EXPOSE_JUDGE=1 або не задано) суддя віддається ВСІМА
    інструментами, що повертають рішення, однаково — як у джерелі."""

    def check_exposed(self):
        outs = self.all_tool_outputs()
        decs = [d for d in tool_dicts(outs) if "doc_id" in d]
        self.assertGreaterEqual(len(decs), 4)
        for d in decs:                       # search_decisions, get_decision, search_by_case_number (точний і префікс)
            self.assertEqual(d.get("judge"), JUDGE, d)
        # інструменти, що рішень не повертають, судді не містять
        for o in (ct.list_courts(self.db), ct.stats(self.db), ct.court_category_stats(self.db)):
            self.assertNotIn("Зубренко", json.dumps(o, ensure_ascii=False))
        self.assertEqual(set(ct.stats(self.db)["meta"]) - set(ct.PUBLIC_META_KEYS), set())
        self.assertEqual(ct.search_decisions(self.db, query="Зубренко")["count"], 3)   # пошук за суддею як у джерелі

    def test_default_unset(self):
        self.set_mode(None)
        self.assertTrue(ct.expose_judge())
        self.check_exposed()

    def test_explicit_one(self):
        self.set_mode("1")
        self.check_exposed()

    def test_same_fields_everywhere(self):
        self.set_mode("1")
        a = ct.search_decisions(self.db, query="боргу")["decisions"][0]
        b = ct.get_decision(self.db, a["doc_id"])
        c = ct.search_by_case_number(self.db, a["cause_num"])["decisions"][0]
        self.assertEqual(a["judge"], b["judge"])
        self.assertEqual(a["judge"], c["judge"])

    def test_bad_value(self):
        self.set_mode("maybe")
        with self.assertRaises(RuntimeError):
            ct.expose_judge()


class JudgeHidden(EnvMixin, Base):
    """COURT_EXPOSE_JUDGE=0: жодна відповідь жодного інструмента не містить ключа чи значення судді,
    і за суддею шукати не можна."""

    def setUp(self):
        super().setUp()
        self.set_mode("0")

    def test_no_judge_key_or_value_in_any_tool(self):
        outs = self.all_tool_outputs()
        self.assertTrue(all(outs))
        for o in outs:
            text = json.dumps(o, ensure_ascii=False)
            for part in ("Зубренко", "Прокопович", "Ґеорґій"):
                self.assertNotIn(part, text)
            self.assertFalse([k for k in keys(o) if any(p in str(k).lower() for p in ct.FORBIDDEN_KEY_PARTS)])
            self.assertNotIn("judge", text.lower())

    def test_cannot_search_by_judge(self):
        for q in ("Зубренко", "Прокопович", "Зубренко Ґеорґій", "judge", "judge:Зубренко", "judge : Зубренко"):
            self.assertEqual(ct.search_decisions(self.db, query=q)["count"], 0, q)
        self.assertEqual(ct.search_decisions(self.db, query="боргу")["count"], 3)

    def test_select_has_no_judge_column(self):
        self.assertNotIn("judge", ct._select().lower())
        self.assertNotIn("judge", ct._select_case().lower())

    def test_decision_fields_allowlist(self):
        allowed = {"doc_id", "cause_num", "adjudication_date", "receipt_date", "date_publ", "court",
                   "court_code", "justice_kind", "judgment_form", "category", "category_code", "doc_url",
                   "status", "found", "note"}
        for d in tool_dicts(self.all_tool_outputs()):
            if "doc_id" in d:
                self.assertEqual(set(d) - allowed, set(), d)

    def test_stats_meta_allowlist(self):
        for use_cache in (True, False):
            m = ct.stats(self.db, use_cache=use_cache)["meta"]
            self.assertEqual(set(m) - set(ct.PUBLIC_META_KEYS), set())
        con = court_db.connect(self.db)
        court_db.set_meta(con, "stats_cache", json.dumps({"decisions": 1, "meta": {"judge_x": JUDGE, "last_update": "d"}}))
        con.commit(); con.close()
        out = ct.stats(self.db)
        self.assertEqual(out["meta"], {"last_update": "d"})

    def test_scrub_is_defence_in_depth(self):
        got = ct._scrub({"a": [{"judge": "x", "Judge_name": "y", "ok": 1}], "judge": 2})
        self.assertEqual(got, {"a": [{"ok": 1}]})


class InputLimits(Base):
    """C-M1: довжини, кількість слів."""

    def test_arg_length_limits(self):
        long = "а" * (ct.MAX_ARG_LEN + 1)
        for fn, kw in ((ct.search_decisions, dict(query=long)),
                       (ct.search_decisions, dict(court="а" * 101)),
                       (ct.search_decisions, dict(justice_kind="а" * 101)),
                       (ct.list_courts, dict(name="а" * 101)),
                       (ct.list_courts, dict(region="а" * 101)),
                       (ct.search_by_case_number, dict(case_number="1" * 121)),
                       (ct.court_category_stats, dict(court="а" * 101)),
                       (ct.court_category_stats, dict(category="а" * 101)),
                       (ct.court_category_stats, dict(group_by="а" * 31))):
            with self.assertRaises(ct.ToolError, msg=str(kw)) as cm:
                fn(self.db, **kw)
            self.assertEqual(cm.exception.code, "too_long")
        # 1 МБ у старих LIKE-параметрах — відмова, а не OperationalError
        with self.assertRaises(ct.ToolError):
            ct.search_decisions(self.db, court="x" * 1_000_000)

    def test_at_limit_ok(self):
        self.assertEqual(ct.search_decisions(self.db, court="а" * 100)["count"], 0)
        ct.list_courts(self.db, name="а" * 100)

    def test_fts_word_count(self):
        ok = " ".join(["боргу"] * ct.MAX_QUERY_WORDS)
        self.assertEqual(ct.search_decisions(self.db, query=ok)["count"], 3)
        with self.assertRaises(ct.ToolError) as cm:
            ct.search_decisions(self.db, query=ok + " боргу")
        self.assertEqual(cm.exception.code, "too_many_words")
        with self.assertRaises(ct.ToolError) as cm:
            ct.search_decisions(self.db, query="а" * (ct.MAX_WORD_LEN + 1))
        self.assertEqual(cm.exception.code, "too_long")

    def test_like_wildcards_are_literal(self):
        self.assertEqual(ct.search_decisions(self.db, court="%")["count"], 0)
        self.assertEqual(ct.list_courts(self.db, name="_")["count"], 0)
        self.assertEqual(ct.list_courts(self.db, name="Київ")["count"], 1)

    def test_deadline_kept(self):
        self.assertEqual(ct.STATS_DEADLINE, 5.0)
        con = ct.open_ro(self.db, deadline=0.01)
        con.execute("PRAGMA query_only")
        con.set_progress_handler(ct._Deadline(0.01), 1000)
        with self.assertRaises(sqlite3.OperationalError) as cm:
            con.execute("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT COUNT(*) FROM c").fetchone()
        self.assertIn("interrupted", str(cm.exception))
        con.close()

    def test_sql_errors_are_mapped(self):
        @ct.guarded
        def boom(kind):
            raise sqlite3.OperationalError(kind)
        for msg, code in (("interrupted", "too_heavy"), ("database is locked", "busy"), ("no such table: x", "db_error")):
            with self.assertRaises(ct.ToolError) as cm:
                boom(msg)
            self.assertEqual(cm.exception.code, code)
            self.assertNotIn("no such table", str(cm.exception))   # внутрішніх деталей не віддаємо


class ResponseSize(Base):
    long_names = True
    rows = 150

    def test_response_capped_with_flag(self):
        r = ct.search_by_case_number(self.db, "100/1/26", limit=200)
        self.assertLessEqual(ct._size(r), ct.MAX_RESPONSE_BYTES)
        self.assertTrue(r["response_truncated"] and r["truncated"])
        self.assertLess(r["count"], 150)
        self.assertEqual(r["count"], len(r["decisions"]))
        self.assertIn("response_note", r)

    def test_fields_clipped(self):
        r = ct.get_decision(self.db, 1)
        for k in ("court", "category", "doc_url"):
            self.assertLessEqual(len(r[k]), ct.MAX_FIELD_CHARS + 1)
        self.assertLessEqual(ct._size(r), ct.MAX_RESPONSE_BYTES)

    def test_small_response_unflagged(self):
        r = ct.search_by_case_number(self.db, "100/1/26", limit=2)
        self.assertNotIn("response_truncated", r)


class Concurrency(Base):
    def test_busy_when_slots_exhausted(self):
        gate, entered = threading.Event(), threading.Semaphore(0)
        real = ct.open_ro

        def slow(*a, **k):
            entered.release()
            gate.wait(10)
            return real(*a, **k)
        ct.open_ro = slow
        res, th = [], []
        try:
            for _ in range(ct.MAX_CONCURRENT):
                t = threading.Thread(target=lambda: res.append(ct.list_courts(self.db)))
                t.start(); th.append(t)
            for _ in range(ct.MAX_CONCURRENT):
                self.assertTrue(entered.acquire(timeout=5))
            with self.assertRaises(ct.ToolError) as cm:      # 5-й запит
                ct.stats(self.db)
            self.assertEqual(cm.exception.code, "busy")
            self.assertIn("спробуйте пізніше", str(cm.exception))
            gate.set()
            for t in th:
                t.join(10)
            self.assertEqual(len(res), ct.MAX_CONCURRENT)
        finally:
            gate.set()
            ct.open_ro = real
        self.assertEqual(ct.list_courts(self.db)["count"], 1)   # слоти звільнено
        self.assertEqual(ct.MAX_CONCURRENT, 4)

    def test_slot_released_after_error(self):
        for _ in range(ct.MAX_CONCURRENT * 3):
            with self.assertRaises(ct.ToolError):
                ct.get_decision(self.db, "x")
        self.assertEqual(ct.get_decision(self.db, 1)["found"], True)


class InputValidation(Base):
    """C-L1: doc_id, дати, NUL/керівні символи, єдиний формат помилок."""

    def test_doc_id(self):
        bad = ["²", "9" * 100, str(2**63), 2**63, "0", 0, -1, "-1", True, 1.5, "１２", "1\x00", "", " ", "1 2",
               "1e3", None, [], {}, "٣"]
        for v in bad:
            with self.assertRaises(ct.ToolError, msg=repr(v)) as cm:
                ct.get_decision(self.db, v)
            self.assertRegex(str(cm.exception), FMT)
        self.assertTrue(ct.get_decision(self.db, " 1 ")["found"])
        self.assertTrue(ct.get_decision(self.db, 1)["found"])
        self.assertFalse(ct.get_decision(self.db, str(2**63 - 1))["found"])

    def test_dates(self):
        bad = ["2026-99-99", "2026-02-30", "2026-13-01", "２０２６-１１-０５", "1800-01-01", "2101-01-01",
               "1989-12-31", "2026-1-1", "26-11-05", "2026/11/05", "2026-11-05 ", "2026-11-05\n", "2026-11-05\x00",
               "2026-11-05T00:00", 20261105, True]
        for v in bad:
            for fn, kw in ((ct.search_decisions, lambda v=v: dict(query="боргу", date_from=v)),
                           (ct.search_decisions, lambda v=v: dict(query="боргу", date_to=v)),
                           (ct.court_category_stats, lambda v=v: dict(date_from=v)),
                           (ct.court_category_stats, lambda v=v: dict(date_to=v))):
                with self.assertRaises(ct.ToolError, msg=repr(v)) as cm:
                    fn(self.db, **kw())
                self.assertRegex(str(cm.exception), FMT)
        for good in ("2024-02-29", "1990-01-01", "2100-12-31", "2026-11-05"):
            ct.search_decisions(self.db, query="боргу", date_from=good)
        with self.assertRaises(ct.ToolError):
            ct.court_category_stats(self.db, date_from="2026-02-01", date_to="2026-01-01")

    def test_control_chars_rejected_before_search(self):
        for ch in ("\x00", "\x01", "\x1b", "\x7f", "\n", "\t", "\r", " ", "\x85"):
            v = "а" + ch + "б"
            for fn, kw in ((ct.search_decisions, dict(query=v)), (ct.search_decisions, dict(court=v)),
                           (ct.search_decisions, dict(justice_kind=v)), (ct.search_decisions, dict(court=ch)),
                           (ct.list_courts, dict(name=v)), (ct.list_courts, dict(region=v)),
                           (ct.search_by_case_number, dict(case_number="1/1" + ch)),
                           (ct.court_category_stats, dict(court=v)), (ct.court_category_stats, dict(category=v)),
                           (ct.court_category_stats, dict(group_by="court" + ch))):
                with self.assertRaises(ct.ToolError, msg=f"{fn.__name__} {kw!r}") as cm:
                    fn(self.db, **kw)
                self.assertEqual(cm.exception.code, "invalid_chars")

    def test_nul_does_not_become_wide_search(self):
        with self.assertRaises(ct.ToolError):
            ct.search_decisions(self.db, court="\x00")

    def test_types_and_limit(self):
        for v in (True, False, 1.0, "5", None, 0, 51, -1):
            with self.assertRaises(ct.ToolError, msg=repr(v)):
                ct.search_decisions(self.db, query="боргу", limit=v)
        with self.assertRaises(ct.ToolError):
            ct.search_decisions(self.db, query=5)
        with self.assertRaises(ct.ToolError):
            ct.search_by_case_number(self.db, "1/1", prefix="yes")
        with self.assertRaises(ct.ToolError):
            ct.search_by_case_number(self.db, 5)
        with self.assertRaises(ct.ToolError):
            ct.court_category_stats(self.db, court=5)

    def test_ascii_digit_court_code(self):
        # арабо-індійські та інші цифри Unicode — не код суду, а назва (без збігів), без винятків
        self.assertEqual(ct.search_decisions(self.db, court="٠١٠١")["count"], 0)
        self.assertEqual(ct.search_decisions(self.db, court="101")["count"], 3)

    def test_unified_error_format(self):
        calls = [lambda: ct.search_decisions(self.db), lambda: ct.search_decisions(self.db, query="  "),
                 lambda: ct.get_decision(self.db, "x"), lambda: ct.list_courts(self.db, limit=0),
                 lambda: ct.search_by_case_number(self.db, "  "), lambda: ct.search_by_case_number(self.db, "1", prefix=True),
                 lambda: ct.court_category_stats(self.db, group_by="judge"), lambda: ct.court_category_stats(self.db, limit=201)]
        for c in calls:
            with self.assertRaises(ct.ToolError) as cm:
                c()
            self.assertRegex(str(cm.exception), FMT)


if __name__ == "__main__":
    unittest.main()
