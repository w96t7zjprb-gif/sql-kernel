import datetime
import json
import os
import time
from bisect import bisect_left, bisect_right
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

import datetime
import json
import os
import re
import sys
import time
import tkinter as tk
from bisect import bisect_left, bisect_right
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import cmp_to_key
from pathlib import Path
from tkinter import messagebox, ttk


class SQLError(Exception):
    code = "SQLError"
    def __init__(self, message):
        self.message = message
        super().__init__(message)
    def __str__(self): return "[%s] %s" % (self.code, self.message)
class SQLSyntaxError(SQLError):code = "SyntaxError"
class SQLSemanticError(SQLError):code = "SemanticError"
class SQLConstraintError(SQLError):code = "ConstraintError"
class SQLExecutionError(SQLError):
    code = "ExecutionError"

#词法分析
OPERATORS = ["<=", ">=", "<>", "!=", "!>", "!<", "=", ">", "<", "+", "-", "*", "/", "%"]

class Token:
    __slots__ = ("kind", "value", "text")
    def __init__(self, kind, value, text):
        self.kind = kind
        self.value = value
        self.text = text
#词法分析器
def tokenize(sql):
    tokens = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch.isspace():
            i += 1
            continue
        #单行注释 ：一直读到换行，换行也跳过；到末尾没有换行就直接跳到结尾
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            if j < 0: raise SQLSyntaxError("块注释没有闭合")
            i = j + 2
            continue
        if ch == "'":
            value, i = _read_string(sql, i, "'")
            tokens.append(Token("str", value, value))
            continue
        if ch == '"':
            value, i = _read_string(sql, i, '"')
            tokens.append(Token("qid", value, value))
            continue
        if ch.isdigit():
            j = i
            while j < n and (sql[j].isdigit() or sql[j] == "."): j += 1
            text = sql[i:j]
            tokens.append(Token("num", float(text) if "." in text else int(text), text))
            i = j
            continue
        if ch.isalpha() or ch == "_":
            j = i
            while j < n and (sql[j].isalnum() or sql[j] in "_$"): j += 1
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
            else:
                raise SQLSyntaxError("无法识别的字符 %r" % ch)
            i += 1
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
    raise SQLSyntaxError("字符串没有闭合")

NULL = None
INT_KINDS = {"smallint", "int"}
NUM_KINDS = ("numeric", "decimal")
#判断类型
class SQLType:
    __slots__ = ("kind", "length", "precision", "scale")
    def __init__(self, kind, length=None, precision=None, scale=None):
        self.kind = kind
        self.length = length
        self.precision = precision
        self.scale = scale
    def to_json(self):
        return {"kind": self.kind, "length": self.length, "precision": self.precision, "scale": self.scale}
    @staticmethod
    #从字典恢复对象
    def from_json(obj):
        return SQLType(obj["kind"], obj.get("length"), obj.get("precision"), obj.get("scale"))
def parse_type(name, args=None):
    args = list(args or [])
    if name in ("char", "character"): return SQLType("char", int(args[0]) if args else 1)
    if name in ("varchar", "character varying"): return SQLType("varchar", int(args[0]) if args else 255)
    if name in INT_KINDS or name == "bigint": return SQLType("int" if name == "bigint" else name)
    if name in NUM_KINDS:return SQLType("numeric", precision=int(args[0]) if args else 10, scale=int(args[1]) if len(args) > 1 else 0)
    if name == "date": return SQLType("date")
    raise SQLSyntaxError("不支持的数据类型 %s" % name)
def _as_decimal(v):
    if isinstance(v, bool): return Decimal(1 if v else 0)
    if isinstance(v, Decimal): return v
    if isinstance(v, int): return Decimal(v)
    if isinstance(v, float): return Decimal(str(v))
    if isinstance(v, str):
        s = v.strip()
        if not s: return None
        try: return Decimal(s)
        except InvalidOperation: return None
    return None
#类型转换
def coerce(value, t):
    if value is NULL or t is None: return value
    kind = t.kind
    if kind in ("char", "varchar"):
        if not isinstance(value, str): value = format_value(value)
        if t.length and len(value) > t.length: raise SQLSemanticError("值 %r 超出 %s 的长度" % (value, t))
        return value
    if kind in INT_KINDS:
        d = _as_decimal(value)
        if d is None or d != d.to_integral_value(): raise SQLSemanticError("%r 不能当整数用" % (value,))
        return int(d)
    if kind == "numeric":
        d = _as_decimal(value)
        if d is None: raise SQLSemanticError("%r 不能当数值用" % (value,))
        # SQL 的 NUMERIC 是四舍五入，Python 默认是银行家舍入，得显式指定
        return d.quantize(Decimal(1).scaleb(-(t.scale or 0)), rounding=ROUND_HALF_UP)
    if kind == "date": return _parse_date(value)
    return value
def _parse_date(v):
    if isinstance(v, datetime.date): return v
    try: return datetime.date.fromisoformat(str(v).strip())
    except (ValueError, TypeError): raise SQLSemanticError("%r 不是合法日期（要 yyyy-mm-dd）" % (v,))
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
#排序，升序：NULL 排在最后；降序：NULL 排在最前面
def compare_for_sort(a, b, desc=False):
    if a is NULL and b is NULL: return 0
    if a is NULL: return -1 if desc else 1
    if b is NULL: return 1 if desc else -1
    c = compare(a, b) or 0
    return -c if desc else c
#值变成字符串，用于持久化存文件
def encode_value(v):
    if v is NULL: return "N"
    if isinstance(v, bool): return "B:1" if v else "B:0"
    if isinstance(v, int): return "I:%d" % v
    if isinstance(v, Decimal): return "D:%s" % v
    if isinstance(v, datetime.date): return "T:%s" % v.isoformat()
    return "S:%s" % v
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

# ========== 页与堆文件 ==========
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
                base = HEADER_SIZE + i * SLOT_SIZE
                self.slots.append((int.from_bytes(raw[base:base + 2], "little"), int.from_bytes(raw[base + 2:base + 4], "little")))
    #剩余空间
    def free_space(self):
        return self.free_end - HEADER_SIZE - SLOT_SIZE * self.n_slots
    #是否能放下一条 size 大小的 blob
    def can_hold(self, size):
        return size + SLOT_SIZE <= self.free_space()
    #头部信息
    def _write_header(self):
        self.data[0:2] = self.n_slots.to_bytes(2, "little")
        self.data[2:4] = self.free_end.to_bytes(2, "little")
        for i, (off, ln) in enumerate(self.slots):
            base = HEADER_SIZE + i * SLOT_SIZE
            self.data[base:base + 2] = off.to_bytes(2, "little")
            self.data[base + 2:base + 4] = ln.to_bytes(2, "little")
    #碎片整理
    def compact(self):
        live = [(i, off, ln) for i, (off, ln) in enumerate(self.slots) if ln > 0]
        new_slots = [(0, 0)] * self.n_slots
        cursor = PAGE_SIZE
        content = bytearray()
        for i, off, ln in live:
            content = bytes(self.data[off:off + ln]) + content
            cursor -= ln
            new_slots[i] = (cursor, ln)
        self.data = bytearray(PAGE_SIZE)
        if content: self.data[cursor:PAGE_SIZE] = content
        self.slots = new_slots
        self.free_end = cursor
        self._write_header()
    #插入一条记录
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
    #根据 slot 号取出 blob
    def get(self, slot):
        off, ln = self.slots[slot]
        return bytes(self.data[off:off + ln]) if ln else None
    def delete(self, slot):
        self.slots[slot] = (0, 0)
        self._write_header()
    # 能在原地放下就原地改，否则删了重插
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
    #遍历
    def iter_records(self):
        for slot, (off, ln) in enumerate(self.slots):
            if ln > 0: yield slot, bytes(self.data[off:off + ln])
    #写磁盘
    def to_bytes(self):
        self._write_header()
        return bytes(self.data)
    #堆文件管理
class HeapFile:
    def __init__(self, path, max_pages=256):
        self.path = Path(path)
        self.max_pages = max_pages
        self.pages = {}
        self.dirty = set()
        self.page_count = 0
        if self.path.exists(): self.page_count = self.path.stat().st_size // PAGE_SIZE
    #加载一页进缓存，缓存里没有就从磁盘读
    def _load_page(self, page_no):
        if page_no not in self.pages:
            page = Page(page_no)
            if page_no < self.page_count:
                with open(self.path, "rb") as fh:
                    fh.seek(page_no * PAGE_SIZE)
                    raw = fh.read(PAGE_SIZE)
                if len(raw) == PAGE_SIZE: page = Page(page_no, raw)
            self.pages[page_no] = page
        return self.pages[page_no]
    #写回磁盘
    def _write_page(self, page):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "r+b" if self.path.exists() else "w+b") as fh:
            if page.page_no >= self.page_count: self.page_count = page.page_no + 1
            fh.seek(page.page_no * PAGE_SIZE)
            fh.write(page.to_bytes())
            need = self.page_count * PAGE_SIZE
            if fh.seek(0, os.SEEK_END) < need: fh.write(b"\x00" * (need - fh.tell()))
        self.dirty.discard(page.page_no)
    #缓存超了就淘汰：先挑干净的页，全是脏页就先写一页再淘汰。
    def _evict(self):
        while len(self.pages) > self.max_pages:
            victim = next((no for no in self.pages if no not in self.dirty), None)
            if victim is None:
                victim = next(iter(self.pages))
                self._write_page(self.pages[victim])
            self.pages.pop(victim, None)
    #插入一行：从最后一页向前找能放下的页；找不到就新建页
    def insert(self, values):
        blob = _encode_row(values)
        for page_no in range(self.page_count - 1, -1, -1):   # 从最后一页往前找
            slot = self._load_page(page_no).insert(blob)
            if slot is not None:
                self.dirty.add(page_no)
                return (page_no, slot)
        page = self._load_page(self.page_count)
        self.page_count += 1
        slot = page.insert(blob)
        if slot is None: raise SQLExecutionError("一行超过一页，放不下")
        self.dirty.add(page.page_no)
        self._evict()
        return (page.page_no, slot)
    #读一行
    def get(self, rid):
        raw = self._load_page(rid[0]).get(rid[1])
        return None if raw is None else _decode_row(raw)
    def update(self, rid, values):
        page = self._load_page(rid[0])
        if not page.update(rid[1], _encode_row(values)): raise SQLExecutionError("这一页放不下更新后的行")
        self.dirty.add(rid[0])
    def delete(self, rid):
        self._load_page(rid[0]).delete(rid[1])
        self.dirty.add(rid[0])
    def scan(self):
        for page_no in range(self.page_count):
            for slot, raw in self._load_page(page_no).iter_records(): yield (page_no, slot), _decode_row(raw)
    #把所有脏页全部刷到磁盘
    def flush(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for page_no in list(self.dirty):
            page = self.pages.get(page_no)
            if page is not None: self._write_page(page)
        if not self.path.exists(): self.path.write_bytes(b"")
        with open(self.path, "r+b") as fh:
            need = max(self.page_count, 1) * PAGE_SIZE
            if fh.seek(0, os.SEEK_END) < need: fh.write(b"\x00" * (need - fh.tell()))
def _encode_row(values):
    return json.dumps([encode_value(v) for v in values], ensure_ascii=False).encode("utf-8")
def _decode_row(raw):
    return [decode_value(s) for s in json.loads(raw.decode("utf-8"))]

#  B+ 树索引 
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
    #从根往下走，找到key应该落在哪个叶子节点
    def _leaf_for(self, key):
        node = self.root
        while isinstance(node, _Inner): node = node.children[bisect_right(node.keys, key)]
        return node
    #查找返回这个key对应的所有RID
    def search(self, key):
        leaf = self._leaf_for(key)
        idx = bisect_left(leaf.keys, key)
        if idx < len(leaf.keys) and leaf.keys[idx] == key: return list(leaf.rids[idx])
        return []
    #遍历所有索引
    def entries(self):
        node = self.root
        while isinstance(node, _Inner): node = node.children[0]
        while node is not None:
            for key, rids in zip(node.keys, node.rids): yield key, rids
            node = node.next
    #插入一个索引项
    def insert(self, key, rid):
        path = []
        node = self.root
        while isinstance(node, _Inner):
            idx = bisect_right(node.keys, key)
            path.append((node, idx))
            node = node.children[idx]
        idx = bisect_left(node.keys, key)
        if idx < len(node.keys) and node.keys[idx] == key:
            node.rids[idx].append(rid)
            return
        node.keys.insert(idx, key)
        node.rids.insert(idx, [rid])
        if len(node.keys) > self.order: self._split_leaf(node, path)
    #叶子节点：拆成左、右两片，右节点第一个key上浮到父节点
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
    #内部节点分裂：中间key往上提，左右两个子节点
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
        for i, item in enumerate(rids):
            if item == rid:
                rids.pop(i)
                if not rids:
                    leaf.keys.pop(idx)
                    leaf.rids.pop(idx)
                return True
        return False

# 专门存表结构、列定义、约束、索引信息
class Column:
    __slots__ = ("name", "type", "not_null", "unique", "default_sql", "check_sql")
    def __init__(self, name, type_, not_null=False, unique=False,
                 default_sql=None, check_sql=None):
        self.name = name
        self.type = type_
        self.not_null = not_null
        self.unique = unique
        self.default_sql = default_sql
        self.check_sql = check_sql
    def to_json(self):
        return {"name": self.name, "type": self.type.to_json(),
                "not_null": self.not_null, "unique": self.unique,
                "default": self.default_sql, "check": self.check_sql}
    @staticmethod
    def from_json(obj):
        return Column(obj["name"], SQLType.from_json(obj["type"]),
                      obj.get("not_null", False), obj.get("unique", False),
                      obj.get("default"), obj.get("check"))
#外键约束
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
    def from_json(obj):
        return ForeignKey(obj["cols"], obj["ref_table"], obj.get("ref_cols"),
                          obj.get("on_delete", "restrict"), obj.get("on_update", "restrict"))
#check约束
class CheckConstraint:
    __slots__ = ("sql",)
    def __init__(self, sql):
        self.sql = sql
    def to_json(self):
        return {"sql": self.sql}
    @staticmethod
    def from_json(obj): return CheckConstraint(obj["sql"])
#一张表的完整描述
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
    #根据列名查在一行里的下标
    def column_index(self, name):
        low = name.lower()
        for i, col in enumerate(self.columns):
            if col.name.lower() == low: return i
        return None
    def require_column(self, name):
        idx = self.column_index(name)
        if idx is None: raise SQLSemanticError("表 %s 里没有列 %s" % (self.name, name))
        return idx
    @property
    def width(self):
        return len(self.columns)
    #所有需要保证唯一的列组合：主码、UNIQUE 约束、列上的 UNIQUE。
    def unique_sets(self):
        out = [list(self.pk)] if self.pk else []
        out.extend([list(u) for u in self.uniques])
        for col in self.columns:
            if col.unique: out.append([col.name])
        return out
    def to_json(self):
        return {"key": self.key, "name": self.name, "columns": [c.to_json() for c in self.columns], "pk": self.pk, "uniques": self.uniques, "fks": [f.to_json() for f in self.fks], "checks": [c.to_json() for c in self.checks]}
    @staticmethod
    def from_json(obj):
        t = TableMeta(obj["key"], obj["name"], [Column.from_json(c) for c in obj["columns"]])
        t.pk = obj.get("pk", [])
        t.uniques = obj.get("uniques", [])
        t.fks = [ForeignKey.from_json(f) for f in obj.get("fks", [])]
        t.checks = [CheckConstraint.from_json(c) for c in obj.get("checks", [])]
        return t
    def describe(self):
        lines = ["表 %s（%d 列）" % (self.key, len(self.columns))]
        for col in self.columns:
            flags = []
            if col.name in self.pk: flags.append("PRIMARY KEY")
            if col.not_null: flags.append("NOT NULL")
            if col.unique: flags.append("UNIQUE")
            if col.default_sql: flags.append("DEFAULT " + col.default_sql)
            if col.check_sql: flags.append("CHECK(%s)" % col.check_sql)
            lines.append("  %-12s %-14s %s" % (col.name, str(col.type), " ".join(flags)))
        for fk in self.fks:
            lines.append("  FOREIGN KEY (%s) REFERENCES %s(%s) ON DELETE %s ON UPDATE %s" % (",".join(fk.cols), fk.ref_table, ",".join(fk.ref_cols or []), fk.on_delete, fk.on_update))
        for chk in self.checks: lines.append("  CHECK (%s)" % chk.sql)
        return "\n".join(lines)
#索引
class IndexMeta:
    def __init__(self, name, table_key, cols, unique=False):
        self.name = name
        self.table_key = table_key
        self.cols = list(cols)          
        self.unique = unique
    def to_json(self):
        return {"name": self.name, "table": self.table_key, "cols": self.cols, "unique": self.unique}
    @staticmethod
    def from_json(obj):
        return IndexMeta(obj["name"], obj["table"], [tuple(c) for c in obj["cols"]], obj.get("unique", False))
#总目录
class Catalog:
    def __init__(self):
        self.tables = {}        
        self.indexes = {}       
    def resolve(self, name):
        low = name.lower()
        if low in self.tables: return low
        matches = [k for k in self.tables if k.split(".")[-1] == low]
        return matches[0] if len(matches) == 1 else None
    def add_table(self, table):
        self.tables[table.key] = table
    def drop_table(self, key):
        self.tables.pop(key, None)
        for name in [n for n, idx in self.indexes.items() if idx.table_key == key]: self.indexes.pop(name, None)
    def indexes_of(self, table_key):
        return [idx for idx in self.indexes.values() if idx.table_key == table_key]
    def to_json(self):
        return {"tables": [t.to_json() for t in self.tables.values()], "indexes": [i.to_json() for i in self.indexes.values()]}
    def save(self, path):
        Path(path).write_text(json.dumps(self.to_json(), ensure_ascii=False, indent=2), encoding="utf-8")
    @staticmethod
    def load(path, base_dir):
        obj = json.loads(Path(path).read_text(encoding="utf-8"))
        cat = Catalog()
        for item in obj.get("tables", []):
            table = TableMeta.from_json(item)
            table.heap_path = Path(base_dir) / "tables" / (table.key.replace(".", "__") + ".tbl")
            cat.tables[table.key] = table
        for item in obj.get("indexes", []): cat.indexes[item["name"].lower()] = IndexMeta.from_json(item)
        return cat


#表达式节点
class BExpr:__slots__ = ("type",)
class BColumn(BExpr):
    __slots__ = ("depth", "index", "name", "type")
    def __init__(self, depth, index, name, type_):
        self.depth = depth
        self.index = index
        self.name = name
        self.type = type_
class BLiteral(BExpr):
    __slots__ = ("value", "type")
    def __init__(self, value, type_=None):
        self.value = value
        self.type = type_
#一元运算
class BUnary(BExpr):
    __slots__ = ("op", "child")
    def __init__(self, op, child):
        self.op = op
        self.child = child
        self.type = "boolean" if op == "not" else child.type
#二元运算
class BBinary(BExpr):
    __slots__ = ("op", "left", "right", "type")
    def __init__(self, op, left, right):
        self.op = op
        self.left = left
        self.right = right
        self.type = "boolean" if op in ("and", "or", "=", "<", ">", "<=", ">=", "<>") else ( "numeric" if _is_number_type(left.type) or _is_number_type(right.type) else left.type)
#函数调用
class BFunc(BExpr):
    __slots__ = ("name", "args", "distinct", "type")
    def __init__(self, name, args, distinct=False, type_=None):
        self.name = name
        self.args = args
        self.distinct = distinct
        self.type = type_
#NULL
class BIsNull(BExpr):
    __slots__ = ("child", "neg", "type")
    def __init__(self, child, neg):
        self.child = child
        self.neg = neg
        self.type = "boolean"
#BETWEEN AND
class BBetween(BExpr):
    __slots__ = ("child", "low", "high", "neg", "type")
    def __init__(self, child, low, high, neg):
        self.child = child
        self.low = low
        self.high = high
        self.neg = neg
        self.type = "boolean"
class BInList(BExpr):
    __slots__ = ("child", "items", "neg", "type")
    def __init__(self, child, items, neg):
        self.child = child
        self.items = items
        self.neg = neg
        self.type = "boolean"
#link
class BLike(BExpr):
    __slots__ = ("child", "pattern", "escape", "neg", "type")
    def __init__(self, child, pattern, escape, neg):
        self.child = child
        self.pattern = pattern
        self.escape = escape
        self.neg = neg
        self.type = "boolean"
#in子查询
class BInQuery(BExpr):
    __slots__ = ("child", "query", "neg", "type")
    def __init__(self, child, query, neg):
        self.child = child
        self.query = query
        self.neg = neg
        self.type = "boolean"
class BExists(BExpr):
    __slots__ = ("query", "neg", "type")
    def __init__(self, query, neg):
        self.query = query
        self.neg = neg
        self.type = "boolean"
class BScalarQuery(BExpr):
    __slots__ = ("query", "type")
    def __init__(self, query):
        self.query = query
        self.type = None
def _is_number_type(t):
    return isinstance(t, SQLType) and (t.kind in INT_KINDS or t.kind == "numeric")
#算子
class Plan:
    def __init__(self):
        self.children = []
    def label(self):
        return type(self).__name__
class Empty(Plan):
    def label(self): return "Empty"
#全表扫
class TableScan(Plan):
    def __init__(self, table_key, alias, columns):
        super().__init__()
        self.table_key = table_key
        self.alias = alias
        self.columns = columns
#走索引
class IndexScan(Plan):
    def __init__(self, table_key, alias, columns, index_name, key):
        super().__init__()
        self.table_key = table_key
        self.alias = alias
        self.columns = columns
        self.index_name = index_name
        self.key = key
#WHERE选择
class Filter(Plan):
    def __init__(self, child, cond):
        super().__init__()
        self.children = [child]
        self.cond = cond
#投影
class Project(Plan):
    def __init__(self, child, items):
        super().__init__()
        self.children = [child]
        self.items = items            
#聚合
class Aggregate(Plan):
    def __init__(self, child, group_exprs, aggs):
        super().__init__()
        self.children = [child]
        self.group_exprs = group_exprs
        self.aggs = aggs              # [(BFunc, 显示名)]
    def label(self):
        return "Aggregate(group=[%s] aggs=[%s])" % (
            ",".join(e.text() for e in self.group_exprs), ",".join(n for _f, n in self.aggs))
class Distinct(Plan):
    def __init__(self, child):
        super().__init__()
        self.children = [child]
class Sort(Plan):
    def __init__(self, child, keys):
        super().__init__()
        self.children = [child]
        self.keys = keys              
class Limit(Plan):
    def __init__(self, child, limit, offset):
        super().__init__()
        self.children = [child]
        self.limit = limit
        self.offset = offset
class JoinPlan(Plan):
    def __init__(self, left, right, cond):
        super().__init__()
        self.children = [left, right]
        self.cond = cond
#绑定查询对象
class BoundQuery:
    def __init__(self, plan, columns):
        self.plan = plan
        self.columns = columns        

#语法树,描述语法结构
class Expr:
    pass
#字面量
class Literal(Expr):
    def __init__(self, value): self.value = value
#列名
class ColumnRef(Expr):
    def __init__(self, name, qualifier=None):
        self.name = name
        self.qualifier = qualifier
#*
class Star(Expr):
    def __init__(self, qualifier=None): self.qualifier = qualifier
#一元运算
class Unary(Expr):
    def __init__(self, op, operand):
        self.op = op
        self.operand = operand
class Binary(Expr):
    def __init__(self, op, left, right):
        self.op = op
        self.left = left
        self.right = right
class IsNull(Expr):
    def __init__(self, operand, neg):
        self.operand = operand
        self.neg = neg
class Between(Expr):
    def __init__(self, operand, low, high, neg):
        self.operand = operand
        self.low = low
        self.high = high
        self.neg = neg
class InList(Expr):
    def __init__(self, operand, items, neg):
        self.operand = operand
        self.items = items
        self.neg = neg
class InQuery(Expr):
    def __init__(self, operand, query, neg):
        self.operand = operand
        self.query = query
        self.neg = neg
class Exists(Expr):
    def __init__(self, query, neg):
        self.query = query
        self.neg = neg
class Like(Expr):
    def __init__(self, operand, pattern, escape, neg):
        self.operand = operand
        self.pattern = pattern
        self.escape = escape
        self.neg = neg
class Func(Expr):
    def __init__(self, name, args, distinct=False, star=False):
        self.name = name
        self.args = args
        self.distinct = distinct
        self.star = star
class SubQuery(Expr):
    def __init__(self, query): self.query = query
class SelectItem:
    def __init__(self, expr, alias=None):
        self.expr = expr
        self.alias = alias#别名
class BaseTable:
    def __init__(self, name, alias=None):
        self.name = name
        self.alias = alias
class JoinRef:
    def __init__(self, left, right, on):
        self.left = left
        self.right = right
        self.on = on
class OrderKey:
    def __init__(self, expr, desc):
        self.expr = expr
        self.desc = desc
#整条SELECT语句
class Query:
    def __init__(self, distinct, items, table_refs, where, group_by, having, order_by, limit):
        self.distinct = distinct
        self.items = items
        self.table_refs = table_refs
        self.where = where
        self.group_by = group_by
        self.having = having
        self.order_by = order_by
        self.limit = limit            
#CreatTable里列定义
class ColumnDef:
    def __init__(self, name, type_, not_null=False, unique=False,
                 default=None, check=None, references=None):
        self.name = name
        self.type = type_
        self.not_null = not_null
        self.unique = unique
        self.primary = False
        self.default = default
        self.check = check
        self.references = references  # FkRef 或 None
#外键
class FkRef:
    def __init__(self, cols, ref_table, ref_cols, on_delete, on_update):
        self.cols = cols
        self.ref_table = ref_table
        self.ref_cols = ref_cols
        self.on_delete = on_delete
        self.on_update = on_update
class TableConstraint:
    def __init__(self, kind, cols=None, expr=None, ref=None):
        self.kind = kind              
        self.cols = cols
        self.expr = expr
        self.ref = ref
class CreateTable:
    def __init__(self, name, columns, constraints):
        self.name = name
        self.columns = columns
        self.constraints = constraints
class DropTable:
    def __init__(self, name): self.name = name
class CreateIndex:
    def __init__(self, name, table, cols, unique):
        self.name = name
        self.table = table
        self.cols = cols              # [(列名, desc)]
        self.unique = unique
class DropIndex:
    def __init__(self, name): self.name = name
class Insert:
    def __init__(self, table, columns, rows, query):
        self.table = table
        self.columns = columns
        self.rows = rows
        self.query = query
class Update:
    def __init__(self, table, assignments, where):
        self.table = table
        self.assignments = assignments
        self.where = where
class Delete:
    def __init__(self, table, where):
        self.table = table
        self.where = where

#语法分析，递归下降
AGG_FUNCS = {"count", "sum", "avg", "max", "min"}
RESERVED = {
    "from", "where", "group", "having", "order", "limit", "offset", "join", "inner",
    "on", "and", "or", "not", "in", "like", "between", "is", "as", "select", "set",
    "values", "asc", "desc", "union",
}
COMPARE_OPS = {"=", ">", "<", ">=", "<=", "<>", "!=", "!>", "!<"}
def expr_sql(e):
    if isinstance(e, Literal):
        if isinstance(e.value, str): return "'%s'" % e.value.replace("'", "''")
        return format_value(e.value)
    if isinstance(e, ColumnRef): return ("%s.%s" % (e.qualifier, e.name)) if e.qualifier else e.name
    if isinstance(e, Unary): return "%s %s" % (e.op.upper(), expr_sql(e.operand))
    if isinstance(e, Binary): return "(%s %s %s)" % (expr_sql(e.left), e.op.upper(), expr_sql(e.right))
    if isinstance(e, IsNull): return "%s IS %sNULL" % (expr_sql(e.operand), "NOT " if e.neg else "")
    if isinstance(e, Between):
        return "%s %sBETWEEN %s AND %s" % (expr_sql(e.operand), "NOT " if e.neg else "", expr_sql(e.low), expr_sql(e.high))
    if isinstance(e, InList):
        return "%s %sIN (%s)" % (expr_sql(e.operand), "NOT " if e.neg else "", ", ".join(expr_sql(i) for i in e.items))
    if isinstance(e, Like):
        return "%s %sLIKE %s" % (expr_sql(e.operand), "NOT " if e.neg else "", expr_sql(e.pattern))
    if isinstance(e, Func):
        inner = "*" if e.star else ", ".join(expr_sql(a) for a in e.args)
        return "%s(%s%s)" % (e.name.upper(), "DISTINCT " if e.distinct else "", inner)
    return "?"
class Parser:
    def __init__(self, sql, tokens=None):
        self.sql = sql
        self.tokens = tokens if tokens is not None else tokenize(sql)
        self.pos = 0
    #取词
    def peek(self):
        return self.tokens[self.pos]
    def next_value(self):
        i = self.pos + 1
        return self.tokens[i].value if i < len(self.tokens) else ""
    def advance(self):
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok
    def at_word(self, *words):
        tok = self.peek()
        return tok.kind == "word" and tok.value in words
    def eat_word(self, *words):
        if self.at_word(*words): return self.advance().value
        return None
    def expect_word(self, word):
        if not self.at_word(word): raise SQLSyntaxError("这里应该是 %s，实际是 %r" % (word.upper(), self.peek().text))
        return self.advance()
    def at_op(self, op):
        return self.peek().kind == "op" and self.peek().value == op
    def eat_punct(self, ch):
        if self.peek().kind == "punct" and self.peek().value == ch:
            self.advance()
            return True
        return False
    def expect_punct(self, ch):
        if not self.eat_punct(ch): raise SQLSyntaxError("这里缺一个 %s" % ch)
    def ident(self):
        tok = self.peek()
        if tok.kind in ("word", "qid"):
            if tok.kind == "word" and tok.value in RESERVED: raise SQLSyntaxError("%s 是关键字，不能当名字" % tok.text)
            self.advance()
            return tok.value if tok.kind == "qid" else tok.text
        raise SQLSyntaxError("这里应该是一个名字，实际是 %r" % tok.text)
    def qualified_name(self):
        name = self.ident()
        while self.eat_punct("."): name = name + "." + self.ident()
        return name
    #语句
    def parse_statement(self):
        stmt = self.parse_statement_body()
        while self.peek().kind == "punct" and self.peek().value == ";":
            self.advance()
        if self.peek().kind != "eof":
            tok = self.peek()
            raise SQLSyntaxError(
                "语句在 %r 之后还有多余内容（常见原因是关键字拼错，"
                "例如把 WHERE 写成了 WHER，被当成表别名后剩下的条件没人接）" % tok.text)
        return stmt
    def parse_statement_body(self):
        if self.at_word("select"): return self.parse_query()
        if self.at_word("explain"): self.advance()
        if self.at_word("create"): return self.parse_create()
        if self.at_word("drop"): return self.parse_drop()
        if self.at_word("insert"): return self.parse_insert()
        if self.at_word("update"): return self.parse_update()
        if self.at_word("delete"): return self.parse_delete()
        raise SQLSyntaxError("不认识的语句开头 %r" % self.peek().text)
    def parse_query(self):
        self.expect_word("select")
        distinct = False
        if self.eat_word("distinct"): distinct = True
        else: self.eat_word("all")
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
        if limit is None and offset is None: return None
        return (limit, offset or 0)
    def parse_order_key(self):
        expr = self.parse_expr()
        if self.eat_word("desc"): return OrderKey(expr, True)
        self.eat_word("asc")
        return OrderKey(expr, False)
    def parse_select_item(self):
        expr = self.parse_expr()
        alias = None
        if self.eat_word("as"): alias = self.ident()
        elif self.peek().kind in ("word", "qid") and not ( self.peek().kind == "word" and self.peek().value in RESERVED):
            alias = self.ident()
        return SelectItem(expr, alias)
    def parse_table_ref(self):
        left = self.parse_table_primary()
        while True:
            if self.eat_word("join"): pass
            elif self.at_word("inner") and self.next_value() == "join":
                self.advance()
                self.advance()
            else: return left
            right = self.parse_table_primary()
            self.expect_word("on")
            left = JoinRef(left, right, self.parse_expr())
    def parse_table_primary(self):
        name = self.qualified_name()
        alias = None
        if self.eat_word("as"): alias = self.ident()
        elif self.peek().kind in ("word", "qid") and not ( self.peek().kind == "word" and self.peek().value in RESERVED):
            alias = self.ident()
        return BaseTable(name, alias)
    # ---- 表达式 ----
    def parse_expr(self):
        return self.parse_or()
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
            self.advance()
            neg = True
        if self.eat_word("between"):
            low = self.parse_additive()
            self.expect_word("and")
            return Between(left, low, self.parse_additive(), neg)
        if self.eat_word("in"):
            if self.peek().kind == "punct" and self.peek().value == "(" \
                    and self.next_value() == "select":
                self.advance()
                query = self.parse_query()
                self.expect_punct(")")
                return InQuery(left, query, neg)
            self.expect_punct("(")
            items = [self.parse_expr()]
            while self.eat_punct(","): items.append(self.parse_expr())
            self.expect_punct(")")
            return InList(left, items, neg)
        if self.eat_word("like"):
            escape = None
            if self.eat_word("escape"): escape = self.advance().value
            return Like(left, self.parse_additive(), escape, neg)
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
            op = self.advance().value
            return Unary(op, self.parse_unary())
        return self.parse_primary()
    def parse_primary(self):
        tok = self.peek()
        if tok.kind == "num":
            self.advance()
            return Literal(tok.value)
        if tok.kind == "str":
            self.advance()
            return Literal(tok.value)
        if tok.kind == "punct" and tok.value == "(":
            self.advance()
            if self.at_word("select"):
                query = self.parse_query()
                self.expect_punct(")")
                return SubQuery(query)
            expr = self.parse_expr()
            self.expect_punct(")")
            return expr
        if tok.kind == "op" and tok.value == "*":
            self.advance()
            return Star()
        if tok.kind in ("word", "qid"):
            if self.at_word("null"):
                self.advance()
                return Literal(None)
            if self.at_word("exists"):
                self.advance()
                self.expect_punct("(")
                query = self.parse_query()
                self.expect_punct(")")
                return Exists(query, False)
            if tok.kind == "word" and tok.value in AGG_FUNCS and \
                    self.next_value() == "(":
                return self.parse_func()
            name = self.advance()
            if self.peek().kind == "punct" and self.peek().value == "(": raise SQLSyntaxError("不支持的函数 %s" % name.text)
            if self.peek().kind == "punct" and self.peek().value == ".":
                if self.next_value() == "*":
                    self.advance()
                    self.advance()
                    return Star(name.text)
                self.advance()
                return ColumnRef(self.ident(), name.text)
            return ColumnRef(name.text)
        raise SQLSyntaxError("这里看不懂：%r" % tok.text)
    def parse_func(self):
        name = self.advance().value
        self.expect_punct("(")
        distinct = bool(self.eat_word("distinct"))
        star = False
        args = []
        if self.peek().kind == "op" and self.peek().value == "*":
            self.advance()
            star = True
        elif not (self.peek().kind == "punct" and self.peek().value == ")"):
            args.append(self.parse_expr())
            while self.eat_punct(","): args.append(self.parse_expr())
        self.expect_punct(")")
        return Func(name, args, distinct, star)
    def expect_number(self):
        tok = self.peek()
        if tok.kind != "num": raise SQLSyntaxError("这里要一个数字，实际是 %r" % tok.text)
        self.advance()
        return int(tok.value)
    #数据定义
    def parse_create(self):
        self.expect_word("create")
        unique = False
        if self.eat_word("unique"): unique = True
        if self.eat_word("index"):
            name = self.ident()
            self.expect_word("on")
            table = self.qualified_name()
            self.expect_punct("(")
            cols = [self.parse_index_col()]
            while self.eat_punct(","): cols.append(self.parse_index_col())
            self.expect_punct(")")
            return CreateIndex(name, table, cols, unique)
        if unique: raise SQLSyntaxError("UNIQUE 后面只能跟 INDEX")
        self.expect_word("table")
        return self.parse_create_table()
    def parse_index_col(self):
        name = self.ident()
        desc = False
        if self.eat_word("desc"): desc = True
        else: self.eat_word("asc")
        return (name, desc)
    def parse_create_table(self):
        name = self.qualified_name()
        self.expect_punct("(")
        columns, constraints = [], []
        while True:
            if self.at_word("constraint"):
                self.advance()
                self.ident()           
            if self.at_word("primary", "unique", "foreign", "check"):
                constraints.append(self.parse_table_constraint())
            else: columns.append(self.parse_column_def())
            if not self.eat_punct(","): break
        self.expect_punct(")")
        for col in columns:
            if col.primary and not any(c.kind == "pk" for c in constraints):
                constraints.append(TableConstraint("pk", [col.name]))
        return CreateTable(name, columns, constraints)
    def parse_column_def(self):
        name = self.ident()
        type_ = self.parse_type()
        col = ColumnDef(name, type_)
        while True:
            if self.eat_word("not"):
                self.expect_word("null")
                col.not_null = True
            elif self.at_word("primary"):
                self.advance()
                self.expect_word("key")
                col.primary = True
            elif self.eat_word("unique"): col.unique = True
            elif self.eat_word("default"): col.default = self.parse_expr()
            elif self.eat_word("check"):
                self.expect_punct("(")
                col.check = self.parse_expr()
                self.expect_punct(")")
            elif self.eat_word("references"): col.references = self.parse_references([name])
            else: return col
    def parse_references(self, cols):
        ref_table = self.qualified_name()
        ref_cols = None
        if self.eat_punct("("):
            ref_cols = [self.ident()]
            while self.eat_punct(","): ref_cols.append(self.ident())
            self.expect_punct(")")
        on_delete = on_update = "restrict"
        while self.eat_word("on"):
            if self.eat_word("delete"): on_delete = self.parse_action()
            elif self.eat_word("update"): on_update = self.parse_action()
            else: raise SQLSyntaxError("ON 后面只能是 DELETE 或 UPDATE")
        return FkRef(cols, ref_table, ref_cols, on_delete, on_update)
    def parse_action(self):
        if self.eat_word("cascade"): return "cascade"
        if self.eat_word("restrict"): return "restrict"
        if self.eat_word("set"):
            self.expect_word("null")
            return "set null"
        self.expect_word("no")
        self.expect_word("action")
        return "restrict"
    def parse_table_constraint(self):
        if self.eat_word("primary"):
            self.expect_word("key")
            self.expect_punct("(")
            cols = self.parse_name_list()
            return TableConstraint("pk", cols)
        if self.eat_word("unique"):
            self.expect_punct("(")
            return TableConstraint("unique", self.parse_name_list())
        if self.eat_word("foreign"):
            self.expect_word("key")
            self.expect_punct("(")
            cols = self.parse_name_list()
            self.expect_word("references")
            return TableConstraint("fk", ref=self.parse_references(cols))
        self.expect_word("check")
        self.expect_punct("(")
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
        if name.kind != "word": raise SQLSyntaxError("这里应该是类型名")
        word = name.text.lower()
        if word == "double":
            self.eat_word("precision")
            word = "numeric"
        if word == "character":
            if self.eat_word("varying"): word = "varchar"
            else: word = "char"
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
    #数据操纵
    def parse_insert(self):
        self.expect_word("insert")
        self.expect_word("into")
        table = self.qualified_name()
        columns = None
        if self.eat_punct("("):
            columns = [self.ident()]
            while self.eat_punct(","): columns.append(self.ident())
            self.expect_punct(")")
        if self.at_word("select"): return Insert(table, columns, [], self.parse_query())
        self.expect_word("values")
        rows = []
        while True:
            self.expect_punct("(")
            row = [self.parse_expr()]
            while self.eat_punct(","): row.append(self.parse_expr())
            self.expect_punct(")")
            rows.append(row)
            if not self.eat_punct(","): break
        return Insert(table, columns, rows, None)
    def parse_update(self):
        self.expect_word("update")
        table = self.qualified_name()
        self.expect_word("set")
        assignments = [(self.ident(), self.parse_assign_value())]
        while self.eat_punct(","): assignments.append((self.ident(), self.parse_assign_value()))
        where = self.parse_expr() if self.eat_word("where") else None
        return Update(table, assignments, where)
    def parse_assign_value(self):
        if not self.eat_op("="): raise SQLSyntaxError("SET 后面要写 列 = 值")
        return self.parse_expr()
    def eat_op(self, op):
        if self.at_op(op):
            self.advance()
            return True
        return False
    def parse_delete(self):
        self.expect_word("delete")
        self.expect_word("from")
        table = self.qualified_name()
        where = self.parse_expr() if self.eat_word("where") else None
        return Delete(table, where)
def split_statements(text):
    statements, buf = [], []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "'":
            j = i + 1
            while j < n:
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        j += 2
                        continue
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
            piece = "".join(buf).strip()
            if piece: statements.append(piece)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail: statements.append(tail)
    return statements
