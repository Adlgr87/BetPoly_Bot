"""Avisos al usuario (F-04): una tabla de salida y un renderizador.

Diseño en tres piezas, cada una en su capa:

* **Persistencia** — ``bot.database`` escribe la fila dentro de la transacción
  que liquida la predicción o aplica el evento on-chain, con ``INSERT OR
  IGNORE`` y clave única ``(user_id, kind, ref_id)``. Si la liquidación se
  repite, el aviso no se duplica; si el proceso cae justo después, el aviso
  sigue ahí.
* **Texto** — este módulo, sin dependencias de Telegram: convierte la fila en el
  mensaje HTML. Se puede probar sin red.
* **Envío** — ``main.job_dispatch_notifications`` cada 30 s, en lotes, con
  reintentos y backoff (``mark_notification_failed``). Un fallo de Telegram
  nunca bloquea la cadena ni la liquidación.

Solo se avisa de lo que el usuario puede hacer algo: ganó puntos, se le
reembolsó la apuesta, o tiene un pago esperando en el contrato. Una apuesta
perdida no genera mensaje (para eso está ``/misbets``).
"""
from __future__ import annotations

import json
from typing import Any

from bot.database import (
    NOTIFICATION_MARKET_CLAIM,
    NOTIFICATION_MARKET_REFUND,
    NOTIFICATION_PREDICTION_RESULT,
)
from bot.formatting import esc
from bot.formatting import points as fmt_points

KIND_PREDICTION_RESULT = NOTIFICATION_PREDICTION_RESULT
KIND_MARKET_CLAIM = NOTIFICATION_MARKET_CLAIM
KIND_MARKET_REFUND = NOTIFICATION_MARKET_REFUND


def render(row: dict[str, Any]) -> str | None:
    """Texto HTML del aviso, o ``None`` si la fila no es representable."""
    try:
        payload = json.loads(row.get("payload") or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    kind = row.get("kind")
    if kind == KIND_PREDICTION_RESULT:
        return _prediction_result(payload)
    if kind == KIND_MARKET_CLAIM:
        return _market_claim(payload)
    if kind == KIND_MARKET_REFUND:
        return _market_refund(payload)
    return None


def _prediction_result(payload: dict[str, Any]) -> str:
    title = esc(payload.get("title") or "tu predicción")
    payout = int(payload.get("payout") or 0)
    stake = int(payload.get("stake") or 0)
    if payload.get("result") == "refund":
        return (
            "↩️ <b>Reembolso</b>\n\n"
            f"Nadie acertó en <b>{title}</b>, así que se devuelve lo apostado.\n"
            f"🪙 Recuperas <b>{fmt_points(payout or stake)}</b> puntos."
        )
    winning = esc(payload.get("winning_label") or "—")
    return (
        "🎉 <b>¡Ganaste!</b>\n\n"
        f"<b>{title}</b> se resolvió a favor de <b>{winning}</b>.\n"
        f"🪙 Apostaste {fmt_points(stake)} y cobras <b>{fmt_points(payout)}</b> puntos.\n\n"
        "Mira tu perfil con /misbets."
    )


def _market_claim(payload: dict[str, Any]) -> str:
    title = esc(payload.get("title") or "el mercado")
    market_id = payload.get("market_id")
    return (
        "🏁 <b>Mercado resuelto</b>\n\n"
        f"<b>{title}</b> (mercado #{market_id}) ya tiene ganadora y tu apuesta estaba ahí.\n\n"
        "Tu pago está en el contrato: ábrelo en la Mini App y fírmalo desde tu wallet. "
        "El bot nunca puede mover tus fondos."
    )


def _market_refund(payload: dict[str, Any]) -> str:
    title = esc(payload.get("title") or "el mercado")
    market_id = payload.get("market_id")
    return (
        "🛑 <b>Mercado cancelado</b>\n\n"
        f"<b>{title}</b> (mercado #{market_id}) se canceló y tu apuesta se reembolsa "
        "por completo.\n\n"
        "Reclama el reembolso desde la Mini App con tu wallet."
    )


__all__ = [
    "KIND_MARKET_CLAIM",
    "KIND_MARKET_REFUND",
    "KIND_PREDICTION_RESULT",
    "render",
]
