"""
Local NL -> SQL engine. Pure Python, no AI, no internet.

Pipeline: prepare (quotes, fillers, spoken numbers) -> schema parse (DDL, keys)
-> intent detection -> table resolution (scored) -> entity/condition extraction
-> clause building (SELECT/aggregates/GROUP/ORDER/LIMIT/JOIN, INSERT, UPDATE,
DELETE, DDL) -> dialect-aware SQL.
"""
import re

SAMPLE_SCHEMA = """CREATE TABLE subscribers (id INTEGER PRIMARY KEY, name TEXT, email TEXT, age INTEGER, city TEXT);
CREATE TABLE subscription (id INTEGER PRIMARY KEY, subscriber_id INTEGER REFERENCES subscribers(id), plan TEXT, status TEXT, price NUMERIC);"""

SYN = {"name": ["named", "called"], "price": ["cost", "costs", "amount"], "age": ["old"],
       "email": ["mail", "e-mail"], "city": ["town"], "salary": ["pay", "wage"], "quantity": ["qty"]}
ADJ = {"older": ("age", ">"), "younger": ("age", "<"), "cheaper": ("price", "<"),
       "costlier": ("price", ">"), "more expensive": ("price", ">")}
OPS = [(r"greater than or equal to|at least|>=", ">="), (r"less than or equal to|at most|<=", "<="),
       (r"greater than|more than|bigger than|higher than|larger than|over|above|after|>", ">"),
       (r"less than|fewer than|lower than|smaller than|under|below|before|<", "<"),
       (r"not equal to|isn't|!=", "!="), (r"equal to|equals|=", "=")]
OP_RE = "|".join(f"(?:{p})" for p, _ in OPS)
VAL = r"__q\d+__|-?\d+(?:\.\d+)?|[\w@.\-]+"
STOP = {"from", "in", "into", "to", "of", "the", "where", "with", "and", "or", "table", "a", "an", "for",
        "whose", "that", "has", "have", "all", "set", "on", "who", "is", "are", "by", "per", "each", "order",
        "sorted", "ordered", "limit", "top", "first", "group", "grouped", "then", "also", "their"}
TYPES = {"text": "TEXT", "string": "TEXT", "varchar": "VARCHAR(255)", "number": "INTEGER", "int": "INTEGER",
         "integer": "INTEGER", "decimal": "NUMERIC(10,2)", "float": "FLOAT", "money": "NUMERIC(10,2)",
         "date": "DATE", "datetime": "TIMESTAMP", "timestamp": "TIMESTAMP", "boolean": "BOOLEAN", "bool": "BOOLEAN"}
ONES = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                   "fourteen fifteen sixteen seventeen eighteen nineteen".split())}
TENS = {w: 10 * i for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if w != "_"}


# ------------------------------------------------------------ helpers
def sing(w):
    w = w.lower()
    if w.endswith("ies") and len(w) > 4: return w[:-3] + "y"
    if w.endswith(("ss", "us", "is")): return w
    if w.endswith("s") and len(w) > 3: return w[:-1]
    return w

def forms(word):
    s = sing(word)
    return {word.lower(), s, s + "s", (s[:-1] + "ies") if s.endswith("y") else s + "es"}

def is_num(t): return bool(t) and bool(re.search(r"INT|NUM|DEC|FLOAT|DOUBLE|REAL|SERIAL|MONEY", t.upper()))
def guess_type(n):
    if n == "id" or n.endswith("_id"): return "INTEGER"
    if re.search(r"price|amount|cost|salary|total", n): return "NUMERIC(10,2)"
    if re.search(r"age|count|qty|quantity|year|number|score", n): return "INTEGER"
    if re.search(r"date|_at$|time", n): return "TIMESTAMP"
    return "TEXT"
def table_aliases(t): return forms(t.replace("_", " "))
def col_aliases(c):
    a = forms(c.replace("_", " "))
    if c.endswith("_id"): a |= forms(c[:-3].replace("_", " "))
    a |= set(SYN.get(c, []))
    return sorted(a, key=len, reverse=True)
def alt(al): return "|".join(re.escape(a) for a in al)
def blank(s, a, b): return s[:a] + " " * (b - a) + s[b:]

def split_top(s):
    parts, d, cur = [], 0, ""
    for ch in s:
        d += (ch == "(") - (ch == ")")
        if ch == "," and d == 0: parts.append(cur); cur = ""
        else: cur += ch
    if cur.strip(): parts.append(cur)
    return [p.strip() for p in parts]

def parse_schema(ddl):
    tables = {}
    for m in re.finditer(r"create\s+table\s+(?:if\s+not\s+exists\s+)?[\"`\[]?(\w+)[\"`\]]?\s*\(", ddl, re.I):
        i = m.end(); d = 1; j = i
        while j < len(ddl) and d:
            d += (ddl[j] == "(") - (ddl[j] == ")"); j += 1
        cols, fks = {}, {}
        for part in split_top(ddl[i:j - 1]):
            fk = re.match(r"(?:constraint\s+\w+\s+)?foreign\s+key\s*\((\w+)\)\s*references\s+(\w+)\s*\((\w+)\)", part, re.I)
            if fk: fks[fk.group(1).lower()] = (fk.group(2).lower(), fk.group(3).lower()); continue
            if re.match(r"(primary|unique|key|constraint|index|check)\b", part, re.I): continue
            toks = part.split(None, 2)
            name = toks[0].strip('"`[]').lower()
            cols[name] = toks[1].upper() if len(toks) > 1 else guess_type(name)
            r = re.search(r"references\s+(\w+)\s*\((\w+)\)", part, re.I)
            if r: fks[name] = (r.group(1).lower(), r.group(2).lower())
        tables[m.group(1).lower()] = {"cols": cols, "fks": fks}
    if not tables:  # lenient format:  table(col, col)  or  table: col, col
        for line in re.split(r"[\n;]+", ddl):
            m = re.match(r"\s*(\w+)\s*(?:\(([^)]*)\)|:(.*))\s*$", line)
            if m:
                cs = [c.strip().lower() for c in (m.group(2) or m.group(3)).split(",") if c.strip()]
                tables[m.group(1).lower()] = {"cols": {c.split()[0]: (c.split()[1].upper() if len(c.split()) > 1 else guess_type(c)) for c in cs}, "fks": {}}
    for t, meta in tables.items():  # infer foreign keys from <table>_id
        for c in meta["cols"]:
            if c.endswith("_id") and c not in meta["fks"]:
                for t2 in tables:
                    if t2 != t and c[:-3] in forms(t2): meta["fks"][c] = (t2, "id")
    return tables

def prepare(sentence):
    quotes = []
    def q(m):
        quotes.append(m.group(1) if m.group(1) is not None else m.group(2))
        return f" __q{len(quotes) - 1}__ "
    t = sentence.replace("\u201c", '"').replace("\u201d", '"').replace("\u2019", "'")
    t = re.sub(r"\"([^\"]*)\"|(?<!\w)'([^']*)'", q, t)
    t = re.sub(r"\b(please|can you|could you|i want to|i would like to|i need to|kindly|for me)\b", " ", t, flags=re.I)
    t = re.sub(r"[?!;]|\.\s*$", " ", t)
    out, toks, i = [], t.split(), 0
    while i < len(toks):
        w = toks[i].lower()
        if w in TENS:
            n = TENS[w]
            if i + 1 < len(toks) and toks[i + 1].lower() in ONES and 0 < ONES[toks[i + 1].lower()] < 10:
                n += ONES[toks[i + 1].lower()]; i += 1
            out.append(str(n))
        elif w in ONES: out.append(str(ONES[w]))
        else: out.append(toks[i])
        i += 1
    return " ".join(out), quotes

def fmt(v, typ, quotes):
    m = re.fullmatch(r"__q(\d+)__", v)
    s = quotes[int(m.group(1))] if m else v
    if re.fullmatch(r"-?\d+(\.\d+)?", s) and (typ is None or is_num(typ)): return s
    if s.lower() in ("true", "false") and typ and "BOOL" in typ.upper(): return s.upper()
    return "'" + s.replace("'", "''") + "'"


# ------------------------------------------------------------ conditions
def extract(work, cands, schema, quotes, allwords):
    conds, taken = [], []
    free = lambda a, b: all(b <= x or a >= y for x, y in taken)
    def add(a, b, t, c, rhs): conds.append((a, b, t, c, rhs)); taken.append((a, b))
    typ = lambda t, c: schema[t]["cols"].get(c)

    for m in re.finditer(r"\b(older|younger|cheaper|costlier|more expensive)\s+than\s+(-?\d+(?:\.\d+)?)", work, re.I):
        col, op = ADJ[m.group(1).lower()]
        for t, c, al in cands:
            if c == col and free(m.start(), m.end()): add(m.start(), m.end(), t, c, f"{op} {m.group(2)}"); break
    for t, c, al in cands:
        a = alt(al)
        for m in re.finditer(rf"\b(?:{a})\b\s+(?:is\s+|are\s+)?between\s+({VAL})\s+and\s+({VAL})", work, re.I):
            if free(m.start(), m.end()):
                add(m.start(), m.end(), t, c, f"BETWEEN {fmt(m.group(1), typ(t, c), quotes)} AND {fmt(m.group(2), typ(t, c), quotes)}")
        for m in re.finditer(rf"\b(?:{a})\b\s+(?:is\s+)?(not\s+)?(?:null|empty|missing|blank)\b", work, re.I):
            if free(m.start(), m.end()): add(m.start(), m.end(), t, c, "IS NOT NULL" if m.group(1) else "IS NULL")
        for m in re.finditer(rf"\b(?:{a})\b\s+(?:(?:is|are)\s+)?(not\s+)?(contains?|containing|includes?|like|(?:starts?|starting|begins?|beginning)\s+with|(?:ends?|ending)\s+with)\s+({VAL})", work, re.I):
            if not free(m.start(), m.end()): continue
            kind = m.group(2).lower(); raw = fmt(m.group(3), None, quotes)[1:-1]
            pat = raw if kind == "like" else raw + "%" if kind.split()[0].startswith(("start", "begin")) else "%" + raw if kind.startswith("end") else f"%{raw}%"
            add(m.start(), m.end(), t, c, f"{'NOT ' if m.group(1) else ''}LIKE '{pat}'")
    for t, c, al in cands:
        pat = (rf"\b(?:{alt(al)})\b\s+(?:(?:is|are|was|equals?|equal\s+to|=|of|:)\s+)?(?:(not)\s+)?(?:({OP_RE})\s+)?({VAL})"
               rf"((?:\s*(?:,|\bor\b)\s*(?:{VAL}))*)")
        for m in re.finditer(pat, work, re.I):
            v = m.group(3)
            if v.lower() in STOP or v.lower() in allwords or not free(m.start(), m.end()): continue
            op = "="
            if m.group(2):
                op = next(sql for p, sql in OPS if re.fullmatch(p, m.group(2), re.I))
            elif m.group(1): op = "!="
            vals, end = [v], m.end(3)
            for mm in re.finditer(rf"\s*(?:,|\bor\b)\s*({VAL})", m.group(4)):
                if mm.group(1).lower() in STOP or mm.group(1).lower() in allwords: break
                vals.append(mm.group(1)); end = m.start(4) + mm.end()
            if len(vals) > 1 and op in ("=", "!="):
                add(m.start(), end, t, c, f"{'NOT ' if op == '!=' else ''}IN ({', '.join(fmt(x, typ(t, c), quotes) for x in vals)})")
            else:
                add(m.start(), end, t, c, f"{op} {fmt(v, typ(t, c), quotes)}")
    return conds

def build_where(conds, work, qual):
    conds = sorted(conds, key=lambda x: x[0])
    groups = [[]]
    for i, (a, b, t, c, rhs) in enumerate(conds):
        if i and re.search(r"\bor\b", work[conds[i - 1][1]:a], re.I): groups.append([])
        groups[-1].append(f"{t + '.' if qual else ''}{c} {rhs}")
    parts = [" AND ".join(g) for g in groups]
    if len(groups) > 1: parts = [f"({p})" if len(g) > 1 else p for p, g in zip(parts, groups)]
    return " OR ".join(parts)

def joins(primary, used, schema):
    out = []
    for t in sorted(used - {primary}):
        link = None
        for c, (rt, rc) in schema[primary]["fks"].items():
            if rt == t: link = f"{primary}.{c} = {t}.{rc}"
        for c, (rt, rc) in schema[t]["fks"].items():
            if rt == primary: link = f"{t}.{c} = {primary}.{rc}"
        if not link: return None, t
        out.append(f"JOIN {t} ON {link}")
    return out, None


# ------------------------------------------------------------ DDL-style intents
def ddl_intent(text, schema, dialect):
    def tbl(w):
        return next((t for t in schema if w.lower() in table_aliases(t)), w.lower())
    m = re.search(r"\bcreate\s+(?:an?\s+)?index\s+(?:on|for)\s+(\w+)\s+(?:on\s+|for\s+)?(\w+)", text, re.I)
    if m:
        t = tbl(m.group(1)); return f"CREATE INDEX idx_{t}_{m.group(2).lower()} ON {t} ({m.group(2).lower()});", f"Create index on {t}.{m.group(2).lower()}"
    m = re.search(r"\bcreate\s+(?:an?\s+)?(?:new\s+)?table\s+(?:called\s+|named\s+)?(\w+)(?:\s+(?:with|having|containing|columns?|fields?|:)\s*(?:(?:columns?|fields?)\s*)?(.*))?$", text, re.I)
    if m:
        cols = []
        for part in re.split(r",|\band\b", m.group(2) or ""):
            w = part.split()
            if not w: continue
            name = w[0].lower(); ty = TYPES.get(w[-1].lower()) if len(w) > 1 else None
            cols.append(f"{name} {ty or guess_type(name)}" + (" PRIMARY KEY" if name == "id" else ""))
        if not any(c.startswith("id ") for c in cols): cols.insert(0, "id INTEGER PRIMARY KEY")
        return f"CREATE TABLE {m.group(1).lower()} (\n  " + ",\n  ".join(cols) + "\n);", f"Create table {m.group(1).lower()}"
    m = re.search(r"\b(?:drop|delete|remove)\s+(?:the\s+)?table\s+(\w+)", text, re.I)
    if m: t = tbl(m.group(1)); return f"DROP TABLE {t};", f"Drop table {t}"
    m = re.search(r"\b(?:truncate|empty|clear|wipe)\s+(?:the\s+)?(?:table\s+)?(\w+)", text, re.I)
    if m and tbl(m.group(1)) in schema:
        t = tbl(m.group(1)); return (f"DELETE FROM {t};" if dialect == "SQLite" else f"TRUNCATE TABLE {t};"), f"Remove all rows from {t}"
    m = re.search(r"\b(add|drop|remove|delete)\s+(?:an?\s+|the\s+|new\s+)?(?:column|field)\s+(\w+)(?:\s+(?:as\s+|of\s+type\s+)?(\w+))?\s+(?:to|from|in|on)\s+(?:the\s+)?(?:table\s+)?(\w+)", text, re.I)
    if m:
        t, c = tbl(m.group(4)), m.group(2).lower()
        if m.group(1).lower() == "add":
            ty = TYPES.get((m.group(3) or "").lower()) or guess_type(c)
            return f"ALTER TABLE {t} ADD COLUMN {c} {ty};", f"Add column {c} to {t}"
        return f"ALTER TABLE {t} DROP COLUMN {c};", f"Drop column {c} from {t}"
    return None


# ------------------------------------------------------------ main
INTENTS = [("DELETE", r"\b(delete|remove|erase|get rid of)\b"),
           ("SELECT", r"\b(show|list|get|find|display|fetch|select|give|print|what|which|who|count|how many|number of|average|avg|mean|total|sum|maximum|max|minimum|min|highest|lowest|oldest|youngest|cheapest)\b"),
           ("UPDATE", r"\b(update|change|modify|set|rename|make|edit)\b"),
           ("INSERT", r"\b(add|insert|create|register|enroll|new)\b")]

def translate(sentence, ddl="", dialect="PostgreSQL"):
    schema = parse_schema(ddl or SAMPLE_SCHEMA)
    text, quotes = prepare(sentence)
    if not text.strip(): return {"sql": "-- Say or type something first.", "explanation": ""}
    r = ddl_intent(text, schema, dialect)
    if r: return {"sql": r[0], "explanation": r[1]}

    best = [(m.start(), i, n) for i, (n, p) in enumerate(INTENTS) for m in [re.search(p, text, re.I)] if m]
    intent = min(best)[2] if best else "SELECT"

    # table resolution (scored)
    scores = {}
    for t, meta in schema.items():
        tal = alt(sorted(table_aliases(t), key=len, reverse=True)); s = 0; first = 9999
        m = re.search(rf"\b(?:{tal})\b", text, re.I)
        if m: s += 2; first = m.start()
        if re.search(rf"\b(?:from|into|in|to|of|on)\s+(?:the\s+)?(?:{tal})\b(?!\s+\d)", text, re.I): s += 3
        for c in meta["cols"]:
            if c != "id" and re.search(rf"\b(?:{alt(col_aliases(c))})\b", text, re.I): s += 1.5
        scores[t] = (s, -first)
    ranked = sorted(schema, key=lambda t: scores[t], reverse=True)
    if not ranked or scores[ranked[0]][0] < 2:
        return {"sql": f"-- I couldn't find a table in your request. Known tables: {', '.join(schema)}.", "explanation": "Mention a table name, or update the Schema box."}
    primary = ranked[0]
    others = [t for t in ranked[1:] if scores[t][0] > 0] if intent == "SELECT" else []
    scope = [primary] + others
    cands = [(t, c, col_aliases(c)) for t in scope for c in schema[t]["cols"]]
    allwords = {a.lower() for t in schema for a in table_aliases(t)} | {a for _, _, al in cands for a in al}
    tword = {a.lower() for t in schema for a in table_aliases(t)}
    work = text
    tal = alt(sorted(table_aliases(primary), key=len, reverse=True))
    idm = re.search(rf"\b(?:{tal})\s+(\d+)\b", work, re.I) if "id" in schema[primary]["cols"] else None
    pre = []
    if idm: pre.append((idm.start(), idm.end(), primary, "id", f"= {idm.group(1)}")); work = blank(work, idm.start(), idm.end())
    typ = lambda t, c: schema[t]["cols"].get(c)

    # ---- INSERT
    if intent == "INSERT":
        pc = [x for x in cands if x[0] == primary]
        conds = [c for c in extract(work, pc, schema, quotes, allwords) if c[4].startswith("= ")] + pre
        if not conds: return {"sql": "-- INSERT needs values, e.g. add subscriber with name John and age 25.", "explanation": ""}
        conds.sort(key=lambda x: x[0])
        return {"sql": f"INSERT INTO {primary} ({', '.join(c[3] for c in conds)}) VALUES ({', '.join(c[4][2:] for c in conds)});",
                "explanation": f"Insert a row into {primary}"}

    # ---- UPDATE
    if intent == "UPDATE":
        assigns, cond_text = [], work
        for c in schema[primary]["cols"]:
            m = re.search(rf"\b(?:change|update|set|make|edit)\s+(?:the\s+)?(?:{alt(col_aliases(c))})\s+(?:of|for)\s+(.+?)\s+(?:to|as|=)\s+({VAL})\s*$", work, re.I)
            if m: assigns.append((c, m.group(2))); cond_text = m.group(1); break
        if not assigns:
            for c in schema[primary]["cols"]:
                for m in re.finditer(rf"\b(?:{alt(col_aliases(c))})\s+(?:to|as|=)\s+({VAL})", work, re.I):
                    assigns.append((c, m.group(1))); work = blank(work, m.start(), m.end())
            cond_text = work
        if not assigns: return {"sql": "-- UPDATE needs something like: set status to cancelled where subscriber 13.", "explanation": ""}
        pc = [x for x in cands if x[0] == primary]
        conds = extract(cond_text, pc, schema, quotes, allwords) + ([] if cond_text != work else [])
        if not conds and not (cond_text == work and pre): conds = pre if pre else conds
        if not conds: return {"sql": "-- Refused: UPDATE without a condition would change every row.", "explanation": "Add a condition such as 'where subscriber 13'."}
        sets = ", ".join(f"{c} = {fmt(v, typ(primary, c), quotes)}" for c, v in assigns)
        return {"sql": f"UPDATE {primary} SET {sets} WHERE {build_where(conds, cond_text, False)};", "explanation": f"Update {primary}"}

    # ---- DELETE
    if intent == "DELETE":
        pc = [x for x in cands if x[0] == primary]
        conds = extract(work, pc, schema, quotes, allwords) + pre
        if not conds: return {"sql": "-- Refused: DELETE without a condition would wipe the whole table.", "explanation": "Add a condition such as 'where status is cancelled'."}
        return {"sql": f"DELETE FROM {primary} WHERE {build_where(conds, text, False)};", "explanation": f"Delete matching rows from {primary}"}

    # ---- SELECT
    limit = None
    m = re.search(r"\b(top|first|limit)\s+(\d+)\b", work, re.I)
    topword = bool(m and m.group(1).lower() != "limit")
    if m: limit = int(m.group(2)); work = blank(work, m.start(), m.end())
    distinct = bool(re.search(r"\b(distinct|unique)\b", work, re.I))
    A = lambda: "|".join(f"(?:{alt(al)})" for _, _, al in cands)
    def locate(prefix):
        for t, c, al in cands:
            m = re.search(rf"\b(?:{prefix})\s+(?:of\s+)?(?:the\s+)?\b(?:{alt(al)})\b", work, re.I)
            if m: return t, c, m
    aggs = []
    m = re.search(r"\b(?:total\s+)?(?:number\s+of|count|how\s+many)\b", work, re.I)
    if m: aggs.append(("COUNT", None, None, "total_count")); work = blank(work, m.start(), m.end())
    for fn, pre_re in [("AVG", "average|avg|mean"), ("SUM", "total|sum"), ("MAX", "maximum|max|highest|largest|biggest|oldest"), ("MIN", "minimum|min|lowest|smallest|youngest|cheapest")]:
        r = locate(pre_re)
        if r: t, c, m = r; aggs.append((fn, t, c, f"{fn.lower()}_{c}")); work = blank(work, m.start(), m.end())
    group = order = None; desc = False
    nt = lambda al: alt([a for a in al if a.lower() not in tword] or ["\x00"])
    for t, c, al in cands:
        m = re.search(rf"\b(?:per|for\s+each|grouped\s+by|group\s+by|by\s+each|each)\s+(?:the\s+)?\b(?:{nt(al)})\b", work, re.I)
        if m and not group: group = (t, c); work = blank(work, m.start(), m.end())
    for t, c, al in cands:
        m = re.search(rf"\b(?:sorted|ordered|order|sort|ranked|rank)\s+by\s+(?:the\s+)?\b(?:{nt(al)})\b(?:\s+(desc\w*|asc\w*|highest|lowest|high\s+to\s+low|low\s+to\s+high))?", work, re.I)
        if m and not order:
            order = (t, c); desc = bool(m.group(1) and re.match(r"desc|highest|high", m.group(1), re.I)); work = blank(work, m.start(), m.end())
    for t, c, al in cands:
        m = re.search(rf"\bby\s+(?:the\s+)?\b(?:{nt(al)})\b", work, re.I)
        if m:
            if aggs and not group: group = (t, c)
            elif not order: order = (t, c); desc = topword
            work = blank(work, m.start(), m.end()); break
    conds = extract(work, cands, schema, quotes, allwords) + pre
    for a, b, *_ in conds: work = blank(work, a, b)

    sel = []
    if not aggs:
        m = re.search(r"\b(?:show|list|get|find|display|fetch|select|give|print)\b(?:\s+me)?\s+(.*?)(?=\s+(?:from|of|in|where|with|whose|for|who|that|older|younger|sorted|ordered)\b|$)", work, re.I)
        phrase = m.group(1) if m else ""
        found = []
        for t, c, al in cands:
            for mm in re.finditer(rf"\b(?:{alt([a for a in al if a.lower() not in tword])})\b" if [a for a in al if a.lower() not in tword] else r"(?!x)x", phrase, re.I):
                found.append((mm.start(), t, c))
        seen = set()
        sel = [(t, c) for _, t, c in sorted(found) if not ((t, c) in seen or seen.add((t, c)))]
    if group and not aggs: aggs.append(("COUNT", None, None, "total_count"))
    if aggs and limit and not group:
        for t, c, al in cands:
            if t == primary and c != "id" and re.search(rf"\b(?:{nt(al)})\b", work, re.I): group = (t, c); break
    if aggs and group and limit and not order: order = ("AGG", aggs[0][3]); desc = True

    used = {t for _, _, t, _, _ in conds} | {t for t, _ in sel} | {a[1] for a in aggs if a[1]} | ({group[0]} if group else set()) | ({order[0]} if order and order[0] != "AGG" else set())
    used.add(primary)
    jn, bad = joins(primary, used, schema)
    if jn is None: return {"sql": f"-- No relationship between {primary} and {bad} in the schema (add a REFERENCES clause).", "explanation": ""}
    q = len(used) > 1
    ref = lambda t, c: f"{t}.{c}" if q else c
    cols = []
    if group: cols.append(ref(*group))
    for fn, t, c, al in aggs: cols.append(f"{fn}({ref(t, c) if c else '*'}) AS {al}")
    if not aggs: cols = [ref(t, c) for t, c in sel] or ["*"]
    sql = "SELECT " + ("DISTINCT " if distinct else "") + (f"TOP {limit} " if limit and dialect == "SQL Server" else "") + ", ".join(cols)
    sql += f" FROM {primary}" + "".join(" " + j for j in jn)
    if conds: sql += " WHERE " + build_where(conds, text, q)
    if group and aggs: sql += f" GROUP BY {ref(*group)}"
    if order: sql += " ORDER BY " + (order[1] if order[0] == "AGG" else ref(*order)) + (" DESC" if desc else "")
    if limit and dialect == "Oracle": sql += f" FETCH FIRST {limit} ROWS ONLY"
    elif limit and dialect != "SQL Server": sql += f" LIMIT {limit}"
    info = f"Intent SELECT · table {primary}" + (f" · joined {', '.join(sorted(used - {primary}))}" if q else "") + (f" · {len(conds)} filter(s)" if conds else "")
    return {"sql": sql + ";", "explanation": info}


if __name__ == "__main__":
    import sys
    print(translate(" ".join(sys.argv[1:]))["sql"])
