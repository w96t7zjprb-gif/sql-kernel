import re
from decimal import Decimal
from functools import cmp_to_key
from pathlib import Path

from kernel_core import *  # noqa: F401,F403  语法树、计划节点、存储、类型
from kernel_core import _as_decimal, _is_number_type  # noqa: F401  模块内部辅助

# 语义绑定 
class ColumnMeta:
    __slots__ = ("name", "col_name", "table_alias", "index", "type")
    def __init__(self, name, col_name, table_alias, index, type_):
        self.name = name            # 显示名（表名.列名）
        self.col_name = col_name
        self.table_alias = table_alias
        self.index = index          # 在组合行里的位置，多表连接后会被改成全局下标
        self.type = type_
class TableBinding:
    def __init__(self, alias, table, columns):
        self.alias = alias
        self.table = table
        self.columns = columns
class Scope:
    """一层查询块的作用域。找不到就往 parent 找，depth 每往外一层加一。"""
    def __init__(self, bindings, parent=None):
        self.bindings = bindings
        self.parent = parent
    def resolve(self, qualifier, name):
        low = name.lower()
        qlow = qualifier.lower() if qualifier else None
        for b in self.bindings:
            if qlow is not None and b.alias.lower() != qlow: continue
            for col in b.columns:
                if col.col_name.lower() == low: return BColumn(0, col.index, col.name, col.type)
        if self.parent is not None:
            outer = self.parent.resolve(qualifier, name)
            if outer is not None: return BColumn(outer.depth + 1, outer.index, outer.name, outer.type)
        return None
    def all_columns(self):
        return [col for b in self.bindings for col in b.columns]
class Binder:
    def __init__(self, catalog, indexes):
        self.catalog = catalog
        self.indexes = indexes
    def bind_query(self, query, outer=None):
        scope = outer or Scope([])
        plan, columns = self._bind_select(query, scope)
        return BoundQuery(plan, columns)
    def table_scope(self, table, alias=None):
        """单表作用域：约束校验、CHECK 和 DEFAULT 绑定都用它。"""
        alias = alias or table.key
        binding = TableBinding(alias, table, self._make_columns(table, alias))
        return Scope([binding])
    def _make_columns(self, table, alias):
        return [ColumnMeta("%s.%s" % (alias, col.name), col.name, alias, i, col.type)
                for i, col in enumerate(table.columns)]
    # ---- FROM ----
    def _collect_from(self, refs):
        """把 FROM 里所有表摊平，返回 (连接计划, 绑定列表, ON 条件列表)。"""
        plans, bindings, conds = [], [], []
        for ref in refs:
            plan, new_bindings, new_conds = self._collect_ref(ref)
            plans.append(plan)
            bindings.extend(new_bindings)
            conds.extend(new_conds)
        offset = 0
        for binding in bindings:
            for i, col in enumerate(binding.columns): col.index = offset + i
            offset += len(binding.columns)
        if not plans: return Empty(), [], conds
        chain = plans[0]
        for plan in plans[1:]: chain = JoinPlan(chain, plan, None)
        return chain, bindings, conds
    def _collect_ref(self, ref):
        if isinstance(ref, BaseTable):
            key = self.catalog.resolve(ref.name)
            if key is None: raise SQLSemanticError("表 %s 不存在" % ref.name)
            table = self.catalog.tables[key]
            alias = ref.alias or key
            binding = TableBinding(alias, table, self._make_columns(table, alias))
            return TableScan(key, alias, [c.col_name for c in binding.columns]), [binding], []
        left_plan, left_bindings, left_conds = self._collect_ref(ref.left)
        right_plan, right_bindings, right_conds = self._collect_ref(ref.right)
        return (JoinPlan(left_plan, right_plan, None), left_bindings + right_bindings, left_conds + right_conds + [ref.on])
    # ---- SELECT ----
    def _bind_select(self, query, scope):
        chain, bindings, conds = self._collect_from(query.table_refs)
        inner = Scope(bindings, scope) if bindings else scope
        if query.where is not None:
            conds.append(query.where)
            if len(bindings) == 1: chain = self._try_index_scan(chain, bindings[0], query.where, inner, conds)
        cond = _and_all([self.bind_expr(c, inner) for c in conds])
        if cond is not None: chain = Filter(chain, cond)
        has_agg = bool(query.group_by) or any( _has_aggregate(item.expr) for item in query.items) or ( query.having is not None and _has_aggregate(query.having))
        if query.having is not None and not has_agg: raise SQLSemanticError("HAVING 得和 GROUP BY 或者聚合函数一起用")
        if has_agg: return self._finish_aggregate(query, chain, inner)
        return self._finish_plain(query, chain, inner)
    def _finish_plain(self, query, child, scope):
        items = []
        for item in query.items:
            if isinstance(item.expr, Star):
                named = [c for c in scope.all_columns() if not item.expr.qualifier or c.table_alias.lower() == item.expr.qualifier.lower()]
                if not named: raise SQLSemanticError("这里没有可以展开的列")
                for col in named: items.append((BColumn(0, col.index, col.name, col.type), col.name))
                continue
            bound = self.bind_expr(item.expr, scope)
            default = expr_sql(item.expr) if isinstance(item.expr, Func) else _item_name(item.expr, bound)
            items.append((bound, item.alias or default))
        names = [name for _e, name in items]
        if query.order_by:
            keys = [(self._order_expr(key.expr, items, scope), key.desc) for key in query.order_by]
            child = Sort(child, keys)       # 排序作用在原表行上，所以能排没投影出去的列
        plan = Project(child, items)
        if query.distinct: plan = Distinct(plan)
        if query.limit is not None: plan = Limit(plan, query.limit[0], query.limit[1])
        return plan, names
    def _order_expr(self, expr, items, scope):
        """ORDER BY 三种写法：结果列序号、结果列名/别名、原表上的任意表达式。"""
        if isinstance(expr, Literal) and isinstance(expr.value, int):
            idx = expr.value - 1
            if not 0 <= idx < len(items): raise SQLSemanticError("ORDER BY %d 超出结果列数" % expr.value)
            return items[idx][0]
        if isinstance(expr, ColumnRef):
            low = expr.name.lower()
            for item_expr, name in items:
                if name.lower() == low: return item_expr
        return self.bind_expr(expr, scope)
    def _finish_aggregate(self, query, child, scope):
        groups = [self.bind_expr(e, scope) for e in query.group_by]
        aggs = []
        for item in query.items: _collect_aggregates(item.expr, self, scope, aggs)
        if query.having is not None: _collect_aggregates(query.having, self, scope, aggs)
        group_texts = [g.text() for g in groups]
        agg_texts = [f.text() for f, _n in aggs]
        plan = Aggregate(child, groups, aggs)
        if query.having is not None:
            plan = Filter(plan, self._map_to_agg(self.bind_expr(query.having, scope),
                                                 group_texts, agg_texts))
        items = []
        for item in query.items:
            if isinstance(item.expr, Star): raise SQLSemanticError("带 GROUP BY 的查询不能写 *")
            bound = self._map_to_agg(self.bind_expr(item.expr, scope), group_texts, agg_texts)
            default = expr_sql(item.expr) if isinstance(item.expr, Func) else _item_name(item.expr, bound)
            items.append((bound, item.alias or default))
        names = [name for _e, name in items]
        plan = Project(plan, items)
        if query.order_by:
            keys = []
            lowered = [n.lower() for n in names]
            for key in query.order_by:
                expr = key.expr
                if isinstance(expr, Literal) and isinstance(expr.value, int):
                    idx = expr.value - 1
                    if not 0 <= idx < len(names): raise SQLSemanticError("ORDER BY %d 超出结果列数" % expr.value)
                    keys.append((BColumn(0, idx, names[idx], None), key.desc))
                elif isinstance(expr, ColumnRef) and expr.name.lower() in lowered:
                    idx = lowered.index(expr.name.lower())
                    keys.append((BColumn(0, idx, names[idx], None), key.desc))
                elif isinstance(expr, ColumnRef):
                    idx = next((i for i, item in enumerate(query.items)
                                if isinstance(item.expr, ColumnRef) and
                                item.expr.name.lower() == expr.name.lower()), None)
                    if idx is None: raise SQLSemanticError("分组查询的 ORDER BY 只认结果列")
                    keys.append((BColumn(0, idx, names[idx], None), key.desc))
                else: raise SQLSemanticError("分组查询的 ORDER BY 只认结果列")
            plan = Sort(plan, keys)
        if query.distinct: plan = Distinct(plan)
        if query.limit is not None: plan = Limit(plan, query.limit[0], query.limit[1])
        return plan, names
    def _map_to_agg(self, bound, group_texts, agg_texts):
        """把「按原表绑出来的表达式」改成「按聚合输出行取值」。"""
        if isinstance(bound, BColumn):
            text = bound.name
            for i, g in enumerate(group_texts):
                if g == text: return BColumn(0, i, text, bound.type)
            raise SQLSemanticError("列 %s 没有出现在 GROUP BY 里，不能单独取" % text)
        if isinstance(bound, BFunc) and bound.name.lower() in AGG_FUNCS:
            text = bound.text()
            for i, a in enumerate(agg_texts):
                if a == text: return BColumn(0, len(group_texts) + i, text, bound.type)
        return _rebuild(bound, lambda child: self._map_to_agg(child, group_texts, agg_texts))
    # ---- 索引改写 ----
    def _try_index_scan(self, child, binding, where, scope, conds):
        eq = _equality_on(where)
        if eq is None: return child
        col_name, value_expr = eq
        if binding.table.column_index(col_name) is None: return child
        for meta in self.catalog.indexes_of(binding.table.key):
            if not meta.cols or meta.cols[0][0].lower() != col_name.lower(): continue
            tree = self.indexes.get(meta.name.lower())
            if tree is None: continue
            key = _const_value(self.bind_expr(value_expr, scope))
            if key is None: continue
            conds.clear()               # 等值条件已经由索引负责了
            return IndexScan(binding.table.key, binding.alias, [c.col_name for c in binding.columns], meta.name, (encode_value(key),))   # 树里存的是编码后的键
        return child
    # ---- 表达式 ----
    def bind_expr(self, expr, scope, allow_agg=True):
        if isinstance(expr, Literal):
            if expr.value is None: return BLiteral(None, None)
            t = SQLType("char") if isinstance(expr.value, str) else ( SQLType("numeric") if isinstance(expr.value, float) else SQLType("int"))
            return BLiteral(coerce(expr.value, t), t)
        if isinstance(expr, ColumnRef):
            resolved = scope.resolve(expr.qualifier, expr.name)
            if resolved is None: raise SQLSemanticError("列 %s 不存在" % expr.name)
            return resolved
        if isinstance(expr, Unary): return BUnary(expr.op, self.bind_expr(expr.operand, scope, allow_agg))
        if isinstance(expr, Binary):
            left = self.bind_expr(expr.left, scope, allow_agg)
            right = self.bind_expr(expr.right, scope, allow_agg)
            if expr.op in ("+", "-", "*", "/", "%") and \
                    not (_is_number_type(left.type) and _is_number_type(right.type)):
                raise SQLSemanticError("算术两边都得是数值")
            return BBinary(expr.op, left, right)
        if isinstance(expr, IsNull): return BIsNull(self.bind_expr(expr.operand, scope, allow_agg), expr.neg)
        if isinstance(expr, Between):
            return BBetween(self.bind_expr(expr.operand, scope, allow_agg), self.bind_expr(expr.low, scope, allow_agg), self.bind_expr(expr.high, scope, allow_agg), expr.neg)
        if isinstance(expr, InList):
            return BInList(self.bind_expr(expr.operand, scope, allow_agg), [self.bind_expr(e, scope, allow_agg) for e in expr.items], expr.neg)
        if isinstance(expr, InQuery):
            node = BInQuery(self.bind_expr(expr.operand, scope, allow_agg), self.bind_query(expr.query, scope), expr.neg)
            return node
        if isinstance(expr, Exists): return BExists(self.bind_query(expr.query, scope), expr.neg)
        if isinstance(expr, SubQuery):
            node = BScalarQuery(self.bind_query(expr.query, scope))
            node.type = SQLType("numeric")
            return node
        if isinstance(expr, Like):
            return BLike(self.bind_expr(expr.operand, scope, allow_agg), self.bind_expr(expr.pattern, scope, allow_agg), expr.escape, expr.neg)
        if isinstance(expr, Func):
            name = expr.name.lower()
            args = [self.bind_expr(a, scope, allow_agg) for a in expr.args]
            if name in AGG_FUNCS:
                if not allow_agg: raise SQLSemanticError("%s 不能出现在这里" % name.upper())
                if not expr.star and len(args) != 1: raise SQLSemanticError("%s 只接受一个参数" % name.upper())
                return BFunc(name, args, expr.distinct, SQLType("numeric"))
            raise SQLSemanticError("不支持的函数 %s" % name)
        if isinstance(expr, Star): raise SQLSemanticError("* 只能单独当目标列")
        raise SQLSemanticError("这个表达式绑定不了")
def _and_all(conds):
    result = None
    for cond in conds: result = cond if result is None else BBinary("and", result, cond)
    return result
def _item_name(ast_expr, bound):
    if isinstance(ast_expr, ColumnRef): return bound.name
    if isinstance(bound, BLiteral): return format_value(bound.value)
    if isinstance(bound, (BFunc, BBinary, BUnary)): return bound.text()
    return "expr"
def _has_aggregate(expr):
    if isinstance(expr, Func) and expr.name.lower() in AGG_FUNCS: return True
    return any(_has_aggregate(child) for child in _children_of(expr))
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
        bound = binder.bind_expr(expr, scope)
        if all(bound.text() != f.text() for f, _n in aggs): aggs.append((bound, bound.text()))
        return
    for child in _children_of(expr): _collect_aggregates(child, binder, scope, aggs)
def _rebuild(bound, map_child):
    """按原节点类型重造一个绑定表达式，子节点交给 map_child 映射。"""
    if isinstance(bound, BUnary): return BUnary(bound.op, map_child(bound.child))
    if isinstance(bound, BBinary): return BBinary(bound.op, map_child(bound.left), map_child(bound.right))
    if isinstance(bound, BIsNull): return BIsNull(map_child(bound.child), bound.neg)
    if isinstance(bound, BBetween):
        return BBetween(map_child(bound.child), map_child(bound.low),
                        map_child(bound.high), bound.neg)
    if isinstance(bound, BInList):
        return BInList(map_child(bound.child), [map_child(i) for i in bound.items], bound.neg)
    if isinstance(bound, BLike):
        return BLike(map_child(bound.child), map_child(bound.pattern), bound.escape, bound.neg)
    if isinstance(bound, BFunc):
        return BFunc(bound.name, [map_child(a) for a in bound.args], bound.distinct, bound.type)
    return bound

# ========== 执行器 ==========
# 每个计划节点返回一个行列表，上层再加工；行是扁平 list，列位置在绑定期定好了。
class Ctx:
    __slots__ = ("row", "outers", "engine")
    def __init__(self, row=None, outers=(), engine=None):
        self.row = row
        self.outers = outers
        self.engine = engine
    def inside(self, row):
        """进一层子查询：当前行压进外层栈，depth 就正好对上下标。"""
        return Ctx(None, self.outers + ((row,),) if row is not None else self.outers, self.engine)
def run_plan(plan, ctx):
    if isinstance(plan, TableScan): return [row for _rid, row in ctx.engine.heaps[plan.table_key].scan()]
    if isinstance(plan, IndexScan):
        tree = ctx.engine.indexes[plan.index_name.lower()]
        heap = ctx.engine.heaps[plan.table_key]
        return [heap.get(rid) for rid in tree.search(plan.key)]
    if isinstance(plan, Empty): return [[]]
    if isinstance(plan, Filter):
        return [row for row in run_plan(plan.children[0], ctx) if truth(eval_expr(plan.cond, row, ctx)) is True]
    if isinstance(plan, Project):
        return [[eval_expr(e, row, ctx) for e, _n in plan.items] for row in run_plan(plan.children[0], ctx)]
    if isinstance(plan, Aggregate): return _run_aggregate(plan, ctx)
    if isinstance(plan, Distinct):
        seen, out = set(), []
        for row in run_plan(plan.children[0], ctx):
            key = tuple(encode_value(v) for v in row)
            if key not in seen:
                seen.add(key)
                out.append(row)
        return out
    if isinstance(plan, Sort):
        rows = run_plan(plan.children[0], ctx)
        def cmp(a, b):
            for expr, desc in plan.keys:
                c = compare_for_sort(eval_expr(expr, a, ctx), eval_expr(expr, b, ctx), desc)
                if c: return c
            return 0
        return sorted(rows, key=cmp_to_key(cmp))
    if isinstance(plan, Limit):
        rows = run_plan(plan.children[0], ctx)
        start = plan.offset
        return rows[start:] if plan.limit is None else rows[start:start + plan.limit]
    if isinstance(plan, JoinPlan):
        left_rows = run_plan(plan.children[0], ctx)
        right_rows = run_plan(plan.children[1], ctx)
        out = []
        for left in left_rows:
            for right in right_rows:
                merged = left + right
                if plan.cond is None or truth(eval_expr(plan.cond, merged, ctx)) is True: out.append(merged)
        return out
    raise SQLExecutionError("不认识的计划节点 %s" % type(plan).__name__)
def _run_aggregate(plan, ctx):
    rows = run_plan(plan.children[0], ctx)
    groups = {}
    for row in rows:
        key = tuple(encode_value(eval_expr(g, row, ctx)) for g in plan.group_exprs)
        groups.setdefault(key, []).append(row)
    if not plan.group_exprs and not groups:
        groups[()] = []                 # 空表上 COUNT(*) 也得给一行 0
    out = []
    for key, group_rows in groups.items():
        if group_rows: values = [eval_expr(g, group_rows[0], ctx) for g in plan.group_exprs]
        else: values = [decode_value(k) for k in key]
        for func, _name in plan.aggs: values.append(_eval_aggregate(func, group_rows, ctx))
        out.append(values)
    return out
def _eval_aggregate(func, rows, ctx):
    name = func.name.lower()
    if name == "count":
        if not func.args: return len(rows)
        if func.distinct:
            seen = {encode_value(eval_expr(func.args[0], r, ctx)) for r in rows if eval_expr(func.args[0], r, ctx) is not NULL}
            return len(seen)
        return sum(1 for r in rows if eval_expr(func.args[0], r, ctx) is not NULL)
    values = [eval_expr(func.args[0], r, ctx) for r in rows]
    values = [v for v in values if v is not NULL]
    if name == "sum": return sum(values) if values else NULL
    if name == "avg": return sum((_as_decimal(v) for v in values), Decimal(0)) / len(values) if values else NULL
    if name == "max": return max(values, key=cmp_to_key(compare)) if values else NULL
    if name == "min": return min(values, key=cmp_to_key(compare)) if values else NULL
    raise SQLExecutionError("不支持的聚合函数 %s" % name)
def eval_expr(expr, row, ctx):
    if isinstance(expr, BLiteral): return expr.value
    if isinstance(expr, BColumn): return row[expr.index] if expr.depth == 0 else ctx.outers[-expr.depth][expr.index]
    if isinstance(expr, BUnary):
        value = eval_expr(expr.child, row, ctx)
        if expr.op == "not": return sql_not(value)
        if expr.op == "-": return NULL if value is NULL else -_as_decimal(value)
        return value
    if isinstance(expr, BBinary): return _eval_binary(expr, row, ctx)
    if isinstance(expr, BIsNull):
        value = eval_expr(expr.child, row, ctx)
        return value is not NULL if expr.neg else value is NULL
    if isinstance(expr, BBetween):
        value = eval_expr(expr.child, row, ctx)
        lo = compare(value, eval_expr(expr.low, row, ctx))
        hi = compare(value, eval_expr(expr.high, row, ctx))
        hit = None if lo is None or hi is None else (lo >= 0 and hi <= 0)
        return sql_not(hit) if expr.neg else hit
    if isinstance(expr, BInList):
        value = eval_expr(expr.child, row, ctx)
        if value is NULL: return NULL
        unknown = False
        for item in expr.items:
            c = compare(value, eval_expr(item, row, ctx))
            if c == 0: return False if expr.neg else True
            if c is None: unknown = True
        return NULL if unknown else (True if expr.neg else False)
    if isinstance(expr, BLike):
        value = eval_expr(expr.child, row, ctx)
        if value is NULL: return NULL
        hit = like_match(format_value(value), format_value(eval_expr(expr.pattern, row, ctx)), expr.escape)
        return not hit if expr.neg else hit
    if isinstance(expr, BInQuery):
        value = eval_expr(expr.child, row, ctx)
        if value is NULL: return NULL
        rows = run_plan(expr.query.plan, ctx.inside(row))
        hit = any(compare(value, r[0]) == 0 for r in rows if r)
        return not hit if expr.neg else hit
    if isinstance(expr, BExists):
        hit = bool(run_plan(expr.query.plan, ctx.inside(row)))
        return not hit if expr.neg else hit
    if isinstance(expr, BScalarQuery):
        rows = run_plan(expr.query.plan, ctx.inside(row))
        return rows[0][0] if rows and rows[0] else NULL
    raise SQLExecutionError("表达式求值失败")
def _eval_binary(expr, row, ctx):
    op = expr.op
    left = eval_expr(expr.left, row, ctx)
    if op in ("and", "or"):
        right = eval_expr(expr.right, row, ctx)
        return sql_and(left, right) if op == "and" else sql_or(left, right)
    right = eval_expr(expr.right, row, ctx)
    if op in ("=", "<", ">", "<=", ">=", "<>"):
        c = compare(left, right)
        if c is None: return NULL
        if op == "=": return c == 0
        if op == "<>": return c != 0
        if op == "<": return c < 0
        if op == ">": return c > 0
        if op == "<=": return c <= 0
        return c >= 0
    if left is NULL or right is NULL: return NULL
    a, b = _as_decimal(left), _as_decimal(right)
    if a is None or b is None: raise SQLExecutionError("算术两边都得是数值")
    if op == "+": return a + b
    if op == "-": return a - b
    if op == "*": return a * b
    if op in ("/", "%"):
        if b == 0: raise SQLExecutionError("除数是 0")
        return a / b if op == "/" else a % b
    raise SQLExecutionError("不支持的运算 %s" % op)
def like_match(text, pattern, escape=None):
    """% 匹配任意长度，_ 匹配一个字符，其它按字面比。"""
    parts = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if escape and ch == escape and i + 1 < len(pattern):
            parts.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        parts.append(".*" if ch == "%" else "." if ch == "_" else re.escape(ch))
        i += 1
    return re.fullmatch("".join(parts), text, re.S) is not None
def _equality_on(where):
    """WHERE 是「列 = 常量」就返回 (列名, 常量表达式)，否则 None。"""
    if isinstance(where, Binary) and where.op == "=":
        if isinstance(where.left, ColumnRef) and _is_const(where.right): return where.left.name, where.right
        if isinstance(where.right, ColumnRef) and _is_const(where.left): return where.right.name, where.left
    return None
def _is_const(expr):
    if isinstance(expr, Literal): return True
    return isinstance(expr, Unary) and _is_const(expr.operand)
def _const_value(bound):
    if isinstance(bound, BLiteral): return bound.value
    if isinstance(bound, BUnary) and bound.op == "-":
        value = _const_value(bound.child)
        return None if value is None else -_as_decimal(value)
    return None

# ========== 完整性约束 ==========
# 三类完整性都在这一节：实体完整性（主码 / UNIQUE）、参照完整性（外码 + 级联）、
# 用户定义完整性（NOT NULL / CHECK / 默认值）。
def check_row(engine, table, row, skip_rid=None):
    for i, col in enumerate(table.columns):
        if col.not_null and row[i] is NULL: raise SQLConstraintError("表 %s 的列 %s 不能为空" % (table.name, col.name))
    for cols in table.unique_sets():
        indexes = [table.require_column(c) for c in cols]
        key = tuple(encode_value(row[i]) for i in indexes)
        if any(row[i] is NULL for i in indexes): continue
        for rid, other in engine.heaps[table.key].scan():
            if skip_rid is not None and rid == skip_rid: continue
            if tuple(encode_value(other[i]) for i in indexes) == key:
                raise SQLConstraintError("表 %s 违反唯一性约束 (%s)" % (table.name, ",".join(cols)))
    for i, col in enumerate(table.columns):
        if col.check_sql: _ensure_check(engine, table, col.check_sql, row, col.name)
    for chk in table.checks: _ensure_check(engine, table, chk.sql, row, None)
    for fk in table.fks: _ensure_fk(engine, table, fk, row)
def _ensure_check(engine, table, sql, row, column_name):
    bound = engine.bound_check(table, sql)
    value = eval_expr(bound, row, Ctx(row, (), engine))
    if truth(value) is False:
        where = ("列 %s 的 CHECK(%s)" % (column_name, sql)) if column_name else ("CHECK(%s)" % sql)
        raise SQLConstraintError("表 %s 的 %s 不满足" % (table.name, where))
def _ensure_fk(engine, table, fk, row):
    indexes = [table.require_column(c) for c in fk.cols]
    values = [row[i] for i in indexes]
    if any(v is NULL for v in values):
        return                       # 外码有一列为空就不查了
    parent_key = engine.catalog.resolve(fk.ref_table)
    if parent_key is None: raise SQLSemanticError("外码引用的表 %s 不存在" % fk.ref_table)
    parent = engine.catalog.tables[parent_key]
    ref_cols = fk.ref_cols or parent.pk
    ref_indexes = [parent.require_column(c) for c in ref_cols]
    for _rid, prow in engine.heaps[parent_key].scan():
        if [prow[i] for i in ref_indexes] == values: return
    raise SQLConstraintError("表 %s 的外码 (%s) 在 %s 里找不到对应的行" % (table.name, ",".join(fk.cols), parent.name))
def referencing_fks(engine, table_key):
    """找出所有「外码指向这张表」的 (子表, 外码)。级联删除和更新要用。"""
    out = []
    for child in engine.catalog.tables.values():
        for fk in child.fks:
            if engine.catalog.resolve(fk.ref_table) == table_key: out.append((child, fk))
    return out
def delete_rows(engine, table, rids):
    """删之前先看有没有别的表引用着这几行，有就拦下来（不搞级联）。"""
    for child, fk in referencing_fks(engine, table.key):
        ref_indexes = [table.require_column(c) for c in (fk.ref_cols or table.pk)]
        child_indexes = [child.require_column(c) for c in fk.cols]
        for rid in rids:
            parent_row = engine.heaps[table.key].get(rid)
            key = [parent_row[i] for i in ref_indexes]
            if any(v is NULL for v in key): continue
            for _crid, crow in engine.heaps[child.key].scan():
                if [crow[i] for i in child_indexes] == key:
                    raise SQLConstraintError("表 %s 的这行还被 %s 引用着，先删 %s" % (table.name, child.name, child.name))
    for rid in rids:
        row = engine.heaps[table.key].get(rid)
        engine.heaps[table.key].delete(rid)
        for name in engine.indexes_of_table(table.key): engine.index_row_delete(table, row, rid, name)
def update_rows(engine, table, rid, old_row, new_row):
    """改主码的时候，被别的表引用着就不让改。"""
    changed = [i for i in range(len(old_row)) if old_row[i] != new_row[i]]
    ref_indexes = [table.require_column(c) for c in table.pk] if table.pk else []
    if not any(i in ref_indexes for i in changed): return
    for child, fk in referencing_fks(engine, table.key):
        child_indexes = [child.require_column(c) for c in fk.cols]
        parent_ref_indexes = ref_indexes[:len(child_indexes)]
        old_key = [old_row[i] for i in parent_ref_indexes]
        for _crid, row in engine.heaps[child.key].scan():
            if [row[i] for i in child_indexes] == old_key:
                raise SQLConstraintError("表 %s 的主码被 %s 引用着，不能改"
                                         % (table.name, child.name))

# ========== 引擎 ==========
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
        if self.catalog_path.exists(): self.catalog = Catalog.load(self.catalog_path, self.dir)
        else: self.catalog = Catalog()
        self.heaps = {}
        self.indexes = {}
        for table in self.catalog.tables.values(): self.heaps[table.key] = HeapFile(table.heap_path)
        self.binder = Binder(self.catalog, self.indexes)
        self._check_cache = {}
        self._load_indexes()
    # ---- 持久化 ----
    def flush(self):
        self.catalog.save(self.catalog_path)
        for heap in self.heaps.values(): heap.flush()
    def _load_indexes(self):
        for meta in self.catalog.indexes.values():
            tree = BPlusTree()
            heap = self.heaps.get(meta.table_key)
            table = self.catalog.tables.get(meta.table_key)
            if heap is not None and table is not None:
                for rid, row in heap.scan():
                    key = _index_key(table, meta, row)
                    if key is not None: tree.insert(key, rid)
            self.indexes[meta.name.lower()] = tree
    def indexes_of_table(self, table_key):
        return [m.name.lower() for m in self.catalog.indexes_of(table_key)]
    def index_row_add(self, table, row, rid, name):
        meta = self.catalog.indexes.get(name)
        key = _index_key(table, meta, row) if meta else None
        if key is not None: self.indexes[name].insert(key, rid)
    def index_row_delete(self, table, row, rid, name):
        meta = self.catalog.indexes.get(name)
        key = _index_key(table, meta, row) if meta else None
        if key is not None: self.indexes[name].delete(key, rid)
    # ---- 执行入口 ----
    def execute(self, sql):
        stmt = Parser(sql).parse_statement()
        result = self._dispatch(stmt)
        self.flush()
        return result
    def _dispatch(self, stmt):
        if isinstance(stmt, Query): return self.run_query(stmt)
        if isinstance(stmt, CreateTable): return self.create_table(stmt)
        if isinstance(stmt, DropTable): return self.drop_table(stmt)
        if isinstance(stmt, CreateIndex): return self.create_index(stmt)
        if isinstance(stmt, DropIndex): return self.drop_index(stmt)
        if isinstance(stmt, Insert): return self.insert(stmt)
        if isinstance(stmt, Update): return self.update(stmt)
        if isinstance(stmt, Delete): return self.delete(stmt)
        raise SQLExecutionError("这条语句还没实现：%s" % type(stmt).__name__)
    def run_query(self, query):
        bound = self.binder.bind_query(query)
        rows = run_plan(bound.plan, Ctx(None, (), self))
        return Result(bound.columns, rows, "共 %d 行" % len(rows))
    def bound_check(self, table, sql):
        key = (table.key, sql)
        if key not in self._check_cache:
            self._check_cache[key] = self.binder.bind_expr(
                Parser(sql).parse_expr(), self.binder.table_scope(table))
        return self._check_cache[key]
    # ---- 数据定义 ----
    def create_table(self, stmt):
        key = stmt.name.lower()
        if key in self.catalog.tables: raise SQLSemanticError("表 %s 已经存在" % stmt.name)
        columns = []
        for col in stmt.columns:
            if any(c.name.lower() == col.name.lower() for c in columns): raise SQLSemanticError("列 %s 重复了" % col.name)
            columns.append(Column(col.name, col.type, col.not_null, col.unique,
                                  expr_sql(col.default) if col.default is not None else None,
                                  expr_sql(col.check) if col.check is not None else None))
        table = TableMeta(key, stmt.name, columns)
        for cons in stmt.constraints:
            if cons.kind == "pk": table.pk = cons.cols
            elif cons.kind == "unique": table.uniques.append(cons.cols)
            elif cons.kind == "check": table.checks.append(CheckConstraint(expr_sql(cons.expr)))
            elif cons.kind == "fk": table.fks.append(_make_fk(self.catalog, stmt.name, cons.ref))
        for col in stmt.columns:
            if col.references is not None: table.fks.append(_make_fk(self.catalog, stmt.name, col.references))
        for name in table.pk: table.require_column(name)
        for cols in table.uniques:
            for name in cols: table.require_column(name)
        # 外码可能指向自己（课程表的前修课就是），所以先登记再校验
        self.catalog.add_table(table)
        try:
            for fk in table.fks:
                parent_key = self.catalog.resolve(fk.ref_table)
                if parent_key is None: raise SQLSemanticError("外码引用的表 %s 还没有建" % fk.ref_table)
                parent = self.catalog.tables[parent_key]
                ref = fk.ref_cols or parent.pk
                if len(ref) != len(fk.cols):
                    raise SQLSemanticError("外码 %s 的列数和被引用的 %s 对不上"
                                           % (",".join(fk.cols), fk.ref_table))
                for name in fk.cols: table.require_column(name)
                for name in ref: parent.require_column(name)
                fk.ref_table = parent_key
                fk.ref_cols = ref
        except SQLError:
            self.catalog.tables.pop(key, None)
            raise
        table.heap_path = self.dir / "tables" / (key + ".tbl")
        self.heaps[key] = HeapFile(table.heap_path)
        self.flush()
        return Result(message="建表 %s，共 %d 列" % (stmt.name, len(columns)))
    def drop_table(self, stmt):
        key = self.catalog.resolve(stmt.name)
        if key is None: raise SQLSemanticError("表 %s 不存在" % stmt.name)
        heap = self.heaps.pop(key, None)
        if heap is not None:
            heap.pages.clear()
            if heap.path.exists(): heap.path.unlink()
        for name in [n for n, m in self.catalog.indexes.items() if m.table_key == key]:
            self.catalog.indexes.pop(name, None)
            self.indexes.pop(name, None)
        self.catalog.drop_table(key)
        self.flush()
        return Result(message="删表 %s" % stmt.name)
    def create_index(self, stmt):
        name = stmt.name.lower()
        if name in self.catalog.indexes: raise SQLSemanticError("索引 %s 已经存在" % stmt.name)
        key = self.catalog.resolve(stmt.table)
        if key is None: raise SQLSemanticError("表 %s 不存在" % stmt.table)
        table = self.catalog.tables[key]
        cols = []
        for col_name, desc in stmt.cols:
            table.require_column(col_name)
            cols.append((table.columns[table.column_index(col_name)].name, desc))
        meta = IndexMeta(name, key, cols, stmt.unique)
        tree = BPlusTree()
        for rid, row in self.heaps[key].scan():
            k = _index_key(table, meta, row)
            if k is not None:
                if stmt.unique and tree.search(k):
                    raise SQLConstraintError("列 %s 上已经有重复值，建不了唯一索引"
                                            % ",".join(c for c, _d in cols))
                tree.insert(k, rid)
        self.catalog.indexes[name] = meta
        self.indexes[name] = tree
        self.flush()
        return Result(message="建索引 %s on %s(%s)"
                      % (stmt.name, table.name, ",".join(c for c, _d in cols)))
    def drop_index(self, stmt):
        name = stmt.name.lower()
        if name not in self.catalog.indexes: raise SQLSemanticError("索引 %s 不存在" % stmt.name)
        self.catalog.indexes.pop(name)
        self.indexes.pop(name, None)
        self.flush()
        return Result(message="删索引 %s" % stmt.name)
    # ---- 数据操纵 ----
    def insert(self, stmt):
        key = self.catalog.resolve(stmt.table)
        if key is None: raise SQLSemanticError("表 %s 不存在" % stmt.table)
        table = self.catalog.tables[key]
        heap = self.heaps[key]
        if stmt.columns: indices = [table.require_column(c) for c in stmt.columns]
        else: indices = list(range(table.width))
        if stmt.query is not None:
            bound = self.binder.bind_query(stmt.query)
            source_rows = run_plan(bound.plan, Ctx(None, (), self))
            if len(bound.columns) != len(indices): raise SQLSemanticError("INSERT ... SELECT 的列数和目标列对不上")
        else:
            scope = self.binder.table_scope(table)
            source_rows = []
            for row_exprs in stmt.rows:
                if len(row_exprs) != len(indices): raise SQLSemanticError("VALUES 里值的个数和列数对不上")
                bound = [self.binder.bind_expr(e, scope, allow_agg=False) for e in row_exprs]
                source_rows.append([eval_expr(b, [], Ctx([], (), self)) for b in bound])
        count = 0
        index_names = self.indexes_of_table(key)
        for source in source_rows:
            values = [NULL] * table.width
            for pos, col_index in enumerate(indices): values[col_index] = source[pos]
            for i, col in enumerate(table.columns):
                if values[i] is NULL and col.default_sql:
                    bound = self.bound_check(table, col.default_sql)
                    values[i] = eval_expr(bound, values, Ctx(values, (), self))
            values = [coerce(values[i], table.columns[i].type) for i in range(table.width)]
            check_row(self, table, values)
            rid = heap.insert(values)
            for name in index_names: self.index_row_add(table, values, rid, name)
            count += 1
        return Result(message="向 %s 插入了 %d 行" % (table.name, count))
    def update(self, stmt):
        key = self.catalog.resolve(stmt.table)
        if key is None: raise SQLSemanticError("表 %s 不存在" % stmt.table)
        table = self.catalog.tables[key]
        heap = self.heaps[key]
        scope = self.binder.table_scope(table)
        where = self.binder.bind_expr(stmt.where, scope, allow_agg=False) if stmt.where else None
        assigns = [(table.require_column(c), self.binder.bind_expr(e, scope, allow_agg=False))
                   for c, e in stmt.assignments]
        targets = []
        for rid, row in heap.scan():
            if where is not None and truth(eval_expr(where, row, Ctx(row, (), self))) is not True: continue
            fresh = list(row)
            for idx, expr in assigns: fresh[idx] = eval_expr(expr, row, Ctx(row, (), self))
            fresh = [coerce(fresh[i], table.columns[i].type) for i in range(table.width)]
            targets.append((rid, row, fresh))
        index_names = self.indexes_of_table(key)
        for rid, old_row, new_row in targets:
            check_row(self, table, new_row, skip_rid=rid)
            update_rows(self, table, rid, old_row, new_row)
            for name in index_names: self.index_row_delete(table, old_row, rid, name)
            heap.update(rid, new_row)
            for name in index_names: self.index_row_add(table, new_row, rid, name)
        return Result(message="更新了 %d 行" % len(targets))
    def delete(self, stmt):
        key = self.catalog.resolve(stmt.table)
        if key is None: raise SQLSemanticError("表 %s 不存在" % stmt.table)
        table = self.catalog.tables[key]
        heap = self.heaps[key]
        scope = self.binder.table_scope(table)
        where = self.binder.bind_expr(stmt.where, scope, allow_agg=False) if stmt.where else None
        rids = [rid for rid, row in heap.scan()
                if where is None or truth(eval_expr(where, row, Ctx(row, (), self))) is True]
        delete_rows(self, table, rids)
        return Result(message="删除了 %d 行" % len(rids))
def _make_fk(catalog, table_name, ref):
    ref_key = catalog.resolve(ref.ref_table)
    return ForeignKey(ref.cols, ref_key or ref.ref_table.lower(), ref.ref_cols,
                      ref.on_delete, ref.on_update)
def _index_key(table, meta, row):
    """索引键：按索引定义的列顺序拼成元组，任意一列为 NULL 就不进索引。"""
    values = []
    for col_name, _desc in meta.cols:
        idx = table.column_index(col_name)
        if idx is None: return None
        value = row[idx]
        if value is NULL: return None
        values.append(encode_value(value))
    return tuple(values)

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
 ('20140001', '李勇', '男', 21, 'CS'),
 ('20140002', '刘辰', '女', 20, 'IS'),
 ('20140003', '王敏', '女', 20, 'MA'),
 ('20140004', '张立', '男', 19, 'IS'),
 ('20140005', '陈东', '男', 18, 'IS'),
 ('20140007', '欧阳娜娜', '女', 20, 'CS'),
 ('20140008', '刘洋', '男', 22, 'MA');
INSERT INTO Courses VALUES
 ('C1', 'DBMS', NULL, 4),
 ('C2', 'O.S.', 'C1', 3),
 ('C3', 'DB', 'C2', 4),
 ('C4', 'OS', 'C3', 4),
 ('C5', 'DB_Design', 'C4', 2);
INSERT INTO SC VALUES
 ('20140001', 'C1', 92), ('20140001', 'C2', 85), ('20140001', 'C3', 88),
 ('20140002', 'C1', 80), ('20140002', 'C2', 90),
 ('20140003', 'C1', 75),
 ('20140004', 'C1', NULL),
 ('20140005', 'C1', 60),
 ('20140007', 'C1', 95),
 ('20140008', 'C3', 70);
CREATE INDEX idx_sc_grade ON SC(Grade);
"""
def load_demo(engine):
    for sql in split_statements(DEMO_SCRIPT): engine.execute(sql)
