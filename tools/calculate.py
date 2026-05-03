"""
tools/calculate.py — Safe mathematical expression evaluator.
Uses AST parsing to evaluate arithmetic without arbitrary code execution.
"""
import ast
import logging
from mcp import types

log = logging.getLogger(__name__)


# ── _calculate ────────────────────────────────────────────────────────────────

async def _calculate(expression: str) -> list[types.TextContent]:
    try:
        tree = ast.parse(expression, mode="eval")
        result = _eval(tree.body)
        return [types.TextContent(type="text", text=f"{expression} = {result}")]
    except Exception as e:
        return [types.TextContent(type="text", text=f"Error: {e}")]


def _eval(node):
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        raise ValueError("Invalid constant")

    if isinstance(node, ast.BinOp):
        left = _eval(node.left)
        right = _eval(node.right)

        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            if right == 0:
                raise ValueError("Division by zero")
            return left / right
        if isinstance(node.op, ast.Mod):
            if right == 0:
                raise ValueError("Modulo by zero")
            return left % right
        if isinstance(node.op, ast.Pow):
            if abs(right) > 1000:
                raise ValueError("Exponent too large")
            result = left ** right
            if abs(result) > 1e100:
                raise ValueError("Result too large")
            return result

        raise ValueError("Unsupported operation")

    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.UAdd):
            return _eval(node.operand)
        if isinstance(node.op, ast.USub):
            return -_eval(node.operand)
        raise ValueError("Unsupported unary operation")

    raise ValueError("Unsupported expression")
