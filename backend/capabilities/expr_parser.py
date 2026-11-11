"""opl-expr/v1 表达式语言：词法、AST、解析与定义期校验。

设计要点（对齐 docs/能力层元模型设计.md 与 V2-04 切片范围）：
- 多行顺序绑定：``name = expr``，最后一行为返回值；
- 白名单函数：if / if_null / coalesce / lookup / ref / abs / round / min / max / str / contains / starts_with；
- 字面量：数字 / 字符串 / true / false / null / dict 字面量 {k: v}；
- 标识符：绑定名、``this.<attr>``（实例属性）、声明输入名；
- 三元运算符 c ? a : b 解析期归一为 if(c, a, b) 调用（统一求值路径）；
- 定义期校验：语法、白名单函数、参数个数、未知标识符、常量折叠除零、ref 实参字面量；
- 求值语义（null 传播、DIV_ZERO、NOT_FOUND/AMBIGUOUS）见 expr_eval.py。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional


# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------

class ExprError(Exception):
    """表达式解析/校验/求值错误，带稳定错误码。"""

    def __init__(self, code: str, message: str, line: Optional[int] = None):
        self.code = code
        self.message = message
        self.line = line
        suffix = f"（第 {line} 行）" if line else ""
        super().__init__(f"[{code}] {message}{suffix}")

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "line": self.line}


# 错误码常量（定义期 → 422；运行期 → 单实例错误标记）
E_SYNTAX = "SYNTAX_ERROR"
E_UNKNOWN_FUNC = "UNKNOWN_FUNCTION"
E_UNKNOWN_IDENT = "UNKNOWN_IDENTIFIER"
E_ARG_COUNT = "ARG_COUNT_ERROR"
E_CONST_DIV_ZERO = "CONSTANT_DIV_ZERO"
E_REF_BAD_ARG = "REF_BAD_ARGUMENT"
E_TYPE = "TYPE_ERROR"
E_DIV_ZERO = "DIV_ZERO"
E_NOT_FOUND = "LOOKUP_NOT_FOUND"
E_AMBIGUOUS = "LOOKUP_AMBIGUOUS"
E_MISSING_ATTR = "MISSING_INPUT_ATTR"
E_REF_NOT_RESOLVED = "REF_NOT_RESOLVED"


# ---------------------------------------------------------------------------
# AST 节点
# ---------------------------------------------------------------------------

@dataclass
class Num:
    value: float
    line: int


@dataclass
class Str:
    value: str
    line: int


@dataclass
class Bool:
    value: bool
    line: int


@dataclass
class Null:
    line: int


@dataclass
class Ident:
    name: str
    line: int


@dataclass
class Member:
    base: Any  # 规范要求为 Ident('this')
    attr: str
    line: int


@dataclass
class Unary:
    op: str  # '-' | 'not'
    operand: Any
    line: int


@dataclass
class BinOp:
    op: str
    left: Any
    right: Any
    line: int


@dataclass
class Call:
    func: str
    args: list
    line: int


@dataclass
class DictLit:
    items: list  # [(Str key, value)]
    line: int


@dataclass
class Bind:
    name: str
    expr: Any
    line: int


@dataclass
class Program:
    binds: list = field(default_factory=list)
    final: Any = None
    line: int = 0


# ---------------------------------------------------------------------------
# 词法
# ---------------------------------------------------------------------------

_KEYWORDS = {"true", "false", "null", "and", "or", "not"}
_TWO_CHAR = ("==", "!=", "<=", ">=", "&&", "||")
_SINGLE = set("+-*/%<>=!&|(){}[],:.?")
_TOKEN_RE = re.compile(
    r"(\d+\.\d+|\d+)"
    r"|('(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")"
    r"|([A-Za-z_][A-Za-z0-9_]*)"
)


def _tokenize(source: str) -> list[tuple[str, Any, int]]:
    tokens: list[tuple[str, Any, int]] = []
    line = 1
    i = 0
    n = len(source)
    while i < n:
        ch = source[i]
        if ch == "\n":
            line += 1
            i += 1
            continue
        if ch.isspace():
            i += 1
            continue
        if ch == "#":
            while i < n and source[i] != "\n":
                i += 1
            continue
        two = source[i:i + 2]
        if two in _TWO_CHAR:
            tokens.append((two, two, line))
            i += 2
            continue
        if ch in _SINGLE:
            tokens.append((ch, ch, line))
            i += 1
            continue
        m = _TOKEN_RE.match(source, i)
        if not m or m.end() == i:
            raise ExprError(E_SYNTAX, f"无法识别的字符 {ch!r}", line)
        text = m.group(0)
        if m.group(2):  # 字符串
            tokens.append(("STR", re.sub(r"\\(.)", r"\1", text[1:-1]), line))
        elif m.group(3):  # 标识符/关键字
            name = text
            tokens.append(("KW", name, line) if name in _KEYWORDS else ("IDENT", name, line))
        else:  # 数字
            tokens.append(("NUM", float(text), line))
        i = m.end()
    return tokens


# ---------------------------------------------------------------------------
# 解析（递归下降）
# ---------------------------------------------------------------------------

class _Parser:
    def __init__(self, tokens: list[tuple[str, Any, int]]):
        self.tokens = tokens
        self.pos = 0

    def peek(self, offset: int = 0) -> Optional[tuple[str, Any, int]]:
        idx = self.pos + offset
        return self.tokens[idx] if idx < len(self.tokens) else None

    def advance(self) -> tuple[str, Any, int]:
        tok = self.peek()
        if tok is None:
            raise ExprError(E_SYNTAX, "表达式意外结束")
        self.pos += 1
        return tok

    def expect(self, value: str) -> tuple[str, Any, int]:
        tok = self.advance()
        if tok[1] != value:
            raise ExprError(E_SYNTAX, f"期望 {value!r}，实际 {tok[1]!r}", tok[2])
        return tok

    def parse_program(self) -> Program:
        binds: list[Bind] = []
        final: Any = None
        while self.peek() is not None:
            tok = self.peek()
            if tok[0] == "IDENT" and (self.peek(1) or (None, None, None))[1] == "=":
                name = self.advance()[1]
                self.expect("=")
                binds.append(Bind(name, self.parse_expr(), tok[2]))
            else:
                if final is not None:
                    raise ExprError(E_SYNTAX, "最后一个表达式之后不应再有内容", tok[2])
                final = self.parse_expr()
        if final is None:
            raise ExprError(E_SYNTAX, "表达式不能为空")
        return Program(binds=binds, final=final, line=self.tokens[-1][2] if self.tokens else 0)

    def parse_expr(self) -> Any:
        return self.parse_ternary()

    def parse_ternary(self) -> Any:
        cond = self.parse_or()
        tok = self.peek()
        if tok and tok[1] == "?":
            self.advance()
            a = self.parse_ternary()
            self.expect(":")
            b = self.parse_ternary()
            return Call("if", [cond, a, b], tok[2])
        return cond

    def parse_or(self) -> Any:
        left = self.parse_and()
        while True:
            tok = self.peek()
            if tok and (tok[0] == "KW" and tok[1] == "or" or tok[1] == "||"):
                self.advance()
                left = BinOp("or", left, self.parse_and(), tok[2])
            else:
                return left

    def parse_and(self) -> Any:
        left = self.parse_not()
        while True:
            tok = self.peek()
            if tok and (tok[0] == "KW" and tok[1] == "and" or tok[1] == "&&"):
                self.advance()
                left = BinOp("and", left, self.parse_not(), tok[2])
            else:
                return left

    def parse_not(self) -> Any:
        tok = self.peek()
        if tok and (tok[0] == "KW" and tok[1] == "not" or tok[1] == "!"):
            self.advance()
            return Unary("not", self.parse_not(), tok[2])
        return self.parse_cmp()

    def parse_cmp(self) -> Any:
        left = self.parse_add()
        while True:
            tok = self.peek()
            if tok and tok[1] in ("==", "!=", "<", ">", "<=", ">="):
                self.advance()
                left = BinOp(tok[1], left, self.parse_add(), tok[2])
            else:
                return left

    def parse_add(self) -> Any:
        left = self.parse_mul()
        while True:
            tok = self.peek()
            if tok and tok[1] in ("+", "-"):
                self.advance()
                left = BinOp(tok[1], left, self.parse_mul(), tok[2])
            else:
                return left

    def parse_mul(self) -> Any:
        left = self.parse_unary()
        while True:
            tok = self.peek()
            if tok and tok[1] in ("*", "/", "%"):
                self.advance()
                left = BinOp(tok[1], left, self.parse_unary(), tok[2])
            else:
                return left

    def parse_unary(self) -> Any:
        tok = self.peek()
        if tok and tok[1] == "-":
            self.advance()
            return Unary("-", self.parse_unary(), tok[2])
        return self.parse_postfix()

    def parse_postfix(self) -> Any:
        node = self.parse_primary()
        while True:
            tok = self.peek()
            if tok and tok[1] == "." and (self.peek(1) or (None, None, None))[0] == "IDENT":
                self.advance()
                attr = self.advance()[1]
                node = Member(node, attr, tok[2])
            elif tok and tok[1] == "(":
                self.advance()
                args = []
                if self.peek() and self.peek()[1] != ")":
                    args.append(self.parse_expr())
                    while self.peek() and self.peek()[1] == ",":
                        self.advance()
                        args.append(self.parse_expr())
                self.expect(")")
                node = Call(node, args, tok[2])
            else:
                return node

    def parse_primary(self) -> Any:
        tok = self.advance()
        kind, value, line = tok
        if kind == "NUM":
            return Num(value, line)
        if kind == "STR":
            return Str(value, line)
        if kind == "KW":
            if value == "true":
                return Bool(True, line)
            if value == "false":
                return Bool(False, line)
            if value == "null":
                return Null(line)
            raise ExprError(E_SYNTAX, f"关键字 {value!r} 不能出现在此处", line)
        if kind == "IDENT":
            return Ident(value, line)
        if value == "(":
            node = self.parse_expr()
            self.expect(")")
            return node
        if value == "{":
            return self._parse_dict(line)
        raise ExprError(E_SYNTAX, f"意外的 token {value!r}", line)

    def _parse_dict(self, line: int) -> DictLit:
        items = []
        if self.peek() and self.peek()[1] != "}":
            while True:
                ktok = self.advance()
                if ktok[0] in ("STR", "IDENT"):
                    key = Str(ktok[1], ktok[2])
                else:
                    raise ExprError(E_SYNTAX, "字典键必须是字符串或标识符", ktok[2])
                self.expect(":")
                items.append((key, self.parse_expr()))
                tok = self.peek()
                if tok and tok[1] == ",":
                    self.advance()
                    continue
                break
        self.expect("}")
        return DictLit(items, line)


def parse(source: str) -> Program:
    return _Parser(_tokenize(source)).parse_program()


# ---------------------------------------------------------------------------
# AST 遍历
# ---------------------------------------------------------------------------

def iter_nodes(node: Any) -> Iterator[Any]:
    """深度遍历所有 AST 节点（含 DictLit.items 的 (key, value) 元组）。"""
    if node is None:
        return
    if isinstance(node, (list, tuple)):
        for x in node:
            yield from iter_nodes(x)
        return
    if not hasattr(node, "__dataclass_fields__"):
        return
    yield node
    for f in node.__dataclass_fields__.values():
        yield from iter_nodes(getattr(node, f.name))


# ---------------------------------------------------------------------------
# 定义期校验
# ---------------------------------------------------------------------------

WHITELIST_FUNCTIONS: dict[str, tuple[int, int]] = {
    "if": (3, 3),
    "if_null": (2, 2),
    "coalesce": (1, -1),
    "lookup": (2, 3),
    "ref": (1, 2),
    "abs": (1, 1),
    "round": (1, 2),
    "min": (2, -1),
    "max": (2, -1),
    "str": (1, 1),
    "contains": (2, 2),
    "starts_with": (2, 2),
    # HF-06：受限字符串 / 日期内置（只读、无副作用）
    "len": (1, 1),
    "split": (2, 3),
    "regex_match": (2, 2),
    "upper": (1, 1),
    "lower": (1, 1),
    "date_add": (3, 3),
    "date_diff": (2, 2),
    # HF-07：受限声明式集合能力。刻意不引入 lambda / 数组字面量语法，
    # 聚合维度一律用字符串字段名传参，便于定义期静态校验与运行期审计。
    "sum": (1, 1),
    "map": (2, 2),
    "filter": (3, 3),
    "group_by": (3, 4),
    # HF-03：lookup 优先级回退 / 多命中列表（默认 lookup() 仍为严格模式）
    "lookup_first": (3, 5),
    "coalesce_lookup": (3, -1),
    "lookup_all": (2, 3),
}


def validate(program: Program, input_names: Optional[set[str]] = None) -> list[dict[str, Any]]:
    """定义期静态校验，返回 issue 列表（空 = 通过）。

    检查：白名单函数、参数个数、未知标识符（绑定/输入/this）、
    常量折叠除零（字面量 1/0 等）、ref() 实参必须为字符串字面量。
    """
    input_names = input_names or set()
    issues: list[dict[str, Any]] = []
    bound: set[str] = set()

    def issue(code: str, message: str, line: Optional[int]) -> None:
        issues.append({"code": code, "message": message, "line": line})

    for bind in program.binds:
        if bind.name in bound:
            issue(E_SYNTAX, f"绑定名重复: {bind.name}", bind.line)
        bound.add(bind.name)

    nodes: list[Any] = []
    for b in program.binds:
        nodes.extend(iter_nodes(b.expr))
    nodes.extend(iter_nodes(program.final))

    # Call 的函数名 Ident 与 Member 的基座 Ident（this）不参与标识符解析，避免误报
    skip_ids: set[int] = set()
    for node in nodes:
        if isinstance(node, Call) and isinstance(node.func, Ident):
            skip_ids.add(id(node.func))
        if isinstance(node, Member) and isinstance(node.base, Ident):
            skip_ids.add(id(node.base))

    for node in nodes:
        if isinstance(node, Call):
            func = node.func
            if isinstance(func, Ident):
                func = func.name
            if not isinstance(func, str) or func not in WHITELIST_FUNCTIONS:
                issue(E_UNKNOWN_FUNC, f"函数不在白名单: {func}", node.line)
                continue
            lo, hi = WHITELIST_FUNCTIONS[func]
            if len(node.args) < lo or (hi != -1 and len(node.args) > hi):
                issue(E_ARG_COUNT, f"函数 {func} 参数个数 {len(node.args)} 超出范围", node.line)
            # HF-06/07/03：新增内置涉及字段名/排序/聚合口径时，要求静态可判定，
            # 避免通过动态标识符绕过定义期引用治理与审计。
            if func in ("map", "filter", "group_by"):
                field_args = [1] if func in ("map", "filter") else [1, 2]
                for idx in field_args:
                    if idx < len(node.args) and not isinstance(node.args[idx], Str):
                        issue(E_TYPE, f"{func}() 第 {idx + 1} 个参数必须是字符串字段名", node.line)
                if func == "group_by" and len(node.args) > 3:
                    agg = node.args[3]
                    if not isinstance(agg, Str) or agg.value not in ("sum", "count"):
                        issue(E_TYPE, "group_by() 第 4 个参数必须是字符串字面量 'sum'/'count'", node.line)
            if func in ("lookup_first", "lookup_all"):
                if len(node.args) > 1 and not isinstance(node.args[1], DictLit):
                    issue(E_TYPE, f"{func}() 维度参数必须是字典字面量", node.line)
            if func == "lookup_first":
                if len(node.args) > 3 and not isinstance(node.args[3], (Str, Null)):
                    issue(E_TYPE, "lookup_first() 排序字段必须是字符串字面量或 null", node.line)
                if len(node.args) > 4 and not isinstance(node.args[4], Bool):
                    issue(E_TYPE, "lookup_first() descending 必须是布尔字面量", node.line)
            if func == "coalesce_lookup":
                for idx, arg in enumerate(node.args[2:], start=3):
                    if not isinstance(arg, DictLit):
                        issue(E_TYPE, f"coalesce_lookup() 第 {idx} 个参数起每组维度必须是字典字面量", node.line)
            if func == "ref":
                if not node.args or not isinstance(node.args[0], Str):
                    issue(E_REF_BAD_ARG, "ref() 第一个参数必须是字符串字面量（能力名）", node.line)
                if len(node.args) > 1 and not isinstance(node.args[1], Str):
                    issue(E_REF_BAD_ARG, "ref() 第二个参数（版本）必须是字符串字面量", node.line)
        elif isinstance(node, Ident):
            if id(node) in skip_ids:
                continue
            if node.name == "this":
                issue(E_SYNTAX, "this 必须配合属性使用: this.<attr>", node.line)
            elif node.name not in bound and node.name not in input_names:
                issue(E_UNKNOWN_IDENT, f"未知标识符: {node.name}（未绑定且不是声明输入）", node.line)
        elif isinstance(node, Member):
            base = node.base
            while isinstance(base, Member):
                base = base.base
            if not (isinstance(base, Ident) and base.name == "this"):
                issue(E_SYNTAX, "成员访问仅支持 this.<attr>", node.line)
        elif isinstance(node, BinOp) and node.op == "/":
            lv = _const_num(node.left)
            rv = _const_num(node.right)
            if lv is not None and rv is not None and rv == 0:
                issue(E_CONST_DIV_ZERO, "常量折叠检测到除零（字面量 1/0 等）", node.line)

    return issues


def _const_num(node: Any) -> Optional[float]:
    if isinstance(node, Num):
        return node.value
    if isinstance(node, Unary) and node.op == "-" and isinstance(node.operand, Num):
        return -node.operand.value
    return None


def extract_refs(program: Program) -> list[dict[str, Any]]:
    """收集表达式中所有 ref() 引用: [{name, version|None, line}]。"""
    refs: list[dict[str, Any]] = []
    for node in iter_nodes(program):
        if not isinstance(node, Call):
            continue
        func = node.func if isinstance(node.func, str) else getattr(node.func, "name", None)
        if func != "ref":
            continue
        name = node.args[0].value if node.args and isinstance(node.args[0], Str) else None
        version = None
        if len(node.args) > 1 and isinstance(node.args[1], Str):
            version = node.args[1].value
        if name:
            refs.append({"name": name, "version": version, "line": node.line})
    return refs


def extract_attr_refs(program: Program) -> set[str]:
    """收集表达式引用的 this.<attr> 属性名集合（输入依赖声明/溯源）。"""
    attrs: set[str] = set()
    for node in iter_nodes(program):
        if isinstance(node, Member):
            base = node.base
            while isinstance(base, Member):
                base = base.base
            if isinstance(base, Ident) and base.name == "this":
                attrs.add(node.attr)
    return attrs
