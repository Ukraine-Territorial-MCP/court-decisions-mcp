"""Автотести court_db / court_tools / update_daily на синтетичних даних (stdlib, без мережі).
update_daily тестується з --zip; мережеві функції НЕ викликаються.
Усе тут — «на підмінах» (синтетичні CSV), не на реальному архіві."""
import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import court_db
import court_tools as ct
import update_daily

H = "doc_id\tcourt_code\tjudgment_code\tjustice_kind\tcategory_code\tcause_num\tadjudication_date\treceipt_date\tjudge\tdoc_url\tstatus\tdate_publ"


def docs(rows):
    return H + "\n" + "\n".join("\t".join(map(str, r)) for r in rows) + "\n"


R1 = [(1, "0101", "2", "1", "10", "100/1/26", "2026-01-05 00:00:00+02", "2026-01-01 00:00:00+02", "Тест А.А.", "http://x/1", 1, "2026-01-06 00:00:00+02"),
      (2, "0102", "3", "2", "11", "200/2/26", "2026-02-05 00:00:00+02", "2026-02-01 00:00:00+02", "Тест Б.Б.", "", 1, "2026-02-06 00:00:00+02")]
R2 = R1 + [(3, "0101", "2", "1", "10", "300/3/26", "2026-02-09 00:00:00+02", "", "Тест В.В.", "http://x/3", 1, "2026-02-10 00:00:00+02")]
REFS = dict([
    ("courts.csv", "court_code\tname\tinstance_code\tregion_code\n0101\tКиївський районний суд\t1\t01\n0102\tЛьвівський апеляційний суд\t2\t02\n"),
    ("justice_kinds.csv", "justice_kind\tname\n1\tЦивільне\n2\tКримінальне\n"),
    ("cause_categories.csv", "category_code\tname\n10\tСтягнення боргу\n11\tКрадіжка\n"),
    ("regions.csv", "region_code\tname\n01\tКиїв\n02\tЛьвівська\n"),
    ("instances.csv", "instance_code\tname\n1\tПерша\n2\tАпеляційна\n"),
    ("judgment_forms.csv", "judgment_code\tname\n2\tУхвала\n3\tВирок\n"),
])


def mkdir(tmp, rows):
    d = Path(tmp) / "csv"
    d.mkdir(exist_ok=True)
    for k, v in REFS.items():
        (d / k).write_text(v, encoding="utf-8")
    (d / "documents.csv").write_text(docs(rows), encoding="utf-8")
    return d


def mkzip(tmp, rows, name):
    d = mkdir(tmp, rows)
    z = Path(tmp) / name
    with zipfile.ZipFile(z, "w") as f:
        for p in d.iterdir():
            f.write(p, p.name)
    return z


class Core(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.db = Path(self.t.name) / "t.db"
        con = court_db.connect(self.db)
        d = mkdir(self.t.name, R1)
        court_db.load_refs(con, d)
        self.res = court_db.import_documents(con, d, log=io.StringIO())
        con.close()

    def tearDown(self):
        self.t.cleanup()

    def test_import(self):
        self.assertEqual(self.res, (2, 2))

    def test_idempotent(self):
        con = court_db.connect(self.db)
        self.assertEqual(court_db.import_documents(con, Path(self.t.name) / "csv", log=io.StringIO()), (2, 0))
        con.close()

    def test_search_fts(self):
        r = ct.search_decisions(self.db, query="борг")
        self.assertEqual([x["doc_id"] for x in r["decisions"]], [1])
        self.assertEqual(ct.search_decisions(self.db, query="200/2/26")["count"], 1)

    def test_filters(self):
        self.assertEqual(ct.search_decisions(self.db, court="Львів")["count"], 1)
        self.assertEqual(ct.search_decisions(self.db, justice_kind="2")["count"], 1)
        self.assertEqual(ct.search_decisions(self.db, date_from="2026-02-01")["count"], 1)

    def test_validation(self):
        bad = [dict(), dict(query="x", limit=0), dict(query="x", limit=999),
               dict(query="x", date_from="5.1.2026"), dict(query='"*')]
        for kw in bad:
            with self.assertRaises(ValueError, msg=str(kw)):
                ct.search_decisions(self.db, **kw)

    def test_fts_operators_are_literal(self):
        ct.search_decisions(self.db, query='борг OR NEAR( "')  # не має кидати sqlite-помилку

    def test_get(self):
        self.assertTrue(ct.get_decision(self.db, 1)["found"])
        self.assertFalse(ct.get_decision(self.db, 99)["found"])
        with self.assertRaises(ValueError):
            ct.get_decision(self.db, "1; DROP")

    def test_courts_stats(self):
        self.assertEqual(ct.list_courts(self.db, region="Льв")["count"], 1)
        self.assertEqual(ct.stats(self.db)["decisions"], 2)

    def test_readonly(self):
        con = ct.open_ro(self.db)
        with self.assertRaises(Exception):
            con.execute("DELETE FROM decisions")
        con.close()

    def test_update_zip_incremental(self):
        z = mkzip(self.t.name, R2, "u.zip")
        con = court_db.connect(self.db)
        quiet = lambda *_: None
        self.assertEqual(update_daily.update_year(con, 2026, self.t.name, zip_path=z, log=quiet), 1)
        self.assertEqual(update_daily.update_year(con, 2026, self.t.name, zip_path=z, log=quiet), 0)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], 3)
        con.close()


if __name__ == "__main__":
    unittest.main()
