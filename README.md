# NL2SQL Studio — Natural Language to SQL Assistant

NL2SQL Studio converts plain English questions into production-grade, dialect-aware SQL queries (PostgreSQL, MySQL, SQLite, SQL Server, Oracle). It runs 100% locally with zero cloud dependencies, offline token safety, and interactive execution previews.

## 🔗 Links
- **Prototype Link**: [http://localhost:8765](http://localhost:8765) *(Local live instance; see run instructions below)*
- **GitHub Repository**: [https://github.com/palmersingh50-maker/NLP-TO-SQL](https://github.com/palmersingh50-maker/NLP-TO-SQL)
- **Video Reel Demo**: [https://drive.google.com/file/d/1Vswp081gceSOKoQKcdLXIBzH5k_Gwkaq/view?usp=drive_link](https://drive.google.com/file/d/1Vswp081gceSOKoQKcdLXIBzH5k_Gwkaq/view?usp=drive_link)

## 👥 Team Details
- **Team ID**: PE-B81-17
- **Team Name**: THE MYTHIC
- **Batch**: Batch 83
- **Faculty Guide**: Dr. Inderjeet Gupta (SCSE4041)
- **Team Members**:
  - `S24CSEU2171` — **Maulik Mani Awasthi** (Team Leader, Project Management & Pipeline Architecture)
  - `S24CSEU2165` — **Palmer Singh Soodan** (Frontend & NLP Engineer, Core Engine & Web Studio UI)
  - `S24CSEU2161` — **Arjit Jain** (Prompt Engineering, Iteration Logs & Evaluation)
  - `S24CSEU2882` — **Raghav Chandra** (Testing Suite, Edge Cases & Safety Auditor)

---

## 🚀 How to Run

### Option 1: macOS One-Click
Double-click `run.command` in the project root folder.

### Option 2: Terminal / CLI
```bash
# Clone the repository
git clone https://github.com/palmersingh50-maker/NLP-TO-SQL.git
cd NLP-TO-SQL

# Run the local server (Python 3.10+ standard library, no pip install required)
python3 server.py
```
Open your browser at **http://localhost:8765**.

---

## 📂 Repository Structure
```
NLP-TO-SQL/
├── README.md               # Overview, team info, links, run guide
├── CHANGELOG.md            # Dated changes since Milestone 1
├── server.py               # Local HTTP server with hot-reload & safety checks
├── run.command             # One-click launch script for macOS
├── engine/
│   └── nl2sql.py           # NLP translation pipeline, DDL parser, window CTEs
├── static/
│   └── index.html          # Responsive dark/light web UI with syntax highlighter
├── prompts/
│   ├── v1/                 # Initial prompt baseline
│   │   └── P2_sql_generator_v1.txt
│   ├── v2/                 # Intermediate schema-grounded prompt
│   │   └── P2_sql_generator_v2.txt
│   ├── v3/                 # Few-shot CoT prompt
│   │   └── P2_sql_generator_v3.txt
│   └── final/              # Production prompts
│       ├── P1_schema_filter.txt
│       ├── P2_sql_generator.txt
│       └── P3_safety_auditor.txt
├── test-data/
│   ├── test_table.json     # 12 test inputs (Normal, Edge, Invalid)
│   └── test_results.json   # Recorded outputs and Pass/Fail evaluations
└── docs/
    ├── diagrams/           # Workflow and architecture diagrams
    ├── M2_PE-B81-17_NL2SQLStudio.pdf   # Official Milestone 2 Submission PDF
    └── TheMythic_TechnicalReport.pdf   # Supporting Technical Report
```

---

## 🧠 Key Features Built in Milestone 2
1. **Conversational DDL**: Translates phrases like `"create table employee and it must inclued the id, name and months columns"` into clean schema DDL.
2. **Temporal Period-over-Period (LAG Window Function)**: Automatically constructs CTEs with `LAG() OVER (PARTITION BY id ORDER BY month)` for queries like `"which employees salary has been increased from previous month"`.
3. **Multi-Word & OR-Merge**: Intelligently captures compound values like `'New York'` and condenses multiple OR conditions into `IN (...)`.
4. **Interactive Dark/Light UI**: Complete UI with SQL keyword coloring, intent breakdown chips, and instant query execution history.
