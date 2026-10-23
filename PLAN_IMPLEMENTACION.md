# Plan de implementación — BetPoly_Bot

**Versión:** 1.0 · 2026-10-23
**Base:** `AUDITORIA.md` sobre el commit `6b50853`
**Objetivo:** cerrar los hallazgos de la auditoría en **fases verificables**, sin romper el invariante no-custodial ni la arquitectura de capas.

---

## 0. Principios que ninguna fase puede violar

Estas reglas son el marco de evaluación de **todas** las decisiones técnicas del plan. Si una solución las incumple, se descarta aunque sea más rápida de escribir.

| # | Invariante | Consecuencia práctica |
| :--- | :--- | :--- |
| P1 | **No custodia** | El backend nunca genera, guarda ni usa claves privadas, ni firma. Toda operación con fondos se entrega **sin firmar** al usuario/admin. |
| P2 | **Fuente de verdad = la cadena** | SQLite es un *espejo* reconstruible. Ninguna decisión de dinero (¿quién ganó?, ¿cuánto pagó?) se toma con datos locales. |
| P3 | **Enteros, nunca flotantes** | Todos los montos viajan en `nanoTON` (int) o puntos (int). El formateo a decimal ocurre solo al renderizar. |
| P4 | **Host objetivo: 1 vCPU compartida, ~512 MB RAM** | Sin Redis, sin Postgres, sin workers externos, sin polling agresivo. SQLite + asyncio + jobs de `python-telegram-bot`. |
| P5 | **Idempotencia por diseño** | Todo evento on-chain se procesa *exactly-once* (`chain_txs` PK). Todo envío de Telegram debe poder reintentarse sin duplicar. |
| P6 | **Una sola fuente por concepto** | Una fórmula económica, un builder de payload por operación, un formateador de importes. Si aparece una segunda copia, es un bug. |
| P7 | **Capas con dirección única** | `web/` → `bot/api.py` → servicios (`markets`, `database`) → `bot/chain/*`. Los handlers de Telegram usan los mismos servicios. Nadie salta capas. |
| P8 | **Todo lo nuevo entra con test** | Ninguna fase se cierra sin pruebas que fallen si el defecto vuelve. |

### Arquitectura de referencia (estado objetivo)

```
┌──────────────┐        ┌───────────────────┐
│  Mini App    │        │  Telegram (bot)   │
│ web/ (JS)    │        │ bot/handlers/*.py │
└──────┬───────┘        └─────────┬─────────┘
       │ HTTP/JSON                │ callbacks + jobs
       ▼                          ▼
┌─────────────────────────────────────────────┐
│ bot/api.py   (aiohttp: sesión, CSP, límites)│
└───────────────────┬─────────────────────────┘
                    ▼
┌─────────────────────────────────────────────┐    ┌──────────────────┐
│ SERVICIOS  bot/markets.py · bot/economics.py│◄───│ bot/chain/*      │
│            bot/database.py                  │    │ ton_proof, bodies│
│                                             │    │ indexer, toncenter│
└───────────────────┬─────────────────────────┘    └────────┬─────────┘
                    ▼                                       │
            ┌───────────────┐                               ▼
            │ SQLite (WAL)  │◄──────────────────── TON (lectura)
            │  + outbox     │
            └───────────────┘
```

**Reglas de importación que se verificarán automáticamente (Fase 0):**
- `bot/api.py` no importa `telegram`.
- `bot/handlers/*` no importan `bot/chain/toncenter` ni `aiohttp`.
- `bot/chain/*` no importan `telegram` ni `aiohttp` (excepto el cliente HTTP, que sí usa `aiohttp`).
- `bot/keyboards.py` no lee `config` (es puro, recibe datos).
- `web/` no contiene constantes económicas: las recibe de `/api/config` o del detalle del mercado.

---

## 1. Decisiones de arquitectura (ADR)

Cada decisión incluye la alternativa descartada y **por qué es subóptima para este proyecto**. Son el núcleo de la frase «verificar que las partes integradas sean las óptimas».

### ADR-01 · Se mantiene el stack (aiohttp + SQLite + asyncio + PTB)
**Decisión:** no migrar a FastAPI/Postgres/Redis ni a un ORM.
**Por qué:** el despliegue objetivo es un VPS de 512 MB. Un Postgres añade ~150-200 MB de RSS y un servicio más que operar; Redis añade otro. El volumen esperado (miles de filas, un solo proceso) no lo justifica. SQLite en WAL ya está bien configurado (`busy_timeout`, `synchronous=NORMAL`).
**Se revisaría si:** se pasa a multi-instancia (más de un proceso escribiendo) o >1 M de apuestas. Ahí sí Postgres + outbox con `SKIP LOCKED`.

### ADR-02 · Cursor del indexer: «última posición confirmada» + *dead-letter*, no outbox completa
**Decisión:** el cursor solo avanza a la última tx **procesada con éxito**. Una tx que falla se reintenta en el mismo ciclo (hasta 3 veces, es idempotente); si sigue fallando, se aparca en una tabla `chain_tx_failures` y **solo entonces** el cursor la salta, para no detener la cabeza del stream. Un job de reconciliación reintenta la dead-letter cada 5 min y el fallo se muestra en `/api/indexer` y en el panel de admin.
**Alternativa descartada A — outbox por transacción:** cola durable por tx con `status/attempts/next_at`. Más correcta, pero duplica el estado que ya vive en `chain_txs` y añade ~200 líneas. Con el volumen actual, la dead-letter obtiene el 95 % del beneficio.
**Alternativa descartada B — reconciliación periódica completa:** reescanear todo el contrato cada N minutos. Coste de API lineal con la historia; inaceptable en el tier gratuito de toncenter.
**Por qué es óptimo aquí:** corrige la pérdida de eventos (S-01) con ~80 líneas, sin bloquear el indexer de forma permanente y sin infraestructura nueva.

### ADR-03 · `upsert_market` se separa en «crear marcador» y «enriquecer desde el evento»
**Decisión:** dos funciones explícitas en lugar de un `INSERT ... ON CONFLICT DO UPDATE` con `CASE` condicionales.
**Por qué:** el SQL condicional (`title = CASE WHEN ... LIKE 'Mercado #%' ...`) es ilegible, difícil de testear y propenso a sobrescribir datos buenos con basura. Dos funciones nombradas expresan la intención: `insert_market_placeholder(...)` (solo lo llama el camino de apuesta huérfana) y `enrich_market_from_event(...)` (solo lo llama el evento de creación, y solo rellena campos vacíos o de marcador).
**Alternativa descartada:** un flag `authoritative=True/False` en la misma función. Funciona, pero deja un parámetro booleano cuyo significado hay que recordar en cada llamada — exactamente el tipo de ambigüedad que produjo el bug.

### ADR-04 · Economía en un solo módulo puro (`bot/economics.py`)
**Decisión:** `distributable()`, `payout()`, `multiplier()` y `preview_payout()` como funciones puras, consumidas por `database`, `markets`, handlers y la API. El frontend **no calcula**: recibe `multiplier` y `potential_payout` ya netos.
**Por qué:** hoy hay cinco implementaciones que discrepan en la comisión (F-03). Un módulo puro es trivial de cubrir con vectores y de comparar contra el contrato.
**Alternativa descartada:** calcular en JS con `fee_bps` del config. Sigue siendo una segunda implementación, que es el problema de origen.
**Verificación:** un fixture JSON compartido por `pytest` y `escrow.test.js` con los mismos vectores; si el contrato y Python divergen, falla CI.

### ADR-05 · Administración del contrato como `PreparedTx` en los servicios, expuesta en bot y Mini App
**Decisión:** `prepare_withdraw_fees / prepare_set_fee / prepare_set_paused / prepare_change_admin` viven junto a las demás en `bot/markets.py` y devuelven `PreparedTx` (payload + deeplink). El bot las ofrece en el panel; la Mini App en la pestaña admin. **El servidor nunca firma.**
**Por qué:** reutiliza el patrón existente, respeta P1 y elimina S-03 sin inventar un segundo mecanismo de firma.
**Alternativa descartada:** script CLI de administración. Rompe la experiencia y deja el problema de fondo (nadie ve el estado del contrato).

### ADR-06 · Lectura del estado del contrato vía `runGetMethod` con caché TTL
**Decisión:** añadir `TonCenterClient.run_get_method(...)` y un `EscrowState` (admin, fee_bps, min_stake, paused, fees, balance) con caché de 30 s por proceso.
**Por qué:** los getters ya existen en el contrato (`get_fee_bps`, `get_paused`, `get_fees`, `get_total_locked`, `get_market_data`). Sin caché, abrir el panel de admin dispararía 5 llamadas a toncenter por pulsación.
**Alternativa descartada:** leer el *data cell* crudo con `get_account_state`. Acopla el backend a la disposición exacta del almacenamiento (cambia con el contrato) y ya está muerto en el código.

### ADR-07 · Notificaciones mediante outbox en SQLite
**Decisión:** tabla `notifications` (`id, user_id, kind, payload_json, status, attempts, next_at`) escrita por los productores (liquidación, evento on-chain) y drenada por un job cada 30 s, con `notify_limiter` y reintentos.
**Por qué:** hoy el envío ocurriría dentro del request o del indexer; un fallo de Telegram retrasaría o rompería la indexación (violando P5 y P2).
**Alternativa descartada:** enviar desde el indexer con `try/except pass`. Pierde mensajes y bloquea el loop.

### ADR-08 · `X-Forwarded-For` solo con proxy declarado
**Decisión:** nuevo `TRUST_PROXY` (bool) + `TRUSTED_PROXIES` (CIDRs). Si el `request.remote` no está en la lista, se ignora la cabecera. Si está, se toma el **hop derecho no confiable**, no el primero (el primero lo controla el atacante).
**Por qué:** corrige S-04 sin romper despliegues detrás de un reverse proxy real.
**Alternativa descartada:** eliminar la cabecera por completo. Detrás de un proxy legítimo, todos los usuarios compartirían IP y el límite global se agotaría con el primer pico.

### ADR-09 · La CSP se mantiene estricta; se elimina el `onclick` inline
**Decisión:** cambiar el botón de `app.js:186` por delegación de eventos (ya usada en el resto del archivo). No añadir `'unsafe-inline'`.
**Por qué:** añadir `unsafe-inline` debilitaría la CSP de toda la Mini App para arreglar un solo botón.
**Alternativa descartada:** `nonce` por respuesta. Requiere tocar el HTML servido y complica el servido estático, sin beneficio frente a quitar un atributo.

### ADR-10 · `@tonconnect/ui` se sirve desde el propio dominio
**Decisión:** vendorizar el bundle (`dist/tonconnect-ui.min.js`) en `web/static/vendor/`, con hash SRI y CSP `script-src 'self'`.
**Por qué:** elimina F-12 (CDN sin integridad en el script que construye las transacciones), quita `unpkg.com` de la CSP y ahorra una conexión TLS en el arranque de la Mini App (importante en móvil).
**Alternativa descartada:** SRI apuntando a unpkg. Protege la integridad, pero mantiene la dependencia externa y el coste de red.

### ADR-11 · Guardarraíles de arquitectura como tests permanentes
**Decisión:** `tests/test_architecture.py` (reglas de importación de P7) y `tests/test_wiring.py` (toda ruta de la API la consume el frontend **o** está documentada; todo `callback_data` tiene handler; todo `build_*` de `bodies.py` tiene un consumidor; todo símbolo exportado tiene uso).
**Por qué:** los hallazgos S-03, D-02 y F-01 son «partes desconectadas»: no los detecta ningún test funcional. Estos tests convierten la conectividad en un invariante verificable.

---

## 2. Mapa de fases

| Fase | Estado | Tema | Hallazgos que cierra | Días* | Depende de |
| :--- | :--- | :--- | :--- | :--- | :--- |
| ✅ | **0** | Red de seguridad (CI, baseline) | F-10, F-11, T-01 (base), T-04, R-10 | 1 | — |
| ✅ | **1** | Integridad del dinero on-chain | **S-01, S-02, S-07**, F-05, F-06 | 4 | 0 |
| ✅ | **2** | Administración del contrato | **S-03**, D-03 | 3-4 | 0, 1 |
| ✅ | **3** | Endurecimiento de la API | **S-04, S-05**, D-01 (api), D-02 | 2 | 0 |
| ⬜ | **4** | Economía única y coherencia mostrada | F-03, H-*, U-05 | 2-3 | 1 |
| ⬜ | **5** | Capa Telegram: navegación, entrada, notificaciones | F-01, F-14, F-02, F-04, **S-06**, U-01…U-04, U-06 | 4 | 1, 4 |
| ⬜ | **6** | Rendimiento y activos | R-01…R-09 | 2-3 | 1, 5 |
| ⬜ | **7** | Mantenibilidad, dependencias, contrato-tooling | 5.1, D-04, D-05, D-06, F-07, F-08, F-09, F-12, F-13, T-02 | 3 | 3, 4 |

\* Días de trabajo efectivo de una persona, indicativos. Las fases 3 y 6 pueden ejecutarse en paralelo si hay más de un desarrollador; 1 → 2 → 5 es una cadena estricta.

```
Fase 0 ──► Fase 1 ──► Fase 2
              │  └──► Fase 4 ──► Fase 5 ──► Fase 6
              └──► Fase 3 ──┴──► Fase 7
```

**Regla de oro:** ninguna fase empieza sin que la anterior tenga su gate en verde. Las fases 1 y 2 tocan dinero y no admiten «lo arreglo luego».

---

## 3. Fase 0 — Red de seguridad

**Objetivo:** que a partir de aquí todo cambio sea verificable automáticamente. Hoy 263 tests solo corren si alguien se acuerda.

### Cambios

| Archivo | Acción |
| :--- | :--- |
| `.github/workflows/ci.yml` | **Nuevo.** 4 jobs: `lint` (ruff), `test` (pytest con cobertura), `contract` (build + hash + `escrow.test.js`), `audit` (pip-audit + npm audit). Disparadores: push y PR. |
| `requirements-dev.txt` | **Nuevo.** `pytest`, `pytest-asyncio`, `pytest-cov`, `ruff`, `vulture`, `bandit`, `pip-audit`. Separado del runtime (corrige F-11). |
| `.gitignore` | Añadir `.coverage`, `htmlcov/`, `coverage.xml`, `.mypy_cache/` (corrige T-04). |
| `pyproject.toml` (o `ruff.toml`) | **Nuevo.** Fijar reglas de ruff y excluir el ruido actual (`UP006/UP045`, `PLR2004`, `ARG001`) con un `TODO` fechado; sin esto el lint no puede ser bloqueante. |
| `tests/test_architecture.py` | **Nuevo.** Reglas de importación de P7. |
| `tests/test_wiring.py` | **Nuevo.** Rutas ↔ frontend, callbacks ↔ handlers, builders ↔ consumidores. Marca los huérfanos conocidos como `xfail(strict=True)` para que **dejen de estar en xfail** cuando se arreglen (Fase 2/3/7). |
| `pytest.ini` | Umbral de cobertura mínimo global (55 % actual) para evitar regresiones; sube por fase. |

### Criterios de aceptación (verificables)

1. Un PR que rompe un test **no puede** mergearse (branch protection en `main`).
2. `ruff check` y `pytest` corren en <2 min en CI.
3. El job `contract` falla si el hash compilado difiere de `a29c5c51…`.
4. `tests/test_wiring.py` pasa hoy con N `xfail(strict=True)` documentados (un huérfano por caso) y debe quedar **sin ningún `xfail` de conectividad** al terminar las fases 2, 3 y 7; al arreglar un huérfano, `strict=True` obliga a quitar su marca.
5. `docker`/runner limpio: `pip install -r requirements.txt -r requirements-dev.txt && pytest` funciona sin pasos manuales.

### Riesgos
- El lint bloqueante puede generar fricción si se activan todas las reglas de golpe → se activa solo `E,F,W,I` y se amplía en Fase 7.

---

## 4. Fase 1 — Integridad del dinero on-chain

**Objetivo:** que ningún evento de la cadena se pierda y que el espejo local nunca degrade datos buenos. Es la fase de mayor riesgo y la primera que hay que hacer.

### 1.1 · Cursor seguro con dead-letter (cierra **S-01**)

**Diseño:**
- El bucle de `_poll` procesa en orden ascendente y lleva `last_good_lt/hash` (última tx aplicada con éxito).
- El cursor solo se persiste con `last_good`. Un fallo **no** avanza el cursor.
- Reintento inmediato en el mismo ciclo (hasta `MAX_TX_ATTEMPTS = 3`); es seguro porque `chain_txs` garantiza idempotencia.
- Si se agota, se inserta en `chain_tx_failures(tx_hash, lt, op, error, attempts, first_seen, last_seen, parked)`, se **salta** la tx (para liberar la cabeza) y se continúa.
- Job de reconciliación (`run_once` cada `INDEXER_POLL_INTERVAL`, y reintento de la dead-letter cada 5 min) → si tiene éxito, borra la fila.
- `/api/indexer` y el panel de admin muestran `parked_count` y el último error. Umbral de alerta: >0.

**Archivos:** `bot/chain/indexer.py` (bucle y reintentos), `bot/database.py` (+`chain_tx_failures`, `+fail_chain_tx`, `+list_pending_failures`, `+clear_chain_tx_failure`), `bot/api.py` (estado), `bot/handlers/admin.py` (indicador).

**Aceptación:**
- Test de propiedad: se inyectan N txs con fallos aleatorios; tras K ciclos, el estado final de `markets`/`onchain_bets` es **idéntico** al de una ejecución sin fallos.
- Test de poison-pill: una tx que siempre falla no detiene el indexer; aparece en `chain_tx_failures` y se reintenta; al cesar el fallo, se drena.
- El cursor nunca retrocede ni salta una tx con éxito.

### 1.2 · Enriquecimiento de mercados (cierra **S-02**)

**Diseño (ADR-03):**
- `insert_market_placeholder(market_id, ...)`: crea la fila mínima cuando llega una apuesta de un mercado desconocido. Marca `metadata_source='placeholder'`.
- `enrich_market_from_event(market_id, ...)`: llamada **solo** desde `_apply_market_created`. Rellena `title`, `closes_at`, `option_count`, `fee_bps`, `title_hash`, `creator_address`, `created_lt/tx` **solo si** están vacíos o son de marcador (`title LIKE 'Mercado #%'`, `closes_at = 0`). Cambia `metadata_source='event'`.
- Una fila con `metadata_source='event'` no se sobrescribe nunca (protege contra eventos duplicados o reordenados).

**Archivos:** `bot/database.py` (sustituye `upsert_market` en dos funciones), `bot/chain/indexer.py` (dos llamadas), `tests/test_indexer.py` (nuevos casos), `tests/test_database.py`.

**Aceptación:**
- Bet-antes-que-create: al final el mercado tiene título real, `closes_at` correcto y `fee_bps` del evento.
- Create-antes-que-bet: la apuesta no altera el título ni la fecha.
- Duplicado del evento create: no cambia nada y no duplica opciones.
- `test_wiring` ya no marca `upsert_market` como ambiguo.

### 1.3 · Política explícita de cancelación (cierra **F-05**)

**Decisión propuesta:** `prepare_cancel` permite cancelar solo si `closes_at <= now` **o** si el mercado no tiene apuestas. Para una cancelación de emergencia con mercado abierto y apuestas, se exige un segundo parámetro explícito `force=True` que solo acepta el handler tras una confirmación con texto de advertencia (nunca la API de la Mini App).
**Por qué:** hoy la asimetría con `prepare_resolve` no está documentada y permite reembolsar un mercado en curso por error.

**Archivos:** `bot/markets.py`, `bot/handlers/admin.py`, `bot/api.py` (rechaza `force` por HTTP), tests.

**Aceptación:** matriz de casos (abierto con apuestas → rechazado sin `force`; abierto sin apuestas → permitido; cerrado → permitido) cubierta por tests.

### 1.4 · Ventana de resolución (cierra **F-06**)

**Diseño:** al preparar `resolve`/`cancel`, escribir `markets.pending_resolve_at` + `pending_action` local. Mientras esté vigente (TTL 15 min), `prepare_bet` rechaza con `market_resolving` y la UI muestra «en resolución». Se limpia al llegar el evento on-chain o al expirar.
**Por qué:** evita apuestas que llegarían a un mercado ya resuelto y que el contrato rechazaría (gas pagado por el usuario sin beneficio).

**Archivos:** `bot/markets.py`, `bot/database.py` (+columna), `bot/api.py`/`app.js` (estado), tests.

### 1.5 · Desvinculación de wallet (cierra **S-07**)

**Hallazgo estructural verificado:** `wallet_links.address` es `UNIQUE` global y `user_id` es la PK. Mientras la fila exista, la dirección queda bloqueada aunque `is_active = 0`. **No se puede arreglar con un índice parcial: hace falta reconstruir la tabla.**

**Diseño:**
- Nueva tabla `wallet_links_new(id INTEGER PK AUTOINCREMENT, user_id, address, network, public_key, verified_at, is_active)`.
- `CREATE UNIQUE INDEX idx_wallet_active ON wallet_links_new(address) WHERE is_active = 1`.
- `CREATE UNIQUE INDEX idx_wallet_active_user ON wallet_links_new(user_id) WHERE is_active = 1` (preserva «una sola wallet activa por usuario», que hoy garantiza la PK).
- SQLite disponible en el entorno verificado: **3.40.1**; los índices parciales existen desde 3.8.0 → soportado.
- Migración: copiar filas, `DROP`/`RENAME`, dentro de una transacción (`PRAGMA foreign_keys=OFF` si aplica).
- `link_wallet`: comprobar `WHERE address = ? AND is_active = 1`; el índice único hace imposible el duplicado activo incluso con carreras.
- `unlink_wallet`: `UPDATE ... SET is_active = 0` (conserva historial, libera la dirección).
- `get_wallet_owner`: ya filtra `is_active = 1` → ahora es coherente por construcción.

**Archivos:** `bot/database.py` (esquema + migración), tests de migración (`DB` antigua → nueva, sin pérdida), tests de carrera (dos usuarios, misma dirección).

**Aceptación:** tras `unlink`, otro usuario puede vincular la misma dirección; dos vinculaciones concurrentes de la misma address producen exactamente un ganador (`WalletConflict` en el otro); el historial se conserva.

### Gate de la fase

```bash
.venv/bin/python -m pytest tests/ -q                 # todo verde
.venv/bin/python -m pytest tests/test_indexer.py -q  # incluye propiedad + poison-pill
.venv/bin/python -m pytest tests/test_database.py -q # incluye migración de wallet_links
```

Métrica de salida: **cero** filas en `chain_tx_failures` tras un backfill completo simulado; cobertura de `bot/chain/indexer.py` ≥ 90 %.

---

## 5. Fase 2 — Administración del contrato

**Objetivo:** que el administrador pueda operar el escrow (fees, pausa, comisión, cambio de admin) desde el bot y la Mini App, **sin que el servidor firme**.

### 2.1 · Lectura del estado del contrato (ADR-06)

`TonCenterClient.run_get_method(address, method, stack)` → `{'ok', 'value'}`; `EscrowState` con `admin`, `fee_bps`, `min_stake`, `paused`, `fees`, `total_locked`, `balance`, `read_at`, caché TTL 30 s. Inyectable para tests.

### 2.2 · Servicios

En `bot/markets.py`, junto a las existentes:

| Función | Validaciones antes de construir |
| :--- | :--- |
| `prepare_withdraw_fees(user_id, amount_nano)` | admin; `0 < amount <= state.fees`; no tocar `total_locked` (el contrato lo impone, se valida antes para dar mensaje claro) |
| `prepare_set_fee(user_id, fee_bps)` | admin; `0 <= fee_bps <= 1000`; aviso si sube |
| `prepare_set_paused(user_id, paused)` | admin; confirmación explícita |
| `prepare_change_admin(user_id, new_address)` | admin; dirección válida, distinta de la actual, no cero |

Todas devuelven `PreparedTx` con `deeplink` (Tonkeeper) y `payload` (base64 para TON Connect) → reutilizan la infraestructura existente sin tocar la firma.

### 2.3 · Panel en el bot

Implementado como submenú `admin_contract` (botón «⚙️ Contrato escrow» del panel `/admin`) con:
`⚙️ Estado del contrato` · `💸 Retirar comisiones` · `⚖️ Cambiar comisión` · `⏸ Pausar/Reanudar` · `👤 Cambiar administrador`.

Flujos de entrada de texto (importe / bps / dirección) con un `ConversationHandler` propio, `conversation_timeout=300`, cancelable con `/cancelar`. Doble confirmación en `change_admin` y `paused`.

### 2.4 · Panel en la Mini App

En `#view-admin`: tarjeta de estado (pausado, fee, fees acumuladas) y acciones firmadas por TON Connect a través de `POST /api/admin/contract/{action}/prepare`.

### 2.5 · `ESCROW_ADMIN_ADDRESS` cobra sentido (cierra **D-03**)

Se usa para: (a) mostrar al admin la dirección del contrato y la suya; (b) avisar si la wallet conectada ≠ admin del contrato; (c) impedir `prepare_*` cuando el destino del deeplink no coincide con la admin esperada.

### Aceptación

- Cada cuerpo generado se ejecuta contra el contrato compilado en `@ton/sandbox` y devuelve exit `0` (amplía `escrow.test.js` o añade `tests/admin-ops.test.js`).
- `tests/test_contract_sync.py` verifica paridad de **opcodes** entre `bodies.py` y `escrow.fc` (`0x1A0A0101..0104`).
- Ningún endpoint devuelve una tx firmada ni acepta una clave: se inspecciona el payload de respuesta en un test.
- `test_wiring` deja de marcar los 4 builders como huérfanos.

### Riesgo principal
`withdraw_fees` mueve dinero real: se prueba primero en testnet con `DEPLOY_VALUE_NANO` pequeño y se documenta el runbook en `README.md` (nuevo apartado «Operación del contrato»).

---

## 6. Fase 3 — Endurecimiento de la API

### 3.1 · IP real y límites (cierra **S-04**)

- `config.py`: `TRUST_PROXY` (bool, default `False`), `TRUSTED_PROXIES` (lista CIDR, default vacía).
- `bot/api.py`: `_client_ip()` → si `TRUST_PROXY` y `remote` ∈ CIDRs, recorrer `X-Forwarded-For` de derecha a izquierda y devolver el primer hop fuera de la lista; si no, `remote`.
- Añadir **backstop global** por endpoint (p. ej. 300 req/min en `/api/session`) para que la rotación de claves no anule la protección.
- `RateLimiter`: evicción LRU real (hoy solo borra cubetas vacías), y devolver `Retry-After` al cliente.

**Aceptación:** el test de 40 peticiones con `X-Forwarded-For` rotado devuelve **429** cuando `TRUST_PROXY=False`; con `TRUST_PROXY=True` y un proxy declarado, la IP se resuelve correctamente; sin proxy, `remote` manda.

### 3.2 · CSP y botón de reintento (cierra **S-05**)

Sustituir `onclick="location.reload()"` (`app.js:186`) por `data-reload` + listener delegado ya existente. Añadir test que asegure que el HTML/JS generado no contiene `on*=` inline (grep en CI).

### 3.3 · Duplicación en la API (cierra **D-01** parcial)

Helpers `_user_payload(user, user_id)`, `_require_admin(request)`, `_parse_market_id(request)`, `_error(code, msg)`. Elimina los tres clones detectados por jscpd.

### 3.4 · Endpoints sin consumidor (cierra **D-02**)

**Decisión:** no borrar, **cablear**:
- `DELETE /api/wallet` → botón «Desvincular» en la vista de wallet de la Mini App (con confirmación).
- `POST /api/admin/markets/{id}/cancel/prepare` → botón «Cancelar mercado» en el panel admin de la Mini App.
- `GET /api/health` → documentado como endpoint de orquestador (queda `xfail` justificado en `test_wiring` con motivo explícito).

**Aceptación:** `test_wiring` sin huérfanos salvo el justificado; e2e de desvincular y cancelar desde el TestClient.

---

## 7. Fase 4 — Economía única y coherencia de lo mostrado

### 4.1 · `bot/economics.py`

```python
def distributable(total: int, fee_bps: int) -> int
def payout(stake: int, distributable: int, winning_pool: int) -> int
def multiplier(total: int, option_pool: int, fee_bps: int) -> float | None
def preview_payout(stake: int, option_pool: int, total: int, fee_bps: int) -> int
```

Consumidores: `bot/database.py` (liquidación de puntos), `bot/markets.py` (`estimate_payout`, `market_view.multiplier`), `bot/handlers/{admin,markets}.py`, `bot/api.py` (`quote` opcional).

### 4.2 · El frontend deja de calcular

`/api/markets/{id}` devuelve por opción `multiplier` ya neto y, para la hoja de apuesta, `fee_bps`. `app.js:660` pasa a usar `potential_payout` del servidor cuando exista, o la fórmula con `fee_bps`. Se añade un test que compara el valor mostrado con `estimate_payout`.

### 4.3 · Configuración de las constantes de dinero (cierra **H-***)

| Nueva variable | Default | Sustituye |
| :--- | :--- | :--- |
| `MAX_STAKE_NANO` | `5_000_000_000_000` (5000 TON) | literal `bot/markets.py:235` |
| `BET_GAS_NANO` / `CREATE_GAS_NANO` / `ADMIN_GAS_NANO` / `CLAIM_GAS_NANO` | actuales | `bot/markets.py:42-45` |
| `MIN_MARKET_DURATION_MIN` / `MAX_MARKET_DURATION_DAYS` | 15 / 365 | `bot/markets.py:47-48` |
| `TX_TTL_SECONDS` | 300 | `bot/markets.py:49` |

`public_config()` expone `max_stake_ton`, `min_market_duration_min`, `bet_gas_ton` y el `fee_bps` por mercado (columna `markets.fee_bps`, ya poblada desde la Fase 1.2); `help.py` y los valores por defecto de `web/index.html`/`app.js` se alimentan de ahí (una sola copia).

**Aceptación:** `grep` en CI que falla si reaparecen los literales `30_000_000`, `0.05` o `5_000 * NANO` fuera de `config.py`/`markets.py`. Parity test Python ↔ contrato con vectores compartidos.

---

## 8. Fase 5 — Capa Telegram: navegación, entrada y notificaciones

> Es la capa con 0 % de cobertura y donde se concentran F-01, F-02, F-04 y S-06.

### 5.1 · Menú consistente (cierra **F-01** y **F-14**)

**Diseño:** `bot/handlers/common.py: menu_for(update, wallet=None)` que resuelve `is_admin` con `is_admin(update.effective_user.id)` y construye el teclado. Se sustituyen las 8 llamadas directas (`predictions.py:83,93,307,321,450,493`, `main.py:159`, `start.py:64,93`). `back_to_menu_keyboard` recibe el mismo contexto o se elimina en favor del helper.

**Aceptación:** test que recorre todas las pantallas y verifica que para un admin la fila «🔧 Admin» está presente; para un no-admin, ausente.

### 5.2 · Importe de apuesta sin corrupción (cierra **S-06**)

`bot/parsing.py: parse_points(text) -> int`:
- Enteros con separador de miles válido (`1.000`, `1,000`, `1 000`) → 1000.
- Decimales (`10.5`, `1,5`) → **error explícito**: «los puntos son enteros; si querías 10, escribe 10».
- Cero/negativos/`> max_bet_points` → error con el límite.
- Pantalla de confirmación mostrando el importe final y el pago potencial (calculado con `economics.preview_payout`).

**Aceptación:** tabla de casos en `tests/test_parsing.py`; el flujo nunca apuesta un valor distinto del confirmado.

### 5.3 · Modo grupo (cierra **F-02**)

Dos opciones; **recomiendo la A**:
- **A. Implementarlo:** `per_chat=True` en la conversación, y el paso de importe acepta mensajes de grupo **solo** si son respuesta al mensaje del bot (`update.message.reply_to_message.message_id == context.user_data['amount_prompt_id']`). Un `MessageHandler` específico en el `ConversationHandler`, en lugar del global de `main.py:324`.
- **B. Declararlo parcial:** cambiar el README a «modo grupo: crear/ver ✅, apostar ⬜» y bloquear el flujo con un mensaje claro.

Si no hay tiempo para A, B es honesto y elimina la promesa falsa. **Decisión del dueño del producto.**

### 5.4 · Notificaciones (cierra **F-04**, ADR-07)

- Tabla `notifications(id, user_id, kind, payload_json, status, attempts, next_at, created_at)` + índice `(status, next_at)`.
- Productores: `settle_prediction` (una por usuario con resultado), indexer al procesar `market_resolved`/`cancelled` (aviso de «reclama tu pago»).
- Consumidor: `job_dispatch_notifications` cada 30 s, lote 20, `notify_limiter`, reintentos con backoff exponencial, `status='sent'` al confirmar. Dedupe por `(user_id, kind, ref_id)`.
- `/misbets` sigue funcionando como fuente de verdad.

**Aceptación:** test de dedupe (dos liquidaciones no duplican aviso), test de reintento (fallo de Telegram → `attempts++` y `next_at` futuro), y un test de que el indexer no bloquea si Telegram falla.

### 5.5 · Inconsistencias de UX (U-01…U-06)

Formato *friendly* en `/start`, fecha de ejemplo dinámica (`+7 días`), texto de estado vacío coherente, listado del panel admin alineado con el teclado (o paginado), formato de multiplicador unificado, `/help` consciente del rol.

**Gate de la fase:** cobertura de `bot/handlers/*` ≥ **70 %** (desde 0 %) y `bot/keyboards.py` ≥ 60 %.

---

## 9. Fase 6 — Rendimiento y activos

| # | Acción | Verificación |
| :--- | :--- | :--- |
| R-04 | Índice `idx_onchain_bets_bettor(bettor, id DESC)` | test que ejecuta `EXPLAIN QUERY PLAN` y falla si aparece `SCAN` |
| R-05 | Índice compuesto `markets(status, closes_at)` | idem (elimina el B-TREE temporal) |
| R-01 | Una sola consulta para la página de predicciones (JOIN + `GROUP BY`) | test de conteo de queries con un `Connection` instrumentado |
| R-02/R-03 | `get_user_stats(user_id)` con `SUM/COUNT` en SQL + `ranking_position(user_id)` | test de equivalencia con el cálculo actual |
| R-06 | `icon.png` a 512×512 WebP/PNG optimizado (<80 KB) | test que falla si un asset estático supera el presupuesto |
| R-09 | `Cache-Control: public, max-age=31536000, immutable` para assets versionados; gzip en memoria para `/static/*.js|css` | test de cabeceras |
| R-08 | Polling adaptativo: `setInterval` 25 s → backoff 15/30/60 s; `ETag` en `/api/markets` con `304` | test de que un segundo GET devuelve 304 |

**Presupuesto de arranque de la Mini App:** HTML+CSS+JS+icono < 350 KB comprimidos (hoy el icono solo son 921 KB).

---

## 10. Fase 7 — Mantenibilidad, dependencias y tooling del contrato

| Bloque | Acción |
| :--- | :--- |
| Código muerto (5.1) | Eliminar los 19 símbolos listados. `vulture --min-confidence 80` en CI con lista blanca vacía. |
| Fachada `bot/chain/__init__.py` | **Eliminar** re-exports y dejar el paquete vacío; los tests importan submódulos (ya lo hacen). |
| D-04 | Sustituir el `UPDATE ... points = 1000` por migraciones versionadas con `PRAGMA user_version` (1..N), cada una idempotente y con test de ida y vuelta. |
| D-05 | `upsert_user`: `username = COALESCE(excluded.username, users.username)`. |
| D-06 | Tests de teclados (cubierto parcialmente en Fase 5). |
| F-07 | `parse_message_body`: capturar `(BodyError, Exception)` → `except Exception` con re-lanzado de `BodyError` ya presente; eliminar la tupla. |
| F-08/F-09 | Panel admin paginado; `dev_server.py` con `--help`, `--port`, y manifest configurable (o `PUBLIC_BASE_URL` en https cuando exista túnel). |
| F-12 | Vendorizar `@tonconnect/ui` (ADR-10) con SRI; CSP `script-src 'self'`. |
| F-13 | `deploy.js`: si no alcanza `active` en el plazo → `process.exit(1)` y **no** escribir `deploy.json`; comparar el hash compilado con `CODE_HASH` antes de desplegar. |
| T-02 | PTB 21.6 → 22.8. Impacto verificado sobre este código: no usa `filters.CHAT`, ni `Defaults.quote`, ni `proxy_url`, ni `*_timeout` en `run_polling`. Revisar `disable_web_page_preview` (sustituido por `link_preview_options` en la API 7.0) y `BaseRequest` en los dobles de test. Actualizar en un PR aislado con la suite completa. |
| R-10 | Normalizar ruff (`UP006/UP045`, `PLR2004`, `ARG001`, `PLC0415`), activar reglas nuevas por bloques. |

---

## 11. Trazabilidad: cada hallazgo tiene fase

| Fase | Hallazgos |
| :--- | :--- |
| 0 | F-10, F-11, T-01 (base), T-04, R-10 |
| 1 | **S-01, S-02, S-07**, F-05, F-06 |
| 2 | **S-03**, D-03 |
| 3 | **S-04, S-05**, D-01 (API), D-02 |
| 4 | F-03, H-*, U-05 |
| 5 | F-01, F-14, F-02, F-04, **S-06**, U-01…U-04, U-06, T-01 (objetivo) |
| 6 | R-01…R-09 |
| 7 | 5.1, D-04, D-05, D-06, F-07, F-08, F-09, F-12, F-13, T-02, R-10 |

Sin asignar: **ninguno**. Los 6 bloques del encargo quedan cubiertos: funcionalidad (1, 2, 5), hardcodeo (4), duplicación (3, 4, 7), scripts no funcionales (0, 7), partes desconectadas (1, 2, 3, 7), optimalidad (6) y seguridad (1, 2, 3, 4).

---

## 12. Definición de «terminado» (global)

Una fase se considera cerrada solo si cumple **las siete**:

1. Tests nuevos que **fallan sin el cambio** (se demuestra localmente antes de mergear).
2. `pytest` verde y cobertura **no decrece**; sube al umbral de la fase.
3. `ruff` y `vulture` sin nuevas alertas.
4. `test_architecture.py` y `test_wiring.py` verdes (sin `xfail` nuevos).
5. Contrato: hash `a29c5c51…` intacto salvo que la fase lo modifique explícitamente, en cuyo caso se actualiza `CODE_HASH`, README y `deploy.json` en el mismo commit.
6. `README.md` y `.env.example` actualizados si cambia configuración u operación.
7. Entrada en `CHANGELOG` de la fase con: qué se arregló, qué se decidió y qué quedó pendiente.

---

## 13. Lo que este plan deliberadamente **no** hace

Para evitar que el alcance se infle sin control:

- **No** migra a Postgres, Redis, Celery, Docker Compose multi-servicio ni a un framework web (ADR-01).
- **No** introduce un firmante en el servidor ni un *relayer* de gas: rompería P1 y la propuesta del producto.
- **No** implementa el oráculo automático (F013) ni retiros fiat (F014): son features nuevas, no deuda.
- **No** reescribe el contrato FunC. Solo se amplía `escrow.test.js` para cubrir las operaciones de admin.
- **No** añade observabilidad pesada (Prometheus/Grafana). Se usa `/api/health`, `/api/indexer` y logs estructurados, suficiente para 512 MB.

---

## 14. Decisiones que necesito del dueño del producto

Con la recomendación por defecto ya integrada en el plan:

| # | Decisión | Recomendación |
| :--- | :--- | :--- |
| 1 | Modo grupo (F-02): implementar o declarar parcial | **Implementar (A)**; si urge, declarar parcial (B) y cerrar la promesa del README |
| 2 | Cancelación con `force` en mercado abierto (F-05) | Permitir solo desde el bot, nunca por API, con confirmación escrita |
| 3 | Admin del contrato en la Mini App (Fase 2.4) | Sí, pero **después** del bot: el panel de Telegram es la superficie primaria |
| 4 | Prioridad entre Fase 3 y Fase 4 si solo hay un desarrollador | **Fase 3 primero** (seguridad) |
| 5 | Ventana de resolución (F-06) | 15 min de bloqueo de apuestas |

---

## 15. Orden de arranque sugerido

```
Semana 1:  Fase 0 (CI)  →  Fase 1.1 + 1.2   (el dinero primero)
Semana 2:  Fase 1.3-1.5  →  gate de Fase 1
Semana 3:  Fase 2 completa
Semana 4:  Fase 3  →  Fase 4
Semana 5:  Fase 5 (menú, parsing, notificaciones)
Semana 6:  Fase 6  →  Fase 7
```

Al final: 55 % → **≥ 80 %** de cobertura, cero hallazgos críticos/altos abiertos, y los guardarraíles (`test_architecture`, `test_wiring`, presupuesto de assets) impidiendo que reaparezcan.
