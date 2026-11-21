# Changelog — NL2SQL Studio

All notable changes to this project since Milestone 1 are documented in this file.

## [Milestone 2] - 2026-11-21

### Added
- **Multi-Word & OR Condition Handling**: Extracted and merged multi-token literal values (e.g., `'New York'`, `'San Francisco'`) and collapsed adjacent same-column `OR` expressions into `IN (...)` syntax.
- **HAVING Clause Inference**: Added pattern recognition for aggregate threshold filters (`"cities with more than 3 subscribers"` -> `HAVING COUNT(*) > 3`).
- **Conversational DDL Table Generator**: Enhanced DDL parser to handle natural English phrases (e.g. `"create table employee and it must inclued the id, name and months columns"`), stripping conversational filler and inferring column data types.
- **Temporal & Row-to-Row Comparison (LAG Window Function)**: Implemented CTE generation using `LAG() OVER (PARTITION BY id ORDER BY month)` for period-over-period comparisons (`"which employees salary has been increased from previous month"`).
- **Interactive Studio UI**: Built responsive dark/light theme web interface with live SQL syntax highlighting, query history with timestamps, schema inspection drawer, example chips, and safety badges.
- **Dynamic Hot-Reloading Server**: Updated `server.py` with SO_REUSEADDR and per-request engine reloads to support seamless query evaluation without server restart.

### Changed
- Expanded stop words to clean pronoun contamination (`we`, `they`, `do`, `have`, `us`).
- Improved status word adjective mapping (`active`, `pending`, `cancelled`, `premium`).
- Fixed syntax highlighter placeholder collision bug in `static/index.html` where numeric tokens collided with regex placeholders.

### Milestone 1 Baseline - 2026-11-02
- Basic keyword intent matching (SELECT, INSERT, UPDATE, DELETE).
- Single-condition WHERE clause extraction for numeric and single-word equality.
- Primitive CLI prototype.
