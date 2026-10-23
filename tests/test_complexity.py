"""Complejidad bajo control (R-07): un trinquete, no una convención.

No es una métrica de vanidad. `verify_ton_proof` y `settle_prediction` eran las
dos funciones más enrevesadas del proyecto y las dos tocan dinero: una decide si
una wallet es de quien dice ser y la otra reparte el pozo. Se partieron en pasos
con nombre, y este test impide que vuelvan a crecer sin que alguien lo vea.

La métrica es McCabe simplificada sobre el AST (sin dependencias nuevas): 1 más
cada `if`/`for`/`while`/`except`/`assert`, cada rama extra de `and`/`or`, cada
`if` de comprensión y cada `if` en línea.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict

ROOT = Path(__file__).resolve().parents[1]
MAX_COMPLEXITY = 12

#: Funciones que hoy superan el techo. Es un trinquete: una entrada solo puede
#: bajar, y ninguna función nueva puede entrar en la lista. Fase 7 las divide.
BASELINE: Dict[str, int] = {
    "bot/api.py::verify_wallet": 19,
    "bot/chain/bodies.py::prepare_transaction_payload": 16,
    "bot/chain/indexer.py::_poll": 14,
    "bot/chain/indexer.py::_fetch": 18,
    "bot/chain/indexer.py::_process_tx": 26,
    "bot/chain/toncenter.py::_request": 14,
    "bot/database.py::enrich_market_from_event": 18,
    "bot/handlers/admin.py::_show_admin_panel": 14,
    "bot/handlers/markets.py::market_view_callback": 19,
    "bot/handlers/predictions.py::bet_amount_message": 14,
    "bot/handlers/stats.py::_send_profile": 14,
    "bot/markets.py::prepare_create_market": 14,
    "bot/markets.py::market_view": 15,
    "bot/security.py::validate_init_data": 22,
    "config.py::validate": 20,
    "main.py::post_init": 14,
}

#: El núcleo de dinero, con tope duro y sin entrada en el baseline.
HARD_LIMITS = {
    "bot/chain/ton_proof.py::verify_ton_proof": 8,
    "bot/database.py::settle_prediction": 8,
    "bot/database.py::_plan_payouts": 8,
}

_BRANCHES = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler, ast.IfExp, ast.Assert)


def complexity(node: ast.AST) -> int:
    total = 1
    for child in ast.walk(node):
        if isinstance(child, _BRANCHES):
            total += 1
        elif isinstance(child, ast.BoolOp):
            total += len(child.values) - 1
        elif isinstance(child, ast.comprehension):
            total += len(child.ifs) + 1
    return total


def _python_files() -> list[Path]:
    skip = (".venv", "tests", "contracts", "__pycache__")
    return [
        path
        for path in ROOT.rglob("*.py")
        if not any(part in skip for part in path.relative_to(ROOT).parts)
    ]


def scores() -> Dict[str, int]:
    found: Dict[str, int] = {}
    for path in _python_files():
        relative = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found[f"{relative}::{node.name}"] = complexity(node)
    return found


def test_the_test_reads_the_project() -> None:
    """Sanidad: que no pase por no haber encontrado nada."""
    assert len(scores()) > 100


def test_no_new_function_crosses_the_ceiling() -> None:
    offenders = {
        name: value
        for name, value in scores().items()
        if value > MAX_COMPLEXITY and name not in BASELINE
    }
    assert not offenders, (
        f"funciones nuevas por encima de {MAX_COMPLEXITY} de complejidad: {offenders}. "
        "Divídelas en pasos con nombre o justifícalas en el baseline."
    )


def test_baseline_only_shrinks() -> None:
    current = scores()
    problems = []
    for name, limit in BASELINE.items():
        value = current.get(name)
        if value is None:
            problems.append(f"{name} ya no existe: quítala del baseline")
        elif value > limit:
            problems.append(f"{name} subió de {limit} a {value}")
    assert not problems, "; ".join(problems)


def test_money_core_has_a_hard_limit() -> None:
    current = scores()
    for name, limit in HARD_LIMITS.items():
        value = current.get(name)
        assert value is not None, f"{name} desapareció"
        assert value <= limit, f"{name} tiene complejidad {value} (tope {limit})"


def test_refactored_functions_stay_out_of_the_baseline() -> None:
    for name in HARD_LIMITS:
        assert name not in BASELINE, f"{name} no puede volver al baseline"


def test_no_function_is_absurdly_complex() -> None:
    worst = max(scores().items(), key=lambda item: item[1])
    assert worst[1] <= 30, f"{worst[0]} tiene complejidad {worst[1]}"
