"""opl-expr/v1 求值器：白名单求值 + null 传播 + 四要素溯源采集。

运行期语义（V2-04 切片约定）：
- null 传播：算术运算任一操作数为 null → 结果 null（显式标记 NULL_INPUT，不当 0）；
  显式守卫用 if_null()/coalesce()。
- 除法：分母为 null → null；分母为 0 且分子非 null → DIV_ZERO（单实例错误）。
- lookup：0 命中 → LOOKUP_NOT_FOUND；多命中 → LOOKUP_AMBIGUOUS（单实例错误，批不断）。
- this.<attr>：属性缺失 → MISSING_INPUT_ATTR（单实例错误）；属性值为 null → null 传播并标记。
- 比较：==/!= 支持任意类型（含 null）；< > <= >= 遇 null 或类型不可比 → TYPE_ERROR。
- 短路求值：and/or、if()/三元 只执行实际取到的分支（lookup 副作用不扩散）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .expr_parser import (
    BinOp, Bool, Call, DictLit, E_TYPE, E_DIV_ZERO, E_MISSING_ATTR,
    ExprError, Ident, Member, Null, Num, Program, Str, Unary,
)

__all__ = ["EvalContext", "evaluate", "LookupFn", "RefFn", "TraceCollector"]


@dataclass
class TraceCollector:
    """单次执行的溯源采集（四要素：能力版本 + 输入实例 + 输入属性 + 原始值）。"""

    used_inputs: list[dict[str, Any]] = field(default_factory=list)
    null_inputs: list[str] = field(default_factory=list)
    lookups: list[dict[str, Any]] = field(default_factory=list)
    refs: list[dict[str, Any]] = field(default_factory=list)
    _seen: set = field(default_factory=set, repr=False)

    def note_input(self, attr: str, value: Any) -> None:
        if attr not in self._seen:
            self._seen.add(attr)
            self.used_inputs.append({"attr": attr, "value": value})
            if value is None:
                self.null_inputs.append(attr)

    def to_dict(self) -> dict[str, Any]:
        return {
            "inputs": self.used_inputs,
            "null_inputs": self.null_inputs,
            "lookups": self.lookups,
            "refs": self.refs,
        }


# lookup(object_type, dims, attr, trace) 与 ref(name, version) 由执行层注入；
# 命中元数据（matched_record_id/matched_code）由执行层闭包写入 trace。
LookupFn = Callable[[str, dict[str, Any], Optional[str], "TraceCollector"], Any]
RefFn = Callable[[str, Optional[str]], Any]


@dataclass
class EvalContext:
    project_id: str
    object_type: str
    record_id: str
    instance: dict[str, Any]
    lookup: LookupFn
    ref: RefFn
    trace: TraceCollector = field(default_factory=TraceCollector)
    _env: dict[str, Any] = field(default_factory=dict, repr=False)
    _depth: int = field(default=0, repr=False)


MAX_DEPTH = 64


def evaluate(program: Program, ctx: EvalContext) -> Any:
    for bind in program.binds:
        ctx._env[bind.name] = _eval(bind.expr, ctx)
    return _eval(program.final, ctx)


def _eval(node: Any, ctx: EvalContext) -> Any:
    ctx._depth += 1
    if ctx._depth > MAX_DEPTH:
        raise ExprError(E_SYNTAX, f"求值嵌套过深（>{MAX_DEPTH}），疑似循环引用")
    try:
        return _eval_node(node, ctx)
    finally:
        ctx._depth -= 1


def _eval_node(node: Any, ctx: EvalContext) -> Any:
    if isinstance(node, Num):
        return node.value
    if isinstance(node, Str):
        return node.value
    if isinstance(node, Bool):
        return node.value
    if isinstance(node, Null):
        return None
    if isinstance(node, Ident):
        if node.name in ctx._env:
            return ctx._env[node.name]
        raise ExprError(E_SYNTAX, f"运行时未定义标识符: {node.name}", node.line)
    if isinstance(node, Member):
        base = node.base
        while isinstance(base, Member):
            base = base.base
        if isinstance(base, Ident) and base.name == "this":
            if node.attr not in ctx.instance:
                raise ExprError(E_MISSING_ATTR,
                                 f"实例缺少输入属性 {node.attr}（对象 {ctx.object_type}）",
                                 node.line)
            value = ctx.instance[node.attr]
            ctx.trace.note_input(node.attr, value)
            return value
        raise ExprError(E_SYNTAX, "成员访问仅支持 this.<attr>", node.line)
    if isinstance(node, Unary):
        if node.op == "not":
            return not _truthy(_eval(node.operand, ctx))
        v = _eval(node.operand, ctx)
        if v is None:
            return None
        if not isinstance(v, (int, float)):
            raise ExprError(E_TYPE, f"一元负号要求数值，实际 {type(v).__name__}", node.line)
        return -v
    if isinstance(node, BinOp):
        return _eval_binop(node, ctx)
    if isinstance(node, Call):
        return _eval_call(node, ctx)
    if isinstance(node, DictLit):
        out: dict[str, Any] = {}
        for key, val in node.items:
            out[key.value] = _eval(val, ctx)
        return out
    raise ExprError(E_SYNTAX, f"未知 AST 节点: {type(node).__name__}")


def _truthy(v: Any) -> bool:
    if v is None or v is False:
        return False
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return len(v) > 0
    return True


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _eval_binop(node: BinOp, ctx: EvalContext) -> Any:
    op = node.op
    if op == "and":
        left = _eval(node.left, ctx)
        if not _truthy(left):
            return left
        return _eval(node.right, ctx)
    if op == "or":
        left = _eval(node.left, ctx)
        if _truthy(left):
            return left
        return _eval(node.right, ctx)
    if op in ("==", "!="):
        left = _eval(node.left, ctx)
        right = _eval(node.right, ctx)
        res = (left == right) if op == "==" else (left != right)
        return res
    if op in ("<", ">", "<=", ">="):
        left = _eval(node.left, ctx)
        right = _eval(node.right, ctx)
        if left is None or right is None:
            raise ExprError(E_TYPE, "比较运算操作数为 null（如需用 if_null 显式守卫）", node.line)
        if not (_is_num(left) and _is_num(right)) and not (isinstance(left, str) and isinstance(right, str)):
            raise ExprError(E_TYPE, f"不可比较的类型: {type(left).__name__} {op} {type(right).__name__}", node.line)
        if op == "<":
            return left < right
        if op == ">":
            return left > right
        if op == "<=":
            return left <= right
        return left >= right
    # 算术
    left = _eval(node.left, ctx)
    right = _eval(node.right, ctx)
    if op == "+":
        if isinstance(left, str) and isinstance(right, str):
            return left + right
    if left is None or right is None:
        return None  # null 传播（显式标记由输入采集承担）
    if not (_is_num(left) and _is_num(right)):
        raise ExprError(E_TYPE, f"算术运算类型错误: {type(left).__name__} {op} {type(right).__name__}", node.line)
    if op == "+":
        return left + right
    if op == "-":
        return left - right
    if op == "*":
        return left * right
    if op == "/":
        if right == 0:
            raise ExprError(E_DIV_ZERO, "除零（DIV_ZERO）", node.line)
        return left / right
    if op == "%":
        if right == 0:
            raise ExprError(E_DIV_ZERO, "取模零（DIV_ZERO）", node.line)
        return left % right
    raise ExprError(E_SYNTAX, f"未知运算符: {op}", node.line)


def _eval_call(node: Call, ctx: EvalContext) -> Any:
    func = node.func if isinstance(node.func, str) else getattr(node.func, "name", str(node.func))
    line = node.line

    if func == "if":
        cond = _eval(node.args[0], ctx)
        if cond is None:
            raise ExprError(E_TYPE, "if() 条件为 null（需 if_null 显式守卫）", line)
        return _eval(node.args[1] if _truthy(cond) else node.args[2], ctx)
    if func == "if_null":
        v = _eval(node.args[0], ctx)
        return v if v is not None else _eval(node.args[1], ctx)
    if func == "coalesce":
        for arg in node.args:
            v = _eval(arg, ctx)
            if v is not None:
                return v
        return None
    if func == "lookup":
        otype = _eval(node.args[0], ctx)
        dims_raw = _eval(node.args[1], ctx)
        if not isinstance(otype, str):
            raise ExprError(E_TYPE, "lookup() 对象类型必须是字符串", line)
        if not isinstance(dims_raw, dict):
            raise ExprError(E_TYPE, "lookup() 维度参数必须是字典 {attr: value}", line)
        attr = _eval(node.args[2], ctx) if len(node.args) > 2 else None
        if attr is not None and not isinstance(attr, str):
            raise ExprError(E_TYPE, "lookup() 属性名必须是字符串或 null", line)
        result = ctx.lookup(otype, dims_raw, attr, ctx.trace)
        return result
    if func == "ref":
        name = _eval(node.args[0], ctx)
        if not isinstance(name, str):
            raise ExprError(E_TYPE, "ref() 能力名必须是字符串", line)
        version = _eval(node.args[1], ctx) if len(node.args) > 1 else None
        if version is not None and not isinstance(version, str):
            raise ExprError(E_TYPE, "ref() 版本必须是字符串", line)
        try:
            value = ctx.ref(name, version)
        except ExprError:
            raise
        ctx.trace.refs.append({"name": name, "version": version, "value": value})
        return value
    if func in ("abs", "round", "min", "max", "str", "contains", "starts_with"):
        return _builtin(func, node.args, ctx, line)
    raise ExprError(E_SYNTAX, f"函数不在白名单: {func}", line)


def _builtin(func: str, args: list, ctx: EvalContext, line: int) -> Any:
    if func == "abs":
        v = _eval(args[0], ctx)
        return None if v is None else abs(v)
    if func == "round":
        v = _eval(args[0], ctx)
        if v is None:
            return None
        nd = 0
        if len(args) > 1:
            nd = _eval(args[1], ctx)
            if nd is None or not isinstance(nd, int):
                nd = int(nd) if nd is not None else 0
        return round(v, nd)
    if func in ("min", "max"):
        vals = [_eval(a, ctx) for a in args]
        if any(v is None for v in vals):
            return None
        return min(vals) if func == "min" else max(vals)
    if func == "str":
        v = _eval(args[0], ctx)
        return "null" if v is None else str(v)
    if func == "contains":
        s, sub = _eval(args[0], ctx), _eval(args[1], ctx)
        if s is None or sub is None:
            return None
        return sub in s
    if func == "starts_with":
        s, pre = _eval(args[0], ctx), _eval(args[1], ctx)
        if s is None or pre is None:
            return None
        return s.startswith(pre)
    raise ExprError(E_SYNTAX, f"函数不在白名单: {func}", line)
