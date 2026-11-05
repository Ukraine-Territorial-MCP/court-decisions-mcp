#!/usr/bin/env python3
"""Живий тест через реальний MCP SDK (mcp==2.3.0): судді не віддаються, перевірка входу, єдиний формат помилок.
Сервер стартує на 127.0.0.1:18941 і зупиняється тут (try/finally). Працює на будь-якій базі зі схемою court_db
(для перевірки суддів краще база, де judge непорожнє; на sample.db релізу judge порожнє).
Запуск: python tests/test_mcp_privacy.py data/sample.db"""
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 18941
URL = f"http://127.0.0.1:{PORT}/mcp"
HDR = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


def rpc(method, params=None, id_=1):
    body = json.dumps({"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}).encode()
    with urllib.request.urlopen(urllib.request.Request(URL, body, HDR), timeout=30) as r:
        return json.loads(r.read())


def main(db):
    p = subprocess.Popen([sys.executable, str(ROOT / "mcp_server.py"), "--db", db, "--port", str(PORT)],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(40):
            try:
                rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                   "clientInfo": {"name": "t", "version": "0"}})
                break
            except Exception:
                if p.poll() is not None:
                    raise SystemExit("сервер завершився: " + p.stdout.read().decode()[-800:])
                time.sleep(0.5)
        else:
            raise SystemExit("сервер не піднявся")

        def call(n, a):
            r = rpc("tools/call", {"name": n, "arguments": a}, id_=3)
            res = r["result"]
            if res.get("isError"):
                return res
            return res.get("structuredContent") or json.loads(res["content"][0]["text"])

        def raw(n, a):
            r = rpc("tools/call", {"name": n, "arguments": a}, id_=4)
            assert "result" in r, r
            return r["result"]

        import re
        import sqlite3
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        judges = [r[0] for r in con.execute("SELECT DISTINCT judge FROM decisions WHERE judge<>'' LIMIT 30")]
        first = con.execute("SELECT doc_id, cause_num FROM decisions WHERE cause_num<>'' LIMIT 1").fetchone()
        con.close()
        blobs = []
        for n, a in (("search_decisions", {"query": "кредит", "limit": 50}), ("get_decision", {"doc_id": str(first[0])}),
                     ("list_courts", {"limit": 50}), ("stats", {}),
                     ("search_by_case_number", {"case_number": first[1]}),
                     ("court_category_stats", {"limit": 50})):
            res = raw(n, a)
            assert not res.get("isError"), (n, res)
            blobs.append(json.dumps(res, ensure_ascii=False))
        import os
        exposed = os.environ.get("COURT_EXPOSE_JUDGE", "1").strip() not in ("0", "false", "no", "off")
        if exposed:   # search_decisions, get_decision, search_by_case_number віддають judge однаково
            for i in (0, 1, 4):
                assert '\\"judge\\"' in blobs[i] or '"judge"' in blobs[i], ("немає judge", i, blobs[i][:200])
            for i in (2, 3, 5):
                assert "judge" not in blobs[i].lower(), i
            print("OK COURT_EXPOSE_JUDGE=1: judge є в усіх інструментах, що повертають рішення")
        else:
            for b in blobs:
                assert "judge" not in b.lower(), b[:200]
                for j in judges:
                    assert j not in b, "ім'я судді у відповіді"
            if judges:
                r = call("search_decisions", {"query": judges[0].split()[0], "limit": 5})
                assert not isinstance(r, dict) or judges[0] not in json.dumps(r, ensure_ascii=False), r
            print("OK COURT_EXPOSE_JUDGE=0: ні ключа, ні значень судді (значень у вибірці:", len(judges), ")")
        for n, a in (("get_decision", {"doc_id": "²"}), ("get_decision", {"doc_id": "9" * 100}),
                     ("search_decisions", {"query": "x", "date_from": "2026-99-99"}),
                     ("search_decisions", {"court": "\x00"}), ("search_decisions", {"query": "а" * 201}),
                     ("search_decisions", {"query": "a b c d e f g h i"}),
                     ("search_decisions", {"query": "кредит", "limit": True}),
                     ("search_decisions", {"query": "кредит", "limit": "5"}),
                     ("search_by_case_number", {"case_number": "1/1/1", "prefix": "yes"}),
                     ("list_courts", {"name": "x" * 1_000_000})):
            res = raw(n, a)
            assert res.get("isError"), (n, a, str(res)[:200])
            txt = res["content"][0]["text"]
            assert "Traceback" not in txt and "/srv/" not in txt and "sqlite" not in txt.lower(), txt
        res = raw("get_decision", {"doc_id": "²"})
        assert re.search(r"\[invalid_input\]", res["content"][0]["text"]), res
        print("OK перевірка входу й єдиний формат помилок (через SDK)")
    finally:
        p.terminate()
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()


if __name__ == "__main__":
    main(sys.argv[1])
