"""La única implementación de la economía pari-mutuel del proyecto.

Antes de este módulo la misma fórmula existía cinco veces (contrato, `markets`,
`database`, handlers y JavaScript) y no coincidían: el usuario veía
multiplicadores sin descontar la comisión mientras el contrato sí la descontaba
(F-03). Aquí vive **una** versión, en aritmética entera, y todo lo demás la llama:

* ``bot/database.py`` — liquidación de los puntos del modo sandbox;
* ``bot/markets.py`` — estimación de pago y multiplicadores de la API;
* ``bot/handlers/*`` — lo que el bot muestra antes de resolver o apostar;
* ``bot/api.py`` — el *quote* que alimenta la hoja de apuesta de la Mini App.

El contrato (`contracts/escrow.fc`) hace exactamente lo mismo con
``muldiv(total_pool, fee_bps, 10000)``; los vectores compartidos viven en
``tests/test_economics.py`` para que cualquier divergencia falle en CI.
"""
from __future__ import annotations

#: Los importes se guardan en la unidad mínima (nanoTON / puntos), así que las
#: comisiones se expresan en puntos base: 1 % = 100 bps.
BPS_DENOMINATOR = 10_000


def distributable(total: int, fee_bps: int) -> int:
    """Pool que se reparte entre los ganadores tras descontar la comisión.

    Redondea la comisión **hacia abajo** (``muldiv`` del contrato), de modo que
    el redondeo nunca deja el pozo sin fondos suficientes.
    """
    total = int(total)
    if total <= 0:
        return 0
    bps = max(0, min(int(fee_bps), BPS_DENOMINATOR))
    fee = (total * bps) // BPS_DENOMINATOR
    return total - fee


def fee_of(total: int, fee_bps: int) -> int:
    """Comisión efectiva (lo que ``distributable`` descuenta)."""
    total = int(total)
    if total <= 0:
        return 0
    bps = max(0, min(int(fee_bps), BPS_DENOMINATOR))
    return (total * bps) // BPS_DENOMINATOR


def payout(stake: int, distributable_pool: int, winning_pool: int) -> int:
    """Pago de una apuesta ganadora: ``stake · distributable / winning_pool``.

    Siempre redondea hacia abajo y nunca devuelve negativos. Si no hay pozo
    ganador no hay pago (el administrador debe cancelar y reembolsar).
    """
    stake = int(stake)
    distributable_pool = int(distributable_pool)
    winning_pool = int(winning_pool)
    if stake <= 0 or distributable_pool <= 0 or winning_pool <= 0:
        return 0
    return (stake * distributable_pool) // winning_pool


def multiplier(total: int, option_pool: int, fee_bps: int) -> float | None:
    """Multiplicador **neto** de una opción, o ``None`` si aún no hay pozo.

    Es lo que se muestra al usuario («x2.45» significa que cada TON apostado
    devuelve 2.45 TON ya descontada la comisión), por eso aplica el fee.
    """
    total = int(total)
    option_pool = int(option_pool)
    if option_pool <= 0 or total <= 0:
        return None
    return round(distributable(total, fee_bps) / option_pool, 4)


def preview_payout(stake: int, option_pool: int, total: int, fee_bps: int) -> int:
    """Pago que tendría una apuesta nueva si el mercado cerrase ahora mismo.

    Incluye la propia apuesta en el pozo (total y opción) antes de calcular el
    reparto: es exactamente la pregunta que responde la hoja de apuesta.
    """
    stake = int(stake)
    if stake <= 0:
        return 0
    new_total = int(total) + stake
    new_pool = int(option_pool) + stake
    return payout(stake, distributable(new_total, fee_bps), new_pool)


__all__ = [
    "BPS_DENOMINATOR",
    "distributable",
    "fee_of",
    "multiplier",
    "payout",
    "preview_payout",
]
