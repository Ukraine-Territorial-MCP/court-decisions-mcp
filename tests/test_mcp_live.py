#!/usr/bin/env python3
"""Живий тест MCP на 127.0.0.1:18940: initialize, tools/list, виклики. Сервер стартує і зупиняється тут (try/finally).
Запуск: python tests/test_mcp_live.py data/sample.db   (потрібен пакет mcp==2.3.0)
Реальний сервер і реальна SDK; 6 інструментів."""
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 18940
URL = f"http://127.0.0.1:{PORT}/mcp"
HDR = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


def rpc(method, params=None, id_=1):
    body = json.dumps({"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}).encode()
    req = urllib.request.Request(URL, body, HDR)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def main(db):
    p = subprocess.Popen([sys.executable, str(ROOT / "mcp_server.py"), "--db", db, "--port", str(PORT)],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(40):
            try:
                init = rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                          "clientInfo": {"name": "t", "version": "0"}})
                break
            except Exception:
                if p.poll() is not None:
                    raise SystemExit("сервер завершився: " + p.stdout.read().decode()[-800:])
                time.sleep(0.5)
        else:
            raise SystemExit("сервер не піднявся")
        assert "result" in init, init
        names = {t["name"] for t in rpc("tools/list", id_=2)["result"]["tools"]}
        assert names == {"search_decisions", "get_decision", "list_courts", "stats",
                         "search_by_case_number", "court_category_stats"}, names

        def call(n, a, expect_error=False):
            r = rpc("tools/call", {"name": n, "arguments": a}, id_=3)
            assert "result" in r, (n, a, r)
            err = bool(r["result"].get("isError"))
            assert err == expect_error, (n, a, r)
            sc = r["result"].get("structuredContent") or {}
            if not sc and not err:
                sc = json.loads(r["result"]["content"][0]["text"])
            return sc

        for n, a in (("stats", {}), ("list_courts", {"name": "Київ", "limit": 3}),
                     ("search_decisions", {"query": "борг", "limit": 2})):
            call(n, a)
            print("OK", n)
        # --- search_by_case_number ---
        r = call("search_by_case_number", {"case_number": "447/2493/17", "limit": 3})
        assert r["count"] == 3 and r["truncated"] and r["decisions"][0]["cause_num"] == "447/2493/17", r
        r2 = call("search_by_case_number", {"case_number": ' «447 \\ 2493 \\ 17» ', "limit": 3})
        assert r2["decisions"] == r["decisions"], "нормалізація"
        r = call("search_by_case_number", {"case_number": "947/", "prefix": True, "limit": 5})
        assert r["count"] == 5 and all(d["cause_num"].startswith("947/") for d in r["decisions"]), r
        r = call("search_by_case_number", {"case_number": "000/000/00"})
        assert r["count"] == 0 and r["decisions"] == [] and not r["truncated"], r  # порожній результат
        call("search_by_case_number", {"case_number": "   "}, expect_error=True)      # неправильний вхід
        call("search_by_case_number", {"case_number": "1", "prefix": True}, expect_error=True)
        call("search_by_case_number", {"case_number": "1/1/1", "limit": 201}, expect_error=True)
        print("OK search_by_case_number (точний, нормалізація, префікс, порожній, помилки)")
        # --- court_category_stats ---
        r = call("court_category_stats", {"court": "Київ", "group_by": "category", "limit": 5})
        ns = [g["count"] for g in r["groups"]]
        assert r["count"] == 5 and ns == sorted(ns, reverse=True) and r["total_matched"] > 0, r
        r = call("court_category_stats", {})
        assert r["group_by"] == "court+category" and r["total_matched"] >= 1000, r
        r = call("court_category_stats", {"category": "кредиту", "date_from": "2026-01-01", "date_to": "2026-12-31"})
        assert r["total_matched"] > 0, r
        r = call("court_category_stats", {"court": "такого суду не існує"})
        assert r["count"] == 0 and r["total_matched"] == 0, r                           # порожній результат
        call("court_category_stats", {"group_by": "judge"}, expect_error=True)          # неправильний вхід
        call("court_category_stats", {"date_from": "2026/10/01"}, expect_error=True)
        call("court_category_stats", {"limit": 0}, expect_error=True)
        print("OK court_category_stats (групи, сортування, фільтри, порожній, помилки)")
        print("ВСІ ПЕРЕВІРКИ ПРОЙДЕНО")
    finally:
        p.terminate()
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()


if __name__ == "__main__":
    main(sys.argv[1])
