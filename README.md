# NL2SQL Studio (100% local)
Type or speak English, get SQL. No AI service, no API key, no internet, only Python 3.

## Run (Mac)
Double-click `run.command` (first time: right-click > Open), or run `python3 server.py`.
Your browser opens at http://localhost:8765.

## Use your own database
Paste your `CREATE TABLE ...` statements into the Schema box (or write `table(col, col)` lines).
Primary/foreign keys (`REFERENCES`) and `<table>_id` columns are used for JOINs.

## What it understands
SELECT with column lists, DISTINCT, WHERE (=, >, <, >=, <=, !=, BETWEEN, LIKE, IN, IS NULL, AND/OR,
"older than 30"), COUNT/AVG/SUM/MIN/MAX, GROUP BY ("per city"), ORDER BY, top N, JOINs across related tables,
INSERT, UPDATE, DELETE, CREATE/DROP TABLE, ADD/DROP COLUMN, CREATE INDEX, TRUNCATE.
Dialects: PostgreSQL, MySQL, SQLite, SQL Server (TOP), Oracle (FETCH FIRST).

## Layout
- `server.py` local server + safety warnings
- `static/index.html` the UI (voice works in Chrome/Safari; Chrome's speech uses Google, typing is fully offline)
- `engine/nl2sql.py` the NLP engine (also: `python3 engine/nl2sql.py "show subscribers older than 30"`)

Queries are generated only, never executed. Unfiltered DELETE/UPDATE is refused.
