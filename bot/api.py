"""HTTP API + static hosting for the Telegram Mini App.

Authentication model
--------------------
* ``POST /api/session`` validates Telegram ``initData`` (HMAC with the bot
  token) and returns a short-lived session token plus a single-use nonce.
* ``POST /api/wallet/verify`` validates the TON Connect ``ton_proof``
  (Ed25519) and links the wallet to the Telegram account.
* Everything else requires the ``X-Session`` header.

The API never returns, stores or requests private keys or seed phrases.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

from aiohttp import web

import config
from bot import database, markets, security
from bot.chain.addresses import AddressError, normalize_address
from bot.chain.bodies import BodyError
from bot.chain.ton_proof import ProofError, verify_ton_proof
from bot.chain.toncenter import TonApiError
from bot.formatting import ton
from bot.parsing import parse_fee_bps, parse_nano

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
TON_CLIENT_KEY = web.AppKey("ton_client", object)
MAX_BODY = 64 * 1024

_prepare_limiter = security.RateLimiter(limit=20, window_seconds=60)
_session_limiter = security.RateLimiter(limit=30, window_seconds=60)
_verify_limiter = security.RateLimiter(limit=10, window_seconds=300)


def _settings() -> config.Settings:
    return config.get_settings()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def json_ok(data: Any = None, status: int = 200, **extra: Any) -> web.Response:
    payload: Dict[str, Any] = {"ok": True}
    if data is not None:
        payload["data"] = data
    payload.update(extra)
    return web.json_response(payload, status=status, dumps=_dumps)


def json_error(code: str, message: str, status: int = 400) -> web.Response:
    return web.json_response(
        {"ok": False, "error": {"code": code, "message": message}}, status=status, dumps=_dumps
    )


def _dumps(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


async def read_json(request: web.Request) -> Dict[str, Any]:
    if request.content_length and request.content_length > MAX_BODY:
        raise web.HTTPRequestEntityTooLarge(max_size=MAX_BODY, actual_size=request.content_length)
    try:
        data = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise web.HTTPBadRequest(text="JSON inválido") from exc
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text="Se esperaba un objeto JSON")
    return data


def _client_ip(request: web.Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    return (forwarded.split(",")[0].strip() if forwarded else request.remote) or "?"


def current_user(request: web.Request) -> int:
    token = request.headers.get("X-Session") or request.query.get("session") or ""
    try:
        return security.verify_session(token)
    except security.AuthError as exc:
        raise web.HTTPUnauthorized(text=str(exc)) from exc


def _admin(user_id: int) -> bool:
    return user_id in _settings().admin_ids


def _wallet_view(wallet: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not wallet:
        return None
    return {
        "address": wallet["address"],
        "network": wallet.get("network", "ton"),
        "verified_at": wallet.get("verified_at"),
    }


def public_config() -> Dict[str, Any]:
    settings = _settings()
    return {
        "mode": settings.mode,
        "points_enabled": settings.points_enabled,
        "escrow_enabled": settings.escrow_enabled,
        "escrow_address": markets.friendly_escrow() if settings.escrow_enabled else None,
        "min_stake_nano": str(settings.min_stake_nano),
        "min_stake_ton": ton(settings.min_stake_nano),
        "max_options": settings.max_options,
        "network": settings.ton_network,
        "manifest_url": settings.manifest_url,
        "app_title": settings.app_title,
        "ton_proof_domain": settings.proof_domain,
        "support_url": settings.support_url,
        "bet_gas_ton": ton(markets.BET_GAS_NANO),
        "create_gas_ton": ton(markets.CREATE_GAS_NANO),
        "claim_gas_ton": ton(markets.CLAIM_GAS_NANO),
    }


# ---------------------------------------------------------------------------
# public endpoints
# ---------------------------------------------------------------------------

async def health(request: web.Request) -> web.Response:
    """Liveness probe: answers 200 as long as the process can serve requests."""
    try:
        indexer_state = await database.get_indexer_status()
        db_state = "ok"
    except Exception as exc:  # noqa: BLE001 - the DB may not be initialised yet
        logger.debug("Health check could not read the database: %s", exc)
        indexer_state, db_state = None, "unavailable"
    return json_ok(
        {
            "status": "ok",
            "mode": _settings().mode,
            "db": db_state,
            "indexer": indexer_state,
            "time": int(time.time()),
        }
    )


async def manifest(request: web.Request) -> web.Response:
    settings = _settings()
    base = settings.public_base_url.rstrip("/")
    return web.json_response(
        {
            "url": base,
            "name": settings.app_title,
            "iconUrl": f"{base}/static/icon.png",
            "termsOfUseUrl": f"{base}/terms",
            "privacyPolicyUrl": f"{base}/privacy",
        },
        dumps=_dumps,
    )


async def app_config(request: web.Request) -> web.Response:
    return json_ok(public_config())


def _legal_page(title: str, body: str) -> web.Response:
    return web.Response(
        text=(
            "<!DOCTYPE html><html lang='es'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{title} · BetPoly</title>"
            "<style>body{font-family:system-ui,sans-serif;max-width:720px;margin:0 auto;"
            "padding:32px 20px;line-height:1.6;color:#e8ecf3;background:#0e1116}"
            "h1{font-size:22px}a{color:#2ea6ff}p{color:#c7cfdd}</style></head><body>"
            f"<h1>{title}</h1>{body}"
            "<p><a href='/'>&larr; Volver a la Mini App</a></p></body></html>"
        ),
        content_type="text/html",
    )


async def terms_page(request: web.Request) -> web.Response:
    return _legal_page(
        "Términos de uso",
        "<p>BetPoly es una interfaz de solo lectura sobre un contrato inteligente "
        "de predicciones. El operador del bot no custodia fondos, no ejecuta pagos "
        "y no puede congelar saldos.</p>"
        "<p>Las apuestas son pari-mutuel: los ganadores se reparten el pozo menos la "
        "comisión configurada en el contrato. Los puntos del modo sandbox son virtuales "
        "y no son canjeables por dinero.</p>"
        "<p>Debes ser mayor de edad y cumplir la ley de tu jurisdicción. Las "
        "predicciones sobre eventos pueden estar restringidas en tu país.</p>",
    )


async def privacy_page(request: web.Request) -> web.Response:
    return _legal_page(
        "Privacidad",
        "<p>Guardamos únicamente: tu id de Telegram, tu nombre de usuario público, "
        "la dirección pública de tu wallet y el historial de apuestas que ya es "
        "público en la blockchain.</p>"
        "<p>Nunca solicitamos ni almacenamos frases semilla, claves privadas ni "
        "credenciales de exchanges.</p>"
        "<p>Puedes desvincular tu wallet en cualquier momento desde la Mini App; las "
        "transacciones ya registradas en la blockchain son inmutables.</p>",
    )


async def index(request: web.Request) -> web.StreamResponse:
    file = WEB_DIR / "index.html"
    if not file.exists():  # pragma: no cover - packaging guard
        return web.Response(text="Mini App no encontrada", status=503)
    return web.FileResponse(file, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------

async def create_session(request: web.Request) -> web.Response:
    allowed, retry = _session_limiter.allow(_client_ip(request))
    if not allowed:
        return json_error("rate_limited", "Demasiados intentos. Espera un momento.", 429)

    data = await read_json(request)
    init_data = data.get("init_data") or ""
    dev = False

    if init_data:
        try:
            tg_user = security.validate_init_data(init_data)
        except security.AuthError as exc:
            return json_error(exc.code, str(exc), 401)
        user_id = tg_user.user_id
        await database.upsert_user(user_id, tg_user.username, tg_user.full_name)
    else:
        token = security.dev_session(None)
        if not token:
            return json_error(
                "missing_init_data",
                "Abre esta Mini App desde Telegram para iniciar sesión.",
                401,
            )
        user_id = 0
        dev = True
        await database.upsert_user(0, "dev", "Modo local")

    session = security.issue_session(user_id)
    nonce = await database.create_nonce(
        user_id, "ton_proof", ttl_seconds=_settings().ton_proof_max_age
    )
    wallet = await database.get_wallet(user_id)
    user = await database.get_user(user_id)
    return json_ok(
        {
            "session": session,
            "nonce": nonce,
            "dev_mode": dev,
            "user_id": user_id,
            "user": {
                "user_id": user_id,
                "username": (user or {}).get("username"),
                "full_name": (user or {}).get("full_name"),
                "points": (user or {}).get("points", 0),
                "is_admin": _admin(user_id),
            },
            "wallet": _wallet_view(wallet),
            "config": public_config(),
        }
    )


async def wallet_nonce(request: web.Request) -> web.Response:
    """Issue a fresh single-use nonce for TON Connect ``tonProof``."""
    user_id = current_user(request)
    nonce = await database.create_nonce(
        user_id, "ton_proof", ttl_seconds=_settings().ton_proof_max_age
    )
    return json_ok({"nonce": nonce, "ttl": _settings().ton_proof_max_age})


async def verify_wallet(request: web.Request) -> web.Response:
    user_id = current_user(request)
    allowed, _ = _verify_limiter.allow(str(user_id))
    if not allowed:
        return json_error("rate_limited", "Demasiados intentos de verificación.", 429)

    data = await read_json(request)
    address = data.get("address") or ""
    network = str(data.get("network") or "-239")
    proof = data.get("proof") or {}
    state_init = data.get("state_init") or data.get("wallet_state_init")
    if not address:
        return json_error("missing_address", "Falta la dirección de la wallet.")

    try:
        normalized = normalize_address(address)
    except AddressError as exc:
        return json_error("bad_address", str(exc), 400)

    owner = await database.get_wallet_owner(normalized)
    if owner and int(owner["user_id"]) != int(user_id):
        return json_error(
            "wallet_taken", "Esta wallet ya está vinculada a otra cuenta de Telegram.", 409
        )

    onchain_key = None
    if not state_init:
        indexer: Any = request.app.get(TON_CLIENT_KEY)
        if indexer is not None:
            try:
                onchain_key = await indexer.get_public_key(normalized)
            except TonApiError:
                onchain_key = None
        if not onchain_key:
            return json_error(
                "state_init_required",
                "Falta walletStateInit y no se pudo obtener la clave pública en cadena. "
                "Actualiza tu wallet e inténtalo de nuevo.",
                422,
            )

    try:
        verified = verify_ton_proof(
            address=normalized,
            network=network,
            proof=proof,
            expected_domain=_settings().proof_domain,
            expected_nonce=str(proof.get("payload") or ""),
            max_age_seconds=_settings().ton_proof_max_age,
            state_init=state_init,
            public_key=data.get("public_key"),
            onchain_public_key=onchain_key,
        )
    except ProofError as exc:
        logger.info("ton_proof rejected for user %s: %s", user_id, exc.code)
        return json_error(exc.code, str(exc), 401)

    # Verify the nonce that was issued for *this* user by /api/session.
    consumed = await database.consume_nonce(
        str(proof.get("payload") or ""), user_id, "ton_proof",
        ttl_seconds=_settings().ton_proof_max_age,
    )
    if not consumed:
        return json_error(
            "nonce_reused", "Este proof ya fue usado. Vuelve a abrir la Mini App.", 409
        )

    try:
        await database.link_wallet(
            user_id, verified.address, network="ton", public_key=verified.public_key
        )
    except database.WalletConflict as exc:
        return json_error("wallet_taken", str(exc), 409)

    wallet = await database.get_wallet(user_id)
    return json_ok({"wallet": _wallet_view(wallet), "public_key": verified.public_key})


async def unlink_wallet(request: web.Request) -> web.Response:
    user_id = current_user(request)
    await database.unlink_wallet(user_id)
    return json_ok({"wallet": None})


async def me(request: web.Request) -> web.Response:
    user_id = current_user(request)
    user = await database.get_user(user_id)
    wallet = await database.get_wallet(user_id)
    bets = []
    if wallet:
        bets = await database.list_onchain_bets(wallet["address"], limit=25)
    return json_ok(
        {
            "user": {
                "user_id": user_id,
                "username": (user or {}).get("username"),
                "full_name": (user or {}).get("full_name"),
                "points": (user or {}).get("points", 0),
                "is_admin": _admin(user_id),
            },
            "wallet": _wallet_view(wallet),
            "bets": [
                {
                    "market_id": int(b["market_id"]),
                    "title": b["title"],
                    "option": int(b["option"]),
                    "stake": str(int(b["stake"])),
                    "stake_ton": ton(int(b["stake"])),
                    "claimed": bool(b["claimed"]),
                    "status": b["status"],
                    "winning_option": b["winning_option"],
                    "payout": str(b["payout"]) if b["payout"] is not None else None,
                    "payout_ton": ton(int(b["payout"])) if b["payout"] is not None else None,
                    "tx_hash": b["tx_hash"],
                }
                for b in bets
            ],
        }
    )


# ---------------------------------------------------------------------------
# markets
# ---------------------------------------------------------------------------

async def list_markets(request: web.Request) -> web.Response:
    status = request.query.get("status", "open")
    try:
        limit = int(request.query.get("limit", "25"))
        offset = int(request.query.get("offset", "0"))
    except ValueError:
        return json_error("bad_query", "limit/offset deben ser enteros.")
    status_value = None if status == "all" else status
    markets_list = await database.list_markets(status=status_value, limit=limit, offset=offset)
    return json_ok(
        {
            "markets": [markets.market_view(m) for m in markets_list],
            "escrow": markets.friendly_escrow() if _settings().escrow_enabled else None,
        }
    )


async def get_market(request: web.Request) -> web.Response:
    try:
        market_id = int(request.match_info["market_id"])
    except (KeyError, ValueError):
        return json_error("bad_market", "market_id inválido.")
    user_id = None
    try:
        user_id = current_user(request)
    except web.HTTPException:
        user_id = None
    try:
        detail = await markets.market_detail(market_id, user_id=user_id)
    except markets.MarketError as exc:
        return json_error(exc.code, str(exc), 404)
    return json_ok(detail)


async def prepare_market(request: web.Request) -> web.Response:
    user_id = current_user(request)
    allowed, retry = _prepare_limiter.allow(f"create:{user_id}")
    if not allowed:
        return json_error("rate_limited", "Espera un momento antes de crear otro mercado.", 429)

    data = await read_json(request)
    options = data.get("options") or []
    if isinstance(options, str):
        options = [o.strip() for o in options.split(",")]
    try:
        closes_at = int(data.get("closes_at") or 0)
    except (TypeError, ValueError):
        return json_error("bad_close_date", "closes_at debe ser un timestamp unix.")

    try:
        tx = await markets.prepare_create_market(
            user_id, str(data.get("title") or ""), list(options), closes_at
        )
    except markets.MarketError as exc:
        return json_error(exc.code, str(exc), 400)
    return json_ok(tx.as_dict())


async def prepare_bet(request: web.Request) -> web.Response:
    user_id = current_user(request)
    allowed, _ = _prepare_limiter.allow(f"bet:{user_id}")
    if not allowed:
        return json_error("rate_limited", "Espera un momento antes de apostar de nuevo.", 429)

    try:
        market_id = int(request.match_info["market_id"])
    except (KeyError, ValueError):
        return json_error("bad_market", "market_id inválido.")
    data = await read_json(request)
    try:
        option = int(data.get("option"))
    except (TypeError, ValueError):
        return json_error("bad_option", "Selecciona una opción válida.")
    stake_raw = data.get("stake_nano") or data.get("stake_ton") or data.get("stake")
    try:
        stake = parse_nano(stake_raw)
    except markets.MarketError as exc:
        return json_error(exc.code, str(exc))

    try:
        tx = await markets.prepare_bet(user_id, market_id, option, stake)
    except markets.MarketError as exc:
        status = 404 if exc.code == "market_not_found" else 400
        return json_error(exc.code, str(exc), status)
    return json_ok(tx.as_dict())


async def prepare_claim(request: web.Request) -> web.Response:
    user_id = current_user(request)
    try:
        market_id = int(request.match_info["market_id"])
    except (KeyError, ValueError):
        return json_error("bad_market", "market_id inválido.")
    try:
        tx = await markets.prepare_claim(user_id, market_id)
    except markets.MarketError as exc:
        return json_error(exc.code, str(exc), 404 if exc.code == "market_not_found" else 400)
    return json_ok(tx.as_dict())


async def prepare_resolve(request: web.Request) -> web.Response:
    user_id = current_user(request)
    if not _admin(user_id):
        return json_error("forbidden", "Solo administradores.", 403)
    try:
        market_id = int(request.match_info["market_id"])
    except (KeyError, ValueError):
        return json_error("bad_market", "market_id inválido.")
    data = await read_json(request)
    try:
        winning = int(data.get("winning_option"))
    except (TypeError, ValueError):
        return json_error("bad_option", "Selecciona la opción ganadora.")
    try:
        tx = await markets.prepare_resolve(user_id, market_id, winning)
    except markets.MarketError as exc:
        return json_error(exc.code, str(exc), 400)
    return json_ok(tx.as_dict())


async def prepare_cancel(request: web.Request) -> web.Response:
    user_id = current_user(request)
    if not _admin(user_id):
        return json_error("forbidden", "Solo administradores.", 403)
    try:
        market_id = int(request.match_info["market_id"])
    except (KeyError, ValueError):
        return json_error("bad_market", "market_id inválido.")
    try:
        tx = await markets.prepare_cancel(user_id, market_id)
    except markets.MarketError as exc:
        return json_error(exc.code, str(exc), 400)
    return json_ok(tx.as_dict())


# ---------------------------------------------------------------------------
# escrow administration (payload builders only: the wallet signs)
# ---------------------------------------------------------------------------

#: Errores del dominio que significan "no eres el administrador".
_ADMIN_FORBIDDEN = ("forbidden", "wallet_not_admin")
#: Errores de infraestructura (la cadena o el RPC no respondieron).
_UNAVAILABLE = ("chain_unavailable",)


def _escrow_admin_error(exc: Exception) -> web.Response:
    """Map domain errors to HTTP without leaking chain details."""
    if isinstance(exc, markets.MarketError):
        if exc.code in _ADMIN_FORBIDDEN:
            status = 403
        elif exc.code in _UNAVAILABLE:
            status = 503
        else:
            status = 400
        return json_error(exc.code, str(exc), status)
    raise exc


def _require_web_admin(request: web.Request) -> int:
    user_id = current_user(request)
    if not _admin(user_id):
        raise markets.MarketError("forbidden", "Solo un administrador puede operar el contrato.")
    return user_id


async def admin_contract_state(request: web.Request) -> web.Response:
    try:
        user_id = _require_web_admin(request)
        force = request.query.get("refresh") in ("1", "true", "yes")
        state = await markets.escrow_state(force=force)
    except Exception as exc:  # noqa: BLE001 - mapped below
        return _escrow_admin_error(exc)
    payload = state.as_dict()
    payload["admin_matches_wallet"] = await _admin_matches_wallet(user_id, state)
    return json_ok(payload)


async def _admin_matches_wallet(user_id: int, state: Any) -> Optional[bool]:
    """Whether the linked wallet can actually sign these ops (informational)."""
    if not state.admin:
        return None
    wallet = await database.get_wallet(user_id)
    if not wallet:
        return None
    try:
        return normalize_address(wallet["address"]) == normalize_address(state.admin)
    except (AddressError, ValueError):
        return None


async def admin_contract_withdraw(request: web.Request) -> web.Response:
    try:
        user_id = _require_web_admin(request)
        data = await read_json(request)
        amount = parse_nano(data.get("amount"), field="importe")
        tx = await markets.prepare_withdraw_fees(user_id, amount)
    except Exception as exc:  # noqa: BLE001 - mapped below
        return _escrow_admin_error(exc)
    return json_ok(tx.as_dict())


async def admin_contract_set_fee(request: web.Request) -> web.Response:
    try:
        user_id = _require_web_admin(request)
        data = await read_json(request)
        if data.get("fee_bps") is not None:
            fee_bps = int(data["fee_bps"])
        else:
            fee_bps = parse_fee_bps(data.get("percent"))
        tx = await markets.prepare_set_fee(user_id, fee_bps)
    except (TypeError, ValueError):
        return json_error("bad_fee", "fee_bps debe ser un entero.", 400)
    except Exception as exc:  # noqa: BLE001 - mapped below
        return _escrow_admin_error(exc)
    return json_ok(tx.as_dict())


async def admin_contract_set_paused(request: web.Request) -> web.Response:
    try:
        user_id = _require_web_admin(request)
        data = await read_json(request)
        if not isinstance(data.get("paused"), bool):
            return json_error("bad_flag", "paused debe ser true o false.", 400)
        tx = await markets.prepare_set_paused(user_id, data["paused"])
    except Exception as exc:  # noqa: BLE001 - mapped below
        return _escrow_admin_error(exc)
    return json_ok(tx.as_dict())


async def admin_contract_change_admin(request: web.Request) -> web.Response:
    try:
        user_id = _require_web_admin(request)
        data = await read_json(request)
        tx = await markets.prepare_change_admin(user_id, str(data.get("address") or ""))
    except Exception as exc:  # noqa: BLE001 - mapped below
        return _escrow_admin_error(exc)
    return json_ok(tx.as_dict())


async def indexer_status(request: web.Request) -> web.Response:
    user_id = current_user(request)
    status = await database.get_indexer_status()
    cursor_lt, cursor_hash = await database.get_cursor("escrow")
    payload = {
        "indexer": status,
        "cursor_lt": cursor_lt,
        "cursor_hash": cursor_hash,
        "escrow": markets.friendly_escrow() if _settings().escrow_enabled else None,
        "is_admin": _admin(user_id),
    }
    if _admin(user_id):
        payload["failures"] = await database.list_chain_tx_failures(limit=10)
        async with database.connection() as db:
            async with db.execute(
                "SELECT COUNT(*) AS n FROM markets WHERE status = 'open'"
            ) as cur:
                row = await cur.fetchone()
            payload["open_markets"] = int(row["n"]) if row else 0
            async with db.execute("SELECT COUNT(*) AS n FROM onchain_bets") as cur:
                row = await cur.fetchone()
            payload["total_bets"] = int(row["n"]) if row else 0
    return json_ok(payload)


# ---------------------------------------------------------------------------
# middleware / app factory
# ---------------------------------------------------------------------------

def _error_middleware():
    @web.middleware
    async def middleware(request: web.Request, handler):
        try:
            return await handler(request)
        except web.HTTPException as exc:
            if request.path.startswith("/api/"):
                return json_error(
                    f"http_{exc.status}", exc.reason or "Solicitud rechazada", exc.status
                )
            raise
        except markets.MarketError as exc:
            return json_error(exc.code, str(exc), 400)
        except (BodyError, AddressError) as exc:
            return json_error("bad_request", str(exc), 400)
        except Exception:  # noqa: BLE001 - last resort, no stack traces leaked
            logger.exception("Unhandled API error on %s %s", request.method, request.path)
            return json_error("internal_error", "Error interno del servidor.", 500)

    return middleware


def _headers_middleware(allowed_ancestors: str):
    @web.middleware
    async def middleware(request: web.Request, handler):
        response = await handler(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Robots-Tag", "noindex")
        if request.path.startswith("/api/") or request.path.endswith(".html") or request.path == "/":
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; "
                f"frame-ancestors {allowed_ancestors}; "
                "script-src 'self' https://telegram.org https://unpkg.com; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: blob: https:; "
                "connect-src 'self' https: wss:; "
                "base-uri 'none'; form-action 'none'",
            )
        return response

    return middleware


def create_app(ton_client: Any = None) -> web.Application:
    settings = _settings()
    app = web.Application(
        client_max_size=MAX_BODY,
        middlewares=[_error_middleware(), _headers_middleware(settings.frame_ancestors)],
    )
    app[TON_CLIENT_KEY] = ton_client

    app.router.add_get("/", index)
    app.router.add_get("/tonconnect-manifest.json", manifest)
    app.router.add_get("/terms", terms_page)
    app.router.add_get("/privacy", privacy_page)
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/config", app_config)
    app.router.add_post("/api/session", create_session)
    app.router.add_post("/api/wallet/nonce", wallet_nonce)
    app.router.add_post("/api/wallet/verify", verify_wallet)
    app.router.add_delete("/api/wallet", unlink_wallet)
    app.router.add_get("/api/me", me)
    app.router.add_get("/api/markets", list_markets)
    app.router.add_get("/api/markets/{market_id}", get_market)
    app.router.add_post("/api/markets/prepare", prepare_market)
    app.router.add_post("/api/markets/{market_id}/bets/prepare", prepare_bet)
    app.router.add_post("/api/markets/{market_id}/claim/prepare", prepare_claim)
    app.router.add_post("/api/admin/markets/{market_id}/resolve/prepare", prepare_resolve)
    app.router.add_post("/api/admin/markets/{market_id}/cancel/prepare", prepare_cancel)
    app.router.add_get("/api/admin/contract", admin_contract_state)
    app.router.add_post("/api/admin/contract/withdraw/prepare", admin_contract_withdraw)
    app.router.add_post("/api/admin/contract/set-fee/prepare", admin_contract_set_fee)
    app.router.add_post("/api/admin/contract/set-paused/prepare", admin_contract_set_paused)
    app.router.add_post("/api/admin/contract/change-admin/prepare", admin_contract_change_admin)
    app.router.add_get("/api/indexer", indexer_status)

    if WEB_DIR.exists():
        app.router.add_static("/static", path=str(WEB_DIR / "static"), name="static")
    return app


async def start_server(
    *, host: Optional[str] = None, port: Optional[int] = None, ton_client: Any = None
) -> web.AppRunner:
    settings = _settings()
    app = create_app(ton_client=ton_client)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host or settings.web_host, port or settings.web_port)
    await site.start()
    logger.info(
        "Mini App/API disponible en http://%s:%s (público: %s)",
        host or settings.web_host,
        port or settings.web_port,
        settings.public_base_url,
    )
    return runner
