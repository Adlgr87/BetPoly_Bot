"""Static validation of every ``database.*`` / ``keyboards.*`` / ``bodies.*`` call.

The handlers are only exercised through Telegram at runtime, so a typo in a
keyword argument (``create_nonce(ttl=...)`` instead of ``ttl_seconds=...``)
would only surface in production.  This test parses the source instead of
executing it: it is fast, needs no mocks, and catches exactly that class of bug.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from bot import database, keyboards
from bot.chain import bodies

ROOT = Path(__file__).resolve().parent.parent
MODULES = {
    "database": database,
    "keyboards": keyboards,
}
SOURCES = sorted(
    [*(ROOT / "bot").rglob("*.py"), *(ROOT / "bot" / "handlers").rglob("*.py"), ROOT / "main.py"]
)


def _calls():
    for path in dict.fromkeys(SOURCES):
        if path.name == "__init__.py":
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
                continue
            if func.value.id not in MODULES:
                continue
            yield path, func.value.id, func.attr, node


CALLS = list(_calls())


def test_call_sites_were_found():
    # Sanity check: the scanner is actually looking at the handlers.
    assert len(CALLS) > 60


@pytest.mark.parametrize(
    "path,module_name,name,node",
    CALLS,
    ids=[f"{p.name}:{m}.{n}" for p, m, n, _ in CALLS],
)
def test_call_site_matches_signature(path, module_name, name, node):
    module = MODULES[module_name]
    assert hasattr(module, name), f"{path.name} usa {module_name}.{name}, que no existe"
    target = getattr(module, name)
    if not callable(target) or inspect.isbuiltin(target):
        return
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):  # pragma: no cover - C callables
        return

    parameters = signature.parameters.values()
    has_kwargs = any(p.kind is p.VAR_KEYWORD for p in parameters)
    positional = [
        p for p in parameters
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    has_varargs = any(p.kind is p.VAR_POSITIONAL for p in parameters)

    starred = any(isinstance(arg, ast.Starred) for arg in node.args)
    if not has_varargs and not starred:
        assert len(node.args) <= len(positional), (
            f"{path.name}:{node.lineno} pasa {len(node.args)} posicionales a "
            f"{module_name}.{name}() que acepta {len(positional)}"
        )

    for keyword in node.keywords:
        if keyword.arg is None or has_kwargs:
            continue
        assert keyword.arg in signature.parameters, (
            f"{path.name}:{node.lineno} pasa {keyword.arg}= a {module_name}.{name}(), "
            f"que espera {sorted(signature.parameters)}"
        )


def test_message_body_fields_can_be_built():
    """Every opcode exposed by ``bodies`` must round-trip through its builder."""
    assert bodies.OP_CREATE_MARKET in bodies.KNOWN_OPS
    assert bodies.OP_PLACE_BET in bodies.KNOWN_OPS
    assert bodies.OP_RESOLVE_MARKET in bodies.KNOWN_OPS
    assert bodies.OP_CANCEL_MARKET in bodies.KNOWN_OPS
    assert bodies.OP_CLAIM_PAYOUT in bodies.KNOWN_OPS
    assert set(bodies.EVENT_OPS).issubset(bodies.OP_NAMES)


def test_every_known_op_has_a_name():
    assert set(bodies.KNOWN_OPS) == set(bodies.OP_NAMES) - set(bodies.EVENT_OPS)
