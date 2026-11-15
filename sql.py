"""
轻量级关系型数据库内核与可视化工作台 (SQL Engine Prototype)
核心架构: 词法语法解析 -> 语义绑定与类型规整 -> 查询计划树 -> 执行器
底层存储: 4096 字节定长页堆文件 (Slotted-page) + 内存/磁盘 B+ 树索引
前端界面: Tkinter 现代化响应式可视化交互台
"""

import os
import sys
import re
import json
import time
import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import cmp_to_key
from bisect import bisect_left, bisect_right
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

# =====================================================================
# 1. 异常体系
# =====================================================================

class SQLError(Exception):
    code = "SQLError"
    def __init__(self, msg):
        self.msg = msg
        super().__init__(msg)
    def __str__(self):
        return f"[{self.code}] {self.msg}"

class SQLSyntaxError(SQLError): code = "SyntaxError"
class SQLSemanticError(SQLError): code = "SemanticError"
class SQLConstraintError(SQLError): code = "ConstraintError"
class SQLExecutionError(SQLError): code = "ExecutionError"

# =====================================================================
# 2. 词法分析 (Lexer)
# =====================================================================

OPERATORS = ["<=", ">=", "<>", "!=", "!>", "!<", "=", ">", "<", "+", "-", "*", "/", "%"]
FULLWIDTH_MAP = {"（": "(", "）": ")", "，": ",", "；": ";", "：": ":"}

class Token:
    __slots__ = ("kind", "value", "text")
    def __init__(self, kind, value, text):
        self.kind = kind
        self.value = value
        self.text = text
    def __repr__(self):
        return f"Token({self.kind}, {self.value!r})"

def tokenize(sql):
    tokens = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch.isspace() or ch == "\u3000":
            i += 1
            continue
        if ch in FULLWIDTH_MAP:
            tokens.append(Token("punct", FULLWIDTH_MAP[ch], ch))
            i += 1
            continue
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            if j < 0: raise SQLSyntaxError("未闭合的块注释")
            i = j + 2
            continue
        if ch in ("'", "\u2018"):
            val, i = _read_string(sql, i, "'" if ch == "'" else "\u2019")
            tokens.append(Token("str", val, val))
            continue
        if ch in ('"', "\u201c"):
            val, i = _read_string(sql, i, '"' if ch == '"' else "\u201d")
            tokens.append(Token("qid", val, val))
            continue
        if ch.isdigit():
            j = i
            while j < n and (sql[j].isdigit() or sql[j] == "."): j += 1
            text = sql[i:j]
            tokens.append(Token("num", float(text) if "." in text else int(text), text))
            i = j
            continue
        if ch.isalpha() or ch == "_" or ord(ch) > 127:
            j = i
            while j < n and (sql[j].isalnum() or sql[j] in "_$" or ord(sql[j]) > 127): j += 1
            text = sql[i:j]
            tokens.append(Token("word", text.lower(), text))
            i = j
            continue
        for op in OPERATORS:
            if sql.startswith(op, i):
                tokens.append(Token("op", op, op))
                i += len(op)
                break
        else:
            if ch in "(),.;":
                tokens.append(Token("punct", ch, ch))
                i += 1
            else:
                raise SQLSyntaxError(f"未知字符: {ch!r}")
    tokens.append(Token("eof", "", ""))
    return tokens

def _read_string(sql, start, close_ch):
    i = start + 1
    buf = []
    while i < len(sql):
        ch = sql[i]
        if ch == close_ch:
            if i + 1 < len(sql) and sql[i + 1] == close_ch:
                buf.append(close_ch)
                i += 2
                continue
            return "".join(buf), i + 1
        buf.append(ch)
        i += 1
    raise SQLSyntaxError("未闭合的字符串常量")

# =====================================================================
# 3. 类型系统与三值逻辑
# =====================================================================

NULL = None
INT_KINDS = {"smallint", "int"}
NUM_KINDS = ("numeric", "decimal")

class SQLType:
    __slots__ = ("kind", "length", "precision", "scale")
    def __init__(self, kind, length=None, precision=None, scale=None):
        self.kind = kind
        self.length = length
        self.precision = precision
        self.scale = scale
    def __str__(self):
        if self.kind in ("char", "varchar"):
            return f"{self.kind}({self.length})" if self.length else self.kind
        if self.kind == "numeric":
            return f"numeric({self.precision},{self.scale})"
        return self.kind
    def to_json(self):
        return {"kind": self.kind, "length": self.length, "precision": self.precision, "scale": self.scale}
    @staticmethod
    def from_json(d):
        return SQLType(d["kind"], d.get("length"), d.get("precision"), d.get("scale"))

def parse_type(name, args=None):
    args = list(args or [])
    if name in ("char", "character"): return SQLType("char", int(args[0]) if args else 1)
    if name in ("varchar", "character varying"): return SQLType("varchar", int(args[0]) if args else 255)
    if name in INT_KINDS or name == "bigint": return SQLType("int" if name == "bigint" else name)
    if name in NUM_KINDS:
        return SQLType("numeric", precision=int(args[0]) if args else 10, scale=int(args[1]) if len(args) > 1 else 0)
    if name == "date": return SQLType("date")
    raise SQLSyntaxError(f"不支持的字段类型: {name}")

def _as_decimal(v):
    if isinstance(v, bool): return Decimal(1 if v else 0)
    if isinstance(v, Decimal): return v
    if isinstance(v, (int, float)): return Decimal(str(v))
    if isinstance(v, str):
        s = v.strip()
        if not s: return None
        try: return Decimal(s)
        except InvalidOperation: return None
    return None

def coerce(val, t):
    if val is NULL or t is None: return val
    k = t.kind
    if k in ("char", "varchar"):
        s = val if isinstance(val, str) else format_value(val)
        if t.length and len(s) > t.length:
            raise SQLSemanticError(f"字符串长度超出限制 ({len(s)} > {t.length})")
        return s
    if k in INT_KINDS:
        d = _as_decimal(val)
        if d is None or d != d.to_integral_value():
            raise SQLSemanticError(f"无法将 {val!r} 转换为整数")
        return int(d)
    if k == "numeric":
        d = _as_decimal(val)
        if d is None: raise SQLSemanticError(f"无法将 {val!r} 转换为数值")
        return d.quantize(Decimal(1).scaleb(-(t.scale or 0)), rounding=ROUND_HALF_UP)
    if k == "date":
        if isinstance(val, datetime.date): return val
        try: return datetime.date.fromisoformat(str(val).strip())
        except (ValueError, TypeError): raise SQLSemanticError(f"非法日期格式: {val!r}")
    return val

def compare(a, b):
    if a is NULL or b is NULL: return None
    if isinstance(a, str) and isinstance(b, str): return (a > b) - (a < b)
    if isinstance(a, datetime.date) or isinstance(b, datetime.date): return (a > b) - (a < b)
    da, db = _as_decimal(a), _as_decimal(b)
    if da is not None and db is not None: return (da > db) - (da < db)
    sa, sb = format_value(a), format_value(b)
    return (sa > sb) - (sa < sb)

def truth(v):
    if v is NULL: return None
    if isinstance(v, (int, float, Decimal)): return v != 0
    if isinstance(v, str): return True
    return bool(v)

def sql_not(v):
    t = truth(v)
    return NULL if t is None else (not t)

def sql_and(a, b):
    ta, tb = truth(a), truth(b)
    if ta is False or tb is False: return False
    if ta is None or tb is None: return None
    return True

def sql_or(a, b):
    ta, tb = truth(a), truth(b)
    if ta is True or tb is True: return True
    if ta is None or tb is None: return None
    return False

def compare_for_sort(a, b, desc=False):
    if a is NULL and b is NULL: return 0
    if a is NULL: return -1 if desc else 1
    if b is NULL: return 1 if desc else -1
    c = compare(a, b) or 0
    return -c if desc else c

def encode_value(v):
    if v is NULL: return "N"
    if isinstance(v, bool): return "B:1" if v else "B:0"
    if isinstance(v, int): return f"I:{v}"
    if isinstance(v, Decimal): return f"D:{v}"
    if isinstance(v, datetime.date): return f"T:{v.isoformat()}"
    return f"S:{v}"

def decode_value(s):
    tag, _, rest = s.partition(":")
    if tag == "N": return NULL
    if tag == "I": return int(rest)
    if tag == "D": return Decimal(rest)
    if tag == "B": return rest == "1"
    if tag == "T": return datetime.date.fromisoformat(rest)
    return rest

def format_value(v):
    if v is NULL: return "NULL"
    if isinstance(v, bool): return "TRUE" if v else "FALSE"
    if isinstance(v, Decimal): return format(v, "f")
    if isinstance(v, datetime.date): return v.isoformat()
    return str(v)

# =====================================================================
# 4. 页式存储管理与堆文件 (Page & HeapFile)
# =====================================================================

PAGE_SIZE = 4096
HEADER_SIZE = 4
SLOT_SIZE = 4

class Page:
    __slots__ = ("page_no", "n_slots", "free_end", "slots", "data")
    def __init__(self, page_no, raw=None):
        self.page_no = page_no
        self.data = bytearray(PAGE_SIZE)
        if raw is None:
            self.n_slots = 0
            self.free_end = PAGE_SIZE
            self.slots = []
        else:
            self.data[:] = raw
            self.n_slots = int.from_bytes(raw[0:2], "little")
            self.free_end = int.from_bytes(raw[2:4], "little")
            self.slots = []
            for i in range(self.n_slots):
                b = HEADER_SIZE + i * SLOT_SIZE
                self.slots.append((int.from_bytes(raw[b:b+2], "little"), int.from_bytes(raw[b+2:b+4], "little")))
    def free_space(self):
        return self.free_end - HEADER_SIZE - SLOT_SIZE * self.n_slots
    def can_hold(self, size):
        return size + SLOT_SIZE <= self.free_space()
    def _write_header(self):
        self.data[0:2] = self.n_slots.to_bytes(2, "little")
        self.data[2:4] = self.free_end.to_bytes(2, "little")
        for i, (off, ln) in enumerate(self.slots):
            b = HEADER_SIZE + i * SLOT_SIZE
            self.data[b:b+2] = off.to_bytes(2, "little")
            self.data[b+2:b+4] = ln.to_bytes(2, "little")
    def compact(self):
        live = [(i, off, ln) for i, (off, ln) in enumerate(self.slots) if ln > 0]
        new_slots = [(0, 0)] * self.n_slots
        cursor = PAGE_SIZE
        buf = bytearray()
        for i, off, ln in live:
            buf = bytes(self.data[off:off+ln]) + buf
            cursor -= ln
            new_slots[i] = (cursor, ln)
        self.data = bytearray(PAGE_SIZE)
        if buf: self.data[cursor:PAGE_SIZE] = buf
        self.slots = new_slots
        self.free_end = cursor
        self._write_header()
    def insert(self, blob):
        if not self.can_hold(len(blob)):
            self.compact()
            if not self.can_hold(len(blob)): return None
        self.free_end -= len(blob)
        self.data[self.free_end:self.free_end + len(blob)] = blob
        self.slots.append((self.free_end, len(blob)))
        self.n_slots = len(self.slots)
        self._write_header()
        return self.n_slots - 1
    def get(self, slot):
        off, ln = self.slots[slot]
        return bytes(self.data[off:off+ln]) if ln else None
    def delete(self, slot):
        self.slots[slot] = (0, 0)
        self._write_header()
    def update(self, slot, blob):
        off, ln = self.slots[slot]
        if ln and len(blob) <= ln:
            self.data[off:off + len(blob)] = blob
            self.slots[slot] = (off, len(blob))
            self._write_header()
            return True
        self.slots[slot] = (0, 0)
        if not self.can_hold(len(blob)): self.compact()
        if not self.can_hold(len(blob)): return False
        self.free_end -= len(blob)
        self.data[self.free_end:self.free_end + len(blob)] = blob
        self.slots[slot] = (self.free_end, len(blob))
        self._write_header()
        return True
    def iter_records(self):
        for slot, (off, ln) in enumerate(self.slots):
            if ln > 0: yield slot, bytes(self.data[off:off+ln])
    def to_bytes(self):
        self._write_header()
        return bytes(self.data)

class HeapFile:
    def __init__(self, path, max_pages=256):
        self.path = Path(path)
        self.max_pages = max_pages
        self.pages = {}
        self.dirty = set()
        self.page_count = (self.path.stat().st_size // PAGE_SIZE) if self.path.exists() else 0
    def _load_page(self, page_no):
        if page_no not in self.pages:
            p = Page(page_no)
            if page_no < self.page_count:
                with open(self.path, "rb") as f:
                    f.seek(page_no * PAGE_SIZE)
                    raw = f.read(PAGE_SIZE)
                if len(raw) == PAGE_SIZE: p = Page(page_no, raw)
            self.pages[page_no] = p
        return self.pages[page_no]
    def _write_page(self, page):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "r+b" if self.path.exists() else "w+b") as f:
            if page.page_no >= self.page_count: self.page_count = page.page_no + 1
            f.seek(page.page_no * PAGE_SIZE)
            f.write(page.to_bytes())
            need = self.page_count * PAGE_SIZE
            if f.seek(0, os.SEEK_END) < need: f.write(b"\x00" * (need - f.tell()))
        self.dirty.discard(page.page_no)
    def _evict(self):
        while len(self.pages) > self.max_pages:
            victim = next((no for no in self.pages if no not in self.dirty), None)
            if victim is None:
                victim = next(iter(self.pages))
                self._write_page(self.pages[victim])
            self.pages.pop(victim, None)
    def insert(self, values):
        blob = _encode_row(values)
        for page_no in range(self.page_count - 1, -1, -1):
            slot = self._load_page(page_no).insert(blob)
            if slot is not None:
                self.dirty.add(page_no)
                return (page_no, slot)
        page = self._load_page(self.page_count)
        self.page_count += 1
        slot = page.insert(blob)
        if slot is None: raise SQLExecutionError("单行数据体积超过单页上限")
        self.dirty.add(page.page_no)
        self._evict()
        return (page.page_no, slot)
    def get(self, rid):
        raw = self._load_page(rid[0]).get(rid[1])
        return None if raw is None else _decode_row(raw)
    def update(self, rid, values):
        page = self._load_page(rid[0])
        if not page.update(rid[1], _encode_row(values)):
            raise SQLExecutionError("当前页空间不足，无法原地更新行")
        self.dirty.add(rid[0])
    def delete(self, rid):
        self._load_page(rid[0]).delete(rid[1])
        self.dirty.add(rid[0])
    def scan(self):
        for page_no in range(self.page_count):
            for slot, raw in self._load_page(page_no).iter_records():
                yield (page_no, slot), _decode_row(raw)
    def flush(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for page_no in list(self.dirty):
            p = self.pages.get(page_no)
            if p: self._write_page(p)
        if not self.path.exists(): self.path.write_bytes(b"")
        with open(self.path, "r+b") as f:
            need = max(self.page_count, 1) * PAGE_SIZE
            if f.seek(0, os.SEEK_END) < need: f.write(b"\x00" * (need - f.tell()))

def _encode_row(values):
    return json.dumps([encode_value(v) for v in values], ensure_ascii=False).encode("utf-8")
def _decode_row(raw):
    return [decode_value(s) for s in json.loads(raw.decode("utf-8"))]

# =====================================================================
# 5. B+ 树索引结构
# =====================================================================

class _Leaf:
    __slots__ = ("keys", "rids", "next")
    def __init__(self):
        self.keys = []
        self.rids = []
        self.next = None

class _Inner:
    __slots__ = ("keys", "children")
    def __init__(self):
        self.keys = []
        self.children = []

class BPlusTree:
    def __init__(self, order=64):
        self.order = max(4, order)
        self.root = _Leaf()
    def _leaf_for(self, key):
        curr = self.root
        while isinstance(curr, _Inner):
            curr = curr.children[bisect_right(curr.keys, key)]
        return curr
    def search(self, key):
        leaf = self._leaf_for(key)
        idx = bisect_left(leaf.keys, key)
        return list(leaf.rids[idx]) if (idx < len(leaf.keys) and leaf.keys[idx] == key) else []
    def entries(self):
        curr = self.root
        while isinstance(curr, _Inner): curr = curr.children[0]
        while curr:
            for k, rids in zip(curr.keys, curr.rids): yield k, rids
            curr = curr.next
    def insert(self, key, rid):
        path, curr = [], self.root
        while isinstance(curr, _Inner):
            idx = bisect_right(curr.keys, key)
            path.append((curr, idx))
            curr = curr.children[idx]
        idx = bisect_left(curr.keys, key)
        if idx < len(curr.keys) and curr.keys[idx] == key:
            curr.rids[idx].append(rid)
            return
        curr.keys.insert(idx, key)
        curr.rids.insert(idx, [rid])
        if len(curr.keys) > self.order:
            self._split_leaf(curr, path)
    def _split_leaf(self, leaf, path):
        mid = len(leaf.keys) // 2
        right = _Leaf()
        right.keys, right.rids, right.next = leaf.keys[mid:], leaf.rids[mid:], leaf.next
        leaf.keys, leaf.rids, leaf.next = leaf.keys[:mid], leaf.rids[:mid], right
        if not path:
            self.root = _Inner()
            self.root.keys = [right.keys[0]]
            self.root.children = [leaf, right]
            return
        parent, idx = path.pop()
        parent.keys.insert(idx, right.keys[0])
        parent.children.insert(idx + 1, right)
        if len(parent.keys) > self.order: self._split_inner(parent, path)
    def _split_inner(self, node, path):
        mid = len(node.keys) // 2
        up = node.keys[mid]
        right = _Inner()
        right.keys, right.children = node.keys[mid + 1:], node.children[mid + 1:]
        node.keys, node.children = node.keys[:mid], node.children[:mid + 1]
        if not path:
            self.root = _Inner()
            self.root.keys = [up]
            self.root.children = [node, right]
            return
        parent, idx = path.pop()
        parent.keys.insert(idx, up)
        parent.children.insert(idx + 1, right)
        if len(parent.keys) > self.order: self._split_inner(parent, path)
    def delete(self, key, rid=None):
        leaf = self._leaf_for(key)
        idx = bisect_left(leaf.keys, key)
        if idx >= len(leaf.keys) or leaf.keys[idx] != key: return False
        if rid is None:
            leaf.keys.pop(idx)
            leaf.rids.pop(idx)
            return True
        rids = leaf.rids[idx]
        for i, it in enumerate(rids):
            if it == rid:
                rids.pop(i)
                if not rids:
                    leaf.keys.pop(idx)
                    leaf.rids.pop(idx)
                return True
        return False

# =====================================================================
# 6. 元数据字典 (Catalog)
# =====================================================================

class Column:
    __slots__ = ("name", "type", "not_null", "unique", "default_sql", "check_sql")
    def __init__(self, name, type_, not_null=False, unique=False, default_sql=None, check_sql=None):
        self.name = name
        self.type = type_
        self.not_null = not_null
        self.unique = unique
        self.default_sql = default_sql
        self.check_sql = check_sql
    def to_json(self):
        return {"name": self.name, "type": self.type.to_json(), "not_null": self.not_null,
                "unique": self.unique, "default": self.default_sql, "check": self.check_sql}
    @staticmethod
    def from_json(d):
        return Column(d["name"], SQLType.from_json(d["type"]), d.get("not_null", False),
                      d.get("unique", False), d.get("default"), d.get("check"))

class ForeignKey:
    __slots__ = ("cols", "ref_table", "ref_cols", "on_delete", "on_update")
    def __init__(self, cols, ref_table, ref_cols=None, on_delete="restrict", on_update="restrict"):
        self.cols = list(cols)
        self.ref_table = ref_table
        self.ref_cols = list(ref_cols) if ref_cols else None
        self.on_delete = on_delete
        self.on_update = on_update
    def to_json(self):
        return {"cols": self.cols, "ref_table": self.ref_table, "ref_cols": self.ref_cols,
                "on_delete": self.on_delete, "on_update": self.on_update}
    @staticmethod
    def from_json(d):
        return ForeignKey(d["cols"], d["ref_table"], d.get("ref_cols"),
                          d.get("on_delete", "restrict"), d.get("on_update", "restrict"))

class CheckConstraint:
    __slots__ = ("sql",)
    def __init__(self, sql): self.sql = sql
    def to_json(self): return {"sql": self.sql}
    @staticmethod
    def from_json(d): return CheckConstraint(d["sql"])

class TableMeta:
    def __init__(self, key, name, columns):
        self.key = key
        self.name = name
        self.columns = columns
        self.pk = []
        self.uniques = []
        self.fks = []
        self.checks = []
        self.heap_path = None
    def column_index(self, col_name):
        low = col_name.lower()
        for i, c in enumerate(self.columns):
            if c.name.lower() == low: return i
        return None
    def require_column(self, col_name):
        idx = self.column_index(col_name)
        if idx is None: raise SQLSemanticError(f"表 {self.name} 中不存在列 {col_name}")
        return idx
    @property
    def width(self): return len(self.columns)
    def unique_sets(self):
        res = [list(self.pk)] if self.pk else []
        res.extend([list(u) for u in self.uniques])
        for c in self.columns:
            if c.unique: res.append([c.name])
        return res
    def to_json(self):
        return {"key": self.key, "name": self.name, "columns": [c.to_json() for c in self.columns],
                "pk": self.pk, "uniques": self.uniques, "fks": [f.to_json() for f in self.fks],
                "checks": [c.to_json() for c in self.checks]}
    @staticmethod
    def from_json(d):
        t = TableMeta(d["key"], d["name"], [Column.from_json(c) for c in d["columns"]])
        t.pk = d.get("pk", [])
        t.uniques = d.get("uniques", [])
        t.fks = [ForeignKey.from_json(f) for f in d.get("fks", [])]
        t.checks = [CheckConstraint.from_json(c) for c in d.get("checks", [])]
        return t

class IndexMeta:
    def __init__(self, name, table_key, cols, unique=False):
        self.name = name
        self.table_key = table_key
        self.cols = list(cols)
        self.unique = unique
    def to_json(self):
        return {"name": self.name, "table": self.table_key, "cols": self.cols, "unique": self.unique}
    @staticmethod
    def from_json(d):
        return IndexMeta(d["name"], d["table"], [tuple(c) for c in d["cols"]], d.get("unique", False))

class Catalog:
    def __init__(self):
        self.tables = {}
        self.indexes = {}
    def resolve(self, name):
        low = name.lower()
        if low in self.tables: return low
        m = [k for k in self.tables if k.split(".")[-1] == low]
        return m[0] if len(m) == 1 else None
    def add_table(self, table):
        self.tables[table.key] = table
    def drop_table(self, key):
        self.tables.pop(key, None)
        for name in [n for n, idx in self.indexes.items() if idx.table_key == key]:
            self.indexes.pop(name, None)
    def indexes_of(self, table_key):
        return [idx for idx in self.indexes.values() if idx.table_key == table_key]
    def to_json(self):
        return {"tables": [t.to_json() for t in self.tables.values()],
                "indexes": [i.to_json() for i in self.indexes.values()]}
    def save(self, path):
        Path(path).write_text(json.dumps(self.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
    @staticmethod
    def load(path, base_dir):
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        cat = Catalog()
        for it in d.get("tables", []):
            t = TableMeta.from_json(it)
            t.heap_path = Path(base_dir) / "tables" / f"{t.key.replace('.', '__')}.tbl"
            cat.tables[t.key] = t
        for it in d.get("indexes", []):
            cat.indexes[it["name"].lower()] = IndexMeta.from_json(it)
        return cat

# =====================================================================
# 7. 执行计划与绑定节点
# =====================================================================

class BExpr: __slots__ = ("type",)
class BColumn(BExpr):
    __slots__ = ("depth", "index", "name", "type")
    def __init__(self, depth, index, name, type_):
        self.depth = depth; self.index = index; self.name = name; self.type = type_
    def text(self): return self.name if self.depth == 0 else f"outer.{self.name}"

class BLiteral(BExpr):
    __slots__ = ("value", "type")
    def __init__(self, value, type_=None): self.value = value; self.type = type_
    def text(self): return format_value(self.value)

class BUnary(BExpr):
    __slots__ = ("op", "child", "type")
    def __init__(self, op, child):
        self.op = op; self.child = child; self.type = "boolean" if op == "not" else child.type
    def text(self): return f"{self.op.upper()} {self.child.text()}"

class BBinary(BExpr):
    __slots__ = ("op", "left", "right", "type")
    def __init__(self, op, left, right):
        self.op = op; self.left = left; self.right = right
        self.type = "boolean" if op in ("and", "or", "=", "<", ">", "<=", ">=", "<>") else (
            "numeric" if (_is_num(left.type) or _is_num(right.type)) else left.type
        )
    def text(self): return f"({self.left.text()} {self.op.upper()} {self.right.text()})"

class BFunc(BExpr):
    __slots__ = ("name", "args", "distinct", "type")
    def __init__(self, name, args, distinct=False, type_=None):
        self.name = name; self.args = args; self.distinct = distinct; self.type = type_
    def text(self):
        d = "DISTINCT " if self.distinct else ""
        return f"{self.name.upper()}({d}{','.join(a.text() for a in self.args)})"

class BIsNull(BExpr):
    __slots__ = ("child", "neg", "type")
    def __init__(self, child, neg): self.child = child; self.neg = neg; self.type = "boolean"
    def text(self): return f"{self.child.text()} IS {'NOT ' if self.neg else ''}NULL"

class BBetween(BExpr):
    __slots__ = ("child", "low", "high", "neg", "type")
    def __init__(self, child, low, high, neg):
        self.child = child; self.low = low; self.high = high; self.neg = neg; self.type = "boolean"
    def text(self):
        n = "NOT " if self.neg else ""
        return f"{self.child.text()} {n}BETWEEN {self.low.text()} AND {self.high.text()}"

class BInList(BExpr):
    __slots__ = ("child", "items", "neg", "type")
    def __init__(self, child, items, neg):
        self.child = child; self.items = items; self.neg = neg; self.type = "boolean"
    def text(self):
        n = "NOT " if self.neg else ""
        return f"{self.child.text()} {n}IN ({','.join(i.text() for i in self.items)})"

class BLike(BExpr):
    __slots__ = ("child", "pattern", "escape", "neg", "type")
    def __init__(self, child, pattern, escape, neg):
        self.child = child; self.pattern = pattern; self.escape = escape; self.neg = neg; self.type = "boolean"
    def text(self):
        n = "NOT " if self.neg else ""
        return f"{self.child.text()} {n}LIKE '{self.pattern.value}'"

class BInQuery(BExpr):
    __slots__ = ("child", "query", "neg", "type")
    def __init__(self, child, query, neg):
        self.child = child; self.query = query; self.neg = neg; self.type = "boolean"
    def text(self): return f"{self.child.text()} {'NOT ' if self.neg else ''}IN (subquery)"

class BExists(BExpr):
    __slots__ = ("query", "neg", "type")
    def __init__(self, query, neg): self.query = query; self.neg = neg; self.type = "boolean"
    def text(self): return f"{'NOT ' if self.neg else ''}EXISTS (subquery)"

class BScalarQuery(BExpr):
    __slots__ = ("query", "type")
    def __init__(self, query): self.query = query; self.type = None
    def text(self): return "(subquery)"

def _is_num(t):
    return isinstance(t, SQLType) and (t.kind in INT_KINDS or t.kind == "numeric")

class Plan:
    def __init__(self): self.children = []
    def label(self): return type(self).__name__
    def tree(self, depth=0):
        lines = ["  " * depth + self.label()]
        for c in self.children: lines.extend(c.tree(depth + 1))
        return lines

class Empty(Plan): pass
class TableScan(Plan):
    def __init__(self, table_key, alias, columns):
        super().__init__()
        self.table_key = table_key; self.alias = alias; self.columns = columns
    def label(self):
        as_ = "" if self.alias == self.table_key else f" AS {self.alias}"
        return f"TableScan({self.table_key}{as_})"

class IndexScan(Plan):
    def __init__(self, table_key, alias, columns, index_name, key):
        super().__init__()
        self.table_key = table_key; self.alias = alias; self.columns = columns
        self.index_name = index_name; self.key = key
    def label(self):
        return f"IndexScan({self.table_key}, {self.index_name}, {format_value(self.key)})"

class Filter(Plan):
    def __init__(self, child, cond):
        super().__init__()
        self.children = [child]; self.cond = cond
    def label(self): return f"Filter({self.cond.text()})"

class Project(Plan):
    def __init__(self, child, items):
        super().__init__()
        self.children = [child]; self.items = items
    def label(self): return f"Project({','.join(name for _e, name in self.items)})"

class Aggregate(Plan):
    def __init__(self, child, group_exprs, aggs):
        super().__init__()
        self.children = [child]; self.group_exprs = group_exprs; self.aggs = aggs
    def label(self):
        g = ",".join(e.text() for e in self.group_exprs)
        a = ",".join(n for _f, n in self.aggs)
        return f"Aggregate(group=[{g}] aggs=[{a}])"

class Distinct(Plan):
    def __init__(self, child):
        super().__init__(); self.children = [child]

class Sort(Plan):
    def __init__(self, child, keys):
        super().__init__()
        self.children = [child]; self.keys = keys
    def label(self):
        return f"Sort({','.join(e.text() + (' DESC' if d else '') for e, d in self.keys)})"

class Limit(Plan):
    def __init__(self, child, limit, offset):
        super().__init__()
        self.children = [child]; self.limit = limit; self.offset = offset
    def label(self): return f"Limit({self.limit},{self.offset})"

class JoinPlan(Plan):
    def __init__(self, left, right, cond):
        super().__init__()
        self.children = [left, right]; self.cond = cond
    def label(self): return f"Join({self.cond.text() if self.cond is not None else 'cross'})"

class BoundQuery:
    def __init__(self, plan, columns):
        self.plan = plan; self.columns = columns

# =====================================================================
# 8. AST 抽象语法树与解析器 (Parser)
# =====================================================================

class Expr: pass
class Literal(Expr):
    def __init__(self, value): self.value = value
class ColumnRef(Expr):
    def __init__(self, name, qualifier=None): self.name = name; self.qualifier = qualifier
class Star(Expr):
    def __init__(self, qualifier=None): self.qualifier = qualifier
class Unary(Expr):
    def __init__(self, op, operand): self.op = op; self.operand = operand
class Binary(Expr):
    def __init__(self, op, left, right): self.op = op; self.left = left; self.right = right
class IsNull(Expr):
    def __init__(self, operand, neg): self.operand = operand; self.neg = neg
class Between(Expr):
    def __init__(self, operand, low, high, neg):
        self.operand = operand; self.low = low; self.high = high; self.neg = neg
class InList(Expr):
    def __init__(self, operand, items, neg):
        self.operand = operand; self.items = items; self.neg = neg
class InQuery(Expr):
    def __init__(self, operand, query, neg):
        self.operand = operand; self.query = query; self.neg = neg
class Exists(Expr):
    def __init__(self, query, neg): self.query = query; self.neg = neg
class Like(Expr):
    def __init__(self, operand, pattern, escape, neg):
        self.operand = operand; self.pattern = pattern; self.escape = escape; self.neg = neg
class Func(Expr):
    def __init__(self, name, args, distinct=False, star=False):
        self.name = name; self.args = args; self.distinct = distinct; self.star = star
class SubQuery(Expr):
    def __init__(self, query): self.query = query
class SelectItem:
    def __init__(self, expr, alias=None): self.expr = expr; self.alias = alias
class BaseTable:
    def __init__(self, name, alias=None): self.name = name; self.alias = alias
class JoinRef:
    def __init__(self, left, right, on): self.left = left; self.right = right; self.on = on
class OrderKey:
    def __init__(self, expr, desc): self.expr = expr; self.desc = desc

class Query:
    def __init__(self, distinct, items, table_refs, where, group_by, having, order_by, limit):
        self.distinct = distinct; self.items = items; self.table_refs = table_refs
        self.where = where; self.group_by = group_by; self.having = having
        self.order_by = order_by; self.limit = limit

class ColumnDef:
    def __init__(self, name, type_, not_null=False, unique=False, default=None, check=None, references=None):
        self.name = name; self.type = type_; self.not_null = not_null; self.unique = unique
        self.primary = False; self.default = default; self.check = check; self.references = references

class FkRef:
    def __init__(self, cols, ref_table, ref_cols, on_delete, on_update):
        self.cols = cols; self.ref_table = ref_table; self.ref_cols = ref_cols
        self.on_delete = on_delete; self.on_update = on_update

class TableConstraint:
    def __init__(self, kind, cols=None, expr=None, ref=None):
        self.kind = kind; self.cols = cols; self.expr = expr; self.ref = ref

class CreateTable:
    def __init__(self, name, columns, constraints):
        self.name = name; self.columns = columns; self.constraints = constraints
class DropTable:
    def __init__(self, name): self.name = name
class CreateIndex:
    def __init__(self, name, table, cols, unique):
        self.name = name; self.table = table; self.cols = cols; self.unique = unique
class DropIndex:
    def __init__(self, name): self.name = name
class Insert:
    def __init__(self, table, columns, rows, query):
        self.table = table; self.columns = columns; self.rows = rows; self.query = query
class Update:
    def __init__(self, table, assignments, where):
        self.table = table; self.assignments = assignments; self.where = where
class Delete:
    def __init__(self, table, where): self.table = table; self.where = where

AGG_FUNCS = {"count", "sum", "avg", "max", "min"}
RESERVED = {
    "from", "where", "group", "having", "order", "limit", "offset", "join", "inner",
    "on", "and", "or", "not", "in", "like", "between", "is", "as", "select", "set",
    "values", "asc", "desc", "union"
}
COMPARE_OPS = {"=", ">", "<", ">=", "<=", "<>", "!=", "!>", "!<"}

def expr_sql(e):
    if isinstance(e, Literal):
        return f"'{e.value.replace('\'', '\'\'')}'" if isinstance(e.value, str) else format_value(e.value)
    if isinstance(e, ColumnRef):
        return f"{e.qualifier}.{e.name}" if e.qualifier else e.name
    if isinstance(e, Unary): return f"{e.op.upper()} {expr_sql(e.operand)}"
    if isinstance(e, Binary): return f"({expr_sql(e.left)} {e.op.upper()} {expr_sql(e.right)})"
    if isinstance(e, IsNull): return f"{expr_sql(e.operand)} IS {'NOT ' if e.neg else ''}NULL"
    if isinstance(e, Between):
        return f"{expr_sql(e.operand)} {'NOT ' if e.neg else ''}BETWEEN {expr_sql(e.low)} AND {expr_sql(e.high)}"
    if isinstance(e, InList):
        return f"{expr_sql(e.operand)} {'NOT ' if e.neg else ''}IN ({', '.join(expr_sql(i) for i in e.items)})"
    if isinstance(e, Like):
        return f"{expr_sql(e.operand)} {'NOT ' if e.neg else ''}LIKE {expr_sql(e.pattern)}"
    if isinstance(e, Func):
        inner = "*" if e.star else ", ".join(expr_sql(a) for a in e.args)
        return f"{e.name.upper()}{'DISTINCT ' if e.distinct else ''}({inner})"
    return "?"

class Parser:
    def __init__(self, sql, tokens=None):
        self.sql = sql
        self.tokens = tokens if tokens is not None else tokenize(sql)
        self.pos = 0

    def peek(self): return self.tokens[self.pos]
    def next_value(self):
        i = self.pos + 1
        return self.tokens[i].value if i < len(self.tokens) else ""
    def advance(self):
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok
    def at_word(self, *words):
        t = self.peek()
        return t.kind == "word" and t.value in words
    def eat_word(self, *words):
        if self.at_word(*words): return self.advance().value
        return None
    def expect_word(self, w):
        if not self.at_word(w): raise SQLSyntaxError(f"预期关键字 {w.upper()}，得到 {self.peek().text!r}")
        return self.advance()
    def at_op(self, op): return self.peek().kind == "op" and self.peek().value == op
    def eat_punct(self, ch):
        if self.peek().kind == "punct" and self.peek().value == ch:
            self.advance(); return True
        return False
    def expect_punct(self, ch):
        if not self.eat_punct(ch): raise SQLSyntaxError(f"缺少符号: {ch}")
    def ident(self):
        tok = self.peek()
        if tok.kind in ("word", "qid"):
            if tok.kind == "word" and tok.value in RESERVED:
                raise SQLSyntaxError(f"{tok.text} 是系统保留字，不能用作标识符")
            self.advance()
            return tok.value if tok.kind == "qid" else tok.text
        raise SQLSyntaxError(f"预期标识符，实际为 {tok.text!r}")
    def qualified_name(self):
        name = self.ident()
        while self.eat_punct("."): name = f"{name}.{self.ident()}"
        return name

    def parse_statement(self):
        stmt = self.parse_statement_body()
        while self.peek().kind == "punct" and self.peek().value == ";":
            self.advance()
        if self.peek().kind != "eof":
            tok = self.peek()
            raise SQLSyntaxError(
                f"语句在 {tok.text!r} 之后还有多余内容（常见原因是关键字拼错，"
                "例如把 WHERE 写成了 WHER，被当成表别名后剩下的条件没人接）")
        return stmt

    def parse_statement_body(self):
        if self.at_word("select"): return self.parse_query()
        if self.at_word("explain"): self.advance()
        if self.at_word("create"): return self.parse_create()
        if self.at_word("drop"): return self.parse_drop()
        if self.at_word("insert"): return self.parse_insert()
        if self.at_word("update"): return self.parse_update()
        if self.at_word("delete"): return self.parse_delete()
        raise SQLSyntaxError(f"不支持的 SQL 语句开头: {self.peek().text!r}")

    def parse_query(self):
        self.expect_word("select")
        distinct = bool(self.eat_word("distinct"))
        if not distinct: self.eat_word("all")
        items = [self.parse_select_item()]
        while self.eat_punct(","): items.append(self.parse_select_item())
        refs = []
        if self.eat_word("from"):
            refs.append(self.parse_table_ref())
            while self.eat_punct(","): refs.append(self.parse_table_ref())
        where = self.parse_expr() if self.eat_word("where") else None
        group_by = []
        if self.eat_word("group"):
            self.expect_word("by")
            group_by.append(self.parse_expr())
            while self.eat_punct(","): group_by.append(self.parse_expr())
        having = self.parse_expr() if self.eat_word("having") else None
        order_by = []
        if self.eat_word("order"):
            self.expect_word("by")
            order_by = [self.parse_order_key()]
            while self.eat_punct(","): order_by.append(self.parse_order_key())
        limit = self.parse_limit()
        return Query(distinct, items, refs, where, group_by, having, order_by, limit)

    def parse_limit(self):
        limit = offset = None
        if self.eat_word("limit"):
            if self.eat_word("all"): return None
            limit = self.expect_number()
            if self.eat_punct(","): limit, offset = self.expect_number(), limit
            elif self.eat_word("offset"): offset = self.expect_number()
        elif self.eat_word("offset"): offset = self.expect_number()
        return (limit, offset or 0) if (limit is not None or offset is not None) else None

    def parse_order_key(self):
        expr = self.parse_expr()
        desc = bool(self.eat_word("desc"))
        if not desc: self.eat_word("asc")
        return OrderKey(expr, desc)

    def parse_select_item(self):
        expr = self.parse_expr()
        alias = None
        if self.eat_word("as"): alias = self.ident()
        elif self.peek().kind in ("word", "qid") and self.peek().value not in RESERVED:
            alias = self.ident()
        return SelectItem(expr, alias)

    def parse_table_ref(self):
        left = self.parse_table_primary()
        while True:
            if self.eat_word("join"): pass
            elif self.at_word("inner") and self.next_value() == "join":
                self.advance(); self.advance()
            else: return left
            right = self.parse_table_primary()
            self.expect_word("on")
            left = JoinRef(left, right, self.parse_expr())

    def parse_table_primary(self):
        name = self.qualified_name()
        alias = None
        if self.eat_word("as"): alias = self.ident()
        elif self.peek().kind in ("word", "qid") and self.peek().value not in RESERVED:
            alias = self.ident()
        return BaseTable(name, alias)

    def parse_expr(self): return self.parse_or()
    def parse_or(self):
        left = self.parse_and()
        while self.eat_word("or"): left = Binary("or", left, self.parse_and())
        return left
    def parse_and(self):
        left = self.parse_not()
        while self.eat_word("and"): left = Binary("and", left, self.parse_not())
        return left
    def parse_not(self):
        if self.eat_word("not"): return Unary("not", self.parse_not())
        return self.parse_comparison()

    def parse_comparison(self):
        left = self.parse_additive()
        tok = self.peek()
        if tok.kind == "op" and tok.value in COMPARE_OPS:
            self.advance()
            op = {"!>": "<=", "!<": ">="}.get(tok.value, tok.value)
            return Binary(op, left, self.parse_additive())
        if self.at_word("is"):
            self.advance()
            neg = bool(self.eat_word("not"))
            self.expect_word("null")
            return IsNull(left, neg)
        neg = False
        if self.at_word("not") and self.next_value() in ("between", "in", "like"):
            self.advance(); neg = True
        if self.eat_word("between"):
            low = self.parse_additive()
            self.expect_word("and")
            return Between(left, low, self.parse_additive(), neg)
        if self.eat_word("in"):
            if self.peek().kind == "punct" and self.peek().value == "(" and self.next_value() == "select":
                self.advance()
                q = self.parse_query()
                self.expect_punct(")")
                return InQuery(left, q, neg)
            self.expect_punct("(")
            items = [self.parse_expr()]
            while self.eat_punct(","): items.append(self.parse_expr())
            self.expect_punct(")")
            return InList(left, items, neg)
        if self.eat_word("like"):
            esc = self.advance().value if self.eat_word("escape") else None
            return Like(left, self.parse_additive(), esc, neg)
        return left

    def parse_additive(self):
        left = self.parse_multiplicative()
        while self.peek().kind == "op" and self.peek().value in ("+", "-"):
            op = self.advance().value
            left = Binary(op, left, self.parse_multiplicative())
        return left

    def parse_multiplicative(self):
        left = self.parse_unary()
        while self.peek().kind == "op" and self.peek().value in ("*", "/", "%"):
            op = self.advance().value
            left = Binary(op, left, self.parse_unary())
        return left

    def parse_unary(self):
        if self.at_op("-") or self.at_op("+"):
            return Unary(self.advance().value, self.parse_unary())
        return self.parse_primary()

    def parse_primary(self):
        tok = self.peek()
        if tok.kind in ("num", "str"):
            self.advance()
            return Literal(tok.value)
        if tok.kind == "punct" and tok.value == "(":
            self.advance()
            if self.at_word("select"):
                q = self.parse_query()
                self.expect_punct(")")
                return SubQuery(q)
            expr = self.parse_expr()
            self.expect_punct(")")
            return expr
        if tok.kind == "op" and tok.value == "*":
            self.advance(); return Star()
        if tok.kind in ("word", "qid"):
            if self.at_word("null"):
                self.advance(); return Literal(None)
            if self.at_word("exists"):
                self.advance(); self.expect_punct("(")
                q = self.parse_query()
                self.expect_punct(")")
                return Exists(q, False)
            if tok.kind == "word" and tok.value in AGG_FUNCS and self.next_value() == "(":
                return self.parse_func()
            name = self.advance()
            if self.peek().kind == "punct" and self.peek().value == "(":
                raise SQLSyntaxError(f"不支持的系统函数: {name.text}")
            if self.peek().kind == "punct" and self.peek().value == ".":
                if self.next_value() == "*":
                    self.advance(); self.advance()
                    return Star(name.text)
                self.advance()
                return ColumnRef(self.ident(), name.text)
            return ColumnRef(name.text)
        raise SQLSyntaxError(f"无法解析的表达式语法: {tok.text!r}")

    def parse_func(self):
        name = self.advance().value
        self.expect_punct("(")
        distinct = bool(self.eat_word("distinct"))
        star, args = False, []
        if self.peek().kind == "op" and self.peek().value == "*":
            self.advance(); star = True
        elif not (self.peek().kind == "punct" and self.peek().value == ")"):
            args.append(self.parse_expr())
            while self.eat_punct(","): args.append(self.parse_expr())
        self.expect_punct(")")
        return Func(name, args, distinct, star)

    def expect_number(self):
        tok = self.peek()
        if tok.kind != "num": raise SQLSyntaxError(f"缺少数字常量，实际得到 {tok.text!r}")
        self.advance()
        return int(tok.value)

    def parse_create(self):
        self.expect_word("create")
        unique = bool(self.eat_word("unique"))
        if self.eat_word("index"):
            name = self.ident()
            self.expect_word("on")
            tbl = self.qualified_name()
            self.expect_punct("(")
            cols = [self.parse_index_col()]
            while self.eat_punct(","): cols.append(self.parse_index_col())
            self.expect_punct(")")
            return CreateIndex(name, tbl, cols, unique)
        if unique: raise SQLSyntaxError("UNIQUE 关键字只能用于索引或字段约束")
        self.expect_word("table")
        return self.parse_create_table()

    def parse_index_col(self):
        name = self.ident()
        desc = bool(self.eat_word("desc"))
        if not desc: self.eat_word("asc")
        return (name, desc)

    def parse_create_table(self):
        name = self.qualified_name()
        self.expect_punct("(")
        cols, constraints = [], []
        while True:
            if self.at_word("constraint"):
                self.advance(); self.ident()
            if self.at_word("primary", "unique", "foreign", "check"):
                constraints.append(self.parse_table_constraint())
            else:
                cols.append(self.parse_column_def())
            if not self.eat_punct(","): break
        self.expect_punct(")")
        for col in cols:
            if col.primary and not any(c.kind == "pk" for c in constraints):
                constraints.append(TableConstraint("pk", [col.name]))
        return CreateTable(name, cols, constraints)

    def parse_column_def(self):
        name = self.ident()
        t = self.parse_type()
        col = ColumnDef(name, t)
        while True:
            if self.eat_word("not"):
                self.expect_word("null"); col.not_null = True
            elif self.at_word("primary"):
                self.advance(); self.expect_word("key"); col.primary = True
            elif self.eat_word("unique"): col.unique = True
            elif self.eat_word("default"): col.default = self.parse_expr()
            elif self.eat_word("check"):
                self.expect_punct("("); col.check = self.parse_expr(); self.expect_punct(")")
            elif self.eat_word("references"):
                col.references = self.parse_references([name])
            else:
                return col

    def parse_references(self, cols):
        ref_table = self.qualified_name()
        ref_cols = None
        if self.eat_punct("("):
            ref_cols = [self.ident()]
            while self.eat_punct(","): ref_cols.append(self.ident())
            self.expect_punct(")")
        on_del = on_upd = "restrict"
        while self.eat_word("on"):
            if self.eat_word("delete"): on_del = self.parse_action()
            elif self.eat_word("update"): on_upd = self.parse_action()
            else: raise SQLSyntaxError("ON 子句后缺少 DELETE 或 UPDATE")
        return FkRef(cols, ref_table, ref_cols, on_del, on_upd)

    def parse_action(self):
        if self.eat_word("cascade"): return "cascade"
        if self.eat_word("restrict"): return "restrict"
        if self.eat_word("set"):
            self.expect_word("null"); return "set null"
        self.expect_word("no"); self.expect_word("action")
        return "restrict"

    def parse_table_constraint(self):
        if self.eat_word("primary"):
            self.expect_word("key"); self.expect_punct("(")
            return TableConstraint("pk", self.parse_name_list())
        if self.eat_word("unique"):
            self.expect_punct("(")
            return TableConstraint("unique", self.parse_name_list())
        if self.eat_word("foreign"):
            self.expect_word("key"); self.expect_punct("(")
            cols = self.parse_name_list()
            self.expect_word("references")
            return TableConstraint("fk", ref=self.parse_references(cols))
        self.expect_word("check"); self.expect_punct("(")
        expr = self.parse_expr()
        self.expect_punct(")")
        return TableConstraint("check", expr=expr)

    def parse_name_list(self):
        cols = [self.ident()]
        while self.eat_punct(","): cols.append(self.ident())
        self.expect_punct(")")
        return cols

    def parse_type(self):
        name = self.advance()
        if name.kind != "word": raise SQLSyntaxError("缺少数据类型说明符")
        word = name.text.lower()
        if word == "double":
            self.eat_word("precision"); word = "numeric"
        if word == "character":
            word = "varchar" if self.eat_word("varying") else "char"
        args = []
        if self.eat_punct("("):
            args.append(self.expect_number())
            while self.eat_punct(","): args.append(self.expect_number())
            self.expect_punct(")")
        return parse_type(word, args)

    def parse_drop(self):
        self.expect_word("drop")
        if self.eat_word("index"): return DropIndex(self.ident())
        self.expect_word("table")
        name = self.qualified_name()
        if self.eat_word("cascade") or self.eat_word("restrict"): pass
        return DropTable(name)

    def parse_insert(self):
        self.expect_word("insert"); self.expect_word("into")
        table = self.qualified_name()
        cols = None
        if self.eat_punct("("):
            cols = [self.ident()]
            while self.eat_punct(","): cols.append(self.ident())
            self.expect_punct(")")
        if self.at_word("select"): return Insert(table, cols, [], self.parse_query())
        self.expect_word("values")
        rows = []
        while True:
            self.expect_punct("(")
            row = [self.parse_expr()]
            while self.eat_punct(","): row.append(self.parse_expr())
            self.expect_punct(")")
            rows.append(row)
            if not self.eat_punct(","): break
        return Insert(table, cols, rows, None)

    def parse_update(self):
        self.expect_word("update")
        table = self.qualified_name()
        self.expect_word("set")
        assignments = [(self.ident(), self.parse_assign_val())]
        while self.eat_punct(","): assignments.append((self.ident(), self.parse_assign_val()))
        where = self.parse_expr() if self.eat_word("where") else None
        return Update(table, assignments, where)

    def parse_assign_val(self):
        if not (self.peek().kind == "op" and self.peek().value == "="):
            raise SQLSyntaxError("SET 子句赋值必须为: 字段 = 表达式")
        self.advance()
        return self.parse_expr()

    def parse_delete(self):
        self.expect_word("delete"); self.expect_word("from")
        table = self.qualified_name()
        where = self.parse_expr() if self.eat_word("where") else None
        return Delete(table, where)

def split_statements(text):
    stmts, buf = [], []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "'":
            j = i + 1
            while j < n:
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        j += 2; continue
                    break
                j += 1
            buf.append(text[i:j + 1])
            i = j + 1
            continue
        if text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if ch == ";":
            p = "".join(buf).strip()
            if p: stmts.append(p)
            buf = []; i += 1; continue
        buf.append(ch); i += 1
    t = "".join(buf).strip()
    if t: stmts.append(t)
    return stmts

# =====================================================================
# 9. 语义绑定器 (Binder)
# =====================================================================

class ColumnMeta:
    __slots__ = ("name", "col_name", "table_alias", "index", "type")
    def __init__(self, name, col_name, table_alias, index, type_):
        self.name = name; self.col_name = col_name; self.table_alias = table_alias
        self.index = index; self.type = type_

class TableBinding:
    def __init__(self, alias, table, columns):
        self.alias = alias; self.table = table; self.columns = columns

class Scope:
    def __init__(self, bindings, parent=None):
        self.bindings = bindings; self.parent = parent
    def resolve(self, qualifier, name):
        low = name.lower()
        qlow = qualifier.lower() if qualifier else None
        for b in self.bindings:
            if qlow and b.alias.lower() != qlow: continue
            for col in b.columns:
                if col.col_name.lower() == low: return BColumn(0, col.index, col.name, col.type)
        if self.parent:
            outer = self.parent.resolve(qualifier, name)
            if outer: return BColumn(outer.depth + 1, outer.index, outer.name, outer.type)
        return None
    def all_columns(self):
        return [c for b in self.bindings for c in b.columns]

class Binder:
    def __init__(self, catalog, indexes):
        self.catalog = catalog; self.indexes = indexes

    def bind_query(self, query, outer=None):
        scope = outer or Scope([])
        plan, cols = self._bind_select(query, scope)
        return BoundQuery(plan, cols)

    def table_scope(self, table, alias=None):
        al = alias or table.key
        binding = TableBinding(al, table, self._cols_of(table, al))
        return Scope([binding])

    def _cols_of(self, table, alias):
        return [ColumnMeta(f"{alias}.{c.name}", c.name, alias, i, c.type) for i, c in enumerate(table.columns)]

    def _collect_from(self, refs):
        plans, bindings, conds = [], [], []
        for r in refs:
            p, b, c = self._collect_ref(r)
            plans.append(p); bindings.extend(b); conds.extend(c)
        off = 0
        for b in bindings:
            for i, c in enumerate(b.columns): c.index = off + i
            off += len(b.columns)
        if not plans: return Empty(), [], conds
        root = plans[0]
        for p in plans[1:]: root = JoinPlan(root, p, None)
        return root, bindings, conds

    def _collect_ref(self, r):
        if isinstance(r, BaseTable):
            k = self.catalog.resolve(r.name)
            if k is None: raise SQLSemanticError(f"未找到数据表: {r.name}")
            t = self.catalog.tables[k]
            al = r.alias or k
            b = TableBinding(al, t, self._cols_of(t, al))
            return TableScan(k, al, [c.col_name for c in b.columns]), [b], []
        lp, lb, lc = self._collect_ref(r.left)
        rp, rb, rc = self._collect_ref(r.right)
        return JoinPlan(lp, rp, None), lb + rb, lc + rc + [r.on]

    def _bind_select(self, query, scope):
        chain, bindings, conds = self._collect_from(query.table_refs)
        inner = Scope(bindings, scope) if bindings else scope
        if query.where:
            conds.append(query.where)
            if len(bindings) == 1:
                chain = self._try_index_scan(chain, bindings[0], query.where, inner, conds)
        cond = _and_all([self.bind_expr(c, inner) for c in conds])
        if cond: chain = Filter(chain, cond)
        has_agg = bool(query.group_by) or any(_has_aggregate(i.expr) for i in query.items) or (
            query.having is not None and _has_aggregate(query.having)
        )
        if query.having and not has_agg:
            raise SQLSemanticError("HAVING 子句必须搭配聚合函数或 GROUP BY 使用")
        return self._finish_aggregate(query, chain, inner) if has_agg else self._finish_plain(query, chain, inner)

    def _finish_plain(self, query, child, scope):
        items = []
        for it in query.items:
            if isinstance(it.expr, Star):
                named = [c for c in scope.all_columns() if not it.expr.qualifier or c.table_alias.lower() == it.expr.qualifier.lower()]
                if not named: raise SQLSemanticError("无法通配展开字段")
                for c in named: items.append((BColumn(0, c.index, c.name, c.type), c.name))
                continue
            b = self.bind_expr(it.expr, scope)
            name = it.alias or (expr_sql(it.expr) if isinstance(it.expr, Func) else _item_name(it.expr, b))
            items.append((b, name))
        names = [n for _, n in items]
        if query.order_by:
            keys = [(self._order_expr(k.expr, items, scope), k.desc) for k in query.order_by]
            child = Sort(child, keys)
        plan = Project(child, items)
        if query.distinct: plan = Distinct(plan)
        if query.limit: plan = Limit(plan, query.limit[0], query.limit[1])
        return plan, names

    def _order_expr(self, expr, items, scope):
        if isinstance(expr, Literal) and isinstance(expr.value, int):
            idx = expr.value - 1
            if not (0 <= idx < len(items)): raise SQLSemanticError(f"ORDER BY 下标超出列范围: {expr.value}")
            return items[idx][0]
        if isinstance(expr, ColumnRef):
            low = expr.name.lower()
            for it_e, n in items:
                if n.lower() == low: return it_e
        return self.bind_expr(expr, scope)

    def _finish_aggregate(self, query, child, scope):
        groups = [self.bind_expr(e, scope) for e in query.group_by]
        aggs = []
        for it in query.items: _collect_aggregates(it.expr, self, scope, aggs)
        if query.having: _collect_aggregates(query.having, self, scope, aggs)
        g_txt = [g.text() for g in groups]
        a_txt = [f.text() for f, _ in aggs]
        plan = Aggregate(child, groups, aggs)
        if query.having:
            plan = Filter(plan, self._map_to_agg(self.bind_expr(query.having, scope), g_txt, a_txt))
        items = []
        for it in query.items:
            if isinstance(it.expr, Star): raise SQLSemanticError("聚合查询中不支持单独书写 *")
            b = self._map_to_agg(self.bind_expr(it.expr, scope), g_txt, a_txt)
            name = it.alias or (expr_sql(it.expr) if isinstance(it.expr, Func) else _item_name(it.expr, b))
            items.append((b, name))
        names = [n for _, n in items]
        plan = Project(plan, items)
        if query.order_by:
            keys = []
            low_names = [n.lower() for n in names]
            for k in query.order_by:
                e = k.expr
                if isinstance(e, Literal) and isinstance(e.value, int):
                    idx = e.value - 1
                    if not (0 <= idx < len(names)): raise SQLSemanticError("ORDER BY 序号越界")
                    keys.append((BColumn(0, idx, names[idx], None), k.desc))
                elif isinstance(e, ColumnRef) and e.name.lower() in low_names:
                    idx = low_names.index(e.name.lower())
                    keys.append((BColumn(0, idx, names[idx], None), k.desc))
                else:
                    raise SQLSemanticError("分组排序字段必须在 SELECT 输出投影列表中")
            plan = Sort(plan, keys)
        if query.distinct: plan = Distinct(plan)
        if query.limit: plan = Limit(plan, query.limit[0], query.limit[1])
        return plan, names

    def _map_to_agg(self, bound, group_texts, agg_texts):
        if isinstance(bound, BColumn):
            txt = bound.name
            for i, g in enumerate(group_texts):
                if g == txt: return BColumn(0, i, txt, bound.type)
            raise SQLSemanticError(f"字段 {txt} 未包含在 GROUP BY 子句中")
        if isinstance(bound, BFunc) and bound.name.lower() in AGG_FUNCS:
            txt = bound.text()
            for i, a in enumerate(agg_texts):
                if a == txt: return BColumn(0, len(group_texts) + i, txt, bound.type)
        return _rebuild(bound, lambda ch: self._map_to_agg(ch, group_texts, agg_texts))

    def _try_index_scan(self, child, binding, where, scope, conds):
        eq = _equality_on(where)
        if not eq: return child
        cname, vexpr = eq
        if binding.table.column_index(cname) is None: return child
        for meta in self.catalog.indexes_of(binding.table.key):
            if not meta.cols or meta.cols[0][0].lower() != cname.lower(): continue
            tree = self.indexes.get(meta.name.lower())
            if not tree: continue
            k = _const_value(self.bind_expr(vexpr, scope))
            if k is None: continue
            conds.clear()
            return IndexScan(binding.table.key, binding.alias, [c.col_name for c in binding.columns],
                             meta.name, (encode_value(k),))
        return child

    def bind_expr(self, expr, scope, allow_agg=True):
        if isinstance(expr, Literal):
            if expr.value is None: return BLiteral(None, None)
            t = SQLType("char") if isinstance(expr.value, str) else (
                SQLType("numeric") if isinstance(expr.value, float) else SQLType("int")
            )
            return BLiteral(coerce(expr.value, t), t)
        if isinstance(expr, ColumnRef):
            r = scope.resolve(expr.qualifier, expr.name)
            if not r: raise SQLSemanticError(f"未知列名: {expr.name}")
            return r
        if isinstance(expr, Unary): return BUnary(expr.op, self.bind_expr(expr.operand, scope, allow_agg))
        if isinstance(expr, Binary):
            l = self.bind_expr(expr.left, scope, allow_agg)
            r = self.bind_expr(expr.right, scope, allow_agg)
            if expr.op in ("+", "-", "*", "/", "%") and not (_is_num(l.type) and _is_num(r.type)):
                raise SQLSemanticError("算术运算两侧必须均为数值类型")
            return BBinary(expr.op, l, r)
        if isinstance(expr, IsNull): return BIsNull(self.bind_expr(expr.operand, scope, allow_agg), expr.neg)
        if isinstance(expr, Between):
            return BBetween(self.bind_expr(expr.operand, scope, allow_agg),
                            self.bind_expr(expr.low, scope, allow_agg),
                            self.bind_expr(expr.high, scope, allow_agg), expr.neg)
        if isinstance(expr, InList):
            return BInList(self.bind_expr(expr.operand, scope, allow_agg),
                           [self.bind_expr(it, scope, allow_agg) for it in expr.items], expr.neg)
        if isinstance(expr, InQuery):
            return BInQuery(self.bind_expr(expr.operand, scope, allow_agg), self.bind_query(expr.query, scope), expr.neg)
        if isinstance(expr, Exists): return BExists(self.bind_query(expr.query, scope), expr.neg)
        if isinstance(expr, SubQuery):
            node = BScalarQuery(self.bind_query(expr.query, scope))
            node.type = SQLType("numeric")
            return node
        if isinstance(expr, Like):
            return BLike(self.bind_expr(expr.operand, scope, allow_agg),
                         self.bind_expr(expr.pattern, scope, allow_agg), expr.escape, expr.neg)
        if isinstance(expr, Func):
            name = expr.name.lower()
            args = [self.bind_expr(a, scope, allow_agg) for a in expr.args]
            if name in AGG_FUNCS:
                if not allow_agg: raise SQLSemanticError(f"{name.upper()} 聚合函数在此上下文中无效")
                if not expr.star and len(args) != 1: raise SQLSemanticError(f"{name.upper()} 仅接受单个参数")
                return BFunc(name, args, expr.distinct, SQLType("numeric"))
            raise SQLSemanticError(f"未定义的内置函数: {name}")
        if isinstance(expr, Star): raise SQLSemanticError("通配符 * 仅能出现在投影主表达式中")
        raise SQLSemanticError("不支持绑定的表达式类型")

def _and_all(conds):
    res = None
    for c in conds: res = c if res is None else BBinary("and", res, c)
    return res

def _item_name(ast_expr, bound):
    if isinstance(ast_expr, ColumnRef): return bound.name
    if isinstance(bound, BLiteral): return format_value(bound.value)
    if isinstance(bound, (BFunc, BBinary, BUnary)): return bound.text()
    return "expr"

def _has_aggregate(expr):
    if isinstance(expr, Func) and expr.name.lower() in AGG_FUNCS: return True
    return any(_has_aggregate(c) for c in _children_of(expr))

def _children_of(expr):
    if isinstance(expr, Unary): return [expr.operand]
    if isinstance(expr, Binary): return [expr.left, expr.right]
    if isinstance(expr, IsNull): return [expr.operand]
    if isinstance(expr, Between): return [expr.operand, expr.low, expr.high]
    if isinstance(expr, InList): return [expr.operand] + list(expr.items)
    if isinstance(expr, InQuery): return [expr.operand]
    if isinstance(expr, Like): return [expr.operand, expr.pattern]
    if isinstance(expr, Func): return list(expr.args)
    return []

def _collect_aggregates(expr, binder, scope, aggs):
    if isinstance(expr, Func) and expr.name.lower() in AGG_FUNCS:
        b = binder.bind_expr(expr, scope)
        if all(b.text() != f.text() for f, _ in aggs): aggs.append((b, b.text()))
        return
    for c in _children_of(expr): _collect_aggregates(c, binder, scope, aggs)

def _rebuild(bound, map_child):
    if isinstance(bound, BUnary): return BUnary(bound.op, map_child(bound.child))
    if isinstance(bound, BBinary): return BBinary(bound.op, map_child(bound.left), map_child(bound.right))
    if isinstance(bound, BIsNull): return BIsNull(map_child(bound.child), bound.neg)
    if isinstance(bound, BBetween):
        return BBetween(map_child(bound.child), map_child(bound.low), map_child(bound.high), bound.neg)
    if isinstance(bound, BInList):
        return BInList(map_child(bound.child), [map_child(i) for i in bound.items], bound.neg)
    if isinstance(bound, BLike):
        return BLike(map_child(bound.child), map_child(bound.pattern), bound.escape, bound.neg)
    if isinstance(bound, BFunc):
        return BFunc(bound.name, [map_child(a) for a in bound.args], bound.distinct, bound.type)
    return bound

# =====================================================================
# 10. 物理执行器 (Executor)
# =====================================================================

class Ctx:
    __slots__ = ("row", "outers", "engine")
    def __init__(self, row=None, outers=(), engine=None):
        self.row = row; self.outers = outers; self.engine = engine
    def inside(self, row):
        return Ctx(None, self.outers + ((row,),) if row is not None else self.outers, self.engine)

def run_plan(plan, ctx):
    if isinstance(plan, TableScan):
        return [row for _, row in ctx.engine.heaps[plan.table_key].scan()]
    if isinstance(plan, IndexScan):
        tree = ctx.engine.indexes[plan.index_name.lower()]
        heap = ctx.engine.heaps[plan.table_key]
        return [heap.get(rid) for rid in tree.search(plan.key)]
    if isinstance(plan, Empty): return [[]]
    if isinstance(plan, Filter):
        return [r for r in run_plan(plan.children[0], ctx) if truth(eval_expr(plan.cond, r, ctx)) is True]
    if isinstance(plan, Project):
        return [[eval_expr(e, r, ctx) for e, _ in plan.items] for r in run_plan(plan.children[0], ctx)]
    if isinstance(plan, Aggregate): return _run_aggregate(plan, ctx)
    if isinstance(plan, Distinct):
        seen, out = set(), []
        for r in run_plan(plan.children[0], ctx):
            k = tuple(encode_value(v) for v in r)
            if k not in seen: seen.add(k); out.append(r)
        return out
    if isinstance(plan, Sort):
        rows = run_plan(plan.children[0], ctx)
        def _cmp(a, b):
            for e, desc in plan.keys:
                c = compare_for_sort(eval_expr(e, a, ctx), eval_expr(e, b, ctx), desc)
                if c: return c
            return 0
        return sorted(rows, key=cmp_to_key(_cmp))
    if isinstance(plan, Limit):
        rows = run_plan(plan.children[0], ctx)
        st = plan.offset
        return rows[st:] if plan.limit is None else rows[st:st + plan.limit]
    if isinstance(plan, JoinPlan):
        l_rows = run_plan(plan.children[0], ctx)
        r_rows = run_plan(plan.children[1], ctx)
        out = []
        for l in l_rows:
            for r in r_rows:
                merged = l + r
                if plan.cond is None or truth(eval_expr(plan.cond, merged, ctx)) is True:
                    out.append(merged)
        return out
    raise SQLExecutionError(f"未知执行计划算子: {type(plan).__name__}")

def _run_aggregate(plan, ctx):
    rows = run_plan(plan.children[0], ctx)
    groups = {}
    for r in rows:
        k = tuple(encode_value(eval_expr(g, r, ctx)) for g in plan.group_exprs)
        groups.setdefault(k, []).append(r)
    if not plan.group_exprs and not groups: groups[()] = []
    out = []
    for k, grows in groups.items():
        vals = [eval_expr(g, grows[0], ctx) for g in plan.group_exprs] if grows else [decode_value(x) for x in k]
        for f, _ in plan.aggs: vals.append(_eval_aggregate(f, grows, ctx))
        out.append(vals)
    return out

def _eval_aggregate(func, rows, ctx):
    name = func.name.lower()
    if name == "count":
        if not func.args: return len(rows)
        if func.distinct:
            seen = {encode_value(eval_expr(func.args[0], r, ctx)) for r in rows if eval_expr(func.args[0], r, ctx) is not NULL}
            return len(seen)
        return sum(1 for r in rows if eval_expr(func.args[0], r, ctx) is not NULL)
    vals = [eval_expr(func.args[0], r, ctx) for r in rows]
    vals = [v for v in vals if v is not NULL]
    if name == "sum": return sum(vals) if vals else NULL
    if name == "avg": return (sum((_as_decimal(v) for v in vals), Decimal(0)) / len(vals)) if vals else NULL
    if name == "max": return max(vals, key=cmp_to_key(compare)) if vals else NULL
    if name == "min": return min(vals, key=cmp_to_key(compare)) if vals else NULL
    raise SQLExecutionError(f"不支持的聚合计算: {name}")

def eval_expr(expr, row, ctx):
    if isinstance(expr, BLiteral): return expr.value
    if isinstance(expr, BColumn): return row[expr.index] if expr.depth == 0 else ctx.outers[-expr.depth][expr.index]
    if isinstance(expr, BUnary):
        v = eval_expr(expr.child, row, ctx)
        if expr.op == "not": return sql_not(v)
        if expr.op == "-": return NULL if v is NULL else -_as_decimal(v)
        return v
    if isinstance(expr, BBinary): return _eval_binary(expr, row, ctx)
    if isinstance(expr, BIsNull):
        v = eval_expr(expr.child, row, ctx)
        return (v is not NULL) if expr.neg else (v is NULL)
    if isinstance(expr, BBetween):
        v = eval_expr(expr.child, row, ctx)
        lo = compare(v, eval_expr(expr.low, row, ctx))
        hi = compare(v, eval_expr(expr.high, row, ctx))
        hit = None if (lo is None or hi is None) else (lo >= 0 and hi <= 0)
        return sql_not(hit) if expr.neg else hit
    if isinstance(expr, BInList):
        v = eval_expr(expr.child, row, ctx)
        if v is NULL: return NULL
        unk = False
        for it in expr.items:
            c = compare(v, eval_expr(it, row, ctx))
            if c == 0: return False if expr.neg else True
            if c is None: unk = True
        return NULL if unk else (True if expr.neg else False)
    if isinstance(expr, BLike):
        v = eval_expr(expr.child, row, ctx)
        if v is NULL: return NULL
        hit = like_match(format_value(v), format_value(eval_expr(expr.pattern, row, ctx)), expr.escape)
        return not hit if expr.neg else hit
    if isinstance(expr, BInQuery):
        v = eval_expr(expr.child, row, ctx)
        if v is NULL: return NULL
        rows = run_plan(expr.query.plan, ctx.inside(row))
        hit = any(compare(v, r[0]) == 0 for r in rows if r)
        return not hit if expr.neg else hit
    if isinstance(expr, BExists):
        hit = bool(run_plan(expr.query.plan, ctx.inside(row)))
        return not hit if expr.neg else hit
    if isinstance(expr, BScalarQuery):
        rows = run_plan(expr.query.plan, ctx.inside(row))
        return rows[0][0] if (rows and rows[0]) else NULL
    raise SQLExecutionError("表达式计算异常")

def _eval_binary(expr, row, ctx):
    op = expr.op
    l = eval_expr(expr.left, row, ctx)
    if op in ("and", "or"):
        r = eval_expr(expr.right, row, ctx)
        return sql_and(l, r) if op == "and" else sql_or(l, r)
    r = eval_expr(expr.right, row, ctx)
    if op in ("=", "<", ">", "<=", ">=", "<>"):
        c = compare(l, r)
        if c is None: return NULL
        if op == "=": return c == 0
        if op == "<>": return c != 0
        if op == "<": return c < 0
        if op == ">": return c > 0
        if op == "<=": return c <= 0
        return c >= 0
    if l is NULL or r is NULL: return NULL
    a, b = _as_decimal(l), _as_decimal(r)
    if a is None or b is None: raise SQLExecutionError("算术操作数不为数字")
    if op == "+": return a + b
    if op == "-": return a - b
    if op == "*": return a * b
    if op in ("/", "%"):
        if b == 0: raise SQLExecutionError("除数为零 (Division by Zero)")
        return a / b if op == "/" else a % b
    raise SQLExecutionError(f"未实现的二元操作符: {op}")

def like_match(text, pattern, escape=None):
    parts, i = [], 0
    while i < len(pattern):
        ch = pattern[i]
        if escape and ch == escape and i + 1 < len(pattern):
            parts.append(re.escape(pattern[i + 1]))
            i += 2; continue
        parts.append(".*" if ch == "%" else "." if ch == "_" else re.escape(ch))
        i += 1
    return re.fullmatch("".join(parts), text, re.S) is not None

def _equality_on(where):
    if isinstance(where, Binary) and where.op == "=":
        if isinstance(where.left, ColumnRef) and _is_const(where.right): return where.left.name, where.right
        if isinstance(where.right, ColumnRef) and _is_const(where.left): return where.right.name, where.left
    return None

def _is_const(expr):
    return isinstance(expr, Literal) or (isinstance(expr, Unary) and _is_const(expr.operand))

def _const_value(bound):
    if isinstance(bound, BLiteral): return bound.value
    if isinstance(bound, BUnary) and bound.op == "-":
        v = _const_value(bound.child)
        return None if v is None else -_as_decimal(v)
    return None

# =====================================================================
# 11. 完整性约束校验 (Integrity Constraints)
# =====================================================================

def check_row(engine, table, row, skip_rid=None):
    for i, col in enumerate(table.columns):
        if col.not_null and row[i] is NULL:
            raise SQLConstraintError(f"字段 {table.name}.{col.name} 违反非空约束 (NOT NULL)")
    for cols in table.unique_sets():
        idxs = [table.require_column(c) for c in cols]
        key = tuple(encode_value(row[i]) for i in idxs)
        if any(row[i] is NULL for i in idxs): continue
        for rid, other in engine.heaps[table.key].scan():
            if skip_rid is not None and rid == skip_rid: continue
            if tuple(encode_value(other[i]) for i in idxs) == key:
                raise SQLConstraintError(f"表 {table.name} 违反唯一键约束 ({','.join(cols)})")
    for col in table.columns:
        if col.check_sql: _ensure_check(engine, table, col.check_sql, row, col.name)
    for chk in table.checks:
        _ensure_check(engine, table, chk.sql, row, None)
    for fk in table.fks:
        _ensure_fk(engine, table, fk, row)

def _ensure_check(engine, table, sql, row, col_name):
    bound = engine.bound_check(table, sql)
    val = eval_expr(bound, row, Ctx(row, (), engine))
    if truth(val) is False:
        info = f"字段 {col_name} 的 CHECK({sql})" if col_name else f"CHECK({sql})"
        raise SQLConstraintError(f"数据行不满足表 {table.name} 的约束: {info}")

def _ensure_fk(engine, table, fk, row):
    idxs = [table.require_column(c) for c in fk.cols]
    vals = [row[i] for i in idxs]
    if any(v is NULL for v in vals): return
    pk_key = engine.catalog.resolve(fk.ref_table)
    if pk_key is None: raise SQLSemanticError(f"外键目标表不存在: {fk.ref_table}")
    parent = engine.catalog.tables[pk_key]
    ref_cols = fk.ref_cols or parent.pk
    ref_idxs = [parent.require_column(c) for c in ref_cols]
    for _, prow in engine.heaps[pk_key].scan():
        if [prow[i] for i in ref_idxs] == vals: return
    raise SQLConstraintError(f"表 {table.name} 外键 ({','.join(fk.cols)}) 无法在目标表 {parent.name} 中找到匹配行")

def referencing_fks(engine, table_key):
    out = []
    for child in engine.catalog.tables.values():
        for fk in child.fks:
            if engine.catalog.resolve(fk.ref_table) == table_key: out.append((child, fk))
    return out

def delete_rows(engine, table, rids):
    for child, fk in referencing_fks(engine, table.key):
        p_idxs = [table.require_column(c) for c in (fk.ref_cols or table.pk)]
        c_idxs = [child.require_column(c) for c in fk.cols]
        for rid in rids:
            p_row = engine.heaps[table.key].get(rid)
            key = [p_row[i] for i in p_idxs]
            if any(v is NULL for v in key): continue
            for _, crow in engine.heaps[child.key].scan():
                if [crow[i] for i in c_idxs] == key:
                    raise SQLConstraintError(f"行被子表 {child.name} 引用，无法直接删除")
    for rid in rids:
        row = engine.heaps[table.key].get(rid)
        engine.heaps[table.key].delete(rid)
        for name in engine.indexes_of_table(table.key):
            engine.index_row_delete(table, row, rid, name)

def update_rows(engine, table, rid, old_row, new_row):
    diff = [i for i in range(len(old_row)) if old_row[i] != new_row[i]]
    p_idxs = [table.require_column(c) for c in table.pk] if table.pk else []
    if not any(i in p_idxs for i in diff): return
    for child, fk in referencing_fks(engine, table.key):
        c_idxs = [child.require_column(c) for c in fk.cols]
        parent_target = p_idxs[:len(c_idxs)]
        old_k = [old_row[i] for i in parent_target]
        for _, row in engine.heaps[child.key].scan():
            if [row[i] for i in c_idxs] == old_k:
                raise SQLConstraintError(f"主键正被外部表 {child.name} 引用，更新被拒绝")

# =====================================================================
# 12. 数据库执行引擎 (Engine)
# =====================================================================

class Result:
    def __init__(self, columns=None, rows=None, message=""):
        self.columns = columns or []
        self.rows = rows or []
        self.message = message

class Engine:
    def __init__(self, base_dir):
        self.dir = Path(base_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.catalog_path = self.dir / "catalog.json"
        self.catalog = Catalog.load(self.catalog_path, self.dir) if self.catalog_path.exists() else Catalog()
        self.heaps = {t.key: HeapFile(t.heap_path) for t in self.catalog.tables.values()}
        self.indexes = {}
        self.binder = Binder(self.catalog, self.indexes)
        self._check_cache = {}
        self._load_indexes()

    def flush(self):
        self.catalog.save(self.catalog_path)
        for h in self.heaps.values(): h.flush()

    def _load_indexes(self):
        for meta in self.catalog.indexes.values():
            tree = BPlusTree()
            heap = self.heaps.get(meta.table_key)
            table = self.catalog.tables.get(meta.table_key)
            if heap and table:
                for rid, row in heap.scan():
                    k = _index_key(table, meta, row)
                    if k is not None: tree.insert(k, rid)
            self.indexes[meta.name.lower()] = tree

    def indexes_of_table(self, table_key):
        return [m.name.lower() for m in self.catalog.indexes_of(table_key)]

    def index_row_add(self, table, row, rid, name):
        meta = self.catalog.indexes.get(name)
        k = _index_key(table, meta, row) if meta else None
        if k is not None: self.indexes[name].insert(k, rid)

    def index_row_delete(self, table, row, rid, name):
        meta = self.catalog.indexes.get(name)
        k = _index_key(table, meta, row) if meta else None
        if k is not None: self.indexes[name].delete(k, rid)

    def execute(self, sql):
        stmt = Parser(sql).parse_statement()
        res = self._dispatch(stmt)
        self.flush()
        return res

    def _dispatch(self, stmt):
        if isinstance(stmt, Query): return self.run_query(stmt)
        if isinstance(stmt, CreateTable): return self.create_table(stmt)
        if isinstance(stmt, DropTable): return self.drop_table(stmt)
        if isinstance(stmt, CreateIndex): return self.create_index(stmt)
        if isinstance(stmt, DropIndex): return self.drop_index(stmt)
        if isinstance(stmt, Insert): return self.insert(stmt)
        if isinstance(stmt, Update): return self.update(stmt)
        if isinstance(stmt, Delete): return self.delete(stmt)
        raise SQLExecutionError(f"未支持的语句类型: {type(stmt).__name__}")

    def run_query(self, query):
        bound = self.binder.bind_query(query)
        rows = run_plan(bound.plan, Ctx(None, (), self))
        return Result(bound.columns, rows, f"返回 {len(rows)} 行记录")

    def bound_check(self, table, sql):
        k = (table.key, sql)
        if k not in self._check_cache:
            self._check_cache[k] = self.binder.bind_expr(Parser(sql).parse_expr(), self.binder.table_scope(table))
        return self._check_cache[k]

    def create_table(self, stmt):
        key = stmt.name.lower()
        if key in self.catalog.tables: raise SQLSemanticError(f"数据表 {stmt.name} 已存在")
        cols = []
        for col in stmt.columns:
            if any(c.name.lower() == col.name.lower() for c in cols):
                raise SQLSemanticError(f"存在重复字段名: {col.name}")
            cols.append(Column(col.name, col.type, col.not_null, col.unique,
                               expr_sql(col.default) if col.default else None,
                               expr_sql(col.check) if col.check else None))
        table = TableMeta(key, stmt.name, cols)
        for c in stmt.constraints:
            if c.kind == "pk": table.pk = c.cols
            elif c.kind == "unique": table.uniques.append(c.cols)
            elif c.kind == "check": table.checks.append(CheckConstraint(expr_sql(c.expr)))
            elif c.kind == "fk": table.fks.append(_make_fk(self.catalog, stmt.name, c.ref))
        for col in stmt.columns:
            if col.references: table.fks.append(_make_fk(self.catalog, stmt.name, col.references))
        for n in table.pk: table.require_column(n)
        for u in table.uniques:
            for n in u: table.require_column(n)
        self.catalog.add_table(table)
        try:
            for fk in table.fks:
                pk_key = self.catalog.resolve(fk.ref_table)
                if not pk_key: raise SQLSemanticError(f"引用目标表不存在: {fk.ref_table}")
                parent = self.catalog.tables[pk_key]
                ref = fk.ref_cols or parent.pk
                if len(ref) != len(fk.cols):
                    raise SQLSemanticError(f"外键列数与参照列不一致 ({len(fk.cols)} != {len(ref)})")
                for n in fk.cols: table.require_column(n)
                for n in ref: parent.require_column(n)
                fk.ref_table = pk_key; fk.ref_cols = ref
        except SQLError:
            self.catalog.tables.pop(key, None)
            raise
        table.heap_path = self.dir / "tables" / f"{key}.tbl"
        self.heaps[key] = HeapFile(table.heap_path)
        self.flush()
        return Result(message=f"表 {stmt.name} 创建成功 ({len(cols)} 列)")

    def drop_table(self, stmt):
        key = self.catalog.resolve(stmt.name)
        if not key: raise SQLSemanticError(f"数据表 {stmt.name} 不存在")
        heap = self.heaps.pop(key, None)
        if heap:
            heap.pages.clear()
            if heap.path.exists(): heap.path.unlink()
        for n in [name for name, m in self.catalog.indexes.items() if m.table_key == key]:
            self.catalog.indexes.pop(n, None)
            self.indexes.pop(n, None)
        self.catalog.drop_table(key)
        self.flush()
        return Result(message=f"表 {stmt.name} 已删除")

    def create_index(self, stmt):
        name = stmt.name.lower()
        if name in self.catalog.indexes: raise SQLSemanticError(f"索引 {stmt.name} 已存在")
        key = self.catalog.resolve(stmt.table)
        if not key: raise SQLSemanticError(f"数据表 {stmt.table} 不存在")
        tbl = self.catalog.tables[key]
        cols = []
        for col_name, desc in stmt.cols:
            tbl.require_column(col_name)
            cols.append((tbl.columns[tbl.column_index(col_name)].name, desc))
        meta = IndexMeta(name, key, cols, stmt.unique)
        tree = BPlusTree()
        for rid, row in self.heaps[key].scan():
            k = _index_key(tbl, meta, row)
            if k is not None:
                if stmt.unique and tree.search(k):
                    raise SQLConstraintError(f"无法在含重复数据的列上创建唯一索引: {','.join(c for c, _ in cols)}")
                tree.insert(k, rid)
        self.catalog.indexes[name] = meta
        self.indexes[name] = tree
        self.flush()
        return Result(message=f"索引 {stmt.name} 创建成功")

    def drop_index(self, stmt):
        name = stmt.name.lower()
        if name not in self.catalog.indexes: raise SQLSemanticError(f"索引 {stmt.name} 不存在")
        self.catalog.indexes.pop(name)
        self.indexes.pop(name, None)
        self.flush()
        return Result(message=f"索引 {stmt.name} 已删除")

    def insert(self, stmt):
        key = self.catalog.resolve(stmt.table)
        if not key: raise SQLSemanticError(f"目标表 {stmt.table} 不存在")
        table = self.catalog.tables[key]
        heap = self.heaps[key]
        indices = [table.require_column(c) for c in stmt.columns] if stmt.columns else list(range(table.width))
        if stmt.query:
            b_query = self.binder.bind_query(stmt.query)
            source_rows = run_plan(b_query.plan, Ctx(None, (), self))
            if len(b_query.columns) != len(indices):
                raise SQLSemanticError("查询投影列数与写入列数不匹配")
        else:
            scope = self.binder.table_scope(table)
            source_rows = []
            for r_exprs in stmt.rows:
                if len(r_exprs) != len(indices): raise SQLSemanticError("VALUES 传参数量与目标列不符")
                bound = [self.binder.bind_expr(e, scope, allow_agg=False) for e in r_exprs]
                source_rows.append([eval_expr(b, [], Ctx([], (), self)) for b in bound])
        count = 0
        idx_names = self.indexes_of_table(key)
        for row_src in source_rows:
            vals = [NULL] * table.width
            for p, cidx in enumerate(indices): vals[cidx] = row_src[p]
            for i, c in enumerate(table.columns):
                if vals[i] is NULL and c.default_sql:
                    b_def = self.bound_check(table, c.default_sql)
                    vals[i] = eval_expr(b_def, vals, Ctx(vals, (), self))
            vals = [coerce(vals[i], table.columns[i].type) for i in range(table.width)]
            check_row(self, table, vals)
            rid = heap.insert(vals)
            for n in idx_names: self.index_row_add(table, vals, rid, n)
            count += 1
        return Result(message=f"成功插入 {count} 行至表 {table.name}")

    def update(self, stmt):
        key = self.catalog.resolve(stmt.table)
        if not key: raise SQLSemanticError(f"目标表 {stmt.table} 不存在")
        table = self.catalog.tables[key]
        heap = self.heaps[key]
        scope = self.binder.table_scope(table)
        where = self.binder.bind_expr(stmt.where, scope, allow_agg=False) if stmt.where else None
        assigns = [(table.require_column(c), self.binder.bind_expr(e, scope, allow_agg=False)) for c, e in stmt.assignments]
        targets = []
        for rid, row in heap.scan():
            if where and truth(eval_expr(where, row, Ctx(row, (), self))) is not True: continue
            new_r = list(row)
            for idx, expr in assigns: new_r[idx] = eval_expr(expr, row, Ctx(row, (), self))
            new_r = [coerce(new_r[i], table.columns[i].type) for i in range(table.width)]
            targets.append((rid, row, new_r))
        idx_names = self.indexes_of_table(key)
        for rid, old_r, new_r in targets:
            check_row(self, table, new_r, skip_rid=rid)
            update_rows(self, table, rid, old_r, new_r)
            for n in idx_names: self.index_row_delete(table, old_r, rid, n)
            heap.update(rid, new_r)
            for n in idx_names: self.index_row_add(table, new_r, rid, n)
        return Result(message=f"成功更新 {len(targets)} 行记录")

    def delete(self, stmt):
        key = self.catalog.resolve(stmt.table)
        if not key: raise SQLSemanticError(f"目标表 {stmt.table} 不存在")
        table = self.catalog.tables[key]
        heap = self.heaps[key]
        scope = self.binder.table_scope(table)
        where = self.binder.bind_expr(stmt.where, scope, allow_agg=False) if stmt.where else None
        rids = [rid for rid, row in heap.scan() if where is None or truth(eval_expr(where, row, Ctx(row, (), self))) is True]
        delete_rows(self, table, rids)
        return Result(message=f"成功删除 {len(rids)} 行记录")

def _make_fk(catalog, tname, ref):
    k = catalog.resolve(ref.ref_table)
    return ForeignKey(ref.cols, k or ref.ref_table.lower(), ref.ref_cols, ref.on_delete, ref.on_update)

def _index_key(table, meta, row):
    vals = []
    for cname, _ in meta.cols:
        idx = table.column_index(cname)
        if idx is None: return None
        v = row[idx]
        if v is NULL: return None
        vals.append(encode_value(v))
    return tuple(vals)

# =====================================================================
# 13. 样例初始数据集
# =====================================================================

DEMO_SCRIPT = """
CREATE TABLE Students (
    Sno char(11) PRIMARY KEY,
    Sname char(20) UNIQUE,
    Ssex char(2),
    Sage int,
    Sdept char(20)
);
CREATE TABLE Courses (
    Cno char(4) PRIMARY KEY,
    Cname char(40),
    Cpno char(4),
    Ccredit int,
    FOREIGN KEY (Cpno) REFERENCES Courses(Cno) ON DELETE SET NULL
);
CREATE TABLE SC (
    Sno char(11),
    Cno char(4),
    Grade int CHECK (Grade >= 0 AND Grade <= 100),
    PRIMARY KEY (Sno, Cno),
    FOREIGN KEY (Sno) REFERENCES Students(Sno) ON DELETE CASCADE ON UPDATE CASCADE,
    FOREIGN KEY (Cno) REFERENCES Courses(Cno) ON DELETE CASCADE ON UPDATE CASCADE
);
INSERT INTO Students VALUES
 ('20240001', '张明', '男', 20, 'CS'),
 ('20240002', '李华', '女', 21, 'IS'),
 ('20240003', '王博', '男', 19, 'MA'),
 ('20240004', '赵敏', '女', 20, 'CS'),
 ('20240005', '孙浩', '男', 22, 'IS');
INSERT INTO Courses VALUES
 ('C01', '数据结构', NULL, 4),
 ('C02', '操作系统', 'C01', 3),
 ('C03', '数据库系统原理', 'C02', 4);
INSERT INTO SC VALUES
 ('20240001', 'C01', 92),
 ('20240001', 'C02', 88),
 ('20240002', 'C01', 84),
 ('20240003', 'C03', 95);
CREATE INDEX idx_sc_grade ON SC(Grade);
"""

# =====================================================================
# 14. 现代化可视化工作台 (Modern UI)
# =====================================================================

CLR_BG = "#F1F5F9"         # Slate-100 背景底色
CLR_PANEL = "#FFFFFF"      # 白色卡片背景
CLR_PRIMARY = "#2563EB"    # Blue-600 主色
CLR_PRIMARY_HOV = "#1D4ED8"# Blue-700 悬浮
CLR_SIDEBAR = "#0F172A"    # Slate-900 头部深底
CLR_TEXT = "#0F172A"       # 正文字色
CLR_MUTED = "#64748B"      # 灰色辅助字色
CLR_BORDER = "#E2E8F0"     # 边框线条色
CLR_SUCCESS = "#16A34A"    # 成功绿
CLR_DANGER = "#DC2626"     # 错误红
CLR_ODD_ROW = "#F8FAFC"    # 表格斑马纹浅灰

FONT_CODE = ("Consolas", 10)
FONT_CODE_BOLD = ("Consolas", 10, "bold")
FONT_UI = ("Microsoft YaHei UI", 9)
FONT_UI_BOLD = ("Microsoft YaHei UI", 9, "bold")
FONT_TITLE = ("Microsoft YaHei UI", 11, "bold")

class Workbench(tk.Tk):
    def __init__(self, engine, db_path):
        super().__init__()
        self.engine = engine
        self.db_path = str(db_path)
        self.title("MiniSQL Engine Studio · 数据库内核工作台")
        self.geometry("1260x820")
        self.minsize(1020, 640)
        self.configure(bg=CLR_BG)

        self._init_styles()
        self._build_layout()
        self._setup_sql_highlighter()
        self._refresh_tables()
        self.set_status(f"就绪 · 存储路径: {self.db_path}")

    def _init_styles(self):
        st = ttk.Style(self)
        try: st.theme_use("clam")
        except tk.TclError: pass

        st.configure(".", background=CLR_BG, foreground=CLR_TEXT, font=FONT_UI)
        st.configure("TNotebook", background=CLR_BG, borderwidth=0)
        st.configure("TNotebook.Tab", background="#E2E8F0", foreground=CLR_MUTED,
                     padding=(20, 8), font=FONT_UI_BOLD, borderwidth=0)
        st.map("TNotebook.Tab",
               background=[("selected", CLR_PANEL)],
               foreground=[("selected", CLR_PRIMARY)])

        st.configure("Primary.TButton", background=CLR_PRIMARY, foreground="#FFFFFF",
                     borderwidth=0, focusthickness=0, padding=(14, 6), font=FONT_UI_BOLD)
        st.map("Primary.TButton", background=[("active", CLR_PRIMARY_HOV)])

        st.configure("Secondary.TButton", background="#F8FAFC", foreground=CLR_TEXT,
                     bordercolor=CLR_BORDER, borderwidth=1, focusthickness=0, padding=(10, 5))
        st.map("Secondary.TButton", background=[("active", "#E2E8F0")])

        st.configure("Treeview", background=CLR_PANEL, fieldbackground=CLR_PANEL,
                     foreground=CLR_TEXT, rowheight=28, borderwidth=0, font=FONT_UI)
        st.configure("Treeview.Heading", background="#F8FAFC", foreground=CLR_MUTED,
                     font=FONT_UI_BOLD, borderwidth=1, relief="flat")
        st.map("Treeview.Heading", background=[("active", "#EDF2F7")])

    def _build_layout(self):
        # 顶部导航栏
        header = tk.Frame(self, bg=CLR_SIDEBAR, height=54)
        header.pack(fill="x")
        header.pack_propagate(False)

        title_box = tk.Frame(header, bg=CLR_SIDEBAR)
        title_box.pack(side="left", padx=16, pady=10)
        tk.Label(title_box, text="⚡ MiniSQL", fg="#38BDF8", bg=CLR_SIDEBAR, font=("Consolas", 13, "bold")).pack(side="left")
        tk.Label(title_box, text=" | 关系数据库内核工作台", fg="#94A3B8", bg=CLR_SIDEBAR, font=FONT_UI).pack(side="left", padx=4)

        self.db_badge = tk.Label(header, text=f"DISK: {Path(self.db_path).name}", fg="#CBD5E1",
                                 bg="#1E293B", font=FONT_CODE, padx=10, pady=3)
        self.db_badge.pack(side="right", padx=16)

        # 核心主分割区
        main_paned = tk.PanedWindow(self, orient="horizontal", bg=CLR_BG, sashwidth=6, bd=0)
        main_paned.pack(fill="both", expand=True, padx=14, pady=12)

        # 左侧面板：SQL 编辑区
        left_card = tk.Frame(main_paned, bg=CLR_PANEL, bd=1, relief="solid", highlightthickness=0)
        left_card.configure(highlightbackground=CLR_BORDER)
        main_paned.add(left_card, minsize=380, width=490)

        # SQL 工具条
        sql_bar = tk.Frame(left_card, bg=CLR_PANEL)
        sql_bar.pack(fill="x", padx=14, pady=(12, 6))
        tk.Label(sql_bar, text="SQL 编辑器", font=FONT_UI_BOLD, bg=CLR_PANEL, fg=CLR_TEXT).pack(side="left")

        # 示例快捷下拉
        self.sample_var = tk.StringVar(value="快捷查询模板...")
        sample_cb = ttk.Combobox(sql_bar, textvariable=self.sample_var, width=18, state="readonly",
                                 values=[
                                     "选择: 简单投影查询",
                                     "多表: 自然等值连接",
                                     "分组: 成绩均值统计",
                                     "嵌套: IN 子查询",
                                     "重置: 插入新选课"
                                 ])
        sample_cb.pack(side="right")
        sample_cb.bind("<<ComboboxSelected>>", self._load_template)

        # 编辑器文本框
        text_frame = tk.Frame(left_card, bg=CLR_PANEL)
        text_frame.pack(fill="both", expand=True, padx=14, pady=6)
        self.sql_text = tk.Text(text_frame, wrap="none", font=FONT_CODE, bg="#FFFFFF", fg=CLR_TEXT,
                                insertbackground=CLR_PRIMARY, selectbackground="#BFDBFE",
                                bd=1, relief="solid", undo=True, padx=10, pady=8)
        self.sql_text.pack(fill="both", expand=True)
        self.sql_text.insert("1.0", "-- 输入 SQL 语句 (Ctrl+Enter 快速执行)\nSELECT Sno, Sname, Sdept\nFROM Students\nWHERE Sdept = 'CS';\n")
        self.sql_text.bind("<Control-Return>", lambda e: (self.execute_sql(), "break")[1])

        # 编辑器底部动作条
        act_bar = tk.Frame(left_card, bg=CLR_PANEL)
        act_bar.pack(fill="x", padx=14, pady=(8, 14))
        ttk.Button(act_bar, text="执行 SQL (Ctrl+Enter)", style="Primary.TButton", command=self.execute_sql).pack(side="left")
        ttk.Button(act_bar, text="查看执行计划", style="Secondary.TButton", command=self.explain_sql).pack(side="left", padx=8)
        ttk.Button(act_bar, text="清空", style="Secondary.TButton",
                   command=lambda: self.sql_text.delete("1.0", "end")).pack(side="right")

        # 右侧面板：结果展示 Notebook
        right_card = tk.Frame(main_paned, bg=CLR_BG)
        main_paned.add(right_card, minsize=520)

        self.nb = ttk.Notebook(right_card)
        self.nb.pack(fill="both", expand=True)

        self._build_res_tab()
        self._build_plan_tab()
        self._build_data_tab()

        # 底部状态栏
        bottom_bar = tk.Frame(self, bg=CLR_PANEL, height=32, bd=1, relief="solid")
        bottom_bar.pack(fill="x", side="bottom")
        bottom_bar.pack_propagate(False)
        self.status_var = tk.StringVar(value="准备就绪")
        tk.Label(bottom_bar, textvariable=self.status_var, font=FONT_UI, bg=CLR_PANEL, fg=CLR_MUTED,
                 anchor="w", padx=14).pack(side="left", fill="both", expand=True)

    def _build_res_tab(self):
        tab = tk.Frame(self.nb, bg=CLR_PANEL)
        self.nb.add(tab, text=" 查询结果 ")

        info_bar = tk.Frame(tab, bg=CLR_PANEL)
        info_bar.pack(fill="x", padx=16, pady=(12, 6))
        self.res_badge = tk.Label(info_bar, text="IDLE", font=FONT_UI_BOLD, bg="#E2E8F0", fg=CLR_MUTED, padx=8, pady=2)
        self.res_badge.pack(side="left")
        self.res_detail = tk.Label(info_bar, text="尚未执行查询", font=FONT_UI, bg=CLR_PANEL, fg=CLR_MUTED)
        self.res_detail.pack(side="left", padx=8)

        self.res_tree, self.res_tree_box = self._create_modern_tree(tab)
        self.res_tree_box.pack(fill="both", expand=True, padx=16, pady=6)

        # 专用反馈提示行
        self.msg_banner = tk.Label(tab, text="", font=FONT_UI, bg="#F8FAFC", fg=CLR_TEXT, anchor="w", padx=12, pady=6)
        self.msg_banner.pack(fill="x", side="bottom", padx=16, pady=(0, 10))

    def _build_plan_tab(self):
        tab = tk.Frame(self.nb, bg=CLR_PANEL)
        self.nb.add(tab, text=" 执行计划树 ")

        pbar = tk.Frame(tab, bg=CLR_PANEL)
        pbar.pack(fill="x", padx=16, pady=(12, 6))
        tk.Label(pbar, text="查询代数优化与算子执行树 (Query Plan)", font=FONT_UI_BOLD, bg=CLR_PANEL, fg=CLR_TEXT).pack(side="left")
        tk.Label(pbar, text="缩进代表嵌套算子依赖关系", font=FONT_UI, bg=CLR_PANEL, fg=CLR_MUTED).pack(side="right")

        self.plan_text = tk.Text(tab, font=FONT_CODE, bg="#0F172A", fg="#E2E8F0", bd=0, padx=14, pady=12)
        self.plan_text.pack(fill="both", expand=True, padx=16, pady=(4, 14))

    def _build_data_tab(self):
        tab = tk.Frame(self.nb, bg=CLR_PANEL)
        self.nb.add(tab, text=" 表数据字典与全表浏览 ")

        ctrl_bar = tk.Frame(tab, bg=CLR_PANEL)
        ctrl_bar.pack(fill="x", padx=16, pady=(12, 8))
        tk.Label(ctrl_bar, text="目标数据表:", font=FONT_UI, bg=CLR_PANEL, fg=CLR_MUTED).pack(side="left")
        self.table_selector = ttk.Combobox(ctrl_bar, state="readonly", width=22)
        self.table_selector.pack(side="left", padx=6)
        self.table_selector.bind("<<ComboboxSelected>>", lambda e: self.load_table_data())
        ttk.Button(ctrl_bar, text="刷新表", style="Secondary.TButton", command=self._refresh_tables).pack(side="left", padx=4)

        self.table_stat_lbl = tk.Label(ctrl_bar, text="", font=FONT_UI, bg=CLR_PANEL, fg=CLR_MUTED)
        self.table_stat_lbl.pack(side="right")

        self.data_tree, self.data_tree_box = self._create_modern_tree(tab)
        self.data_tree_box.pack(fill="both", expand=True, padx=16, pady=(4, 14))

    def _create_modern_tree(self, parent):
        wrap = tk.Frame(parent, bg=CLR_PANEL, bd=1, relief="solid")
        tree = ttk.Treeview(wrap, show="headings", selectmode="browse")
        tree.grid(row=0, column=0, sticky="nsew")
        ysb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        ysb.grid(row=0, column=1, sticky="ns")
        xsb = ttk.Scrollbar(wrap, orient="horizontal", command=tree.xview)
        xsb.grid(row=1, column=0, sticky="ew")
        tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)

        tree.tag_configure("odd", background=CLR_ODD_ROW)
        tree.tag_configure("even", background=CLR_PANEL)
        return tree, wrap

    def _setup_sql_highlighter(self):
        kw_pattern = r"\b(SELECT|FROM|WHERE|INSERT|INTO|VALUES|UPDATE|SET|DELETE|CREATE|DROP|TABLE|INDEX|ON|AND|OR|NOT|IN|LIKE|BETWEEN|IS|NULL|GROUP|BY|HAVING|ORDER|LIMIT|OFFSET|JOIN|INNER|PRIMARY|KEY|UNIQUE|FOREIGN|REFERENCES|CHECK|CASCADE|RESTRICT)\b"
        self.sql_text.tag_configure("kw", foreground="#2563EB", font=FONT_CODE_BOLD)
        self.sql_text.tag_configure("str", foreground="#16A34A")
        self.sql_text.tag_configure("comment", foreground="#94A3B8")

        def _rehighlight(event=None):
            text = self.sql_text.get("1.0", "end")
            self.sql_text.tag_remove("kw", "1.0", "end")
            self.sql_text.tag_remove("str", "1.0", "end")
            self.sql_text.tag_remove("comment", "1.0", "end")

            for m in re.finditer(kw_pattern, text, re.IGNORECASE):
                self.sql_text.tag_add("kw", f"1.0+{m.start()}c", f"1.0+{m.end()}c")
            for m in re.finditer(r"'.*?'", text):
                self.sql_text.tag_add("str", f"1.0+{m.start()}c", f"1.0+{m.end()}c")
            for m in re.finditer(r"--.*$", text, re.MULTILINE):
                self.sql_text.tag_add("comment", f"1.0+{m.start()}c", f"1.0+{m.end()}c")

        self.sql_text.bind("<KeyRelease>", _rehighlight)
        _rehighlight()

    def _load_template(self, event=None):
        tpl = {
            "选择: 简单投影查询": "SELECT Sno, Sname, Sage FROM Students WHERE Sage >= 20;",
            "多表: 自然等值连接": "SELECT Students.Sname, SC.Grade\nFROM Students\nJOIN SC ON Students.Sno = SC.Sno\nWHERE SC.Grade >= 85;",
            "分组: 成绩均值统计": "SELECT Cno, COUNT(*) AS Total, AVG(Grade) AS AvgGrade\nFROM SC\nGROUP BY Cno\nHAVING COUNT(*) >= 1;",
            "嵌套: IN 子查询": "SELECT Sname FROM Students WHERE Sno IN (\n    SELECT Sno FROM SC WHERE Grade > 90\n);",
            "重置: 插入新选课": "INSERT INTO SC VALUES ('20240004', 'C03', 91);"
        }.get(self.sample_var.get())
        if tpl:
            self.sql_text.delete("1.0", "end")
            self.sql_text.insert("1.0", tpl)
            self.sql_text.event_generate("<KeyRelease>")

    def set_status(self, text):
        self.status_var.set(f"[{time.strftime('%H:%M:%S')}] {text}")

    def _refresh_tables(self):
        keys = sorted(self.engine.catalog.tables.keys())
        self.table_selector.configure(values=keys)
        if keys and not self.table_selector.get():
            self.table_selector.set(keys[0])
        self.load_table_data()

    def load_table_data(self):
        self.data_tree.delete(*self.data_tree.get_children())
        tkey = self.table_selector.get()
        if not tkey:
            self.table_stat_lbl.configure(text="当前无可用数据表")
            return
        tbl = self.engine.catalog.tables.get(tkey)
        heap = self.engine.heaps.get(tkey)
        if not tbl or not heap: return

        rows = [r for _, r in heap.scan()]
        cols = [c.name for c in tbl.columns]
        cids = [f"c_{i}" for i in range(len(cols))]
        self.data_tree.configure(columns=cids)

        for i, cname in enumerate(cols):
            self.data_tree.heading(cids[i], text=cname)
            self.data_tree.column(cids[i], width=max(100, min(240, 14 * len(cname) + 30)), anchor="w")

        for idx, r in enumerate(rows):
            tag = "even" if idx % 2 == 0 else "odd"
            self.data_tree.insert("", "end", values=[format_value(v) for v in r], tags=(tag,))

        self.table_stat_lbl.configure(text=f"共 {len(rows)} 行记录 · 占用 {heap.page_count} 数据页")

    def execute_sql(self):
        raw_sql = self.sql_text.get("1.0", "end").strip()
        stmts = split_statements(raw_sql)
        if not stmts:
            self.set_status("未检测到有效 SQL 语句")
            return

        t_start = time.perf_counter()
        done, last_res = 0, None

        for stmt in stmts:
            try:
                last_res = self.engine.execute(stmt)
                done += 1
            except SQLError as err:
                elapsed = (time.perf_counter() - t_start) * 1000
                self._render_error(str(err), elapsed)
                return
            except Exception as ex:
                elapsed = (time.perf_counter() - t_start) * 1000
                self._render_error(f"系统运行异常: {ex}", elapsed)
                return

        elapsed = (time.perf_counter() - t_start) * 1000
        self._refresh_tables()
        if last_res:
            self._render_success(last_res, done, elapsed)
            self.nb.select(0)

    def _render_success(self, res, count, elapsed):
        self.res_tree.delete(*self.res_tree.get_children())
        cols, rows = res.columns, res.rows

        if cols:
            cids = [f"c_{i}" for i in range(len(cols))]
            self.res_tree.configure(columns=cids)
            for i, c in enumerate(cols):
                self.res_tree.heading(cids[i], text=c)
                self.res_tree.column(cids[i], width=max(110, min(260, 14 * len(c) + 40)), anchor="w")
            for idx, r in enumerate(rows):
                tag = "even" if idx % 2 == 0 else "odd"
                self.res_tree.insert("", "end", values=[format_value(v) for v in r], tags=(tag,))
        else:
            self.res_tree.configure(columns=["status"])
            self.res_tree.heading("status", text="执行状态")
            self.res_tree.column("status", width=400, anchor="w")
            self.res_tree.insert("", "end", values=[res.message or "执行成功"])

        self.res_badge.configure(text="SUCCESS", bg="#DCFCE7", fg=CLR_SUCCESS)
        self.res_detail.configure(text=f"耗时 {elapsed:.2f} ms · {count} 句执行完成 · {len(rows)} 行")
        self.msg_banner.configure(text=f"✔ {res.message}", fg=CLR_SUCCESS, bg="#F0FDF4")
        self.set_status(f"执行成功: {count} 条语句处理完成, 耗时 {elapsed:.2f} ms")

    def _render_error(self, err_msg, elapsed):
        self.res_tree.delete(*self.res_tree.get_children())
        self.res_tree.configure(columns=["err"])
        self.res_tree.heading("err", text="错误诊断报告")
        self.res_tree.column("err", width=680, anchor="w")
        self.res_tree.insert("", "end", values=[err_msg])

        self.res_badge.configure(text="FAILED", bg="#FEE2E2", fg=CLR_DANGER)
        self.res_detail.configure(text=f"中断于 {elapsed:.2f} ms")
        self.msg_banner.configure(text=f"✖ {err_msg}", fg=CLR_DANGER, bg="#FEF2F2")
        self.set_status(f"执行失败: {err_msg}")
        self.nb.select(0)

    def explain_sql(self):
        sql = self.sql_text.get("1.0", "end").strip().rstrip(";")
        if not sql: return
        try:
            stmt = Parser(sql).parse_statement()
            if not isinstance(stmt, Query):
                self._show_plan_text("该语句为 DDL / DML 操纵命令，无查询优化执行计划树。")
            else:
                lines = self.engine.binder.bind_query(stmt).plan.tree()
                self._show_plan_text("\n".join(lines))
                self.set_status(f"执行计划生成完毕 · 共 {len(lines)} 个操作节点")
            self.nb.select(1)
        except SQLError as err:
            self._show_plan_text(f"无法生成执行计划树:\n{err}")
            self.nb.select(1)

    def _show_plan_text(self, text):
        self.plan_text.delete("1.0", "end")
        self.plan_text.insert("1.0", text)

# =====================================================================
# 15. 程序启动入口
# =====================================================================

def bootstrap(data_dir=".kernel_db", load_mock=True):
    engine = Engine(data_dir)
    if load_mock and not engine.catalog.tables:
        for stmt in split_statements(DEMO_SCRIPT):
            engine.execute(stmt)
    app = Workbench(engine, data_dir)
    app.mainloop()

if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else ".kernel_db"
    bootstrap(target)
