#!/usr/bin/env python3
"""Логіка інструментів MCP (лише читання SQLite). Без залежності від MCP SDK.

Власний код поза фабрикою: factory_sql.sql_lookup вміє лише точний пошук
одного рядка, а safe_path фабрики обмежує файл 5 МБ — тому пошук/FTS/список/
статистика реалізовані тут, а фабричний MCPServer (factory_mcp_runtime)
лише реєструє ці функції.
"""

import functools
import json
import os
import re
import sqlite3
import threading
import unicodedata
from datetime import date
from pathlib import Path

from court_db import norm_cause

MAX_LIMIT = 50
MAX_LIMIT_NEW = 200      # search_by_case_number / court_category_stats
DEFAULT_LIMIT_NEW = 50
STATS_DEADLINE = 5.0     # с; запобіжник для court_category_stats
MAX_CAUSE_LEN = 120
MIN_PREFIX_LEN = 2

# --- межі (знахідка C-M1) і перевірка входу (C-L1) ---
MAX_ARG_LEN = 200            # максимум символів у будь-якому рядковому аргументі
MAX_FILTER_LEN = 100         # court / justice_kind / category / name / region
MAX_QUERY_WORDS = 8          # слів у FTS-запиті (більше — відмова, а не мовчазне обрізання)
MAX_WORD_LEN = 64            # символів у одному слові FTS
MAX_RESPONSE_BYTES = 256 * 1024   # серіалізована відповідь (JSON, UTF-8)
MAX_FIELD_CHARS = 2000       # довжина будь-якого текстового поля у відповіді
MAX_CONCURRENT = 4           # одночасних запитів до бази в одному процесі
DOC_ID_MAX = 2**63 - 1       # межа SQLite INTEGER
YEAR_MIN, YEAR_MAX = 1990, 2100
DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z", re.ASCII)
# Рішення власника: прізвища суддів публічні в джерелі, тому за замовчуванням віддаються ВСІМА інструментами
# однаково (поле `judge`). COURT_EXPOSE_JUDGE=0 — не віддавати ніде і не шукати за суддею.
FORBIDDEN_KEY_PARTS = ("judge", "суддя", "судді")


def expose_judge():
    """Єдине правило для всіх інструментів. Читається при кожному виклику; за замовчуванням 1."""
    v = os.environ.get("COURT_EXPOSE_JUDGE", "1").strip().lower()
    if v in ("1", "true", "yes", "on", ""):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise RuntimeError("COURT_EXPOSE_JUDGE: очікується 1 або 0")
# Дозволені ключі meta у stats() (allowlist; майбутні ключі не потрапляють у відповідь).
PUBLIC_META_KEYS = ("last_update", "last_import_rows")


class ToolError(ValueError):
    """Єдиний формат помилок інструментів: «[код] повідомлення». Підклас ValueError."""

    def __init__(self, code, message):
        super().__init__(f"[{code}] {message}")
        self.code = code


_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT)


def _bad(message, code="invalid_input"):
    return ToolError(code, message)


def _text(value, name, maxlen=MAX_ARG_LEN):
    """Рядковий аргумент: None/'' -> None; інакше str без керівних символів і не довший за maxlen."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise _bad(f"{name}: має бути рядком", "invalid_type")
    if len(value) > maxlen:
        raise _bad(f"{name}: задовгий (>{maxlen} символів)", "too_long")
    for ch in value:
        if unicodedata.category(ch) in ("Cc", "Cs") or ch in "\u2028\u2029":
            raise _bad(f"{name}: недопустимі керівні символи (зокрема нульовий байт)", "invalid_chars")
    return value


def _ascii_digits(v):
    return v.isascii() and v.isdigit()


def _like(v):
    """Екранує %, _ та \\ для LIKE ... ESCAPE '\\'."""
    return v.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _scrub(obj):
    """Страховка: прибирає ключі суддів з будь-якої вкладеності (не покладаємось лише на SELECT)."""
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items()
                if not any(p in str(k).lower() for p in FORBIDDEN_KEY_PARTS)}
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    return obj


def _clip(obj):
    if isinstance(obj, str):
        return obj if len(obj) <= MAX_FIELD_CHARS else obj[:MAX_FIELD_CHARS] + "…"
    if isinstance(obj, dict):
        return {k: _clip(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clip(v) for v in obj]
    return obj


def _size(obj):
    return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _finalize(result):
    """Прибрати поля суддів, обрізати довгі поля, вкластись у MAX_RESPONSE_BYTES (з позначкою)."""
    result = _clip(result if expose_judge() else _scrub(result))
    if _size(result) <= MAX_RESPONSE_BYTES:
        return result
    for key in ("decisions", "courts", "groups"):
        rows = result.get(key)
        if isinstance(rows, list) and rows:
            lo, hi = 0, len(rows)          # найбільше k, для якого відповідь влазить
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if _size({**result, key: rows[:mid]}) <= MAX_RESPONSE_BYTES:
                    lo = mid
                else:
                    hi = mid - 1
            result = {**result, key: rows[:lo], "count": lo, "truncated": True,
                      "response_truncated": True,
                      "response_note": f"Відповідь обрізано до {MAX_RESPONSE_BYTES // 1024} КБ; уточніть запит."}
            break
    if _size(result) > MAX_RESPONSE_BYTES:
        raise _bad("Відповідь завелика навіть після обрізання; уточніть запит", "too_large")
    return result


def guarded(fn):
    """Обгортка публічного інструмента: ліміт одночасних запитів, єдині помилки, фільтр виходу."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not _SLOTS.acquire(blocking=False):
            raise ToolError("busy", f"Зайнято (одночасно не більше {MAX_CONCURRENT} запитів), спробуйте пізніше")
        try:
            return _finalize(fn(*args, **kwargs))
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "interrupted" in msg:
                raise ToolError("too_heavy", "Запит надто важкий (перевищено час): уточніть фільтри") from None
            if "locked" in msg or "busy" in msg:
                raise ToolError("busy", "База тимчасово зайнята, спробуйте пізніше") from None
            raise ToolError("db_error", "Помилка бази даних") from None
        finally:
            _SLOTS.release()
    return wrapper


def open_ro(db_path, deadline=8.0):
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=3)
    con.execute("PRAGMA query_only=ON")
    con.row_factory = sqlite3.Row
    # запобіжник від надто довгих запитів
    if deadline:
        con.set_progress_handler(_Deadline(deadline), 100000)
    return con


class _Deadline:
    def __init__(self, seconds):
        import time
        self.t = time.monotonic
        self.end = self.t() + seconds

    def __call__(self):
        return 1 if self.t() > self.end else 0


def _fts_query(text):
    """Безпечний FTS5-запит: слова -> "слово"* AND ...; без операторів користувача.
    COURT_EXPOSE_JUDGE=1: шукає в усіх колонках індексу (включно з суддею, як у джерелі);
    COURT_EXPOSE_JUDGE=0: лише cause_num, court, category — за суддею не фільтруємо."""
    words = re.findall(r"[\w'’]+", text, flags=re.UNICODE)
    if not words:
        raise _bad("Порожній пошуковий запит")
    if len(words) > MAX_QUERY_WORDS:
        raise _bad(f"Забагато слів у запиті (>{MAX_QUERY_WORDS})", "too_many_words")
    if any(len(w) > MAX_WORD_LEN for w in words):
        raise _bad(f"Задовге слово в запиті (>{MAX_WORD_LEN} символів)", "too_long")
    body = " AND ".join('"%s"*' % w.replace('"', "") for w in words)
    return body if expose_judge() else "{cause_num court category} : (" + body + ")"


def _date(value, name):
    value = _text(value, name, 10)
    if value is None:
        return None
    if not DATE.fullmatch(value):
        raise _bad(f"{name}: очікується YYYY-MM-DD (латинські цифри)", "invalid_date")
    try:
        d = date.fromisoformat(value)
    except ValueError:
        raise _bad(f"{name}: неіснуюча календарна дата", "invalid_date") from None
    if not YEAR_MIN <= d.year <= YEAR_MAX:
        raise _bad(f"{name}: рік поза межами {YEAR_MIN}..{YEAR_MAX}", "invalid_date")
    return value


def _limit(limit, maximum=MAX_LIMIT):
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= maximum:
        raise _bad(f"limit має бути цілим числом 1..{maximum}")
    return limit


def _doc_id(doc_id):
    if isinstance(doc_id, bool) or not isinstance(doc_id, (int, str)):
        raise _bad("doc_id має бути числом", "invalid_type")
    if isinstance(doc_id, str):
        doc_id = _text(doc_id.strip(), "doc_id", 19)
        if doc_id is None or not _ascii_digits(doc_id):
            raise _bad("doc_id має складатися лише з цифр 0-9 (до 19 знаків)")
        doc_id = int(doc_id)
    if not 1 <= doc_id <= DOC_ID_MAX:
        raise _bad(f"doc_id поза межами 1..{DOC_ID_MAX}")
    return doc_id


_SELECT = """
SELECT d.doc_id, d.cause_num, d.adjudication_date, d.receipt_date, d.date_publ,
       c.name AS court, d.court_code, jk.name AS justice_kind, jf.name AS judgment_form,
       cat.name AS category, d.doc_url, d.status
FROM decisions d
LEFT JOIN courts c ON c.court_code = d.court_code
LEFT JOIN justice_kinds jk ON jk.justice_kind = d.justice_kind
LEFT JOIN judgment_forms jf ON jf.judgment_code = d.judgment_code
LEFT JOIN categories cat ON cat.category_code = d.category_code
"""


def _select():
    """SELECT для search_decisions / get_decision; поле judge — за правилом expose_judge()."""
    return _SELECT.replace("cat.name AS category, d.doc_url", "cat.name AS category, "
                           + ("d.judge, " if expose_judge() else "") + "d.doc_url")


@guarded
def search_decisions(db, query=None, court=None, justice_kind=None,
                     date_from=None, date_to=None, limit=20):
    """Пошук метаданих рішень. query — слова (FTS5: номер справи, суд, категорія);
    court — частина назви суду або код; justice_kind — назва або код виду провадження."""
    limit = _limit(limit)
    date_from, date_to = _date(date_from, "date_from"), _date(date_to, "date_to")
    query = _text(query, "query")
    court = _text(court, "court", MAX_FILTER_LEN)
    justice_kind = _text(justice_kind, "justice_kind", MAX_FILTER_LEN)
    where, args, joins = [], [], ""
    if query and query.strip():
        joins = " JOIN decisions_fts f ON f.rowid = d.doc_id"
        where.append("decisions_fts MATCH ?")
        args.append(_fts_query(query))
    if court:
        if _ascii_digits(court):
            where.append("d.court_code = ?")
            args.append(court.zfill(4))
        else:
            where.append("c.name LIKE ? ESCAPE '\\'")
            args.append(f"%{_like(court)}%")
    if justice_kind:
        if _ascii_digits(justice_kind):
            where.append("d.justice_kind = ?")
            args.append(justice_kind)
        else:
            where.append("jk.name LIKE ? ESCAPE '\\'")
            args.append(f"{_like(justice_kind)}%")
    if date_from:
        where.append("d.adjudication_date >= ?")
        args.append(date_from)
    if date_to:
        where.append("d.adjudication_date <= ?")
        args.append(date_to)
    if not where:
        raise _bad("Вкажіть query або хоча б один фільтр")
    sql = _select().replace("FROM decisions d", "FROM decisions d" + joins, 1)
    sql += " WHERE " + " AND ".join(where) + " ORDER BY d.adjudication_date DESC, d.doc_id DESC LIMIT ?"
    con = open_ro(db)
    try:
        rows = [dict(r) for r in con.execute(sql, args + [limit])]
    finally:
        con.close()
    return {"count": len(rows), "limit": limit, "decisions": rows}


@guarded
def get_decision(db, doc_id):
    doc_id = _doc_id(doc_id)
    con = open_ro(db)
    try:
        row = con.execute(_select() + " WHERE d.doc_id = ?", (doc_id,)).fetchone()
    finally:
        con.close()
    if row is None:
        return {"found": False, "doc_id": doc_id}
    out = dict(row)
    out["found"] = True
    out["note"] = "Архів відкритих даних містить лише метадані; текст — за doc_url (якщо є)."
    return out


@guarded
def list_courts(db, name=None, region=None, limit=50):
    limit = _limit(limit)
    name = _text(name, "name", MAX_FILTER_LEN)
    region = _text(region, "region", MAX_FILTER_LEN)
    where, args = [], []
    if name:
        where.append("c.name LIKE ? ESCAPE '\\'")
        args.append(f"%{_like(name)}%")
    if region:
        where.append("r.name LIKE ? ESCAPE '\\'")
        args.append(f"%{_like(region)}%")
    sql = ("SELECT c.court_code, c.name, i.name AS instance, r.name AS region "
           "FROM courts c LEFT JOIN instances i ON i.instance_code=c.instance_code "
           "LEFT JOIN regions r ON r.region_code=c.region_code")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY c.name LIMIT ?"
    con = open_ro(db)
    try:
        rows = [dict(r) for r in con.execute(sql, args + [limit])]
    finally:
        con.close()
    return {"count": len(rows), "courts": rows}


def _public_stats(out):
    """allowlist для meta: у відповідь потрапляють лише PUBLIC_META_KEYS (також зі старого кешу)."""
    out = dict(out)
    out["meta"] = {k: v for k, v in (out.get("meta") or {}).items() if k in PUBLIC_META_KEYS}
    return out


@guarded
def stats(db, use_cache=True):
    # повний підрахунок (refresh_stats) виконується без 8-секундного дедлайну; запити клієнтів — з ним
    con = open_ro(db, deadline=8.0 if use_cache else None)
    try:
        if use_cache:
            cached = con.execute("SELECT value FROM meta WHERE key='stats_cache'").fetchone()
            if cached:
                return _public_stats(json.loads(cached[0]))
        total, dmin, dmax, pmax = con.execute(
            "SELECT COUNT(*), MIN(adjudication_date), MAX(adjudication_date), MAX(date_publ) FROM decisions").fetchone()
        by_kind = [dict(r) for r in con.execute(
            "SELECT jk.name AS justice_kind, COUNT(*) AS n FROM decisions d "
            "LEFT JOIN justice_kinds jk ON jk.justice_kind=d.justice_kind "
            "GROUP BY d.justice_kind ORDER BY n DESC")]
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
        courts = con.execute("SELECT COUNT(*) FROM courts").fetchone()[0]
    finally:
        con.close()
    return _public_stats({"decisions": total, "adjudication_date_min": dmin, "adjudication_date_max": dmax,
            "date_publ_max": pmax, "courts": courts, "by_justice_kind": by_kind,
            "meta": meta, "source": "data.gov.ua, ЄДРСР (ДСА України), CC-BY; лише метадані"})


# ---------------------------------------------------------------------------
# Нові інструменти (6 інструментів): search_by_case_number, court_category_stats
# ---------------------------------------------------------------------------

REGISTRY_NOTE = "Архів відкритих даних містить лише метадані; текст — за doc_url (якщо є)."
_SELECT_CASE = """
SELECT d.doc_id, d.cause_num, d.adjudication_date, d.receipt_date, d.date_publ,
       d.court_code, c.name AS court, jk.name AS justice_kind, jf.name AS judgment_form,
       d.category_code, cat.name AS category, d.doc_url
FROM decisions d
LEFT JOIN courts c ON c.court_code = d.court_code
LEFT JOIN justice_kinds jk ON jk.justice_kind = d.justice_kind
LEFT JOIN judgment_forms jf ON jf.judgment_code = d.judgment_code
LEFT JOIN categories cat ON cat.category_code = d.category_code
"""


def _select_case():
    return _SELECT_CASE.replace("cat.name AS category, d.doc_url", "cat.name AS category, "
                                + ("d.judge, " if expose_judge() else "") + "d.doc_url")


@guarded
def search_by_case_number(db, case_number, prefix=False, limit=DEFAULT_LIMIT_NEW):
    """Рішення за номером справи. Номер нормалізується (пробіли, регістр, '\\' '|' -> '/',
    лапки, «№», тире, латинські двійники літер). prefix=True — пошук за початком номера
    (не менше 2 символів після нормалізації). Використовує індекс ix_dec_cause_norm."""
    limit = _limit(limit, MAX_LIMIT_NEW)
    if not isinstance(case_number, str):
        raise _bad("case_number має бути рядком", "invalid_type")
    case_number = _text(case_number, "case_number", MAX_CAUSE_LEN) or ""
    if not isinstance(prefix, bool):
        raise _bad("prefix має бути true/false", "invalid_type")
    norm = norm_cause(case_number)
    if not norm:
        raise _bad("Порожній номер справи")
    if prefix:
        if len(norm) < MIN_PREFIX_LEN:
            raise _bad(f"Початок номера має містити щонайменше {MIN_PREFIX_LEN} символи")
        # діапазон за двійковим порівнянням: [norm, norm + U+10FFFF)
        cond, args = "d.cause_norm >= ? AND d.cause_norm < ?", [norm, norm + "\U0010ffff"]
    else:
        cond, args = "d.cause_norm = ?", [norm]
    sql = (_select_case() + " WHERE " + cond +
           " ORDER BY d.cause_norm, d.adjudication_date DESC, d.doc_id DESC LIMIT ?")
    con = open_ro(db)
    try:
        rows = [dict(r) for r in con.execute(sql, args + [limit + 1])]
    finally:
        con.close()
    truncated = len(rows) > limit
    rows = rows[:limit]
    return {"query": case_number, "normalized": norm, "mode": "prefix" if prefix else "exact",
            "count": len(rows), "limit": limit, "truncated": truncated,
            "decisions": rows, "note": REGISTRY_NOTE}


_GROUPS = {"court": ("court",), "category": ("category",), "both": ("court", "category"),
           "court,category": ("court", "category")}


def _match(con, table, code_col, value, name):
    """('code', код) для цифр або ('name', casefold-частина назви)."""
    v = (_text(value, name, MAX_FILTER_LEN) or "").strip()
    if not v:
        return None
    return ("code", v.zfill(4) if (name == "court" and _ascii_digits(v)) else v) if _ascii_digits(v) \
        else ("name", v.casefold())


@guarded
def court_category_stats(db, court=None, category=None, date_from=None, date_to=None,
                         group_by="both", limit=DEFAULT_LIMIT_NEW):
    """Кількість рішень за судом і/або категорією, за спаданням кількості.
    court / category — код (цифри) або частина назви (без урахування регістру);
    date_from / date_to — YYYY-MM-DD, за датою ухвалення; group_by — court | category | both."""
    limit = _limit(limit, MAX_LIMIT_NEW)
    date_from, date_to = _date(date_from, "date_from"), _date(date_to, "date_to")
    if date_from and date_to and date_from > date_to:
        raise _bad("date_from пізніше за date_to")
    if group_by in (None, ""):
        group_by = "both"
    group_by = _text(group_by, "group_by", 30)
    if group_by is None or group_by.strip().lower() not in _GROUPS:
        raise _bad("group_by: court, category або both")
    dims = _GROUPS[group_by.strip().lower()]
    con = open_ro(db, deadline=STATS_DEADLINE)
    con.create_function("fold", 1, lambda s: s.casefold() if isinstance(s, str) else "",
                        deterministic=True)
    try:
        where, args = [], []
        for name, val, table, col in (("court", court, "courts", "court_code"),
                                      ("category", category, "categories", "category_code")):
            m = _match(con, table, col, val, name) if val not in (None, "") else None
            if m is None:
                continue
            if m[0] == "code":
                where.append(f"d.{col} = ?")
            else:
                where.append(f"d.{col} IN (SELECT {col} FROM {table} WHERE instr(fold(name), ?) > 0)")
            args.append(m[1])
        if date_from:
            where.append("d.adjudication_date >= ?")
            args.append(date_from)
        if date_to:
            where.append("d.adjudication_date <= ?")
            args.append(date_to)
        w = (" WHERE " + " AND ".join(where)) if where else ""
        cols = ", ".join(f"d.{k}_code" for k in dims)
        sql = (f"SELECT {cols}, COUNT(*) AS n FROM decisions d{w} GROUP BY {cols} "
               f"ORDER BY n DESC, {cols} LIMIT ?")
        try:
            groups = [tuple(r) for r in con.execute(sql, args + [limit + 1])]
            total = con.execute(f"SELECT COUNT(*) FROM decisions d{w}", args).fetchone()[0]
        except sqlite3.OperationalError as e:
            if "interrupted" in str(e):
                raise ToolError("too_heavy", "Запит надто важкий (>%g с): додайте фільтри court/category/date_from/date_to або group_by з одним виміром" % STATS_DEADLINE) from None
            raise
        truncated = len(groups) > limit
        groups = groups[:limit]
        cname = dict(con.execute("SELECT court_code, name FROM courts")) if "court" in dims else {}
        kname = dict(con.execute("SELECT category_code, name FROM categories")) if "category" in dims else {}
    finally:
        con.close()
    out = []
    for g in groups:
        item, i = {}, 0
        for k in dims:
            code = g[i]
            i += 1
            item[f"{k}_code"] = code
            item[k] = (cname if k == "court" else kname).get(code)
        item["count"] = g[i]
        out.append(item)
    return {"group_by": "+".join(dims), "filters": {"court": court or None, "category": category or None,
            "date_from": date_from, "date_to": date_to},
            "total_matched": total, "count": len(out), "limit": limit, "truncated": truncated,
            "groups": out, "note": "Лише рішення, що є в локальній базі (вікно дат ухвалення); категорія може бути порожньою."}
