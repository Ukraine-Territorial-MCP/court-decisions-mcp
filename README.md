# court-decisions-mcp

🇺🇦 [Українською](#українською) · 🇬🇧 [English](#english)

---

## Українською

**Експериментальний MCP-сервер (лише читання) для пошуку метаданих судових рішень України.**
Дані — відкритий набір «Єдиний державний реєстр судових рішень» (ЄДРСР), який публікує
Державна судова адміністрація України на [data.gov.ua](https://data.gov.ua) (ліцензія набору — CC-BY).

> ⚠️ Це **незалежний** проєкт. Він не є офіційним сервісом ДСА, ЄДРСР чи будь-якого державного органу
> і не схвалений ними. Не є юридичною консультацією.

### Що в даних, а чого немає
- Є **лише метадані**: номер справи, суд, вид судочинства, категорія справи, форма рішення, дати, посилання `doc_url` на документ у реєстрі.
- **Тексту рішень немає.**
- Сервер на `mcp.edeputat.com` тримає **вікно за датою ухвалення** (зараз із 2026-10-01), а не весь архів. Рішень по справі за раніші дати там немає.
- Поле `judge` (прізвище судді) публічне в джерелі й **за замовчуванням віддається всіма інструментами однаково** (`COURT_EXPOSE_JUDGE=1`). `COURT_EXPOSE_JUDGE=0` ховає його скрізь і вимикає пошук за суддею. У `data/sample.db` цього репозиторію значення `judge` **прибрано** (порожні).

### Публічна точка
`https://mcp.edeputat.com/court-decisions/mcp` (Streamable HTTP, без автентифікації, з обмеженням частоти запитів).
ПЕРЕВІРИТИ: працює лише після розгортання на сервері; див. стан у релізі.

### Інструменти (6)
| Інструмент | Призначення |
|---|---|
| `search_decisions` | Пошук: `query` (слова з номера справи, суду, категорії, судді), `court`, `justice_kind`, `date_from`/`date_to` (YYYY-MM-DD), `limit` 1..50 |
| `get_decision` | Метадані одного рішення за `doc_id` |
| `list_courts` | Довідник судів: `name`, `region`, `limit` |
| `search_by_case_number` | Усі рішення по справі; нормалізує пробіли, регістр, роздільники `\ \| /`, лапки, «№», тире, латинські двійники літер; `prefix=true` — за початком номера; `limit` 1..200 |
| `court_category_stats` | Кількість рішень за судом і/або категорією; `group_by` = `court`/`category`/`both`; запит довший за 5 с переривається |
| `stats` | Загальна статистика бази |

### Приклади
```jsonc
// tools/call
{"name": "search_by_case_number", "arguments": {"case_number": "910/1234/26"}}
{"name": "search_decisions", "arguments": {"query": "кредит", "date_from": "2026-10-10", "limit": 5}}
{"name": "court_category_stats", "arguments": {"court": "Київ", "group_by": "category", "limit": 10}}
```

### Запуск локально
```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt   # mcp==2.3.0
venv/bin/python mcp_server.py --db data/sample.db                  # 127.0.0.1:18940, шлях /mcp
python3 -I tests/test_core.py && python3 -I tests/test_new_tools.py # тести
venv/bin/python tests/test_mcp_live.py data/sample.db               # живий тест SDK
```
Код сервера залежить лише від стандартної бібліотеки Python, окрім самого пакета `mcp`.

`data/sample.db` — мала вибірка (≈3 200 рядків) без імен суддів; будується `scripts/build_sample.py`.
Повну базу будують `court_db.py` (з CSV-архіву) та `update_daily.py` (щоденне поповнення без перезавантаження бази).

### Умови використання даних і подяка
- Дані: **Державна судова адміністрація України**, набір «Єдиний державний реєстр судових рішень», [data.gov.ua](https://data.gov.ua), ліцензія **CC-BY** (потрібне зазначення авторства — дякуємо ДСА за відкриті дані).
- Посилання `doc_url` ведуть на `od.reyestr.court.gov.ua`; ПЕРЕВІРИТИ окремі умови використання самих документів реєстру — цей проєкт їх не завантажує і не копіює.
- Судові дані можуть стосуватися фізичних осіб; користувач сам відповідає за законність подальшого використання (зокрема щодо персональних даних). Це не юридичний висновок.
- ПЕРЕВІРИТИ: статус полів `status`, `doc_url` (опис у `readme_2026.pdf` до набору не опрацьовано).

### Ліцензія коду
ПЕРЕВІРИТИ: ліцензію ще не обрано, див. `LICENSE`.

---

## English

**Experimental read-only MCP server for searching metadata of Ukrainian court decisions.**
Data: the open dataset "Unified State Register of Court Decisions" (EDRSR) published by the
State Judicial Administration of Ukraine on [data.gov.ua](https://data.gov.ua) (dataset licence: CC-BY).

> ⚠️ This is an **independent** project, not an official service of, nor endorsed by, the State Judicial Administration or any government body. Not legal advice.

### What the data contains
- **Metadata only**: case number, court, type of proceedings, case category, form of decision, dates, and a `doc_url` link to the register.
- **No decision texts.**
- The public server holds a **date window** (adjudication date from 2026-10-01 for now), not the full archive.
- The `judge` field (judge's surname) is public in the source and is **returned identically by all tools by default** (`COURT_EXPOSE_JUDGE=1`). `COURT_EXPOSE_JUDGE=0` hides it everywhere and disables searching by judge. In this repository's `data/sample.db` the values are **blanked**.

### Public endpoint
`https://mcp.edeputat.com/court-decisions/mcp` (Streamable HTTP, no authentication, rate-limited). TO VERIFY: live only after the server-side deployment.

### Tools (6)
`search_decisions`, `get_decision`, `list_courts`, `search_by_case_number`, `court_category_stats`, `stats` — see the table above for arguments.

### Run locally
```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt   # mcp==2.3.0
venv/bin/python mcp_server.py --db data/sample.db                  # 127.0.0.1:18940, path /mcp
python3 -I tests/test_core.py && python3 -I tests/test_new_tools.py
venv/bin/python tests/test_mcp_live.py data/sample.db
```

### Data terms and credit
- Data: **State Judicial Administration of Ukraine**, "Unified State Register of Court Decisions", [data.gov.ua](https://data.gov.ua), licence **CC-BY** — attribution required; we thank the SJA for publishing open data.
- `doc_url` links point to `od.reyestr.court.gov.ua`; TO VERIFY the separate terms of the register's documents — this project does not download or copy them.
- Court data may concern individuals; users are responsible for lawful downstream use (incl. personal-data rules). This is not a legal opinion.

### Code licence
TO VERIFY: no licence chosen yet — see `LICENSE`.

### Limits / Межі
Argument ≤ 200 chars, FTS ≤ 8 words, response ≤ 256 KB (flag `response_truncated`), ≤ 4 concurrent DB queries (otherwise `[busy]`), 5 s / 8 s SQLite deadlines; strict `doc_id` (ASCII digits, 64-bit), real calendar dates `YYYY-MM-DD` (years 1990–2100), NUL/control characters rejected; errors look like `[code] message`.
Аргумент ≤ 200 символів, запит ≤ 8 слів, відповідь ≤ 256 КБ, не більше 4 одночасних запитів до бази; повний опис — у README основного проєкту (copy of fields table: `court_tools.py` constants).
