"""
Decides, without running anything, whether a conditional block in a module's source
can be reached on the machine a distribution is built *for*.

A platform guard is not a heuristic: `if sys.platform == "win32":` never runs on Linux,
and the module it imports can be one that cannot even be imported there. Following both
branches of every `if` is what makes an import closure carry `msvcrt`, `_winapi`,
`ctypes` and friends into a Linux distribution.

Deliberately not `eval`: what this understands is a closed list of expressions, and
anything outside it is *unknown* rather than guessed. The asymmetry is the whole point
-- an unknown condition means both branches are followed, which over-collects, while a
wrongly decided one drops a module the target needs.

@date: 04.09.2026
@author: Baptiste Pestourie
"""

from __future__ import annotations

import ast
import os
import sys
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Final


class NotStaticError(Exception):
    """
    Raised when an expression's value cannot be decided without running the module.

    An exception rather than a sentinel value, because every value a condition can hold
    is a legal answer -- `None`, `False` and `0` included -- so there is nothing left to
    signal "unknown" with.
    """


#: A value this module can work out at build time: what a platform guard compares.
type StaticValue = str | int | bool | None | tuple[StaticValue, ...]

#: The names an expression may refer to, mapped to their value on the target.
type StaticNames = Mapping[str, StaticValue]


@dataclass(frozen=True)
class TargetEnvironment:
    """
    The values a guard is decided against: the machine and interpreter the distribution
    is *for*, which is not necessarily the one building it.

    Three names cover every guard shape found in the standard library and in the
    third-party packages walked so far, which is why the list stops here.
    """

    platform: str
    os_name: str
    version_info: tuple[int, int, int, str, int]

    @classmethod
    def host(cls) -> TargetEnvironment:
        """
        The interpreter running smelt.

        That is the right answer for every distribution smelt can currently assemble:
        bytecode is locked to the interpreter that compiled it and extension modules to
        the platform they were compiled on, so a distribution is always for the machine
        that built it. Cross-compiled distributions are what this class exists for.
        """
        info = sys.version_info
        return cls(
            platform=sys.platform,
            os_name=os.name,
            version_info=(info.major, info.minor, info.micro, info.releaselevel, info.serial),
        )

    @property
    def known_names(self) -> dict[str, StaticValue]:
        """
        The dotted names an expression may refer to, mapped to their value here.

        `sys.version_info`'s fields are spelled out rather than resolved as attribute
        access, so that resolving any name is a single dictionary lookup and module-level
        constants (`static_names`) can join the same mapping.
        """
        fields = ("major", "minor", "micro", "releaselevel", "serial")
        return {
            "sys.platform": self.platform,
            "os.name": self.os_name,
            "sys.version_info": self.version_info,
            **{
                f"sys.version_info.{field}": value
                for field, value in zip(fields, self.version_info, strict=True)
            },
        }


DEFAULT_TARGET: Final[TargetEnvironment] = TargetEnvironment.host()


#: String methods a guard may call on a known value, e.g.
#: `sys.platform.startswith("win")`. All pure, all total, all cheap.
_STRING_METHODS: Final[frozenset[str]] = frozenset(
    {"startswith", "endswith", "lower", "upper", "strip"}
)


def static_names(tree: ast.Module, target: TargetEnvironment = DEFAULT_TARGET) -> StaticNames:
    """
    Everything a condition in `tree` can be decided from: `target`'s own names, plus the
    module's top-level constants that follow from them.

    The constants matter more than they look: hardly any module guards on `sys.platform`
    directly. They assign it to a name once -- `_mswindows = sys.platform == "win32"` in
    `subprocess`, `WIN = sys.platform.startswith("win")` in `click._compat` -- and guard
    on that. Without resolving them, the interesting guards are all undecidable and the
    Windows-only imports behind them get followed anyway.
    """
    names = target.known_names
    bindings = Counter(_iter_bound_names(tree))
    for statement in tree.body:
        binding = _constant_binding(statement)
        if binding is None:
            continue
        name, value_node = binding
        if bindings[name] != 1:
            # Bound more than once, anywhere in the module and at any depth: which
            # binding a given condition sees would depend on control flow.
            continue
        try:
            names[name] = _static_value(value_node, names)
        except NotStaticError:
            continue
    return names


def _constant_binding(statement: ast.stmt) -> tuple[str, ast.expr] | None:
    """
    The `(name, value)` of a single-name assignment statement, annotated or not.
    """
    if isinstance(statement, ast.Assign):
        if len(statement.targets) == 1 and isinstance(statement.targets[0], ast.Name):
            return statement.targets[0].id, statement.value
        return None
    if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
        return (statement.target.id, statement.value) if statement.value else None
    return None


def _iter_bound_names(tree: ast.Module) -> Iterator[str]:
    """
    Every name bound anywhere in the module, at any depth and by any statement.

    Used only to count: a name bound twice is not a constant, wherever the second
    binding is. Erring towards "not a constant" costs a guard that stays undecidable,
    which is the direction this module always leans.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            yield node.id
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node.name
        elif isinstance(node, ast.alias):
            yield node.asname or node.name.partition(".")[0]
        elif isinstance(node, ast.ExceptHandler) and node.name is not None:
            yield node.name
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            yield from node.names


def condition_value(test: ast.expr, names: StaticNames) -> bool | None:
    """
    Whether the `if` condition `test` holds, or None when that cannot be decided without
    running the module -- in which case the caller must assume either branch can be
    taken.
    """
    try:
        return _static_condition(test, names)
    except NotStaticError:
        return None


def _as_static(value: object) -> StaticValue:
    """
    Narrows a `literal_eval` result to a `StaticValue`, or rejects it.

    Sequences and sets collapse to tuples: `sys.platform in {"win32", "cygwin"}` and the
    same guard spelled with a list or a tuple are the same question, and a tuple is the
    one shape the comparisons below need to handle.
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, (tuple, list, set, frozenset)):
        return tuple(_as_static(item) for item in value)
    raise NotStaticError


def _literal_value(node: ast.expr) -> StaticValue:
    """
    The value of `node` if it is a literal, e.g. the right-hand side of a guard.
    """
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        raise NotStaticError from None
    return _as_static(value)


def _dotted_name(node: ast.expr) -> str:
    """
    The dotted source spelling of an attribute chain (`sys.version_info`) or of a bare
    name (`WIN`). Anything else cannot be a known name and is rejected.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted_name(node.value)}.{node.attr}"
    raise NotStaticError


def _static_value(node: ast.expr, names: StaticNames) -> StaticValue:
    """
    The value of `node`: a literal, one of `names`, an index or slice of either, a pure
    string method call on one, or a comparison between any of those. Everything else is
    unknown.

    Comparisons are values too, and have to be, because that is how a module declares
    the constant it later guards on: `WIN = sys.platform == "win32"`.
    """
    if isinstance(node, (ast.Compare, ast.BoolOp)) or (
        isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not)
    ):
        return _static_condition(node, names)
    if isinstance(node, (ast.Name, ast.Attribute)):
        name = _dotted_name(node)
        # `True`/`False`/`None` parse as constants in every version this supports, so
        # falling back to the literal here is only for completeness.
        return names[name] if name in names else _literal_value(node)
    if isinstance(node, ast.Subscript):
        return _static_subscript(node, names)
    if isinstance(node, ast.Call):
        return _static_method_call(node, names)
    return _literal_value(node)


def _static_subscript(node: ast.Subscript, names: StaticNames) -> StaticValue:
    """
    `sys.platform[:3]`, `sys.version_info[:2]`, `sys.version_info[0]`.
    """
    value = _static_value(node.value, names)
    if not isinstance(value, (str, tuple)):
        raise NotStaticError
    if isinstance(node.slice, ast.Slice):
        return value[_static_slice(node.slice, names)]
    index = _static_integer(node.slice, names)
    try:
        return value[index]
    except IndexError:
        raise NotStaticError from None


def _static_integer(node: ast.expr, names: StaticNames) -> int:
    """
    An index or slice bound, which has to be a known integer. `bool` is excluded on
    purpose: `sys.platform[True]` is not a guard, it is a mistake.
    """
    value = _static_value(node, names)
    if not isinstance(value, int) or isinstance(value, bool):
        raise NotStaticError
    return value


def _static_slice(node: ast.Slice, names: StaticNames) -> slice:
    """
    The bounds of a slice expression, all of which must be known integers or absent.
    """
    lower, upper, step = (
        None if bound is None else _static_integer(bound, names)
        for bound in (node.lower, node.upper, node.step)
    )
    return slice(lower, upper, step)


def _static_method_call(node: ast.Call, names: StaticNames) -> StaticValue:
    """
    A `_STRING_METHODS` call on a known string: `sys.platform.startswith("win")`,
    `sys.platform.lower().startswith("win")`.
    """
    if not isinstance(node.func, ast.Attribute) or node.func.attr not in _STRING_METHODS:
        raise NotStaticError
    if node.keywords:
        raise NotStaticError
    subject = _static_value(node.func.value, names)
    if not isinstance(subject, str):
        raise NotStaticError
    arguments = [_static_value(argument, names) for argument in node.args]
    try:
        result = getattr(subject, node.func.attr)(*arguments)
    except TypeError:
        # e.g. `startswith` handed something that is neither a string nor a tuple of
        # them, which means the guard is not the shape this understands.
        raise NotStaticError from None
    if not isinstance(result, (str, bool)):
        raise NotStaticError
    return result


def _ordered[T: (str, int, tuple[StaticValue, ...])](op: ast.cmpop, left: T, right: T) -> bool:
    """
    An ordering comparison between two known values of the same type.
    """
    if isinstance(op, ast.Lt):
        return left < right
    if isinstance(op, ast.LtE):
        return left <= right
    if isinstance(op, ast.Gt):
        return left > right
    if isinstance(op, ast.GtE):
        return left >= right
    raise NotStaticError


def _static_containment(op: ast.In | ast.NotIn, left: StaticValue, right: StaticValue) -> bool:
    """
    `sys.platform in ("win32", "cygwin")`, and its negation.
    """
    if isinstance(right, tuple):
        contained = left in right
    elif isinstance(right, str) and isinstance(left, str):
        contained = left in right
    else:
        raise NotStaticError
    return contained if isinstance(op, ast.In) else not contained


def _static_comparison(op: ast.cmpop, left: StaticValue, right: StaticValue) -> bool:
    """
    Applies one comparison operator to two known values.

    Ordering comparisons require both sides to be of the same type: a mismatch is a
    `TypeError` at runtime, so the guard is not what it looks like and deciding it
    either way would be a guess.
    """
    if isinstance(op, ast.Eq):
        return left == right
    if isinstance(op, ast.NotEq):
        return left != right
    if isinstance(op, (ast.In, ast.NotIn)):
        return _static_containment(op, left, right)
    if isinstance(left, str) and isinstance(right, str):
        return _ordered(op, left, right)
    if isinstance(left, tuple) and isinstance(right, tuple):
        return _ordered(op, left, right)
    if isinstance(left, int) and isinstance(right, int):
        return _ordered(op, left, right)
    raise NotStaticError


def _static_condition(test: ast.expr, names: StaticNames) -> bool:
    """
    Whether `test` holds, raising `NotStaticError` when it cannot be decided.
    """
    if isinstance(test, ast.BoolOp):
        return _static_boolean_operation(test, names)

    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return not _static_condition(test.operand, names)

    if isinstance(test, ast.Compare):
        if len(test.ops) != 1:
            # Chained comparisons (`a < b < c`) do not appear in platform guards.
            raise NotStaticError
        return _static_comparison(
            test.ops[0],
            _static_value(test.left, names),
            _static_value(test.comparators[0], names),
        )

    return bool(_static_value(test, names))


def _static_boolean_operation(test: ast.BoolOp, names: StaticNames) -> bool:
    """
    `and`/`or` over operands that need not all be known: one decisive operand settles
    the whole expression, exactly as short-circuiting would, and otherwise every operand
    has to agree.

    `sys.platform.startswith("win") and WIN` is false on Linux whatever `WIN` turns out
    to be -- and that is the shape `click._compat` guards its Windows console support
    with, which is how `ctypes` ends up in a Linux closure.
    """
    decisive = isinstance(test.op, ast.Or)
    known = 0
    for operand in test.values:
        try:
            value = _static_condition(operand, names)
        except NotStaticError:
            continue
        if value is decisive:
            return decisive
        known += 1
    if known == len(test.values):
        return not decisive
    raise NotStaticError
