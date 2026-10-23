"""Guardarraíles de arquitectura (P7 del plan de implementación).

Estos tests no verifican comportamiento: verifican que la **dirección de las
dependencias** siga siendo la acordada. Si alguien introduce un import que
rompe las capas, el test falla con un mensaje que explica dónde debe vivir esa
responsabilidad.

Las violaciones preexistentes viven en ``KNOWN_VIOLATIONS`` con su motivo y la
fase en la que desaparecen: la lista solo puede encogerse.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent

# (archivo relativo, módulo importado prohibido) -> motivo / fase que lo resuelve
KNOWN_VIOLATIONS: dict[tuple[str, str], str] = {
    ("bot/keyboards.py", "config"): (
        "Los teclados leen config.get_settings() (7 usos). Deben recibir los datos "
        "ya resueltos; se refactoriza en la Fase 5 junto con menu_for()."
    ),
}


def _python_files() -> list[Path]:
    paths = sorted(ROOT.joinpath("bot").rglob("*.py"))
    paths += [ROOT / "main.py", ROOT / "config.py"]
    paths += sorted(ROOT.joinpath("scripts").rglob("*.py"))
    return [p for p in paths if p.is_file() and "__pycache__" not in p.parts]


def _imports(path: Path) -> set[str]:
    """Full dotted names imported anywhere in the file (functions included)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            names.add(node.module)
    return names


def _violations(predicate: Callable[[str], bool], forbidden: str) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in _python_files():
        rel = path.relative_to(ROOT).as_posix()
        if not predicate(rel):
            continue
        for name in _imports(path):
            if name == forbidden or name.startswith(forbidden + "."):
                found.add((rel, forbidden))
                break
    return found


def _assert_rule(rule: str, predicate: Callable[[str], bool], forbidden: str) -> None:
    current = _violations(predicate, forbidden)
    baseline = {key for key in KNOWN_VIOLATIONS if key[1] == forbidden}
    new = current - baseline
    assert not new, (
        f"[{rule}] import prohibido de '{forbidden}' en {sorted(new)}.\n"
        "Respeta la dirección de capas descrita en P7 del plan de implementación."
    )
    stale = baseline - current
    assert not stale, (
        f"[{rule}] estas violaciones ya no existen: {sorted(stale)}. "
        "Elimínalas de KNOWN_VIOLATIONS."
    )


def test_api_layer_does_not_depend_on_telegram() -> None:
    """La API HTTP debe poder servirse sin el bot de Telegram (dev_server, tests)."""
    _assert_rule("api-no-telegram", lambda rel: rel == "bot/api.py", "telegram")


def test_chain_layer_does_not_depend_on_telegram() -> None:
    """bot/chain es infraestructura de cadena, no de mensajería."""
    _assert_rule("chain-no-telegram", lambda rel: rel.startswith("bot/chain/"), "telegram")


def test_chain_layer_only_talks_http_through_toncenter() -> None:
    """Solo el cliente de toncenter abre sesiones HTTP propias."""
    _assert_rule(
        "chain-http-boundary",
        lambda rel: rel.startswith("bot/chain/") and rel != "bot/chain/toncenter.py",
        "aiohttp",
    )


def test_handlers_do_not_touch_the_chain_clients_directly() -> None:
    """Los handlers usan el servicio (markets/database), no los clientes de cadena."""
    handlers = lambda rel: rel.startswith("bot/handlers/")  # noqa: E731
    for forbidden in ("aiohttp", "bot.chain.indexer", "bot.chain.toncenter"):
        _assert_rule("handlers-service-only", handlers, forbidden)


def test_database_has_no_presentation_dependencies() -> None:
    """La persistencia no conoce Telegram ni HTTP: es reutilizable por ambos."""
    for forbidden in ("telegram", "aiohttp"):
        _assert_rule("db-pure", lambda rel: rel == "bot/database.py", forbidden)


def test_web_assets_do_not_hardcode_economics() -> None:
    """La Mini App no debe contener la fórmula pari-mutuel (ADR-04).

    El backend es la única fuente de multiplicadores y pagos potenciales.
    """
    app_js = ROOT.joinpath("web", "static", "app.js").read_text(encoding="utf-8")
    forbidden = ("fee_bps", "house_rake", "winning_pool")
    found = [token for token in forbidden if token in app_js]
    assert not found, (
        f"app.js contiene cálculo económico propio: {found}. "
        "Debe usar el multiplicador / pago potencial que devuelve la API (ADR-04)."
    )


def test_every_module_parses() -> None:
    """Sanidad: que el test no pase por no haber leído nada."""
    files = _python_files()
    assert len(files) > 15, f"se esperaban los módulos del proyecto, hay {len(files)}"
    for path in files:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
