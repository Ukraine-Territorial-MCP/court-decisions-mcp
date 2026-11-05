#!/usr/bin/env python3
"""Щоденне оновлення: завантажує свіжий ZIP поточного року з data.gov.ua
і додає ЛИШЕ нові рішення (INSERT OR IGNORE за doc_id). Лише stdlib.

Це скрипт; розкладу він не встановлює (таймер — див. DEPLOY_PLAN.md).

Приклади:
  python3 update_daily.py --db data/court.db
  python3 update_daily.py --db data/court.db --zip /шлях/edrsr_data_2026.zip   # без мережі
  python3 update_daily.py --db data/court.db --dry-run
"""

import argparse
import json
import shutil
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from datetime import date
from pathlib import Path

import court_db

API = "https://data.gov.ua/api/3/action/package_search"
MIN_FREE_GB = 20
HTTP_TIMEOUT = 60          # секунд на одне з'єднання/читання
MAX_ZIP_BYTES = 3 * 1024**3  # запобіжник: один річний ZIP значно менший
UA = {"User-Agent": "court-decisions-mcp-updater/0.1"}


def http_json(url, params):
    full = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, headers=UA)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.load(r)


def find_zip(year):
    """Повертає (url, size, last_modified) ZIP-ресурсу набору за рік."""
    data = http_json(API, {"q": f"Єдиний державний реєстр судових рішень за {year} рік", "rows": 30})
    for pkg in data["result"]["results"]:
        if f"за {year} рік" not in pkg["title"]:
            continue
        if "Державна судова адміністрація" not in (pkg.get("organization") or {}).get("title", ""):
            continue
        for res in pkg["resources"]:
            if res["name"] == f"edrsr_data_{year}.zip":
                return res["url"], res.get("size"), res.get("last_modified")
    raise RuntimeError(f"Набір ЄДРСР за {year} рік не знайдено в CKAN")


def free_gb(path):
    return shutil.disk_usage(path).free / 1024**3


def download(url, dest, expected=None):
    req = urllib.request.Request(url, headers=UA)
    got = 0
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r, open(dest, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            got += len(chunk)
            if got > MAX_ZIP_BYTES:
                raise RuntimeError("ZIP більший за запобіжник")
            f.write(chunk)
    if expected and got != int(expected):
        raise RuntimeError(f"Розмір {got} != очікуваний {expected}")
    return got


def update_year(con, year, workdir, zip_path=None, dry_run=False, log=print):
    downloaded = False
    court_db.ensure_schema(con)  # cause_norm + індекси зберігаються після оновлень (ідемпотентно)
    if zip_path is None:
        url, size, modified = find_zip(year)
        log(f"[{year}] {url} size={size} modified={modified}")
        if con.execute("SELECT value FROM meta WHERE key=?", (f"zip_modified_{year}",)).fetchone() == (modified,):
            log(f"[{year}] ZIP не змінився з минулого оновлення — пропуск")
            return 0
        need = (int(size or 0) * 6) / 1024**3 + MIN_FREE_GB  # ZIP + розпакований CSV (~5x)
        if free_gb(workdir) < need:
            raise RuntimeError(f"Недостатньо місця: вільно {free_gb(workdir):.1f} ГБ, треба >= {need:.1f}")
        if dry_run:
            log(f"[{year}] dry-run: завантаження пропущено")
            return 0
        zip_path = Path(workdir) / f"edrsr_data_{year}.zip"
        download(url, zip_path, size)
        downloaded = True
    else:
        modified = None
        zip_path = Path(zip_path)

    ex = Path(workdir) / f"ex{year}"
    ex.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(ex)

    court_db.load_refs(con, ex)
    row = con.execute("SELECT MAX(date_publ) FROM decisions").fetchone()
    since_publ = None
    if row and row[0]:
        d = date.fromisoformat(row[0])
        since_publ = date.fromordinal(d.toordinal() - 3).isoformat()  # запас 3 дні
    seen, added = court_db.import_documents(con, ex, since_publ=since_publ)
    log(f"[{year}] прочитано {seen}, нових додано {added} (since_publ={since_publ})")
    if modified:
        court_db.set_meta(con, f"zip_modified_{year}", modified)
    court_db.set_meta(con, "last_update", date.today().isoformat())
    court_db.refresh_stats(con)
    con.execute("PRAGMA optimize")  # оновлює статистику планувальника для нових індексів
    shutil.rmtree(ex, ignore_errors=True)
    if downloaded:
        zip_path.unlink(missing_ok=True)
    return added


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True)
    ap.add_argument("--year", type=int, default=date.today().year)
    ap.add_argument("--zip", help="локальний ZIP замість завантаження (для тестів)")
    ap.add_argument("--workdir", help="тимчасова тека (за замовчуванням — нова у data/tmp)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    db = Path(a.db)
    db.parent.mkdir(parents=True, exist_ok=True)
    own_tmp = a.workdir is None
    workdir = Path(a.workdir) if a.workdir else Path(tempfile.mkdtemp(prefix="upd_", dir=db.parent))
    workdir.mkdir(parents=True, exist_ok=True)
    con = court_db.connect(db)
    try:
        years = [a.year]
        today = date.today()
        if a.zip is None and today.month == 1 and today.day <= 14 and a.year == today.year:
            years.append(a.year - 1)  # на межі років докачуємо хвіст минулого
        total = sum(update_year(con, y, workdir, a.zip if y == a.year else None, a.dry_run) for y in years)
        print(f"Усього нових: {total}")
    finally:
        con.close()
        if own_tmp:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
