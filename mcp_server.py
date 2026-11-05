#!/usr/bin/env python3
"""MCP-сервер судових рішень (метадані ЄДРСР). Лише читання, лише 127.0.0.1.

Реєстрація за шаблоном factory/factory_mcp_runtime.py: MCPServer(...) +
mcp.tool(name=..., description=...)(fn). Логіка — у court_tools.py (власний код,
бо фабрика не вміє search/list/stats; див. REPORT.md).
Потребує пакета `mcp` >= 2 (from mcp.server.mcpserver import MCPServer).
СТАТУС: запускався з mcp==2.3.0 (venv-host), 6 інструментів, див. REPORT.md.
"""
import argparse
import os
import sys
from pathlib import Path

import court_tools as ct

HOST, PORT = "127.0.0.1", 18940


def build_mcp(db):
    from typing import Union
    from mcp.server.mcpserver import MCPServer  # імпорт тут: court_tools тестується без SDK
    import functools
    from mcp.server.mcpserver.exceptions import ToolError as SdkToolError
    from pydantic import StrictBool, StrictInt, StrictStr  # строгі типи: true не стає 1, число не стає рядком
    mcp = MCPServer("court-decisions-ua", title="Судові рішення України (метадані ЄДРСР)",
                    description="Пошук метаданих судових рішень з відкритих даних ДСА/ЄДРСР. Лише читання; тексту рішень в архіві немає.")

    def search_decisions(query: StrictStr = "", court: StrictStr = "", justice_kind: StrictStr = "",
                         date_from: StrictStr = "", date_to: StrictStr = "", limit: StrictInt = 20):
        return ct.search_decisions(db, query, court, justice_kind, date_from, date_to, limit)

    def get_decision(doc_id: Union[StrictStr, StrictInt]):
        return ct.get_decision(db, doc_id)

    def list_courts(name: StrictStr = "", region: StrictStr = "", limit: StrictInt = 50):
        return ct.list_courts(db, name, region, limit)

    def stats():
        return ct.stats(db)

    def search_by_case_number(case_number: StrictStr, prefix: StrictBool = False, limit: StrictInt = 50):
        return ct.search_by_case_number(db, case_number, prefix, limit)

    def court_category_stats(court: StrictStr = "", category: StrictStr = "", date_from: StrictStr = "",
                             date_to: StrictStr = "", group_by: StrictStr = "both", limit: StrictInt = 50):
        return ct.court_category_stats(db, court, category, date_from, date_to, group_by, limit)

    def public(fn):
        """Очікувані помилки (court_tools.ToolError) показуємо клієнтові як «[код] текст»;
        усе інше SDK маскує загальним повідомленням (внутрішніх деталей не віддаємо)."""
        @functools.wraps(fn)
        def wrapper(*a, **k):
            try:
                return fn(*a, **k)
            except ct.ToolError as e:
                raise SdkToolError(str(e)) from None
        return wrapper

    for fn, desc in (
        (search_decisions, "Пошук рішень: query (слова: номер справи, суд, категорія" + (", суддя" if ct.expose_judge() else "") + "; до 8 слів, до 200 символів), court (код або частина назви), justice_kind (код або назва виду провадження), date_from/date_to (YYYY-MM-DD, дата ухвалення), limit (1..50)."),
        (get_decision, "Метадані одного рішення за doc_id (ціле число, лише цифри 0-9)." + (" Поле judge — як у джерелі." if ct.expose_judge() else " Без відомостей про суддів.") + ""),
        (list_courts, "Довідник судів: name, region (частини назв), limit."),
        (search_by_case_number, "Усі рішення по справі за номером (нормалізація: пробіли, регістр, роздільники \\ | /, лапки, №). prefix=true — пошук за початком номера (мін. 2 символи). Повертає метадані, суд, дату, вид, категорію, посилання на реєстр. limit 1..200 (за замовчуванням 50)."),
        (court_category_stats, "Кількість рішень за судом і/або категорією (за спаданням). Усі параметри необов'язкові: court і category — код або частина назви; date_from/date_to (YYYY-MM-DD, дата ухвалення); group_by = court | category | both; limit 1..200 (за замовчуванням 50). Надто важкий запит (>5 с) переривається з помилкою."),
        (stats, "Загальна статистика бази: кількість, діапазон дат, розподіл за видами проваджень."),
    ):
        mcp.tool(name=fn.__name__, description=desc)(public(fn))
    return mcp


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--port", type=int, default=PORT)
    a = ap.parse_args(argv)
    if not Path(a.db).is_file():
        sys.exit(f"Немає бази: {a.db}")
    mcp = build_mcp(a.db)
    kw = {}
    # За reverse proxy заголовок Host = публічне ім'я (не 127.0.0.1), і SDK 2.x відповів би 421.
    # COURT_ALLOWED_HOSTS="mcp.edeputat.com" додає це ім'я до дозволених (захист від DNS-rebinding лишається).
    extra = [h.strip() for h in os.environ.get("COURT_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if extra:
        from mcp.server.transport_security import TransportSecuritySettings
        hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"] + extra + [h + ":*" for h in extra]
        kw["transport_security"] = TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=hosts,
            allowed_origins=["http://127.0.0.1:*", "http://localhost:*"] + [f"https://{h}" for h in extra])
    mcp.run("streamable-http", host=HOST, port=a.port, stateless_http=True, json_response=True, **kw)


if __name__ == "__main__":
    main()
