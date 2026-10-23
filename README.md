# BetPoly_Bot 🎯

Predicciones y mercados de apuestas sobre TON, servidos desde un bot de Telegram y una **Mini App** web. El backend **nunca custodia fondos y nunca firma transacciones**: solo *lee* la blockchain y *prepara* transacciones sin firmar que el usuario (o el admin) firma en su propia wallet.

---

## 🧠 Modelo de custodia (lo importante)

```
usuario ──firma──▶ contrato escrow (contracts/escrow.fc) ──claim──▶ usuario
                         ▲
        backend (sin claves): solo lee eventos y prepara payloads
```

| Regla | Cómo se garantiza |
| :--- | :--- |
| El bot no puede mover tu dinero | No tiene claves; sólo construye mensajes firmables |
| Cada apuesta va directo al contrato | `place_bet` se envía desde tu wallet al escrow |
| El premio lo paga el contrato | `claim_payout` lo firma el ganador; el contrato transfiere |
| La resolución no la decide el bot | `resolve_market` exige la firma del **admin on-chain** |
| Los saldos son los de la cadena | La base de datos es un *espejo* de sólo lectura del índice |
| Los fondos no pueden quedar atrapados | `cancel_market` habilita reembolso íntegro, para siempre |

Dos economías conviven (`BET_MODE`):

* **`points`** — puntos virtuales en SQLite (`/crear`, `/predicciones`, `/misbets`, `/ranking`). Sin blockchain, ideal para demo y comunidad.
* **`escrow`** — TON real en el contrato escrow, vía Mini App + TON Connect.
* **`hybrid`** (por defecto) — ambas; los puntos siguen siendo virtuales y los mercados on-chain se abren desde la web.

> ⚠️ El modo `escrow`/`hybrid` mueve TON real. Verifica la dirección del contrato, el admin y el `fee_bps` **antes** de difundir el bot.

---

## 🏗️ Arquitectura

| Capa | Tecnología |
| :--- | :--- |
| Bot | Python 3.11+, `python-telegram-bot` 21.6 (async, `JobQueue`) |
| Mini App / API | `aiohttp` (sirve `web/`, sin build step) |
| Persistencia | SQLite vía `aiosqlite` (una conexión, `BEGIN IMMEDIATE`) |
| Contrato | FunC (`contracts/escrow.fc`), pari-mutuel, probado en `@ton/sandbox` |
| Lectura on-chain | `toncenter` (`bot/chain/toncenter.py`) + índice incremental |
| Pruebas | `pytest` (offline) + sandbox TVM (`node tests/escrow.test.js`) |

Componentes que corren en un solo loop de asyncio (`main.py`): bot de Telegram (polling), API/Mini App (HTTP), e indexer (sólo lectura, tolerante a fallos de red).

### Estructura

```
BetPoly_Bot/
├── bot/
│   ├── api.py               # API HTTP + hosting de la Mini App
│   ├── chain/
│   │   ├── addresses.py     # direcciones EQ…/0:… , validación
│   │   ├── bodies.py        # opcodes, payloads y eventos (espejo de escrow.fc)
│   │   ├── toncenter.py     # cliente TON Center (transacciones, state init)
│   │   ├── ton_proof.py     # verificación Ed25519 de ton_proof
│   │   └── indexer.py       # proyección on-chain → SQLite (idempotente)
│   ├── markets.py           # reglas de negocio + transacciones SIN FIRMAR
│   ├── database.py          # puntos, predicciones, espejo on-chain
│   ├── security.py          # initData, sesiones HMAC, nonces, rate limiting
│   ├── formatting.py        # helpers HTML/formatos TON y fechas
│   ├── keyboards.py         # teclados inline
│   └── handlers/            # start, predictions, markets, stats, wallet, admin, help
├── web/                     # Mini App (index.html + static/app.js, styles.css, icon.png)
├── contracts/               # escrow.fc, stdlib, build.js, deploy.js, tests
├── tests/                   # suite pytest offline
├── config.py                # configuración tipada desde el entorno
└── main.py                  # entrada: bot + API + indexer
```

---

## 🚀 Puesta en marcha (puntos, sin blockchain)

```bash
git clone https://github.com/Adlgr87/BetPoly_Bot.git
cd BetPoly_Bot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# para desarrollo y tests:
pip install -r requirements-dev.txt

cp .env.example .env      # define BOT_TOKEN y ADMIN_IDS
python main.py --check    # valida configuración sin arrancar nada
python main.py            # bot + Mini App + indexer
```

Para desarrollo sin Telegram: `WEB_ENABLED=true ALLOW_DEV_SESSION=true python main.py` y abre `http://localhost:8080`.

> `ALLOW_DEV_SESSION=true` desactiva la verificación de `initData`: `config.validate()` lo rechaza en producción.

---

## ⛓️ Puesta en marcha on-chain

### 1. Compilar y probar el contrato

```bash
cd contracts
npm install
npm run build     # contracts/build/escrow.code.boc + escrow.fif
npm test          # 22 checks sobre @ton/sandbox (sin red)
```

Hash de referencia del código compilado actual:

```
a29c5c51fe93597967c6d37c976a27e989d13662117165dec1d899fe3292db9b   (80 bits en la raíz)
```

### 2. Desplegar

`deploy.js` usa la mnemónica del operador **una sola vez** (desde tu máquina) para publicar el contrato. El backend no la necesita nunca.

```bash
cd contracts
DRY_RUN=1 \
  ADMIN_ADDRESS="EQ…admin wallet…" \
  INDEXER_ADDRESS="EQ…dirección del bot…" \
  node scripts/deploy.js          # imprime la dirección, no envía nada

MNEMONIC="24 palabras del operador" \
  ADMIN_ADDRESS="EQ…admin wallet…" \
  INDEXER_ADDRESS="EQ…dirección del bot…" \
  NETWORK=testnet TON_API_KEY="…" \
  node scripts/deploy.js
```

Escribe `contracts/build/deploy.json` con dirección, `codeHash`, admin y parámetros. Despliega primero en `testnet`.

El *admin on-chain* debe ser una wallet que controles a mano: es la única que puede resolver, cancelar, cambiar la comisión o retirar fees. **No** debe ser una clave del servidor.

### 3. Configurar el backend

```dotenv
BET_MODE=hybrid
ESCROW_ADDRESS=EQ…        # dirección impresa por deploy.js
ESCROW_ADMIN_ADDRESS=EQ…  # admin on-chain: se usa para avisar si tu wallet no es la correcta
PUBLIC_BASE_URL=https://tu-dominio   # https obligatorio (Telegram + TON Connect)
TON_PROOF_DOMAIN=tu-dominio
SECRET_KEY=<32+ bytes aleatorios>
TON_NETWORK=mainnet
TON_API_KEY=…
```

`PUBLIC_BASE_URL` debe servirse por HTTPS y alojar `/tonconnect-manifest.json` y `/static/icon.png`: TON Connect los descarga y verifica el origen.

---

## 🔌 API HTTP

Todas las rutas devuelven `{"ok": true, "data": …}` o `{"ok": false, "error": {"code", "message"}}`.

| Método | Ruta | Descripción |
| :--- | :--- | :--- |
| GET | `/` | Mini App |
| GET | `/tonconnect-manifest.json` | Manifiesto TON Connect |
| GET | `/terms`, `/privacy` | Páginas legales |
| GET | `/api/health` | Estado del proceso (sin datos sensibles) |
| GET | `/api/config` | Configuración pública (sin secretos) |
| POST | `/api/session` | `init_data` de Telegram → sesión firmada |
| POST | `/api/wallet/nonce` | Nonce de un solo uso para `ton_proof` |
| POST | `/api/wallet/verify` | Verifica `ton_proof` y vincula la wallet |
| DELETE | `/api/wallet` | Desvincula la wallet |
| GET | `/api/me` | Perfil, puntos, wallet |
| GET | `/api/markets` | Mercados del espejo on-chain |
| GET | `/api/markets/{id}` | Detalle + tu apuesta + multiplicadores |
| POST | `/api/markets/prepare` | Transacción **sin firmar** para crear mercado |
| POST | `/api/markets/{id}/bets/prepare` | Transacción sin firmar para apostar |
| POST | `/api/markets/{id}/claim/prepare` | Transacción sin firmar para cobrar |
| POST | `/api/admin/markets/{id}/resolve/prepare` | Resolución (solo admin) |
| POST | `/api/admin/markets/{id}/cancel/prepare` | Cancelación (solo admin) |
| GET | `/api/admin/contract` | Estado on-chain del escrow (solo admin, caché 30 s) |
| POST | `/api/admin/contract/withdraw/prepare` | Retirar comisiones acumuladas (solo admin) |
| POST | `/api/admin/contract/set-fee/prepare` | Cambiar la comisión, `fee_bps`/`percent` (solo admin) |
| POST | `/api/admin/contract/set-paused/prepare` | Pausar o reanudar (solo admin) |
| POST | `/api/admin/contract/change-admin/prepare` | Transferir la administración (solo admin) |
| GET | `/api/indexer` | Cursor, últimas transacciones y estado del índice |

Las sesiones viajan en la cabecera `X-Session`. El backend **nunca** devuelve una transacción firmada.

---

## 🔐 Seguridad

* **Autenticación**: `hash = HMAC_SHA256(bot_token, "WebAppData")` sobre el `init_data`; se valida `auth_date` y se exige `ALLOW_DEV_SESSION=false` en producción.
* **Sesiones**: token `user_id.expira.hmac(secret_key)`; sin `secret_key` persistente las sesiones mueren al reiniciar (solo válido en desarrollo).
* **ton_proof**: digest `sha256("ton-proof-item-v2/" ‖ wc ‖ address_hash ‖ domain_len ‖ domain ‖ timestamp ‖ payload)`, firmado en Ed25519 con la clave del `StateInit` (que debe hashear a la dirección declarada). Dominio exacto, ventana de 600 s, nonce de un solo uso por `(user_id, purpose)`.
* **Rate limiting** por IP/usuario en sesión, preparación de transacciones y verificación de wallets.
* **CSP** con `frame-ancestors` configurable (`FRAME_ANCESTORS`) para permitir Telegram y bloquear el resto.
* **Índice idempotente**: cada transacción se procesa una vez (`chain_txs`), el cursor solo avanza tras confirmar, y un fallo de red nunca bloquea al bot.

La base de datos **no** es una fuente de verdad monetaria: ante cualquier discrepancia, manda la cadena.

---

## 📜 Contrato escrow

Pari-mutuel: cada mercado reparte todo el pozo menos la comisión entre quienes acertaron.

```
distributable = total_pool − floor(total_pool · fee_bps / 10000)
payout        = floor(stake · distributable / winning_pool)
```

Si nadie apostó a la opción ganadora, `resolve_market` falla con `412` y el admin debe `cancel_market`: entonces todos cobran su stake completo.

| Opcode | Operación | Quién |
| :--- | :--- | :--- |
| `0x1A0A0001` | `create_market(title_hash, closes_at, option_count)` | cualquiera |
| `0x1A0A0002` | `place_bet(market_id, option, stake)` | cualquiera (1 apuesta por wallet/mercado) |
| `0x1A0A0003` | `resolve_market(market_id, winning_option)` | admin |
| `0x1A0A0004` | `cancel_market(market_id)` | admin |
| `0x1A0A0005` | `claim_payout(market_id)` | apostador |
| `0x1A0A0101` | `set_paused(0/1)` | admin |
| `0x1A0A0102` | `set_fee(fee_bps ≤ 1000)` | admin |
| `0x1A0A0103` | `set_admin(address)` | admin |
| `0x1A0A0104` | `withdraw_fees(amount)` | admin |

| Exit code | Significado |
| :--- | :--- |
| `401` | No eres el admin |
| `402` | Cuerpo inválido |
| `403` | Contrato en pausa |
| `404` | Parámetros inválidos |
| `405` | Fecha de cierre fuera de rango |
| `406` | Valor enviado insuficiente |
| `407` | Mercado inexistente |
| `408` | Mercado cerrado |
| `409` | Opción inválida |
| `410` | Ya apostaste en este mercado |
| `411` | Ya resuelto / cancelado |
| `412` | Nadie acertó: hay que cancelar |
| `413` | Mercado aún abierto |
| `414` | No tienes nada que cobrar |
| `415` | Ya cobraste |
| `416` | Comisión por encima del 10 % |
| `417` | `withdraw_fees` tocaría fondos bloqueados |

Límites: `min_stake` configurable (0.1 TON por defecto), `max_options = 8`, cierre entre 5 min y 365 días, reserva de 0.01 TON para almacenamiento.

Los eventos (`0xE0A00001…03`) se envían al `indexer` dentro de una cell con un único ref; `bot/chain/bodies.py` la desenvuelve.

---

## 🧪 Pruebas

```bash
.venv/bin/python -m pytest          # suite offline: contrato espejo, puntos, API, seguridad
.venv/bin/python -m ruff check .    # estilo y errores comunes
cd contracts && node tests/escrow.test.js   # contrato real en el sandbox TVM
```

La suite de Python no necesita red, Telegram ni TON API: usa SQLite en `tmp_path`, `FakeClient` para el indexer y claves Ed25519 sintéticas para `ton_proof`.

**CI** (`.github/workflows/ci.yml`) ejecuta en cada push: lint, tests con umbral de
cobertura, compilación del contrato con verificación del hash reproducible,
sandbox TVM, `npm audit` y `pip-audit`.

Dos suites son *guardarraíles* y no pruebas funcionales:

- `tests/test_architecture.py` — dirección de las dependencias entre capas.
- `tests/test_wiring.py` — conectividad: rutas sin consumidor, botones sin
  handler, builders on-chain inalcanzables y comandos prometidos en `/help`.
  Los huérfanos conocidos viven en un baseline que solo puede encogerse.

### Administración del contrato (Fase 2)

El contrato expone siete *getters* (`get_fee_bps`, `get_paused`, `get_fees`,
`get_total_locked`, `get_market_count`, `get_min_stake`, `get_market_data`).
`bot/chain/escrow_state.py` los lee con `runGetMethod` y los sirve como una sola
foto (`EscrowState`) cacheada **30 s**, con *stale-while-error*: si la cadena no
responde se sigue mostrando la última lectura buena marcada como `stale`.

Las cuatro operaciones de administración (`withdraw_fees`, `set_fee`,
`set_paused`, `change_admin`) se preparan **sin firmar** en `bot/markets.py`,
con validaciones previas al envío:

- `withdraw_fees`: `0 < importe ≤ fees` (el contrato solo deja salir comisiones;
  los fondos de usuarios están bloqueados on-chain).
- `set_fee`: `0 ≤ fee_bps ≤ 1000` (0–10 %).
- `change_admin`: dirección válida, distinta de la actual y no cero.
- Si `ESCROW_ADMIN_ADDRESS` está definida, se avisa y se rechaza la preparación
  cuando la wallet vinculada del administrador no coincide con ella: es mejor
  fallar antes de firmar que gastar gas en una transacción condenada a `401`.

Cada acción se confirma dos veces: en el bot (botón “Confirmar” con el valor en
el estado de la conversación) o en la Mini App (`confirm()` nativo) y después en
la wallet del administrador. `ESCROW_ADMIN_ADDRESS` no concede permisos: el
contrato decide, la configuración solo sirve para avisar.

### Runbook de operación (testnet primero)

1. Despliega con `scripts/deploy.js` y anota `codeHash` y la dirección del
   escrow; pon esa dirección en `ESCROW_ADDRESS` y la wallet que controlarás en
   `ESCROW_ADMIN_ADDRESS`.
2. Abre `/admin` → **⚙️ Contrato escrow** y comprueba el estado: si dice
   «tu wallet no es la administradora», no sigas: la firma sería rechazada.
3. Prueba con importes pequeños: `set_fee` → `set_paused`/reanudar → un mercado
   de prueba → `withdraw_fees` por el importe exacto de las comisiones.
4. Verifica cada transacción en el explorador (`to` = escrow, `op` = `0x1A0A…`)
   antes de repetir en mainnet.

`withdraw_fees` mueve TON real: empieza siempre en testnet.

### Resiliencia del indexador

Si una transacción falla al aplicarse, el cursor **no avanza** sobre ella: se
reintenta en el acto y, si el fallo persiste, se aparca en `chain_tx_failures`
con retroceso exponencial (60 s → 5 min → 15 min → 1 h) para que una transacción
envenenada no detenga el resto del flujo. El estado se ve en `/api/indexer`
(admin) y en el panel `/admin`.

---

## 🤖 Comandos del bot

| Comando | Descripción |
| :--- | :--- |
| `/start` | Menú principal y onboarding |
| `/wallet` | Vincular wallet TON (ton_proof) |
| `/mercados` | Mercados on-chain en sólo lectura |
| `/predicciones` | Predicciones de puntos abiertas |
| `/crear` | Crear una predicción de puntos (3 pasos) |
| `/misbets` | Perfil, apuestas y rendimiento |
| `/ranking` | Top 10 por puntos |
| `/admin` | Panel de administrador (resolver, cancelar, estado y operaciones del contrato) |
| `/help` | Ayuda |
| `/cancelar` | Cancelar la operación en curso |

---

## 📊 Product Backlog (estado)

| ID | Feature | Estado |
| :--- | :--- | :--- |
| F001 | Onboarding guiado (`/start` + teclado inline) | ✅ |
| F002 | Crear predicción por pasos (`ConversationHandler`) | ✅ |
| F003 | Listar predicciones abiertas con paginación | ✅ |
| F004 | Apostar puntos con confirmación | ✅ |
| F005 | Perfil e historial (`/misbets`) | ✅ |
| F006 | Ranking global (`/ranking`) | ✅ |
| F007 | Recordatorios de cierre y notificación de resolución | ✅ |
| F008 | Panel de administrador (puntos y on-chain) | ✅ |
| F009 | Ayuda contextual (`/help`, botón “Cómo funciona”) | ✅ |
| F010 | Modo grupo (predicciones por `chat_id`) | ✅ |
| F011 | Mini App con TON Connect y `ton_proof` | ✅ |
| F012 | Contrato escrow pari-mutuel + índice on-chain | ✅ |
| F013 | Resolución oráculo automática (resultados deportivos) | ⬜ pendiente |
| F014 | Retiros fiat / on-ramp | ⬜ pendiente |

---

## ⚠️ Aviso legal

BetPoly es software: no es un operador de juego, no custodia fondos y no garantiza resultados de mercado. Los mercados con TON real pueden estar regulados en tu jurisdicción; el operador que despliegue el contrato es responsable de cumplir la normativa aplicable (licencias, KYC/AML, edad mínima y geobloqueo). Usa `testnet` hasta que tengas asesoría legal.
