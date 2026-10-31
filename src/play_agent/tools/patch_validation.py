"""AST-level validation for apply_patch (architecture Sec7/Sec9, ADR-12).

Uses tree-sitter (TypeScript grammar) to parse the target test file and
confirm, structurally, that a patch:
  1. targets a real, allowlisted locator-builder call (identified by its
     locatorCallId -- a stable position-derived id assigned when evidence
     was collected), and
  2. replaces it with an expression that is itself only a chain of
     allowlisted locator methods called with safe literal arguments (per
     method -- see ARGUMENT_SHAPES below).

This is the enforcement point for Sec9's core safety property: the patch
representation is narrow enough to be exhaustively validated, and
validation happens here, in-process, before anything ever reaches the
sandbox. A patch that fails validation never gets near apply_patch's
actual file write.
"""

from __future__ import annotations

from dataclasses import dataclass

import tree_sitter_typescript as ts_typescript
from tree_sitter import Language, Node, Parser

ALLOWED_LOCATOR_METHODS = frozenset(
    {
        "getByTestId",
        "getByRole",
        "getByText",
        "getByLabel",
        "getByPlaceholder",
        "getByAltText",
        "getByTitle",
        "locator",
        "first",
        "last",
        "nth",
        "filter",
    }
)

_NO_ARGS = {"first", "last"}
_ONE_STRING_ARG = {
    "getByTestId",
    "getByText",
    "getByLabel",
    "getByPlaceholder",
    "getByAltText",
    "getByTitle",
    "locator",
}
_ONE_NUMBER_ARG = {"nth"}
_STRING_PLUS_OPTIONAL_OPTIONS = {"getByRole"}
_OPTIONS_OBJECT_ONLY = {"filter"}

_TS_LANGUAGE = Language(ts_typescript.language_typescript())


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    reason: str | None = None


@dataclass(frozen=True)
class LocatableCall:
    locator_call_id: str
    start_byte: int
    end_byte: int
    source_text: str


def _make_parser() -> Parser:
    return Parser(_TS_LANGUAGE)


def find_locator_calls(source: str) -> list[LocatableCall]:
    parser = _make_parser()
    tree = parser.parse(source.encode("utf-8"))
    calls: list[LocatableCall] = []

    def visit(node: Node) -> None:
        if node.type == "call_expression":
            callee = node.child_by_field_name("function")
            if callee is not None and callee.type == "member_expression":
                method_name_node = callee.child_by_field_name("property")
                if method_name_node is not None:
                    method_name = method_name_node.text.decode("utf-8")
                    if method_name in ALLOWED_LOCATOR_METHODS:
                        start_point = node.start_point
                        locator_call_id = f"L{start_point[0] + 1}C{start_point[1] + 1}"
                        calls.append(
                            LocatableCall(
                                locator_call_id=locator_call_id,
                                start_byte=node.start_byte,
                                end_byte=node.end_byte,
                                source_text=node.text.decode("utf-8"),
                            )
                        )
        for child in node.children:
            visit(child)

    visit(tree.root_node)
    return calls


def validate_new_locator_expression(expression: str) -> ValidationResult:
    parser = _make_parser()
    wrapped = f"({expression});"
    tree = parser.parse(wrapped.encode("utf-8"))

    if tree.root_node.has_error:
        return ValidationResult(valid=False, reason="expression does not parse as valid TS/JS")

    try:
        expr_stmt = tree.root_node.children[0]
        paren_expr = expr_stmt.children[0]
        call_node = paren_expr.named_children[0]
    except (IndexError, AttributeError):
        return ValidationResult(valid=False, reason="unexpected expression shape")

    return _validate_call_chain(call_node)


def _validate_call_chain(node: Node) -> ValidationResult:
    if node.type != "call_expression":
        return ValidationResult(
            valid=False, reason=f"expected a method call, found {node.type}"
        )

    callee = node.child_by_field_name("function")
    args = node.child_by_field_name("arguments")

    if callee is None or args is None:
        return ValidationResult(valid=False, reason="malformed call expression")

    if callee.type == "identifier":
        method_name = callee.text.decode("utf-8")
        receiver_type = "identifier"
        receiver = None
    elif callee.type == "member_expression":
        method_name_node = callee.child_by_field_name("property")
        receiver = callee.child_by_field_name("object")
        if method_name_node is None or receiver is None:
            return ValidationResult(valid=False, reason="malformed member expression")
        method_name = method_name_node.text.decode("utf-8")
        receiver_type = receiver.type
    else:
        return ValidationResult(valid=False, reason=f"unexpected callee type: {callee.type}")

    if method_name not in ALLOWED_LOCATOR_METHODS:
        return ValidationResult(valid=False, reason=f"method '{method_name}' is not allowlisted")

    arg_check = _validate_arguments_for_method(method_name, args.named_children)
    if not arg_check.valid:
        return arg_check

    if receiver is None or receiver_type == "identifier":
        return ValidationResult(valid=True)

    if receiver_type == "call_expression":
        return _validate_call_chain(receiver)

    return ValidationResult(valid=False, reason=f"unexpected receiver type: {receiver_type}")


def _validate_arguments_for_method(method_name: str, args: list[Node]) -> ValidationResult:
    if method_name in _NO_ARGS:
        if args:
            return ValidationResult(valid=False, reason=f"{method_name} takes no arguments")
        return ValidationResult(valid=True)

    if method_name in _ONE_STRING_ARG:
        if len(args) != 1 or not _is_safe_string_literal(args[0]):
            return ValidationResult(
                valid=False, reason=f"{method_name} requires exactly one string literal argument"
            )
        return ValidationResult(valid=True)

    if method_name in _ONE_NUMBER_ARG:
        if len(args) != 1 or not _is_number_literal(args[0]):
            return ValidationResult(
                valid=False, reason=f"{method_name} requires exactly one numeric literal argument"
            )
        return ValidationResult(valid=True)

    if method_name in _STRING_PLUS_OPTIONAL_OPTIONS:
        if not args or not _is_safe_string_literal(args[0]):
            return ValidationResult(
                valid=False, reason=f"{method_name} requires a string literal as its first argument"
            )
        if len(args) > 2:
            return ValidationResult(valid=False, reason=f"{method_name} takes at most two arguments")
        if len(args) == 2:
            return _validate_safe_object_literal(args[1])
        return ValidationResult(valid=True)

    if method_name in _OPTIONS_OBJECT_ONLY:
        if len(args) != 1:
            return ValidationResult(
                valid=False, reason=f"{method_name} requires exactly one options object argument"
            )
        return _validate_safe_object_literal(args[0])

    return ValidationResult(valid=False, reason=f"no argument rule defined for '{method_name}'")


def _is_safe_string_literal(node: Node) -> bool:
    if node.type == "string":
        return True
    if node.type == "template_string":
        return not any(child.type == "template_substitution" for child in node.children)
    return False


def _is_number_literal(node: Node) -> bool:
    return node.type == "number"


def _validate_safe_object_literal(node: Node) -> ValidationResult:
    if node.type != "object":
        return ValidationResult(valid=False, reason=f"expected an object literal, found {node.type}")

    for pair in node.named_children:
        if pair.type != "pair":
            continue
        value = pair.child_by_field_name("value")
        if value is None or not _is_safe_literal_value(value):
            return ValidationResult(
                valid=False, reason="object literal values must be safe literals, not expressions"
            )

    return ValidationResult(valid=True)


def _is_safe_literal_value(node: Node) -> bool:
    if node.type in ("string", "number", "true", "false", "null"):
        return True
    if node.type == "template_string":
        return _is_safe_string_literal(node)
    if node.type == "object":
        return _validate_safe_object_literal(node).valid
    if node.type == "array":
        return all(_is_safe_literal_value(child) for child in node.named_children)
    return node.type == "regex"


def resolve_locator_call(source: str, locator_call_id: str) -> LocatableCall | None:
    for call in find_locator_calls(source):
        if call.locator_call_id == locator_call_id:
            return call
    return None


def validate_patch(source: str, locator_call_id: str, new_locator_expression: str) -> ValidationResult:
    target = resolve_locator_call(source, locator_call_id)
    if target is None:
        return ValidationResult(
            valid=False,
            reason=f"locator_call_id '{locator_call_id}' not found (file may have changed)",
        )

    return validate_new_locator_expression(new_locator_expression)