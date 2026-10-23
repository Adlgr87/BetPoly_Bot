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

import gzip
import hashlib
import ipaddress
import logging
import math
import re
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

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
STATIC_DIR = WEB_DIR / "static"
TON_CLIENT_KEY = web.AppKey("ton_client", object)
MAX_BODY = 64 * 1024

#: R-09 — un año de caché solo para URLs que llevan la huella del contenido.
IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
#: Sin `?v=` el fichero se revalida con ETag: barato y nunca sirve código viejo.
REVALIDATE_CACHE = "no-cache"

#: Tipo MIME por extensión; lo que no esté aquí no se sirve.
STATIC_TYPES = {
    ".js": "text/javascript",
    ".css": "text/css",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".webmanifest": "application/manifest+json",
}
#: Solo texto: recomprimir un PNG cuesta RAM y no ahorra un byte.
COMPRESSIBLE = (".js", ".css", ".svg", ".webmanifest")


class _Asset:
    """Fichero estático en memoria, con su gzip ya hecho y su ETag."""

    __slots__ = ("body", "gzipped", "content_type", "etag")

    def __init__(self, body: bytes, content_type: str, compress: bool) -> None:
        self.body = body
        self.content_type = content_type
        self.etag = '"' + hashlib.sha256(body).hexdigest()[:16] + '"'
        # El gzip se paga una vez, en la primera petición, no en cada sondeo.
        self.gzipped = gzip.compress(body, 6) if compress else None


_assets: Dict[str, _Asset] = {}


def _asset_version() -> str:
    """Huella del conjunto JS/CSS: cambia al cambiar cualquier activo.

    Se usa como ``?v=`` en ``index.html`` para que el navegador pida la versión
    nueva sin perder la caché de un año del resto.
    """
    digest = hashlib.sha256()
    for name in ("app.js", "styles.css"):
        asset = _load_asset(name)
        digest.update(asset.etag.encode() if asset else b"-")
    return digest.hexdigest()[:12]


def _load_asset(name: str) -> Optional[_Asset]:
    """Carga (una vez) un activo de ``web/static`` y lo cachea en memoria.

    Rechaza cualquier ruta que no sea un nombre de fichero simple dentro del
    directorio estático: la ruta viene del cliente.
    """
    if name in _assets:
        return _assets[name]
    if not name or name.startswith(".") or "/" in name or "\\" in name:
        return None
    path = (STATIC_DIR / name).resolve()
    try:
        path.relative_to(STATIC_DIR.resolve())
    except ValueError:
        return None
    suffix = path.suffix.lower()
    content_type = STATIC_TYPES.get(suffix)
    if content_type is None or not path.is_file():
        return None
    charset = "utf-8" if content_type.startswith("text/") or "json" in content_type else None
    declared = content_type if charset is None else f"{content_type}; charset=utf-8"
    asset = _Asset(path.read_bytes(), declared, compress=suffix in COMPRESSIBLE)
    _assets[name] = asset
    return asset


def reset_asset_cache() -> None:
    """Olvida los activos en memoria (tests y recarga en caliente)."""
    _assets.clear()

_prepare_limiter = security.RateLimiter(limit=20, window_seconds=60)
_session_limiter = security.RateLimiter(limit=30, window_seconds=60)
_verify_limiter = security.RateLimiter(limit=10, window_seconds=300)

#: Global backstop per endpoint (limit, window seconds). These are *not* keyed by
#: client: even a client that rotates IPs or sessions hits this ceiling, which is
#: what makes S-04 impossible to exploit by forging headers (see Fase 3).
BACKSTOP_LIMITS: Dict[str, Tuple[int, float]] = {
    "/api/session": (300, 60),
    "/api/wallet/nonce": (300, 60),
    "/api/wallet/verify": (120, 60),
    "/api/markets/prepare": (120, 60),
    "/api/wallet": (120, 60),
}

def _new_backstops(
    limits: Optional[Dict[str, Tuple[int, float]]] = None,
) -> Dict[str, security.RateLimiter]:
    """Fresh limiters per app: a test app must not inherit another's counters."""
    return {
        path: security.RateLimiter(limit=limit, window_seconds=window)
        for path, (limit, window) in (limits or BACKSTOP_LIMITS).items()
    }

#: Market error codes that are not a plain 400.
_MARKET_ERROR_STATUS = {
    "forbidden": 403,
    "wallet_not_admin": 403,
    "market_not_found": 404,
    "chain_unavailable": 503,
}


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


@lru_cache(maxsize=32)
def _networks(cidrs: Tuple[str, ...]) -> Tuple[Any, ...]:
    """Parsed networks, cached: ``_client_ip`` runs on every request."""
    return tuple(ipaddress.ip_network(cidr, strict=False) for cidr in cidrs)


def _is_trusted_proxy(ip: str, cidrs: Tuple[str, ...]) -> bool:
    if not cidrs:
        return False
    try:
        address = ipaddress.ip_address(str(ip).strip().strip("[]"))
    except ValueError:
        return False
    return any(address in network for network in _networks(cidrs))


def _client_ip(request: web.Request) -> str:
    """Peer address used for rate limiting, honouring proxies only when declared.

    ``X-Forwarded-For`` is client controlled, so it is ignored unless
    ``TRUST_PROXY`` is on *and* the direct peer is one of ``TRUSTED_PROXIES``.
    The header list is then walked right to left, returning the first hop that
    is not itself a trusted proxy (the real client as seen by our edge).
    """
    remote = request.remote or "?"
    settings = _settings()
    if not settings.trust_proxy or not _is_trusted_proxy(remote, settings.trusted_proxies):
        return remote
    raw = ",".join(request.headers.getall("X-Forwarded-For", []))
    hops = [hop.strip() for hop in raw.split(",") if hop.strip()]
    for hop in reversed(hops):
        if not _is_trusted_proxy(hop, settings.trusted_proxies):
            return hop
    return hops[0] if hops else remote


def _rate_limited(message: str, retry_after: float = 0.0) -> web.Response:
    """429 + ``Retry-After`` (seconds) so clients can back off correctly."""
    response = json_error("rate_limited", message, 429)
    response.headers["Retry-After"] = str(max(1, math.ceil(retry_after)))
    return response


def current_user(request: web.Request) -> int:
    token = request.headers.get("X-Session") or request.query.get("session") or ""
    try:
        return security.verify_session(token)
    except security.AuthError as exc:
        raise web.HTTPUnauthorized(text=str(exc)) from exc


def _admin(user_id: int) -> bool:
    return user_id in _settings().admin_ids


def _user_payload(user_id: int, user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Public shape of a user, shared by ``/api/session`` and ``/api/me``."""
    return {
        "user_id": user_id,
        "username": (user or {}).get("username"),
        "full_name": (user or {}).get("full_name"),
        "points": (user or {}).get("points", 0),
        "is_admin": _admin(user_id),
    }


def _market_id(request: web.Request) -> Optional[int]:
    """``market_id`` from the URL, or ``None`` when it is not an integer."""
    try:
        return int(request.match_info["market_id"])
    except (KeyError, ValueError):
        return None


def _require_admin(request: web.Request) -> int:
    """Return the caller's id, raising a domain ``forbidden`` when not admin."""
    user_id = current_user(request)
    if not _admin(user_id):
        raise markets.MarketError("forbidden", "Solo administradores.")
    return user_id


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
        "bet_gas_ton": ton(settings.bet_gas_nano),
        "create_gas_ton": ton(settings.create_gas_nano),
        "claim_gas_ton": ton(settings.claim_gas_nano),
        "admin_gas_ton": ton(settings.admin_gas_nano),
        "max_stake_nano": str(settings.max_stake_nano),
        "max_stake_ton": ton(settings.max_stake_nano),
        "min_market_duration_min": settings.min_market_duration_min,
        "max_market_duration_days": settings.max_market_duration_days,
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


_STATIC_REF = re.compile(r'(/static/[\w.\-]+)(?!\?v=)')


async def index(request: web.Request) -> web.StreamResponse:
    """Sirve la Mini App con los activos ya versionados (R-09).

    ``index.html`` nunca se cachea; cada ``/static/...`` de dentro sale con
    ``?v=<huella>``, así que el navegador puede guardarlos para siempre.
    """
    file = WEB_DIR / "index.html"
    if not file.exists():  # pragma: no cover - packaging guard
        return web.Response(text="Mini App no encontrada", status=503)
    html = file.read_text(encoding="utf-8")
    version = _asset_version()
    html = _STATIC_REF.sub(lambda match: f"{match.group(1)}?v={version}", html)
    return web.Response(
        body=html.encode("utf-8"),
        content_type="text/html",
        charset="utf-8",
        headers={"Cache-Control": "no-store"},
    )


async def static_asset(request: web.Request) -> web.StreamResponse:
    """Activo estático con gzip en memoria y caché inmutable si está versionado."""
    asset = _load_asset(request.match_info.get("name", ""))
    if asset is None:
        raise web.HTTPNotFound()
    headers = {"ETag": asset.etag, "Cache-Control": REVALIDATE_CACHE}
    if request.headers.get("If-None-Match") == asset.etag:
        return web.Response(status=304, headers=headers)
    if request.query.get("v") == _asset_version():
        headers["Cache-Control"] = IMMUTABLE_CACHE
    headers["Content-Type"] = asset.content_type
    if asset.gzipped is not None and "gzip" in request.headers.get("Accept-Encoding", ""):
        headers["Content-Encoding"] = "gzip"
        headers["Vary"] = "Accept-Encoding"
        return web.Response(body=asset.gzipped, headers=headers)
    return web.Response(body=asset.body, headers=headers)


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------

async def create_session(request: web.Request) -> web.Response:
    allowed, retry = _session_limiter.allow(_client_ip(request))
    if not allowed:
        return _rate_limited("Demasiados intentos. Espera un momento.", retry)

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
            "user": _user_payload(user_id, user),
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
    allowed, retry = _verify_limiter.allow(str(user_id))
    if not allowed:
        return _rate_limited("Demasiados intentos de verificación.", retry)

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
            "user": _user_payload(user_id, user),
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

def json_cached(request: web.Request, data: Any) -> web.Response:
    """JSON con ``ETag`` y ``304`` cuando el cliente ya tiene esa versión.

    R-08: el sondeo de la Mini App se abarata a unas pocas decenas de bytes
    cuando nada cambió; el cuerpo solo se serializa una vez y se compara su
    huella con ``If-None-Match``.
    """
    body = _dumps({"ok": True, "data": data})
    etag = '"' + hashlib.sha256(body.encode()).hexdigest()[:16] + '"'
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if request.headers.get("If-None-Match") == etag:
        return web.Response(status=304, headers=headers)
    headers["Content-Type"] = "application/json; charset=utf-8"
    return web.Response(body=body.encode(), headers=headers)


async def list_markets(request: web.Request) -> web.Response:
    status = request.query.get("status", "open")
    try:
        limit = int(request.query.get("limit", "25"))
        offset = int(request.query.get("offset", "0"))
    except ValueError:
        return json_error("bad_query", "limit/offset deben ser enteros.")
    status_value = None if status == "all" else status
    markets_list = await database.list_markets(status=status_value, limit=limit, offset=offset)
    return json_cached(
        request,
        {
            "markets": [markets.market_view(m) for m in markets_list],
            "escrow": markets.friendly_escrow() if _settings().escrow_enabled else None,
        },
    )


async def get_market(request: web.Request) -> web.Response:
    market_id = _market_id(request)
    if market_id is None:
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


async def quote_market(request: web.Request) -> web.Response:
    """Pago estimado de una apuesta nueva (la Mini App no calcula economía)."""
    market_id = _market_id(request)
    if market_id is None:
        return json_error("bad_market", "market_id inválido.")
    try:
        option = int(request.query.get("option", ""))
    except (TypeError, ValueError):
        return json_error("bad_option", "Selecciona una opción válida.")
    stake = parse_nano(
        request.query.get("stake_ton") or request.query.get("stake"), field="importe"
    )
    quote = await markets.quote_payout(market_id, option, stake)
    return json_ok(quote)


async def prepare_market(request: web.Request) -> web.Response:
    user_id = current_user(request)
    allowed, retry = _prepare_limiter.allow(f"create:{user_id}")
    if not allowed:
        return _rate_limited("Espera un momento antes de crear otro mercado.", retry)

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
    allowed, retry = _prepare_limiter.allow(f"bet:{user_id}")
    if not allowed:
        return _rate_limited("Espera un momento antes de apostar de nuevo.", retry)

    market_id = _market_id(request)
    if market_id is None:
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
    market_id = _market_id(request)
    if market_id is None:
        return json_error("bad_market", "market_id inválido.")
    try:
        tx = await markets.prepare_claim(user_id, market_id)
    except markets.MarketError as exc:
        return json_error(exc.code, str(exc), 404 if exc.code == "market_not_found" else 400)
    return json_ok(tx.as_dict())


async def prepare_resolve(request: web.Request) -> web.Response:
    user_id = _require_admin(request)
    market_id = _market_id(request)
    if market_id is None:
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
    user_id = _require_admin(request)
    market_id = _market_id(request)
    if market_id is None:
        return json_error("bad_market", "market_id inválido.")
    try:
        tx = await markets.prepare_cancel(user_id, market_id)
    except markets.MarketError as exc:
        return json_error(exc.code, str(exc), 400)
    return json_ok(tx.as_dict())


# ---------------------------------------------------------------------------
# escrow administration (payload builders only: the wallet signs)
# ---------------------------------------------------------------------------

async def admin_contract_state(request: web.Request) -> web.Response:
    user_id = _require_admin(request)
    force = request.query.get("refresh") in ("1", "true", "yes")
    # Los MarketError (chain_unavailable incluido) los traduce el middleware.
    state = await markets.escrow_state(force=force)
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
    user_id = _require_admin(request)
    data = await read_json(request)
    amount = parse_nano(data.get("amount"), field="importe")
    tx = await markets.prepare_withdraw_fees(user_id, amount)
    return json_ok(tx.as_dict())


async def admin_contract_set_fee(request: web.Request) -> web.Response:
    user_id = _require_admin(request)
    data = await read_json(request)
    if data.get("fee_bps") is not None:
        try:
            fee_bps = int(data["fee_bps"])
        except (TypeError, ValueError):
            return json_error("bad_fee", "fee_bps debe ser un entero.")
    else:
        fee_bps = parse_fee_bps(data.get("percent"))
    tx = await markets.prepare_set_fee(user_id, fee_bps)
    return json_ok(tx.as_dict())


async def admin_contract_set_paused(request: web.Request) -> web.Response:
    user_id = _require_admin(request)
    data = await read_json(request)
    if not isinstance(data.get("paused"), bool):
        return json_error("bad_flag", "paused debe ser true o false.")
    tx = await markets.prepare_set_paused(user_id, data["paused"])
    return json_ok(tx.as_dict())


async def admin_contract_change_admin(request: web.Request) -> web.Response:
    user_id = _require_admin(request)
    data = await read_json(request)
    tx = await markets.prepare_change_admin(user_id, str(data.get("address") or ""))
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
            return json_error(
                exc.code, str(exc), _MARKET_ERROR_STATUS.get(exc.code, 400)
            )
        except (BodyError, AddressError) as exc:
            return json_error("bad_request", str(exc), 400)
        except Exception:  # noqa: BLE001 - last resort, no stack traces leaked
            logger.exception("Unhandled API error on %s %s", request.method, request.path)
            return json_error("internal_error", "Error interno del servidor.", 500)

    return middleware


def _backstop_middleware(backstops: Dict[str, security.RateLimiter]):
    """Ceiling per endpoint, independent of the caller's identity.

    Forging ``X-Forwarded-For`` or minting sessions cannot bypass this: the key
    is the endpoint itself, so the "rotate keys until it works" attack (S-04)
    hits a wall even if no proxy is configured.
    """
    @web.middleware
    async def middleware(request: web.Request, handler):
        limiter = backstops.get(request.path)
        if limiter is not None:
            allowed, retry = limiter.allow(request.path)
            if not allowed:
                return _rate_limited(
                    "El servicio recibe demasiadas peticiones. Inténtalo en unos segundos.",
                    retry,
                )
        return await handler(request)

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


def create_app(
    ton_client: Any = None,
    *,
    backstop_limits: Optional[Dict[str, Tuple[int, float]]] = None,
) -> web.Application:
    settings = _settings()
    app = web.Application(
        client_max_size=MAX_BODY,
        middlewares=[
            _error_middleware(),
            _backstop_middleware(_new_backstops(backstop_limits)),
            _headers_middleware(settings.frame_ancestors),
        ],
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
    app.router.add_get("/api/markets/{market_id}/quote", quote_market)
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

    app.router.add_get("/static/{name}", static_asset, name="static")
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
