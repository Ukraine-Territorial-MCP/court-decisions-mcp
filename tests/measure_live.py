#!/usr/bin/env python3
"""Заміри на реальній базі: initialize, tools/list, по виклику кожного інструмента; час і RSS. Сервер стартує/зупиняється тут (try/finally).
Запуск: python tests/measure_live.py data/sample.db"""
import json, subprocess, sys, time, sqlite3, urllib.request
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
PORT = 18940
URL = f"http://127.0.0.1:{PORT}/mcp"
HDR = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

def rss(pid):
    for l in open(f"/proc/{pid}/status"):
        if l.startswith("VmRSS") or l.startswith("VmHWM"):
            yield l.split(":")[0], int(l.split()[1]) // 1024

def rpc(m, p=None, i=1):
    b = json.dumps({"jsonrpc": "2.0", "id": i, "method": m, "params": p or {}}).encode()
    t = time.perf_counter()
    with urllib.request.urlopen(urllib.request.Request(URL, b, HDR), timeout=30) as r:
        d = json.loads(r.read())
    return d, (time.perf_counter() - t) * 1000

def main(db):
    p = subprocess.Popen([sys.executable, str(ROOT / "mcp_server.py"), "--db", db, "--port", str(PORT)],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        t0 = time.perf_counter()
        for _ in range(60):
            try:
                init, ms = rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}); break
            except Exception:
                if p.poll() is not None: raise SystemExit("server died: " + p.stdout.read().decode()[-800:])
                time.sleep(0.25)
        print(f"startup->initialize OK: {(time.perf_counter()-t0)*1000:.0f} мс; serverInfo={init['result'].get('serverInfo')} protocol={init['result'].get('protocolVersion')}")
        print("RSS після старту:", dict(rss(p.pid)))
        tl, ms = rpc("tools/list", i=2)
        print(f"tools/list {ms:.0f} мс:", [t["name"] for t in tl["result"]["tools"]])
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        print("колонки decisions:", [r[1] for r in con.execute("pragma table_info(decisions)")] if con.execute("select 1 from sqlite_master where name='decisions'").fetchone() else [r[0] for r in con.execute("select name from sqlite_master")])
        con.close()
        calls = [("stats", {}), ("list_courts", {"name": "Київ", "limit": 3}),
                 ("search_decisions", {"query": "борг", "limit": 2}),
                 ("search_by_case_number", {"case_number": "447/2493/17", "limit": 50}),
                 ("search_by_case_number", {"case_number": "947/", "prefix": True, "limit": 200}),
                 ("search_by_case_number", {"case_number": "000/000/00"}),
                 ("court_category_stats", {}),
                 ("court_category_stats", {"court": "Київ", "group_by": "category", "limit": 50}),
                 ("court_category_stats", {"category": "Землекористування", "date_from": "2026-10-10", "group_by": "court"}),
                 ("search_decisions", {"query": "", "date_from": "2026-10-15", "date_to": "2026-10-16", "limit": 5})]
        first_id = None
        for n, a in calls:
            r, ms = rpc("tools/call", {"name": n, "arguments": a}, 3)
            txt = json.dumps(r["result"], ensure_ascii=False)
            print(f"{n} {json.dumps(a, ensure_ascii=False)}: {ms:.0f} мс, isError={r['result'].get('isError')}, {len(txt)} байт; {txt[:300]}")
            if n == "search_decisions" and first_id is None:
                sc = r["result"].get("structuredContent") or {}
                import re
                m = re.search(r'"doc_id"\s*:\s*"?(\d+)', txt) or re.search(r'doc_id\\?"\s*:\s*\\?"?(\d+)', txt)
                first_id = m.group(1) if m else None
        print("doc_id для get_decision:", first_id)
        for a in ({"doc_id": first_id}, {"doc_id": "0000000"}):
            r, ms = rpc("tools/call", {"name": "get_decision", "arguments": a}, 4)
            txt = json.dumps(r.get("result", r), ensure_ascii=False)
            print(f"get_decision {a}: {ms:.0f} мс, isError={r.get('result',{}).get('isError')}; {txt[:300]}")
        print("RSS після викликів:", dict(rss(p.pid)))
    finally:
        p.terminate()
        try: p.wait(5)
        except subprocess.TimeoutExpired: p.kill()
        print("процес зупинено, код", p.returncode)

if __name__ == "__main__":
    main(sys.argv[1])
