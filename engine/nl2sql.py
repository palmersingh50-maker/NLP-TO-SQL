"""
Local NL -> SQL engine — Advanced Edition. Pure Python, no AI, no internet.

Pipeline: prepare (quotes, fillers, spoken numbers)
  -> schema parse (DDL + FK inference)
  -> intent detection
  -> table resolution (scored)
  -> status-adjective + superlative pre-processing
  -> condition extraction  (multi-word values, OR-merge, BETWEEN, LIKE, NULL)
  -> clause building (SELECT / aggregates / GROUP / HAVING / ORDER / LIMIT /
     JOIN, INSERT, UPDATE, DELETE, DDL)
  -> dialect-aware SQL output
"""
import re

SAMPLE_SCHEMA = (
    "CREATE TABLE subscribers "
    "(id INTEGER PRIMARY KEY, name TEXT, email TEXT, age INTEGER, city TEXT);\n"
    "CREATE TABLE subscription "
    "(id INTEGER PRIMARY KEY, subscriber_id INTEGER REFERENCES subscribers(id), "
    "plan TEXT, status TEXT, price NUMERIC);"
)

# ---------------------------------------------------------------- vocab tables
SYN = {
    "name":     ["named", "called", "names"],
    "price":    ["cost", "costs", "amount", "revenue", "income", "earnings",
                 "turnover", "fee", "charge", "rate", "spend", "spending"],
    "age":      ["old", "ages"],
    "email":    ["mail", "e-mail", "emails"],
    "city":     ["town", "location", "cities", "towns", "locations", "place", "region"],
    "salary":   ["pay", "wage", "compensation"],
    "quantity": ["qty"],
    "status":   ["state", "condition"],
}

# Comparative adjectives -> (column, operator)
ADJ = {
    "older":          ("age",   ">"),
    "younger":        ("age",   "<"),
    "cheaper":        ("price", "<"),
    "costlier":       ("price", ">"),
    "more expensive": ("price", ">"),
    "less expensive": ("price", "<"),
}

# Superlative adjectives -> (column, is_descending)
# column=None -> pick first numeric col in primary table at runtime
ORD_ADJ = {
    "oldest":          ("age",   True),
    "youngest":        ("age",   False),
    "newest":          ("age",   False),
    "cheapest":        ("price", False),
    "most expensive":  ("price", True),
    "least expensive": ("price", False),
    "highest":         (None,    True),
    "lowest":          (None,    False),
    "largest":         (None,    True),
    "smallest":        (None,    False),
    "biggest":         (None,    True),
}

# Status-adjective words -> (column_name, column_value) inline filter
STATUS_WORDS = {
    "active":     ("status", "active"),
    "inactive":   ("status", "inactive"),
    "cancelled":  ("status", "cancelled"),
    "canceled":   ("status", "cancelled"),
    "pending":    ("status", "pending"),
    "expired":    ("status", "expired"),
    "enabled":    ("status", "enabled"),
    "disabled":   ("status", "disabled"),
    "suspended":  ("status", "suspended"),
    "verified":   ("status", "verified"),
    "unverified": ("status", "unverified"),
    "paid":       ("status", "paid"),
    "unpaid":     ("status", "unpaid"),
    "open":       ("status", "open"),
    "closed":     ("status", "closed"),
    "premium":    ("plan",   "premium"),
    "basic":      ("plan",   "basic"),
    "enterprise": ("plan",   "enterprise"),
    "trial":      ("plan",   "trial"),
}

OPS = [
    (r"greater than or equal to|at least|>=",           ">="),
    (r"less than or equal to|at most|<=",               "<="),
    (r"greater than|more than|bigger than|higher than"
     r"|larger than|over|above|after|>",                 ">"),
    (r"less than|fewer than|lower than|smaller than"
     r"|under|below|before|<",                           "<"),
    (r"not equal to|isn't|!=|is not",                   "!="),
    (r"equal to|equals|=",                              "="),
]
OP_RE = "|".join(f"(?:{p})" for p, _ in OPS)
VAL   = r"__q\d+__|−?\d+(?:\.\d+)?|[\w@.\-]+"

STOP = {
    "from", "in", "into", "to", "of", "the", "where", "with", "and", "or",
    "table", "a", "an", "for", "whose", "that", "has", "have", "all", "set",
    "on", "who", "is", "are", "by", "per", "each", "order", "sorted",
    "ordered", "limit", "top", "first", "group", "grouped", "then", "also",
    "their", "along",
    # pronouns / noise — these were previously caught as SQL values
    "we", "they", "us", "do", "me", "my", "our", "you", "your", "its", "i",
    "it", "he", "she", "him", "her", "them", "any", "some", "many", "much",
    "how", "what", "which", "when", "this", "these", "those", "been", "being",
    "was", "were", "had", "would", "could", "should", "will", "can",
}

TYPES = {
    "text": "TEXT", "string": "TEXT", "varchar": "VARCHAR(255)",
    "number": "INTEGER", "int": "INTEGER", "integer": "INTEGER",
    "decimal": "NUMERIC(10,2)", "float": "FLOAT", "money": "NUMERIC(10,2)",
    "date": "DATE", "datetime": "TIMESTAMP", "timestamp": "TIMESTAMP",
    "boolean": "BOOLEAN", "bool": "BOOLEAN",
}

ONES = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve "
    "thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
)}
TENS = {
    w: 10 * i
    for i, w in enumerate("_ _ twenty thirty forty fifty sixty seventy eighty ninety".split())
    if w != "_"
}


# -------------------------------------------------------------------- helpers
def sing(w):
    w = w.lower()
    if w.endswith("ies") and len(w) > 4: return w[:-3] + "y"
    if w.endswith(("ss", "us", "is")):   return w
    if w.endswith("s") and len(w) > 3:   return w[:-1]
    return w

def forms(word):
    s = sing(word)
    return {word.lower(), s, s + "s", (s[:-1] + "ies") if s.endswith("y") else s + "es"}

def is_num(t):
    return bool(t) and bool(re.search(r"INT|NUM|DEC|FLOAT|DOUBLE|REAL|SERIAL|MONEY", t.upper()))

def guess_type(n):
    if n == "id" or n.endswith("_id"):                            return "INTEGER"
    if re.search(r"price|amount|cost|salary|total|revenue", n):   return "NUMERIC(10,2)"
    if re.search(r"age|count|qty|quantity|year|years|month|months|day|days|number|score", n): return "INTEGER"
    if re.search(r"date|_at$|time", n):                           return "TIMESTAMP"
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
        else:                     cur += ch
    if cur.strip(): parts.append(cur)
    return [p.strip() for p in parts]


def parse_schema(ddl):
    tables = {}
    for m in re.finditer(
        r"create\s+table\s+(?:if\s+not\s+exists\s+)?[`\"\[]?(\w+)[`\"\]]?\s*\(", ddl, re.I
    ):
        i = m.end(); d = 1; j = i
        while j < len(ddl) and d:
            d += (ddl[j] == "(") - (ddl[j] == ")"); j += 1
        cols, fks = {}, {}
        for part in split_top(ddl[i:j - 1]):
            fk = re.match(
                r"(?:constraint\s+\w+\s+)?foreign\s+key\s*\((\w+)\)"
                r"\s*references\s+(\w+)\s*\((\w+)\)", part, re.I
            )
            if fk:
                fks[fk.group(1).lower()] = (fk.group(2).lower(), fk.group(3).lower())
                continue
            if re.match(r"(primary|unique|key|constraint|index|check)\b", part, re.I): continue
            toks = part.split(None, 2)
            name = toks[0].strip('"`[]').lower()
            cols[name] = toks[1].upper() if len(toks) > 1 else guess_type(name)
            r = re.search(r"references\s+(\w+)\s*\((\w+)\)", part, re.I)
            if r: fks[name] = (r.group(1).lower(), r.group(2).lower())
        tables[m.group(1).lower()] = {"cols": cols, "fks": fks}

    if not tables:  # lenient: table(col, col) or table: col, col
        for line in re.split(r"[\n;]+", ddl):
            m = re.match(r"\s*(\w+)\s*(?:\(([^)]*)\)|:(.*))s*$", line)
            if m:
                cs = [c.strip().lower()
                      for c in (m.group(2) or m.group(3)).split(",") if c.strip()]
                tables[m.group(1).lower()] = {
                    "cols": {
                        c.split()[0]: (c.split()[1].upper() if len(c.split()) > 1 else guess_type(c))
                        for c in cs
                    },
                    "fks": {},
                }

    # Infer FK from <table>_id column naming convention
    for t, meta in tables.items():
        for c in meta["cols"]:
            if c.endswith("_id") and c not in meta["fks"]:
                for t2 in tables:
                    if t2 != t and c[:-3] in forms(t2):
                        meta["fks"][c] = (t2, "id")
    return tables


def prepare(sentence):
    quotes = []

    def q(m):
        quotes.append(m.group(1) if m.group(1) is not None else m.group(2))
        return f" __q{len(quotes) - 1}__ "

    t = sentence.replace("\u201c", '"').replace("\u201d", '"').replace("\u2019", "'")
    t = re.sub(r'"([^"]*)"|((?<!\w)\'([^\']*)\')', q, t)
    t = re.sub(
        r"\b(please|can you|could you|i want to|i would like to"
        r"|i need to|kindly|for me)\b", " ", t, flags=re.I
    )
    t = re.sub(r"[?!;]|\.\s*$", " ", t)
    out, toks, i = [], t.split(), 0
    while i < len(toks):
        w = toks[i].lower()
        if w in TENS:
            n = TENS[w]
            if (i + 1 < len(toks)
                    and toks[i + 1].lower() in ONES
                    and 0 < ONES[toks[i + 1].lower()] < 10):
                n += ONES[toks[i + 1].lower()]; i += 1
            out.append(str(n))
        elif w in ONES:
            out.append(str(ONES[w]))
        else:
            out.append(toks[i])
        i += 1
    return " ".join(out), quotes


def fmt(v, typ, quotes):
    m = re.fullmatch(r"__q(\d+)__", v)
    s = quotes[int(m.group(1))] if m else v
    if re.fullmatch(r"-?\d+(\.\d+)?", s) and (typ is None or is_num(typ)): return s
    if s.lower() in ("true", "false") and typ and "BOOL" in typ.upper():    return s.upper()
    return "'" + s.replace("'", "''") + "'"


def _extend_value(text, pos, allwords, max_extra=2):
    """
    After a single-word string value, greedily consume up to max_extra more
    non-STOP, non-allwords tokens (handles 'New York', 'San Francisco', …).
    Returns (extra_words_list, new_end_position).
    """
    _halt = STOP | {"between", "like", "in", "not", "null", "is", "and", "or", "having", "where"}
    extras = []
    for _ in range(max_extra):
        m = re.match(r" +([\w@.\-]+)", text[pos:])
        if not m: break
        word = m.group(1)
        if (word.lower() in _halt
                or word.lower() in allwords
                or re.match(r"-?\d", word)
                or re.fullmatch(OP_RE, word, re.I)):
            break
        extras.append(word)
        pos += len(m.group())
    return extras, pos


# ------------------------------------------------------------------ conditions
def extract(work, cands, schema, quotes, allwords):
    conds, taken = [], []
    free  = lambda a, b: all(b <= x or a >= y for x, y in taken)
    def add(a, b, t, c, rhs): conds.append((a, b, t, c, rhs)); taken.append((a, b))
    typ   = lambda t, c: schema[t]["cols"].get(c)

    # Build a negative-lookahead that prevents group-4 OR-matching from consuming
    # a column-name that starts a new condition (e.g. "or plan is enterprise").
    _aliases = sorted(
        {re.escape(a.lower()) for _, _, al in cands for a in al if len(a) > 1},
        key=len, reverse=True,
    )
    _simple_ops = r"(?:is|are|was|=|!=|>|<|>=|<=)\b"
    _no_new_cond = (rf"(?!(?:{'|'.join(_aliases)})\s+{_simple_ops})" if _aliases else "")
    OR_VAL  = rf"{_no_new_cond}(?:{VAL})"
    group4  = rf"((?:\s*(?:,|\bor\b)\s*{OR_VAL})*)"

    # 1. Comparative adjectives: "older than 30", "cheaper than 50"
    for m in re.finditer(
        r"\b(older|younger|cheaper|costlier|more expensive|less expensive)"
        r"\s+than\s+(-?\d+(?:\.\d+)?)", work, re.I
    ):
        col, op = ADJ[m.group(1).lower()]
        for t, c, al in cands:
            if c == col and free(m.start(), m.end()):
                add(m.start(), m.end(), t, c, f"{op} {m.group(2)}")
                break

    # 2. BETWEEN … AND …
    for t, c, al in cands:
        a = alt(al)
        for m in re.finditer(
            rf"\b(?:{a})\b\s+(?:is\s+|are\s+)?between\s+({VAL})\s+and\s+({VAL})",
            work, re.I,
        ):
            if free(m.start(), m.end()):
                add(m.start(), m.end(), t, c,
                    f"BETWEEN {fmt(m.group(1), typ(t, c), quotes)}"
                    f" AND {fmt(m.group(2), typ(t, c), quotes)}")

    # 3. IS NULL / IS NOT NULL
    for t, c, al in cands:
        a = alt(al)
        for m in re.finditer(
            rf"\b(?:{a})\b\s+(?:is\s+)?(not\s+)?(?:null|empty|missing|blank)\b",
            work, re.I,
        ):
            if free(m.start(), m.end()):
                add(m.start(), m.end(), t, c, "IS NOT NULL" if m.group(1) else "IS NULL")

    # 4. LIKE patterns (contains / starts with / ends with)
    for t, c, al in cands:
        a = alt(al)
        for m in re.finditer(
            rf"\b(?:{a})\b\s+(?:(?:is|are)\s+)?(not\s+)?"
            rf"(contains?|containing|includes?|like"
            rf"|(?:starts?|starting|begins?|beginning)\s+with"
            rf"|(?:ends?|ending)\s+with)\s+({VAL})",
            work, re.I,
        ):
            if not free(m.start(), m.end()): continue
            kind = m.group(2).lower()
            raw  = fmt(m.group(3), None, quotes)[1:-1]
            pat  = (raw          if kind == "like"
                    else raw + "%" if kind.split()[0].startswith(("start", "begin"))
                    else "%" + raw if kind.startswith("end")
                    else f"%{raw}%")
            add(m.start(), m.end(), t, c,
                f"{'NOT ' if m.group(1) else ''}LIKE '{pat}'")

    # 5. Equality / comparison — with multi-word value extension
    for t, c, al in cands:
        full_pat = (
            rf"\b(?:{alt(al)})\b"
            rf"\s+(?:(?:is|are|was|equals?|equal\s+to|=|of|:)\s+)?"
            rf"(?:(not)\s+)?(?:({OP_RE})\s+)?({VAL})"
            rf"{group4}"
        )
        for m in re.finditer(full_pat, work, re.I):
            v0 = m.group(3)
            if v0.lower() in STOP or not free(m.start(), m.end()): continue

            # Attempt to extend a text value: "New" -> "New York"
            end = m.end(3)
            if not re.fullmatch(r"-?\d+(?:\.\d+)?", v0) and not v0.startswith("__q"):
                extras, end = _extend_value(work, end, allwords)
                if extras:
                    v0 = v0 + " " + " ".join(extras)
                elif v0.lower() in allwords:
                    continue          # single-word value that is a table/col name
            else:
                if v0.lower() in allwords: continue

            # Determine operator
            op = "="
            if   m.group(2): op = next(sql for p, sql in OPS if re.fullmatch(p, m.group(2), re.I))
            elif m.group(1): op = "!="

            # Collect additional comma / OR values from group 4
            vals, or_end = [v0], end
            for mm in re.finditer(rf"\s*(?:,|\bor\b)\s*({VAL})", m.group(4)):
                w = mm.group(1)
                if w.lower() in STOP or w.lower() in allwords: break
                vals.append(w)
                or_end = m.start(4) + mm.end()

            final_end = max(end, or_end)
            if not free(m.start(), final_end): continue

            if len(vals) > 1 and op in ("=", "!="):
                rhs = (f"{'NOT ' if op == '!=' else ''}IN "
                       f"({', '.join(fmt(x, typ(t, c), quotes) for x in vals)})")
            else:
                rhs = f"{op} {fmt(v0, typ(t, c), quotes)}"

            add(m.start(), final_end, t, c, rhs)

    return conds


def _merge_or_conditions(conds, text):
    """
    Post-process: merge adjacent same-table/col conditions joined by OR
    into a single IN () expression.
    e.g. plan='premium' OR plan='enterprise'  ->  plan IN ('premium','enterprise')
    """
    if len(conds) < 2: return conds
    conds   = sorted(conds, key=lambda x: x[0])
    merged, used = [], set()
    for i, (a1, b1, t1, c1, rhs1) in enumerate(conds):
        if i in used: continue
        group = [(a1, b1, t1, c1, rhs1)]
        for j, (a2, b2, t2, c2, rhs2) in enumerate(conds[i + 1:], i + 1):
            if j in used or t2 != t1 or c2 != c1: continue
            lo, hi = min(b1, a2), max(b1, a2)
            between = text[lo:hi].lower()
            if re.search(r"\bor\b", between) and not re.search(r"\band\b", between):
                group.append((a2, b2, t2, c2, rhs2))
                used.add(j)
        used.add(i)
        if len(group) == 1:
            merged.append(group[0])
        else:
            vals = []
            for _, _, _, _, rhs in group:
                eq = re.match(r"= (.+)", rhs)
                vals.append(eq.group(1) if eq else rhs)
            merged.append((
                group[0][0], group[-1][1], t1, c1,
                f"IN ({', '.join(vals)})"
            ))
    return merged


def build_where(conds, work, qual):
    conds  = sorted(conds, key=lambda x: x[0])
    groups = [[]]
    for i, (a, b, t, c, rhs) in enumerate(conds):
        if i and re.search(r"\bor\b", work[conds[i - 1][1]:a], re.I):
            groups.append([])
        groups[-1].append(f"{t + '.' if qual else ''}{c} {rhs}")
    parts = [" AND ".join(g) for g in groups]
    if len(groups) > 1:
        parts = [f"({p})" if len(g) > 1 else p for p, g in zip(parts, groups)]
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


# ---------------------------------------------------------------- DDL intents
def ddl_intent(text, schema, dialect):
    def tbl(w):
        return next((t for t in schema if w.lower() in table_aliases(t)), w.lower())

    m = re.search(
        r"\bcreate\s+(?:an?\s+)?index\s+(?:on|for)\s+(\w+)\s+(?:on\s+|for\s+)?(\w+)",
        text, re.I,
    )
    if m:
        t = tbl(m.group(1))
        return (f"CREATE INDEX idx_{t}_{m.group(2).lower()} ON {t} ({m.group(2).lower()});",
                f"Create index on {t}.{m.group(2).lower()}")

    m = re.search(
        r"\b(?:create|make|build|add|new)\s+(?:an?\s+)?(?:new\s+)?"
        r"(?:table\s+(?:called\s+|named\s+)?(\w+)|(\w+)\s+table)\b"
        r"(.*)$",
        text, re.I,
    )
    if m:
        tname = (m.group(1) or m.group(2)).lower()
        rest  = m.group(3) or ""

        clean = re.sub(
            r"\b(?:and\s+)?(?:it\s+|which\s+|that\s+)?(?:must|should|needs?\s+to)?\s*"
            r"(?:include|inclued|includ|contain|contains|containing|have|has|having|"
            r"consist\s+of|composed\s+of)\b",
            " ", rest, flags=re.I,
        )
        clean = re.sub(r"\b(?:with|having|containing|including|includes?|inclued|includ|contain)\b", " ", clean, flags=re.I)
        clean = re.sub(r"\b(?:columns?|fields?)\b", " ", clean, flags=re.I)
        clean = clean.replace(":", " ").replace("(", " ").replace(")", " ")
        clean = re.sub(r"\b(?:the|a|an)\b", " ", clean, flags=re.I)

        parts = re.split(r"[,;]|\band\b", clean)
        cols = []
        seen = set()

        for part in parts:
            toks = [w.strip("\"'`") for w in part.strip().split() if w.lower() not in ("as", "of", "type")]
            if not toks:
                continue
            if len(toks) > 1 and not any(t.lower() in TYPES for t in toks[1:]):
                for t in toks:
                    cname = t.lower()
                    if cname in seen or cname in ("table", "column", "columns", "it", "must", "that", "which"):
                        continue
                    seen.add(cname)
                    pk = " PRIMARY KEY" if cname == "id" else ""
                    cols.append(f"{cname} {guess_type(cname)}{pk}")
            else:
                cname = toks[0].lower()
                if cname in seen or cname in ("table", "column", "columns", "it", "must", "that", "which"):
                    continue
                seen.add(cname)
                ctype = None
                if len(toks) > 1 and toks[-1].lower() in TYPES:
                    ctype = TYPES[toks[-1].lower()]
                elif len(toks) > 1 and toks[1].lower() in TYPES:
                    ctype = TYPES[toks[1].lower()]
                else:
                    ctype = guess_type(cname)
                pk = " PRIMARY KEY" if cname == "id" else ""
                cols.append(f"{cname} {ctype}{pk}")

        if not any(c.startswith("id ") for c in cols):
            cols.insert(0, "id INTEGER PRIMARY KEY")
        return (f"CREATE TABLE {tname} (\n  " + ",\n  ".join(cols) + "\n);",
                f"Create table {tname}")

    m = re.search(r"\b(?:drop|delete|remove)\s+(?:the\s+)?table\s+(\w+)", text, re.I)
    if m:
        t = tbl(m.group(1))
        return f"DROP TABLE {t};", f"Drop table {t}"

    m = re.search(r"\b(?:truncate|empty|clear|wipe)\s+(?:the\s+)?(?:table\s+)?(\w+)", text, re.I)
    if m and tbl(m.group(1)) in schema:
        t   = tbl(m.group(1))
        sql = f"DELETE FROM {t};" if dialect == "SQLite" else f"TRUNCATE TABLE {t};"
        return sql, f"Remove all rows from {t}"

    m = re.search(
        r"\b(add|drop|remove|delete)\s+(?:an?\s+|the\s+|new\s+)?(?:column|field)\s+(\w+)"
        r"(?:\s+(?:as\s+|of\s+type\s+)?(\w+))?\s+(?:to|from|in|on)\s+"
        r"(?:the\s+)?(?:table\s+)?(\w+)",
        text, re.I,
    )
    if m:
        t, c = tbl(m.group(4)), m.group(2).lower()
        if m.group(1).lower() == "add":
            ty = TYPES.get((m.group(3) or "").lower()) or guess_type(c)
            return f"ALTER TABLE {t} ADD COLUMN {c} {ty};", f"Add column {c} to {t}"
        return f"ALTER TABLE {t} DROP COLUMN {c};", f"Drop column {c} from {t}"

    return None


# ---------------------------------------------------------------- Temporal intents
INC_WORDS = {"increase", "increased", "increases", "higher", "greater", "more", "grew", "grown", "rise", "risen", "rose", "hike", "hiked", "hikes"}
DEC_WORDS = {"decrease", "decreased", "decreases", "lower", "less", "fewer", "dropped", "drop", "fell", "fall", "reduced", "reduction"}

def temporal_intent(text, primary, cols):
    regex = (
        r"\b(increase[ds]?|decrease[ds]?|higher|lower|greater|less|more|fewer|"
        r"gr(?:ew|own?)|dropp?(?:ed)?|fell|fall|ris(?:en?|e)|rose|hik(?:ed?|es?)|"
        r"reduc(?:ed?|tion)|chang(?:ed?))\b.*?"
        r"\b(?:from|than|to|over|vs\.?|versus|compared\s+to|compared\s+with)\b.*?"
        r"\b(?:the\s+)?(?:previous|last|prior|past|preceding)\s+(\w+)"
    )
    m = re.search(regex, text, re.I)
    if not m:
        return None

    word = m.group(1).lower()
    time_unit = m.group(2).lower()
    op = ">" if word in INC_WORDS else ("<" if word in DEC_WORDS else "!=")

    # 1. Temporal column
    time_col = None
    for c in cols:
        if c.lower() == time_unit or c.lower().startswith(time_unit) or time_unit in c.lower():
            time_col = c
            break
    if not time_col:
        for c in cols:
            if re.search(r"date|month|year|day|time|period|at$", c, re.I):
                time_col = c
                break
    if not time_col:
        return None

    # 2. Metric column (numeric and not the time_col)
    metric = None
    for c, typ in cols.items():
        if c != time_col and re.search(r"\b" + re.escape(c) + r"\b", text, re.I):
            metric = c
            break
    if not metric:
        for c, typ in cols.items():
            if c != time_col:
                for syn in SYN.get(c, []):
                    if re.search(r"\b" + re.escape(syn) + r"\b", text, re.I):
                        metric = c
                        break
            if metric:
                break
    if not metric:
        for c, typ in cols.items():
            if c != "id" and c != time_col and is_num(typ):
                metric = c
                break
    if not metric:
        return None

    # 3. Partition column
    part_col = None
    for c in ("id", f"{primary}_id", f"{sing(primary)}_id", "emp_id", "employee_id", "user_id", "subscriber_id", "name"):
        if c in cols and c != time_col and c != metric:
            part_col = c
            break

    over_parts = []
    if part_col:
        over_parts.append(f"PARTITION BY {part_col}")
    over_parts.append(f"ORDER BY {time_col}")
    over_clause = " ".join(over_parts)

    direction_desc = "increased" if op == ">" else ("decreased" if op == "<" else "changed")
    sql = (
        f"WITH history AS (\n"
        f"  SELECT *,\n"
        f"    LAG({metric}) OVER ({over_clause}) AS prev_{metric}\n"
        f"  FROM {primary}\n"
        f")\n"
        f"SELECT *\n"
        f"FROM history\n"
        f"WHERE prev_{metric} IS NOT NULL\n"
        f"  AND {metric} {op} prev_{metric};"
    )
    explanation = f"Find rows where {metric} {direction_desc} compared to previous {time_col} using LAG() window function"
    breakdown = {
        "intent": "SELECT",
        "primary_table": primary,
        "filters": 1,
        "order_by": time_col,
        "group_by": f"PARTITION BY {part_col}" if part_col else None,
    }
    return {"sql": sql, "explanation": explanation, "breakdown": breakdown}


# ------------------------------------------------------------------ main
INTENTS = [
    ("DELETE", r"\b(delete|remove|erase|get rid of)\b"),
    ("SELECT", r"\b(show|list|get|find|display|fetch|select|give|print|what|which|who"
               r"|count|how many|number of|average|avg|mean|total|sum|maximum|max"
               r"|minimum|min|highest|lowest|oldest|youngest|cheapest|most|least)\b"),
    ("UPDATE", r"\b(update|change|modify|set|rename|make|edit)\b"),
    ("INSERT", r"\b(add|insert|create|register|enroll|new)\b"),
]


def translate(sentence, ddl="", dialect="PostgreSQL"):
    schema = parse_schema(ddl or SAMPLE_SCHEMA)
    text, quotes = prepare(sentence)
    if not text.strip():
        return {"sql": "-- Say or type something first.", "explanation": "", "breakdown": {}}

    r = ddl_intent(text, schema, dialect)
    if r:
        return {"sql": r[0], "explanation": r[1], "breakdown": {"intent": "DDL"}}

    # Intent detection
    best   = [(m.start(), i, n)
              for i, (n, p) in enumerate(INTENTS)
              for m in [re.search(p, text, re.I)] if m]
    intent = min(best)[2] if best else "SELECT"

    # Table resolution — scored
    scores = {}
    for t, meta in schema.items():
        tal   = alt(sorted(table_aliases(t), key=len, reverse=True))
        s, first = 0, 9999
        m = re.search(rf"\b(?:{tal})\b", text, re.I)
        if m: s += 2; first = m.start()
        if re.search(
            rf"\b(?:from|into|in|to|of|on)\s+(?:the\s+)?(?:{tal})\b(?!\s+\d)", text, re.I
        ):
            s += 3
        for c in meta["cols"]:
            if c != "id" and re.search(rf"\b(?:{alt(col_aliases(c))})\b", text, re.I):
                s += 1.5
        # Bonus: a STATUS_WORD targets this table's column
        for sw, (sc, _) in STATUS_WORDS.items():
            if sc in meta["cols"] and re.search(rf"\b{re.escape(sw)}\b", text, re.I):
                s += 1
        scores[t] = (s, -first)

    ranked = sorted(schema, key=lambda t: scores[t], reverse=True)
    if not ranked or scores[ranked[0]][0] < 1:
        if len(schema) == 1:
            primary = list(schema.keys())[0]
        else:
            return {
                "sql": (f"-- I couldn't find a table in your request."
                        f" Known tables: {', '.join(schema)}."),
                "explanation": "Mention a table name, or update the Schema box.",
                "breakdown": {},
            }
    else:
        primary = ranked[0]

    if intent == "SELECT":
        t_res = temporal_intent(text, primary, schema[primary]["cols"])
        if t_res:
            return t_res

    others = [t for t in ranked[1:] if scores[t][0] > 0] if intent == "SELECT" else []
    scope  = [primary] + others
    cands  = [(t, c, col_aliases(c)) for t in scope for c in schema[t]["cols"]]
    allwords = (
        {a.lower() for t in schema for a in table_aliases(t)}
        | {a for _, _, al in cands for a in al}
    )
    tword  = {a.lower() for t in schema for a in table_aliases(t)}
    work   = text
    tal    = alt(sorted(table_aliases(primary), key=len, reverse=True))

    # ID shortcut: "subscriber 5"
    idm = (
        re.search(rf"\b(?:{tal})\s+(\d+)\b", work, re.I)
        if "id" in schema[primary]["cols"] else None
    )
    pre = []
    if idm:
        pre.append((idm.start(), idm.end(), primary, "id", f"= {idm.group(1)}"))
        work = blank(work, idm.start(), idm.end())

    # Inline status/plan adjective filters ("active subscribers", "premium plan", …)
    # Only for SELECT/DELETE — in INSERT/UPDATE the status words are column values, not filters.
    # Search ALL schema tables so cross-table filters trigger JOINs.
    _taken = [(a, b) for a, b, *_ in pre]
    status_conds = []
    if intent in ("SELECT", "DELETE"):
        for sw, (sc, sv) in STATUS_WORDS.items():
            m = re.search(rf"\b{re.escape(sw)}\b", work, re.I)
            if not m: continue
            # Prefer tables in scope; fall back to any table in schema
            search_order = scope + [t for t in schema if t not in scope]
            for t in search_order:
                if sc not in schema[t]["cols"]: continue
                if all(m.end() <= x or m.start() >= y for x, y in _taken):
                    status_conds.append((m.start(), m.end(), t, sc, f"= '{sv}'"))
                    _taken.append((m.start(), m.end()))
                    work = blank(work, m.start(), m.end())
                    # If the matched table isn't in scope, add it so it gets JOINed
                    if t not in scope:
                        scope.append(t)
                        cands.extend((t, c, col_aliases(c)) for c in schema[t]["cols"])
                        allwords.update(a for _, _, al in cands for a in al)
                    break

    # ---- INSERT --------------------------------------------------------
    if intent == "INSERT":
        pc    = [x for x in cands if x[0] == primary]
        conds = [c for c in extract(work, pc, schema, quotes, allwords)
                 if c[4].startswith("= ")] + pre
        if not conds:
            return {"sql": "-- INSERT needs values, e.g. add subscriber with name John and age 25.",
                    "explanation": "", "breakdown": {}}
        conds.sort(key=lambda x: x[0])
        return {
            "sql": (f"INSERT INTO {primary} ({', '.join(c[3] for c in conds)})"
                    f" VALUES ({', '.join(c[4][2:] for c in conds)});"),
            "explanation": f"Insert a row into {primary}",
            "breakdown":   {"intent": "INSERT", "primary_table": primary},
        }

    # ---- UPDATE --------------------------------------------------------
    if intent == "UPDATE":
        assigns, cond_text = [], work
        for c in schema[primary]["cols"]:
            m = re.search(
                rf"\b(?:change|update|set|make|edit)\s+(?:the\s+)?(?:{alt(col_aliases(c))})"
                rf"\s+(?:of|for)\s+(.+?)\s+(?:to|as|=)\s+({VAL})\s*$",
                work, re.I,
            )
            if m: assigns.append((c, m.group(2))); cond_text = m.group(1); break
        if not assigns:
            for c in schema[primary]["cols"]:
                for m in re.finditer(
                    rf"\b(?:{alt(col_aliases(c))})\s+(?:to|as|=)\s+({VAL})", work, re.I
                ):
                    assigns.append((c, m.group(1)))
                    work = blank(work, m.start(), m.end())
            cond_text = work
        if not assigns:
            return {"sql": "-- UPDATE needs something like: set status to cancelled where subscriber 13.",
                    "explanation": "", "breakdown": {}}
        pc    = [x for x in cands if x[0] == primary]
        conds = extract(cond_text, pc, schema, quotes, allwords) or pre
        if not conds:
            return {"sql": "-- Refused: UPDATE without a condition would change every row.",
                    "explanation": "Add a condition such as 'where subscriber 13'.",
                    "breakdown": {}}
        sets = ", ".join(
            f"{c} = {fmt(v, schema[primary]['cols'].get(c), quotes)}" for c, v in assigns
        )
        return {
            "sql": f"UPDATE {primary} SET {sets} WHERE {build_where(conds, cond_text, False)};",
            "explanation": f"Update {primary}",
            "breakdown":   {"intent": "UPDATE", "primary_table": primary},
        }

    # ---- DELETE --------------------------------------------------------
    if intent == "DELETE":
        pc    = [x for x in cands if x[0] == primary]
        conds = extract(work, pc, schema, quotes, allwords) + pre + status_conds
        if not conds:
            return {"sql": "-- Refused: DELETE without a condition would wipe the whole table.",
                    "explanation": "Add a condition such as 'where status is cancelled'.",
                    "breakdown": {}}
        conds = _merge_or_conditions(conds, text)
        return {
            "sql": f"DELETE FROM {primary} WHERE {build_where(conds, text, False)};",
            "explanation": f"Delete matching rows from {primary}",
            "breakdown":   {"intent": "DELETE", "primary_table": primary,
                            "filters": len(conds)},
        }

    # ---- SELECT --------------------------------------------------------
    limit = None
    m = re.search(r"\b(top|first|limit)\s+(\d+)\b", work, re.I)
    topword = bool(m and m.group(1).lower() != "limit")
    if m: limit = int(m.group(2)); work = blank(work, m.start(), m.end())

    # Superlative adjective → ORDER BY + LIMIT ("youngest 5 subscribers")
    ord_col, ord_desc = None, False
    for adj_phrase, (col, desc) in sorted(ORD_ADJ.items(), key=lambda x: -len(x[0])):
        m = re.search(rf"\b{re.escape(adj_phrase)}\b", work, re.I)
        if not m: continue
        # Resolve col=None to first numeric column in primary table
        resolved = col
        if resolved is None:
            for c, ctype in schema[primary]["cols"].items():
                if c != "id" and is_num(ctype): resolved = c; break
        if resolved is None: continue
        # Verify column exists somewhere in scope
        if not any(c == resolved for t in scope for c in schema[t]["cols"]): continue
        ord_col, ord_desc = resolved, desc
        # Look for inline number: "youngest 5"
        nm = re.search(r"\b(\d+)\b", work[m.end():m.end() + 15])
        if nm and not limit:
            limit = int(nm.group(1))
            work  = blank(work, m.end() + nm.start(), m.end() + nm.end())
        elif not limit:
            limit = 1
        work = blank(work, m.start(), m.end())
        break

    distinct = bool(re.search(r"\b(distinct|unique)\b", work, re.I))

    def locate(prefix):
        for t, c, al in cands:
            m = re.search(
                rf"\b(?:{prefix})\s+(?:of\s+)?(?:the\s+)?\b(?:{alt(al)})\b", work, re.I
            )
            if m: return t, c, m

    # Aggregate detection
    aggs = []
    m = re.search(r"\b(?:total\s+)?(?:number\s+of|count|how\s+many)\b", work, re.I)
    if m: aggs.append(("COUNT", None, None, "total_count")); work = blank(work, m.start(), m.end())
    for fn, pre_re in [
        ("AVG", r"average|avg|mean"),
        ("SUM", r"total|sum"),
        ("MAX", r"maximum|max|highest|largest|biggest"),
        ("MIN", r"minimum|min|lowest|smallest"),
    ]:
        r2 = locate(pre_re)
        if r2:
            t, c, m = r2
            aggs.append((fn, t, c, f"{fn.lower()}_{c}"))
            work = blank(work, m.start(), m.end())

    # "total revenue / income / earnings" → SUM on first numeric column
    m_rev = re.search(
        r"\b(?:total\s+)?(?:revenue|income|earnings|turnover|spend|spending)\b", text, re.I
    )
    if m_rev and not any(a[0] == "SUM" for a in aggs):
        for t in scope:
            for c, ctype in schema[t]["cols"].items():
                if is_num(ctype) and c != "id":
                    aggs.append(("SUM", t, c, f"total_{c}"))
                    break
            if any(a[0] == "SUM" for a in aggs): break

    group = order = None
    desc2 = False
    nt = lambda al: alt([a for a in al if a.lower() not in tword] or ["\x00"])

    # GROUP BY
    for t, c, al in cands:
        m = re.search(
            rf"\b(?:per|for\s+each|grouped\s+by|group\s+by|by\s+each|each)"
            rf"\s+(?:the\s+)?\b(?:{nt(al)})\b",
            work, re.I,
        )
        if m and not group: group = (t, c); work = blank(work, m.start(), m.end())

    # ORDER BY from text; if ord_col already set via ORD_ADJ, skip text search
    if not ord_col:
        for t, c, al in cands:
            m = re.search(
                rf"\b(?:sorted|ordered|order|sort|ranked|rank)\s+by\s+(?:the\s+)?"
                rf"\b(?:{nt(al)})\b"
                rf"(?:\s+(desc\w*|asc\w*|highest|lowest|high\s+to\s+low|low\s+to\s+high))?",
                work, re.I,
            )
            if m and not order:
                order = (t, c)
                desc2 = bool(m.group(1) and re.match(r"desc|highest|high", m.group(1), re.I))
                work  = blank(work, m.start(), m.end())
        for t, c, al in cands:
            m = re.search(rf"\bby\s+(?:the\s+)?\b(?:{nt(al)})\b", work, re.I)
            if m:
                if aggs and not group:   group = (t, c)
                elif not order:          order = (t, c); desc2 = topword
                work = blank(work, m.start(), m.end()); break
    else:
        # Apply ORDER from ORD_ADJ
        for t in scope:
            if ord_col in schema[t]["cols"]:
                order = (t, ord_col); desc2 = ord_desc; break

    # Conditions (runs after all blanking)
    conds  = extract(work, cands, schema, quotes, allwords) + pre + status_conds
    conds  = _merge_or_conditions(conds, text)
    for a, b, *_ in conds: work = blank(work, a, b)

    # HAVING detection — also triggers GROUP BY if not already set
    having = None
    hm = re.search(
        r"\b(?:having|with)\s+(?:more\s+than|greater\s+than|over|above|at\s+least)"
        r"\s+(\d+)\b", text, re.I,
    )
    if hm: having = f"COUNT(*) > {hm.group(1)}"
    else:
        hm = re.search(
            r"\b(?:fewer\s+than|less\s+than|under|below|at\s+most)\s+(\d+)\b", text, re.I,
        )
        if hm and group: having = f"COUNT(*) < {hm.group(1)}"
    # If a HAVING pattern was found but GROUP BY not yet set, infer GROUP BY
    if having and not group:
        for t, c, al in cands:
            # Look for a column in the sentence that makes sense as the grouping key
            if t == primary and c != "id" and not is_num(schema[t]["cols"].get(c, "")):
                if re.search(rf"\b(?:{alt(al)})\b", text, re.I):
                    group = (t, c); break

    # Column selection
    sel = []
    if not aggs:
        m = re.search(
            r"\b(?:show|list|get|find|display|fetch|select|give|print)\b(?:\s+me)?\s+(.*?)"
            r"(?=\s+(?:from|of|in|where|with|whose|for|who|that|older|younger"
            r"|sorted|ordered)\b|$)",
            work, re.I,
        )
        phrase = m.group(1) if m else ""
        found  = []
        for t, c, al in cands:
            safe_al = [a for a in al if a.lower() not in tword]
            if not safe_al: continue
            for mm in re.finditer(rf"\b(?:{alt(safe_al)})\b", phrase, re.I):
                found.append((mm.start(), t, c))
        seen = set()
        sel  = [(t, c) for _, t, c in sorted(found)
                if not ((t, c) in seen or seen.add((t, c)))]

    if group and not aggs:               aggs.append(("COUNT", None, None, "total_count"))
    if aggs and limit and not group:
        for t, c, al in cands:
            if t == primary and c != "id" and re.search(rf"\b(?:{nt(al)})\b", work, re.I):
                group = (t, c); break
    if aggs and group and limit and not order: order = ("AGG", aggs[0][3]); desc2 = True

    # Build table set + JOINs
    used = (
        {t for _, _, t, _, _ in conds}
        | {t for t, _ in sel}
        | {a[1] for a in aggs if a[1]}
        | ({group[0]} if group else set())
        | ({order[0]} if order and order[0] != "AGG" else set())
    )
    used.add(primary)
    jn, bad = joins(primary, used, schema)
    if jn is None:
        return {
            "sql": (f"-- No relationship between {primary} and {bad}"
                    f" in the schema (add a REFERENCES clause)."),
            "explanation": "",
            "breakdown":   {},
        }

    q   = len(used) > 1
    ref = lambda t, c: f"{t}.{c}" if q else c
    cols = []
    if group:
        cols.append(ref(*group))
    for fn, t, c, alias in aggs:
        cols.append(f"{fn}({ref(t, c) if c else '*'}) AS {alias}")
    if not aggs:
        cols = [ref(t, c) for t, c in sel] or ["*"]

    sql  = "SELECT " + ("DISTINCT " if distinct else "")
    sql += (f"TOP {limit} " if limit and dialect == "SQL Server" else "")
    sql += ", ".join(cols)
    sql += f" FROM {primary}" + "".join(" " + j for j in jn)
    if conds:   sql += " WHERE "    + build_where(conds, text, q)
    if group and aggs: sql += f" GROUP BY {ref(*group)}"
    if having:  sql += f" HAVING {having}"
    if order:   sql += " ORDER BY " + (order[1] if order[0] == "AGG" else ref(*order)) + (" DESC" if desc2 else "")
    if limit and dialect == "Oracle":     sql += f" FETCH FIRST {limit} ROWS ONLY"
    elif limit and dialect != "SQL Server": sql += f" LIMIT {limit}"

    joined_tables = sorted(used - {primary})
    info = (f"Intent SELECT · table {primary}"
            + (f" · joined {', '.join(joined_tables)}" if joined_tables else "")
            + (f" · {len(conds)} filter(s)"           if conds          else "")
            + (f" · grouped by {group[1]}"             if group          else "")
            + (f" · having {having}"                   if having         else ""))

    return {
        "sql":         sql + ";",
        "explanation": info,
        "breakdown": {
            "intent":        "SELECT",
            "primary_table": primary,
            "joined_tables": joined_tables,
            "filters":       len(conds),
            "group_by":      group[1] if group else None,
            "having":        having,
            "order_by":      (order[1] if order[0] == "AGG" else ref(*order)) if order else None,
            "order_dir":     "DESC" if desc2 else ("ASC" if order else None),
            "limit":         limit,
            "distinct":      distinct,
        },
    }


if __name__ == "__main__":
    import sys
    print(translate(" ".join(sys.argv[1:]))["sql"])
