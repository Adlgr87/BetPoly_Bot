"""Guardarraíles de conectividad: detectar «partes desconectadas».

La auditoría encontró funcionalidad completa que nadie invocaba (S-03, D-02) y
un menú que perdía el botón de admin (F-01). Ningún test funcional detecta eso,
porque el código *nunca se ejecuta*. Estos tests comparan lo que se **produce**
(rutas HTTP, callback_data, builders de mensajes) con lo que se **consume**
(frontend, handlers, resto del código).

Cada huérfano conocido está en un baseline con su motivo y la fase que lo
cierra. El baseline es un trinquete: si aparece uno nuevo falla, y si uno
desaparece también falla (obligando a limpiar la lista).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API_PY = ROOT / "bot" / "api.py"
APP_JS = ROOT / "web" / "static" / "app.js"
KEYBOARDS_PY = ROOT / "bot" / "keyboards.py"
MAIN_PY = ROOT / "main.py"
BODIES_PY = ROOT / "bot" / "chain" / "bodies.py"

# Ruta de API sin consumidor en el frontend -> motivo. Desde la Fase 3 la lista
# solo conserva el endpoint de sondas, que por diseño no consume la UI.
KNOWN_API_ORPHANS: dict[str, str] = {
    "/api/health": "Endpoint de orquestador / sondas; no lo consume la UI (correcto).",
}

# Builders de cuerpo sin consumidor -> motivo y fase. Vacío desde la Fase 2:
# el panel de administración del contrato ya usa los cuatro.
KNOWN_BUILDER_ORPHANS: dict[str, str] = {}


def _normalise(path: str) -> str:
    """`/api/markets/${id}?x=1` y `/api/markets/{market_id}` -> misma forma."""
    path = path.split("?")[0]
    path = re.sub(r"\$\{[^}]*\}", "{x}", path)
    path = re.sub(r"\{[^}]*\}", "{x}", path)
    return path.rstrip("/") or "/"


def _api_routes() -> set[str]:
    source = API_PY.read_text(encoding="utf-8")
    routes = re.findall(r"app\.router\.add_(?:get|post|put|delete|patch)\(\s*\"([^\"]+)\"", source)
    return {_normalise(route) for route in routes if route.startswith("/api")}


def _frontend_calls() -> set[str]:
    source = APP_JS.read_text(encoding="utf-8")
    calls = re.findall(r"\bapi\(\s*[`'\"]([^`'\"]+)[`'\"]", source)
    return {_normalise(call) for call in calls if call.startswith("/api")}


def _keyboard_callbacks() -> set[str]:
    """Todos los callback_data que el bot puede emitir, con muestras.

    Se escanean los teclados **y** los handlers: algunos flujos de admin
    construyen su teclado en línea junto al mensaje.
    """
    sources = [KEYBOARDS_PY.read_text(encoding="utf-8")]
    sources += [
        path.read_text(encoding="utf-8")
        for path in sorted(ROOT.joinpath("bot", "handlers").glob("*.py"))
    ]
    raw = [value for source in sources for value in re.findall(r"callback_data=f?\"([^\"]+)\"", source)]
    samples = set()
    for value in raw:
        samples.add(re.sub(r"\{[^}]*\}", "1", value).replace("%d", "1").replace("%s", "1"))
    return samples


def _handler_patterns() -> set[str]:
    source = MAIN_PY.read_text(encoding="utf-8")
    patterns = set(re.findall(r"\(\s*r?\"(\^[^\"]*)\"\s*,", source))
    for handler_file in sorted(ROOT.joinpath("bot", "handlers").glob("*.py")):
        text = handler_file.read_text(encoding="utf-8")
        for pattern in re.findall(r"pattern=r?\"([^\"]+)\"", text):
            if pattern.startswith("^"):
                patterns.add(pattern)
    return patterns


def _builder_names() -> set[str]:
    source = BODIES_PY.read_text(encoding="utf-8")
    return set(re.findall(r"^def (build_[a-z_]+)\(", source, flags=re.MULTILINE))


def _builder_consumers() -> set[str]:
    """Builders usados por el producto (los tests no cuentan como consumidor)."""
    used: set[str] = set()
    for path in list(ROOT.joinpath("bot").rglob("*.py")) + [ROOT / "main.py"]:
        if path == BODIES_PY or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        used.update(name for name in _builder_names() if name in text)
    return used


def _assert_ratchet(kind: str, current: set[str], baseline: dict[str, str]) -> None:
    new = current - set(baseline)
    assert not new, (
        f"[{kind}] elementos sin consumidor: {sorted(new)}.\n"
        "Cablea la funcionalidad (handler, botón, llamada) o, si es intencional, "
        "documéntalo en el baseline de este test con su motivo."
    )
    stale = set(baseline) - current
    assert not stale, (
        f"[{kind}] ya no están huérfanos: {sorted(stale)}. "
        "Elimínalos del baseline para mantener el trinquete apretado."
    )


def test_every_api_route_has_a_consumer() -> None:
    """Toda ruta /api/* la usa la Mini App, o está justificada en el baseline."""
    _assert_ratchet("api", _api_routes() - _frontend_calls(), KNOWN_API_ORPHANS)


def test_every_keyboard_callback_has_a_handler() -> None:
    """Ningún botón puede quedar sin pantalla que lo atienda."""
    patterns = _handler_patterns()
    samples = _keyboard_callbacks()
    assert len(samples) > 20, f"se esperaban los teclados del bot, hay {len(samples)}"
    unmatched = sorted(
        sample
        for sample in samples
        if not any(re.fullmatch(pattern, sample) for pattern in patterns)
    )
    assert not unmatched, f"callback_data sin handler: {unmatched}"


def test_every_callback_handler_has_a_producer() -> None:
    """Ningún handler de callback queda muerto (patrón que nadie emite)."""
    samples = _keyboard_callbacks()
    dead = sorted(
        pattern
        for pattern in _handler_patterns()
        if not any(re.fullmatch(pattern, sample) for sample in samples)
    )
    assert not dead, f"patrones de callback sin botón que los emita: {dead}"


def test_every_body_builder_is_reachable() -> None:
    """Los builders de mensajes on-chain deben usarse en algún sitio."""
    _assert_ratchet("builders", _builder_names() - _builder_consumers(), KNOWN_BUILDER_ORPHANS)


def test_every_command_listed_in_help_is_registered() -> None:
    """Lo que /help promete debe existir como comando de Telegram."""
    help_text = ROOT.joinpath("bot", "handlers", "help.py").read_text(encoding="utf-8")
    promised = set(re.findall(r"/([a-z]+)\s+—", help_text))
    assert promised, "el test no encontró comandos en /help: revisa el formato del texto"
    registered = set(re.findall(r"CommandHandler\(\s*\"([a-z]+)\"", MAIN_PY.read_text(encoding="utf-8")))
    registered |= set(
        re.findall(
            r"CommandHandler\(\s*\[\s*\"([a-z]+)\"",
            MAIN_PY.read_text(encoding="utf-8"),
        )
    )
    registered |= set(
        re.findall(
            r"CommandHandler\(\s*(?:\[\s*)?\"([a-z]+)\"",
            ROOT.joinpath("bot", "handlers", "predictions.py").read_text(encoding="utf-8"),
        )
    )
    assert promised <= registered, f"/help promete comandos inexistentes: {sorted(promised - registered)}"
