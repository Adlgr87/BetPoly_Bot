# Auditoría técnica — BetPoly_Bot

**Fecha:** 2026-10-23
**Alcance:** repositorio completo (`bot/`, `config.py`, `main.py`, `web/`, `contracts/`, `scripts/`, `tests/`).
**Commit auditado:** `6b50853` (rama `arena/de3be963-betpoly-bot`, igual a `origin/main`).
**Naturaleza de este documento:** **solo hallazgos documentados**. No se ha modificado ni una línea del proyecto.

> Este informe está ordenado por severidad. Cada hallazgo indica evidencia reproducible (`archivo:línea`), impacto y, cuando aplica, la prueba ejecutada que lo confirma.
> La numeración (`S-*`, `F-*`, `D-*`, `H-*`, `R-*`, `T-*`) es estable para poder referenciarla en instrucciones posteriores.

---

## 0. Resumen ejecutivo

| Área | Veredicto |
| :--- | :--- |
| Contrato escrow (FunC) | ✅ Compila de forma determinista y pasa 22/22 comprobaciones en sandbox TVM |
| Backend no-custodial | ✅ Se mantiene el invariante: nunca hay claves privadas ni firma del lado servidor |
| Persistencia | ⚠️ Transacciones correctas, pero hay pérdida silenciosa de eventos on-chain (S-01) |
| Índice on-chain | ❌ Un error de persistencia descarta el evento para siempre (S-01) |
| API HTTP | ⚠️ Autenticación sólida; limitador de tasa eludible (S-04) |
| Capa Telegram (handlers) | ⚠️ 0 % de cobertura y varios defectos de navegación/entrada (F-01, F-04) |
| Mini App (`web/`) | ⚠️ Cableada correctamente, salvo un botón roto por CSP (S-05) |
| Código muerto / duplicado | ⚠️ 19 símbolos sin uso, 3 clones reales, 1 fachada de paquete sin consumidores |
| Hardcodeo | ⚠️ Sin secretos ni direcciones; sí constantes de dinero/tiempos dispersas |
| Dependencias | ✅ Fijadas y al día salvo `python-telegram-bot` (una major por detrás) |
| Automatización | ❌ No existe CI: los 263 tests nunca se ejecutan automáticamente |

**Conteo:** 7 hallazgos críticos/altos · 15 medios · 25 menores, más 19 símbolos de código muerto.

---

## 1. Metodología y evidencia

Herramientas ejecutadas sobre el commit auditado:

- `ruff` (0.17.2), `vulture` (2.16), `bandit` (1.9.4), `radon`, `jscpd` (4.x), `pip-audit` (2.10.1), `npm audit`.
- `pytest` con `pytest-cov` → **263 tests en verde, cobertura total 55 %**.
- `contracts/`: `node scripts/build.js`, `node tests/escrow.test.js` (22 comprobaciones), `npm audit`, `node scripts/deploy.js` (dry-run real).
- Arranque real de `scripts/dev_server.py` en el puerto 8080 y sondeo con `curl`.
- Pruebas de API con `aiohttp.test_utils.TestClient` sobre bases de datos desechables (`/tmp/audit*.db`), incluidos casos de abuso (rotación de `X-Forwarded-For`, montos límite, `limit`/`offset` extremos).
- Pruebas directas contra `bot.database` y `bot.chain.indexer` con clientes falsos para forzar fallos de persistencia.
- Lectura manual íntegra de `bot/api.py`, `bot/security.py`, `bot/database.py`, `bot/markets.py`, `bot/chain/*`, `bot/handlers/*`, `bot/keyboards.py`, `config.py`, `web/index.html`, `web/static/app.js`, `main.py`, `contracts/scripts/*.js` y secciones de `contracts/escrow.fc`.
- Verificación de versiones publicadas hoy en PyPI y npm contra los *pins* del repositorio.

---

## 2. Lo que sí está verificado y funciona

Para que el listado de problemas no dé una impresión falsa del estado del proyecto:

1. **Contrato:** `node scripts/build.js` reproduce exactamente el hash fijado en `tests/test_contract_sync.py::CODE_HASH` → `a29c5c51fe93597967c6d37c976a27e989d13662117165dec1d899fe3292db9b`. Compilación determinista y verificable.
2. **Suite del contrato:** 22/22 comprobaciones en `@ton/sandbox` (crear, apostar, resolver, reclamar, cancelar, reembolsos, pausa, fees).
3. **Suite Python:** 263 tests, 0 fallos, sin red.
4. **Arranque de la Mini App:** `scripts/dev_server.py` responde `200` en `/`, `/api/health` y devuelve mercados sembrados en `/api/markets`.
5. **`main.py --check`:** valida de verdad y sale con código `2` ante configuración inválida (probado con `BET_MODE=hybrid` sin `ESCROW_ADDRESS` y con `PUBLIC_BASE_URL` en `http://`).
6. **`deploy.js`:** dry-run correcto, exit `1` con dirección inválida, exit `0` con datos válidos; no envía transacciones en dry-run.
7. **Validación de importes (crítica para dinero):** matriz probada contra `/api/markets/{id}/bets/prepare` → `-5e9` → `stake_too_low`, `1e18` → `stake_too_high`, `true` → `bad_amount`, `"1e9"` → `bad_amount`, `"0.000000001"` → `stake_too_low`, sin wallet → `wallet_required`. **Correcto.**
8. **Cadena de autenticación:** `initData` (HMAC) → nonce de un solo uso (`ton_proof`) → `verify_ton_proof` (valida `state_init.hash == address_hash` y luego la firma) → `link_wallet`. Sin fugas: `/api/me` no expone el nonce.
9. **SQLite:** `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=5000`; liquidaciones idempotentes; `chain_txs(tx_hash, lt)` como PK garantiza procesamiento *exactly-once*.
10. **Dependencias:** `npm audit` sin vulnerabilidades; `pip-audit` limpio sobre `requirements.txt`; los *pins* de npm coinciden con las últimas versiones publicadas.
11. **Sin secretos en el código:** ni tokens, ni mnemónicos, ni direcciones de administrador embebidas. Las fixtures de test usan valores evidentemente falsos (`123456789:TEST-TOKEN-…`).
12. **Variables de entorno:** todas las declaradas en `.env.example` se leen en `config.py`; una de ellas (`ESCROW_ADMIN_ADDRESS`) se lee y luego no se usa (ver D-03). *(Una sospecha inicial sobre `FRAME_ANCESTORS` resultó **falsa**: sí se lee en `config.py:254`.)*

---

## 3. Hallazgos críticos y altos

### S-01 — 🔴 El indexador pierde eventos on-chain de forma permanente si falla una escritura
**Evidencia:** `bot/chain/indexer.py:140-157`
En el `except` se marca la tx como no procesada (`unmark_tx_processed`) con el comentario *«retry this tx next poll»*, pero **acto seguido el cursor se actualiza igual** (`if tx.lt >= newest_lt: newest_lt, newest_hash = tx.lt, tx.tx_hash`) y se persiste con `set_cursor(...)`. El siguiente ciclo solo pide transacciones *más nuevas* que el cursor, por lo que la tx fallida queda fuera de la ventana para siempre.

**Prueba ejecutada** (3 txs, fallo forzado en la del medio):
```
run 1 -> fetched 3 processed 2 errores 1
cursor tras run 1: (30, ...)
run 2 -> fetched 0 processed 0
¿tx 20 quedó procesada? True   ← el cursor ya la dejó atrás
```
**Impacto:** una apuesta o una resolución on-chain puede quedar invisible para el backend mientras el dinero sí se movió. No hay reconciliación posterior ni alerta. Es el hallazgo de mayor riesgo del proyecto.

---

### S-02 — 🔴 `upsert_market` ignora silenciosamente cualquier corrección posterior de los datos del mercado
**Evidencia:** `bot/database.py:943` (`INSERT OR IGNORE INTO markets ...`), usado desde `bot/chain/indexer.py:286` y `:321`.
La fila se crea la primera vez y **nunca se actualiza**. El caso real está documentado en el propio código (`indexer.py:319-330`): si una apuesta llega antes que el evento de creación, se crea un marcador con `title="Mercado #N"`, `closes_at=0` y sin `fee_bps`; cuando luego se indexa el evento de creación, los datos buenos se descartan.

**Prueba ejecutada:**
```
upsert_market existente -> created: False | título final: 'Mercado #1' | closes_at: 0
```
**Impacto:** mercado con fecha de cierre de 1970 (aparece siempre como cerrado), título genérico permanente, `option_count` incorrecto y `estimate_payout` calculando con `fee_bps = 0`.

---

### S-03 — 🔴 Funciones de administración del contrato inalcanzables desde cualquier interfaz
**Evidencia:** `bot/chain/bodies.py` define `build_set_paused`, `build_set_fee`, `build_change_admin`, `build_withdraw_fees`; `contracts/escrow.fc` las implementa; **ningún** router, handler, teclado o botón de la Mini App las invoca (búsqueda exhaustiva sobre `bot/`, `main.py`, `web/`).
El indexador sí sabe *leer* esos eventos, lo que confirma que el contrato los emite.

**Impacto:** el administrador **no puede retirar las comisiones acumuladas, pausar el contrato en una emergencia, cambiar la comisión ni transferir la administración** sin construir el mensaje a mano en una wallet. Las fees quedan atrapadas en el contrato. Es una funcionalidad completa (código + contrato) sin puerta de entrada.

---

### S-04 — 🔴 El limitador de tasa es eludible falsificando `X-Forwarded-For`
**Evidencia:** `bot/api.py:82-84`
```python
def _client_ip(request):
    forwarded = request.headers.get("X-Forwarded-For", "")
    return (forwarded.split(",")[0].strip() if forwarded else request.remote) or "?"
```
Se confía en la cabecera sin lista blanca de proxies. Se aplica a `/api/session` (`api.py:243`) y, por clave de usuario, a `/api/wallet/verify`.

**Prueba ejecutada:** 40 peticiones `POST /api/session` rotando `X-Forwarded-For: 10.0.0.N` → **40 × 200, cero 429**.
**Impacto:** un atacante abre sesiones ilimitadas y evita el freno de verificación de wallet; además el diccionario interno del limitador recibe claves arbitrarias (el recorte de memoria solo elimina cubetas vacías, `bot/security.py`).

---

### S-05 — 🔴 La CSP del propio servidor anula el botón «Reintentar» de la Mini App
**Evidencia:** `web/static/app.js:186` inyecta `<button ... onclick="location.reload()">`; `bot/api.py:628-629` publica `script-src 'self' https://telegram.org https://unpkg.com` sin `'unsafe-inline'` ni nonce.
Los manejadores inline (`onclick`) están bloqueados por esa CSP.
**Impacto:** en exactamente el escenario en el que el usuario necesita recuperarse (fallo de arranque de la Mini App), el único botón visible no hace nada. Resto de la UI usa delegación de eventos (`app.js:891-940`), que sí funciona.

---

### S-06 — ✅ El importe de la apuesta se corrompe al escribir decimales: `10.5` se convierte en `105` puntos
**Evidencia:** `bot/handlers/predictions.py:201`
```python
raw = (update.effective_message.text or "").strip().replace(".", "").replace(",", "")
```
Se eliminan los separadores para aceptar `1.000`, pero el efecto colateral es que `10.5` → `105` y `1,5` → `15`: **una apuesta 10× mayor que la tecleada**, sin confirmación adicional del importe.
**Impacto:** pérdida de puntos por un error de entrada previsible; el mensaje de error solo aparece si el texto no es numérico.
**Resuelto (Fase 5):** `bot/parsing.py: parse_points()` distingue miles de decimales y rechaza el decimal con un mensaje explícito («los puntos son enteros: si querías 10, escribe 10»); la confirmación muestra el importe final y el pago potencial de `economics.preview_payout`. Tabla de casos en `tests/test_parsing.py`.

---

### S-07 — 🟠 `unlink_wallet` deja la dirección reservada para siempre y contradice al resto del sistema
**Evidencia:** `bot/database.py:836` comprueba el dueño con `SELECT user_id FROM wallet_links WHERE address = ?` **sin** `is_active = 1`; `bot/database.py:861` (`get_wallet_owner`) **sí** filtra `is_active = 1`.
**Prueba ejecutada:**
```
owner tras unlink (is_active=0): None
otro usuario NO puede vincularla ❌ -> "Esta wallet ya está vinculada a otra cuenta de Telegram."
```
**Impacto:** tras desvincular, la wallet queda inutilizable para cualquier otra cuenta de Telegram de forma permanente, mientras el resto del sistema la considera libre. El mensaje de error es además incomprensible para el usuario final (nadie tiene esa dirección vinculada).

---

## 4. Hallazgos medios — funcionalidad rota o desconectada

### F-01 — ✅ El botón «🔧 Admin» desaparece del menú tras la primera navegación
`bot/keyboards.py` define `main_menu_keyboard(*, is_admin=False, wallet=None)`, pero seis llamadas no pasan el flag: `bot/handlers/predictions.py:83, 93, 307, 321, 450, 493` y `main.py:159`. Solo `bot/handlers/start.py:64, 93` lo hacen bien.
**Impacto:** el administrador pierde el acceso directo en cuanto pulsa cualquier botón; el flag `is_admin` de `back_to_menu_keyboard` además se ignora por completo.
**Resuelto (Fase 5):** `bot/handlers/common.py: menu_for()/back_keyboard()` resuelven el rol en todas las pantallas y `back_to_menu_keyboard(is_admin=…)` ya no ignora el parámetro. `tests/test_handlers_navigation.py` recorre 13 pantallas con rol y sin rol.

### F-02 — ✅ El «modo grupo» está anunciado como completo pero la apuesta no funciona en grupos
`README.md:281` marca **F010 Modo grupo ✅**. Las predicciones sí se guardan por `chat_id` (`bot/database.py:169`, `predictions.py:71`), pero el paso del importe es un `MessageHandler` global restringido a privado: `main.py:324` (`filters.ChatType.PRIVATE`). La conversación además se declara `per_user=True, per_chat=False` (`predictions.py:534`).
**Impacto:** en un grupo el flujo muere justo antes de apostar, y el estado de la conversación puede cruzarse entre chats del mismo usuario.
**Resuelto (Fase 5):** opción A del plan: `per_chat=True` en la conversación de creación y el importe se acepta en grupo solo si responde al mensaje del bot (`amount_prompt_id`); un mensaje suelto se ignora. Cubierto en `tests/test_handlers_flows.py::TestGroupMode`.

### F-03 — ✅ Los multiplicadores que ve el usuario no descuentan la comisión
`bot/handlers/markets.py:111` usa `total / pool`; `bot/markets.py:397` publica `multiplier` igual; `web/static/app.js:660-661` calcula el «Pago potencial» sin fee.
En cambio `estimate_payout` sí aplica `fee_bps` (`bot/markets.py:322`) y el contrato también.
**Impacto:** la UI promete ~2 % más de lo que el contrato pagará (con `FEE_BPS=200`). Cuatro implementaciones distintas de la misma fórmula pari-mutuel en cuatro lenguajes.
**Resuelto (Fase 4):** la fórmula vive solo en `bot/economics.py`; `bot/database.py`, `bot/markets.py` y `bot/handlers/markets.py` la importan, la Mini App pide `GET /api/markets/{id}/quote` y `contracts/vectors/economics.json` fija vectores compartidos con `contracts/tests/escrow.test.js`.

### F-04 — ✅ No existe notificación de resolución a los apostadores
`README.md:278` marca **F007 «Recordatorios de cierre y notificación de resolución» ✅**. Hay recordatorio de cierre (`main.py:331`, `job_reminders`), pero al liquidar una predicción **nadie avisa a los usuarios**: `settle_prediction` (`bot/database.py:606`) no envía mensajes y el resumen solo lo ve el administrador (`bot/handlers/admin.py:233`).
**Impacto:** el usuario debe entrar manualmente a `/misbets` para saber si ganó; la fila del README sobreestima la funcionalidad.
**Resuelto (Fase 5):** tabla de salida `notifications` escrita en la misma transacción que el resultado, con `UNIQUE (user_id, kind, ref_id)`; `bot/notifications.py` redacta y `main.job_dispatch_notifications` envía cada 30 s con backoff. Un fallo de Telegram no bloquea el indexador (test propio).

### F-05 — 🟠 `prepare_cancel` permite cancelar un mercado todavía abierto
`bot/markets.py:365-373` solo exige `status == "open"`; `prepare_resolve` sí valida `closes_at` (`bot/markets.py:341`).
**Impacto:** asimetría deliberada o descuido (no hay comentario que lo justifique): el admin puede reembolsar un mercado en curso, pero no resolverlo antes de tiempo. Conviene decidirlo explícitamente.

### F-06 — 🟠 Carrera entre la resolución on-chain y el espejo local
El admin puede firmar `resolve` cuando el espejo dice `open`; hasta que el indexador procese el evento, la Mini App y el bot siguen ofreciendo apostar en ese mercado. No hay bloqueo optimista ni estado `resolving`.

### F-07 — 🟠 `parse_message_body` captura cualquier excepción
`bot/chain/bodies.py`: `except (AddressError, Exception)` — la tupla es redundante y convierte cualquier bug en «mensaje irrelevante», enmascarando fallos de parseo.

### F-08 — 🟠 La UI del admin lista menos predicciones de las que el teclado permite pulsar
El texto del panel recorta a `predictions[:8]` (`bot/handlers/admin.py:75`) mientras `admin_predictions_keyboard()` (`bot/keyboards.py:201-212`) renderiza una fila por predicción; los botones sobrantes apuntan a predicciones que el usuario no ve listadas.

### F-09 — 🟠 `dev_server.py` sirve un manifest `http://` y no pasa por la validación de configuración
`scripts/dev_server.py:26` fija `PUBLIC_BASE_URL=http://localhost:8080`; TON Connect rechaza orígenes no HTTPS (el propio `config.validate()` lo exige en `bot`/`api`). En el preview local funciona porque la validación no se ejecuta en ese camino, pero el manifest resultante no es usable por wallets reales.

### F-10 — 🟠 No hay CI: 263 tests que nadie ejecuta automáticamente
No existe `.github/`, ni `Makefile`, ni `tox.ini`, ni `pre-commit`. La suite es buena, pero solo se ejecuta si alguien se acuerda.

### F-11 — 🟠 El flujo de instalación documentado no permite ejecutar los tests
`README.md:80` indica `pip install -r requirements.txt` y `README.md:243` invita a `pytest`. `requirements.txt` no incluye `pytest` ni `pytest-asyncio` (necesario por `asyncio_mode = auto` en `pytest.ini`), y no existe `requirements-dev.txt`.

### F-12 — 🟠 Dependencia de CDN sin integridad para el componente que firma transacciones
`web/index.html:11` carga `https://unpkg.com/@tonconnect/ui@5.0.1/dist/tonconnect-ui.min.js` sin `integrity`/`crossorigin`, y la CSP autoriza `unpkg.com` (`bot/api.py:629`). Es el script que construye las transacciones que el usuario firma: un compromiso del CDN afecta directamente a la firma.

### F-13 — 🟠 `deploy.js` puede reportar un despliegue que no ocurrió
`contracts/scripts/deploy.js`: tras 30 intentos × 4 s sin alcanzar `state === 'active'`, el bucle termina sin error y **se escribe igualmente** `contracts/build/deploy.json` con la dirección esperada, con exit `0`.
**Impacto:** falso positivo de despliegue; el operador configura `ESCROW_ADDRESS` de un contrato que puede no existir.

### F-14 — ✅ `main.py` construye el menú sin `is_admin` y el teclado de volver ignora el flag
Complemento de F-01 en el arranque: `main.py:159` arma el teclado del mensaje de bienvenida sin rol, por lo que el primer `/start` de un admin tras reiniciar tampoco muestra la fila.
**Resuelto (Fase 5):** `main.py` ya no construye teclados; el mensaje de error global usa `menu_for(update)`.

---

## 5. Hallazgos bajos — código muerto y duplicación

### 5.1 Código muerto confirmado (sin ninguna referencia)
| Elemento | Ubicación |
| :--- | :--- |
| `TonCenterClient.get_account_state` | `bot/chain/toncenter.py` |
| `_Ordered` | `bot/chain/indexer.py` |
| `IndexResult.fetched` / `.skipped` / `.last_result` | `bot/chain/indexer.py:47,54,112` — se **asignan y nunca se leen**, ni en el código ni en los tests |
| `build_set_paused`, `build_set_fee`, `build_change_admin`, `build_withdraw_fees` | `bot/chain/bodies.py` (ver también S-03) |
| `prepare_transaction_payload` | `bot/chain/bodies.py` |
| `prepare_for_body` | `bot/markets.py:472` — su docstring dice «used by API tests» y **ningún test lo usa** |
| `formatting.link`, `ton_with_unit`, `tx_short`, `parse_iso` | `bot/formatting.py` |
| `addresses.TESTNET`, `derive_address_hash` | `bot/chain/addresses.py:14,89` — sin ninguna referencia |
| `addresses.is_valid_address` | `bot/chain/addresses.py:65` — solo lo usan los tests, no el código de producción |
| `RateLimiter.reset` | `bot/security.py` |
| `Settings.escrow_admin_address` | `config.py:119, 235` — se parsea y nunca se lee |
| `config.__getattr__` (PEP 562) | `config.py:277-293` — ninguno de los alias tiene consumidor |
| `database.market_pool` | `bot/database.py` |

`bot/chain/__init__.py` re-exporta 25 símbolos, pero **ningún módulo de producción importa desde el paquete** (todos usan los submódulos: `from bot.chain.bodies import ...`); solo los tests hacen `from bot.chain import bodies`. La fachada no aporta nada al runtime.

### D-01 — 🟡 Duplicación real (jscpd: 3 clones, 24 líneas, 0.42 %)
- `bot/api.py:282-290` ≈ `bot/api.py:397-405` → construcción del bloque `user` (`username/full_name/points/is_admin`) repetida en `/api/session` y `/api/me`.
- `bot/api.py:532-540` ≈ `bot/api.py:552-560` → validación de admin + parseo de `market_id` en `prepare_resolve` / `prepare_cancel`.
- `bot/handlers/admin.py:315-323` ≈ `bot/handlers/admin.py:361-369` → misma línea de mercado formateada dos veces.

Duplicación **conceptual** (no detectada por jscpd, más relevante que la textual):
- **Cinco** implementaciones de la fórmula pari-mutuel (cuatro fuera de la cadena y el contrato): `bot/database.py:653` (puntos), `bot/markets.py:321` (`estimate_payout`), `bot/handlers/admin.py:174` (confirmación), `web/static/app.js:660` y `contracts/escrow.fc`. Se desincronizan en el tratamiento de la comisión (F-03).
- La fila «Abrir Mini App» está repetida en 6 teclados de `bot/keyboards.py`.
- `bot/handlers/admin.py _show_admin_panel` reimplementa filas que ya existen en `admin_predictions_keyboard()`.
- `create_nonce` (`bot/database.py`) incluye una purga de nonces que duplica `purge_nonces`.
- `bot/markets.py:305-330` y `bot/handlers/markets.py:99-112` recalculan pozos y multiplicadores por separado.

### D-02 — 🟡 Endpoints sin consumidor
| Endpoint | Estado |
| :--- | :--- |
| `DELETE /api/wallet` (`bot/api.py:657`) | Ningún botón de la Mini App lo llama; el bot usa `database.unlink_wallet` directamente |
| `POST /api/admin/markets/{id}/cancel/prepare` (`bot/api.py:665`) | Solo alcanzable desde el panel del bot, no desde la Mini App |
| `GET /api/health` (`bot/api.py:652`) | Correcto, pero no lo consume nada del repo (pensado para el orquestador) |

Se verificaron las 19 rutas contra las 12 llamadas reales de `web/static/app.js`: el resto está cubierto.

### D-03 — 🟡 Configuración sin efecto
- `ESCROW_ADMIN_ADDRESS` se documenta en `.env.example:34` y se parsea, pero nunca se usa (el admin on-chain se conoce vía el contrato y `ADMIN_IDS`).
- `BETPOLY_INSECURE_SECRET_WARNING` (`config.py:214`) se escribe en el entorno y **ningún código la lee**: la advertencia de secreto efímero nunca se muestra.
- `Settings.__getattr__` (ver 5.1).

### D-04 — 🟡 `init_db` «migra» con un literal mágico
`bot/database.py:341-343` ejecuta `UPDATE users SET points = ? WHERE points = 1000 AND ...` — el `1000` es el valor por defecto de `STARTING_POINTS` incrustado en SQL. Solo funciona si el valor histórico fue exactamente 1000 y solo para usuarios vírgenes; comprobado: con `STARTING_POINTS=5000` el saldo real de un usuario nuevo sigue siendo 1000 porque el `UPDATE` corre en el siguiente arranque, no al crearlo.

### D-05 — 🟡 `upsert_user` borra el `username` cuando Telegram no lo envía
`bot/database.py:394-401`: `username = excluded.username` con `None` sobrescribe el valor previo. Un usuario que quite su @usuario del perfil se queda sin nombre de usuario en la base.

### D-06 — 🟡 `bot/keyboards.py` con 17 % de cobertura
Es el módulo con más condicionales de presentación y el menos probado; `back_to_menu_keyboard(is_admin=...)` ignora el parámetro recibido.

---

## 6. Valores hardcodeados (H)

**Estado: H-\* cerrado en la Fase 4.** El gas por operación, el tope de apuesta,
las ventanas de creación y el TTL de firma son ahora campos de `Settings`
(`BET_GAS_NANO`, `CREATE_GAS_NANO`, `ADMIN_GAS_NANO`, `CLAIM_GAS_NANO`,
`MAX_STAKE_NANO`, `MIN_MARKET_DURATION_MIN`, `MAX_MARKET_DURATION_DAYS`,
`TX_TTL_SECONDS`) y `/api/config` los publica en TON para la Mini App. Un test
(`tests/test_economics.py::TestNoMoneyLiterals`) falla si los literales vuelven al
código. La tabla siguiente conserva el diagnóstico original.

No se encontraron secretos, mnemónicos ni direcciones hardcodeadas. Lo que sí está incrustado:

| Constante | Ubicación | Comentario |
| :--- | :--- | :--- |
| Gas: `30_000_000` (bet), `50_000_000` (create/admin), `20_000_000` (claim) | `bot/markets.py:42-45` | Única fuente; bien centralizada… |
| `30_000_000` **repetido** | `bot/handlers/help.py:59` | …salvo aquí, que lo escribe otra vez en texto |
| `0.05` por defecto en HTML y JS | `web/index.html:71`, `web/static/app.js:195` | Tercera copia del gas de creación |
| `5_000 TON` (tope de stake) | `bot/markets.py:235` | Límite de dinero en un `if` inline |
| `MIN/MAX_MARKET_DURATION` (15 min / 365 días) | `bot/markets.py:47-48` | También replicado en JS: `app.js` exige `+900 s` |
| `TX_TTL = 300` | `bot/markets.py:49` | Caducidad de firma |
| `SESSION_TTL=3600`, `MAX_INIT_DATA_AGE=86400`, desfase `300 s`, tope `10_000` | `bot/security.py` | Aceptable, pero no configurable |
| `max_depth=1`, workchain `0`, `max_age_seconds=600` | `bot/chain/ton_proof.py` | Workchain 0 fijo excluye direcciones masterchain |
| Poll 5 s, lote 100, `pages=20`, backoff 300 s | `bot/chain/indexer.py` | Parcialmente configurable vía `INDEXER_*` |
| Timeout 20 s, 3 reintentos | `bot/chain/toncenter.py` | Sin configurar |
| `limit=100/500`, `CHAIN_PAGE_SIZE=5`, `create_limiter(5, 3600)` | `bot/handlers/{admin,stats,markets,predictions}.py` | Paginación y límites dispersos |
| `https://toncenter.com/api/v2` | `config.py` | Configurable (`TON_API_URL`) |
| `app.tonkeeper.com/transfer` | `bot/markets.py` | Deeplink de firma; no configurable |
| `http://localhost:8080` | `config.py:246`, `scripts/dev_server.py:26` | Default inseguro si se despliega sin tocar el `.env` |
| Hosts CSP (`telegram.org`, `unpkg.com`, `*.e2b.app`) | `bot/api.py:626-635` | Mezcla configuración (`FRAME_ANCESTORS`) con literales |
| `1000` (puntos iniciales históricos) | `bot/database.py:341` | Ver D-04 |

---

## 7. Rendimiento y optimalidad (R)

### R-01 — 🟡 N+1 en la página de una predicción
`bot/handlers/predictions.py:104` llama a `get_prediction_pools()` dentro del bucle que recorre la página (una consulta por predicción). Con 8 filas son 8 consultas donde bastaría una sola agregación.

### R-02 — 🟡 El perfil consulta las apuestas dos veces y agrega en Python
`bot/handlers/stats.py:80-81` trae la misma información con `limit=5` y `limit=500` (y `:111` repite el patrón con `limit=100` para las apuestas on-chain) y calcula ganadas/perdidas/beneficio en un bucle. Dos consultas pesadas por `/misbets` y trabajo que SQLite haría mejor con `SUM`/`COUNT`.

### R-03 — 🟡 «Tu posición» no es una posición
`bot/handlers/stats.py:49` muestra `📍 Tu posición` pero imprime los puntos (`me['points']`), no el puesto en el ranking; además nunca se calcula ese puesto (no hay conteo de usuarios con más puntos).

### R-04 — 🟡 Índice faltante en la tabla más caliente
`onchain_bets` solo tiene `idx_onchain_bets_mkt(market_id)`. La consulta de «mis apuestas» (`WHERE bettor = ?`) hace **`SCAN` completo**, verificado con `EXPLAIN QUERY PLAN`. Añadir un índice por `bettor` es la mejora de rendimiento más rentable del proyecto.

### R-05 — 🟡 Planes de consulta que ordenan en memoria
`markets` con `ORDER BY closes_at` sobre un índice por `status` produce `USE TEMP B-TREE FOR RIGHT PART OF ORDER BY`; `_option_pools` agrupa con B-TREE temporal. Aceptable hoy, pero escala mal en cuanto crezcan los mercados.

### R-06 — 🟡 `icon.png` de 921 KB en cada carga de la Mini App
`web/static/icon.png` es 1024×1024, RGB (sin canal alfa), 921 682 bytes, y se sirve tal cual dentro de Telegram, donde el ancho útil ronda los 300 px. Es el activo más pesado del proyecto con diferencia.

### R-07 — 🟡 Complejidad alta en el núcleo de dinero
`radon`: `verify_ton_proof` E36, `_process_tx` D26, `_send_profile` D25, `validate_init_data` D22, `verify_wallet` C19, `settle_prediction` C18, `_fetch` C18, `market_view` C16, `prepare_transaction_payload` C16, `bet_amount_message` C16, `prepare_create_market` C14, `Settings.validate` C14. `verify_ton_proof` y `settle_prediction` son las que más se beneficiarían de dividirse en pasos.

### R-08 — 🟡 Encuesta cada 25 s con la pestaña visible
`web/static/app.js` mantiene `setInterval(... 25000)` sobre `/api/markets` mientras la vista de mercados está activa, además de `pollMarket` (12 × 6 s) y `pollForMarket` (15 × 5 s) tras cada acción. En un servidor de 512 MB con muchos usuarios concurrentes, es el mayor consumidor de CPU. El indexador por defecto también encuesta cada 20 s (`INDEXER_POLL_INTERVAL`).

### R-09 — 🟡 Sin caché ni compresión HTTP
`bot/api.py:669` monta estáticos sin `Content-Encoding` ni `Cache-Control`. aiohttp sí envía `Last-Modified`/`ETag` (revalidación → 304), pero cuando responde `200` manda el icono completo de 921 KB sin comprimir y sin política de caché explícita.

### R-10 — 🔵 Estilo y tipado (ruff)
`UP006`/`UP045` (tipado legacy `List`, `Optional`), `PLR2004` (números mágicos en `bodies.py`), `ARG001` (6 handlers de `api.py` con argumentos sin usar), `PLC0415` (imports dentro de funciones), `UP041`. Nada funcional; es deuda de estilo coherente en todo el repo.

---

## 8. Testing y dependencias (T)

### T-01 — ✅ La capa de UX de Telegram no tiene ni un test
Cobertura por módulo: **todos** los `bot/handlers/*` al **0 %**, `keyboards.py` 17 %, `formatting.py` 48 %, `toncenter.py` 30 %, `scripts/dev_server.py` 0 %, `main.py` nunca se importa. Total 55 %.
Es exactamente donde están F-01, F-02, F-04 y S-06: los defectos se concentran en el código sin cobertura.
**Resuelto (Fase 5):** `tests/telegram_fakes.py` + `tests/test_handlers_navigation.py` + `tests/test_handlers_flows.py` llevan `bot/handlers/*` al **85 %** y `bot/keyboards.py` al **86 %**; CI falla por debajo del 70 % en esa capa. `main.py` (arranque y `build_application`) sigue sin cubrir: se aborda en la Fase 7.

### T-02 — 🔵 Deriva de dependencias
| Paquete | Pin | Última | Estado |
| :--- | :--- | :--- | :--- |
| python-telegram-bot | 21.6 | 22.8 | ⚠️ una major por detrás |
| aiohttp | 3.14.5 | 3.14.5 | ✅ |
| aiosqlite | 0.22.1 | 0.22.1 | ✅ |
| python-dotenv | 1.2.4 | 1.2.4 | ✅ |
| pytoniq-core | 0.2.1 | 0.2.1 | ✅ |
| PyNaCl | 1.6.2 | 1.6.2 | ✅ |
| @ton/core / @ton/sandbox / @ton/ton / func-js | 0.63.1 / 0.45.0 / 16.3.0 / 0.11.0 | idénticas | ✅ |
| @tonconnect/ui | 5.0.1 vía CDN | 5.0.1 | ⚠️ sin pin en `package.json` ni integridad |
| @ton/crypto | **no declarado** | 3.3.0 (transitivo) | ⚠️ `deploy.js` lo importa directamente |

### T-03 — 🔵 `pip-audit` sobre `requirements.txt`: limpio. `npm audit`: 0 vulnerabilidades. `bandit`: 0 HIGH, 3 MEDIUM (`B608` en `bot/database.py:523` — f-string SQL que solo interpola una lista de `?` generada; los valores van parametrizados, es un **falso positivo**; `B104` en `config.py:244` y `dev_server.py:32` — bind `0.0.0.0` deliberado), 4 LOW.

### T-04 — 🔵 `.gitignore` incompleto para artefactos de test
`.coverage` no está ignorado (aparece como archivo sin seguimiento en `git status`); tampoco `htmlcov/`, `coverage.xml` ni `.mypy_cache/`. `preview.db*` sí está cubierto (`*.db`, `*.db-shm`, `*.db-wal`).

---

## 9. Inconsistencias de textos y UX

| # | Hallazgo | Ubicación |
| :--- | :--- | :--- |
| ~~U-01~~ | ✅ `/start` mostraba la dirección en raw `0:hex`. Resuelto en Fase 5: se pinta en *friendly* con `to_friendly` | `bot/handlers/start.py` |
| ~~U-02~~ | ✅ El ejemplo de fecha era `25/12/2025`. Resuelto en Fase 5: `example_close()` genera la fecha a `+7 días` | `bot/handlers/predictions.py` |
| ~~U-03~~ | ✅ Estado vacío coherente. Resuelto en Fase 5: remite a la Mini App y a `/predicciones` | `bot/handlers/markets.py` |
| ~~U-04~~ | ✅ El panel de admin ahora lista exactamente las predicciones de su teclado (test con 10) | `bot/handlers/admin.py` |
| ~~U-05~~ | ✅ «Pago por punto apostado» mezcla puntos con formato de multiplicador `x…` en el mismo bloque. Resuelto en Fase 4: la confirmación habla de «puntos por punto apostado» y del reintegro cuando no hay ganadores | `bot/handlers/admin.py` |
| ~~U-06~~ | ✅ `/help` es consciente del rol: `/admin` solo aparece para administradores | `bot/handlers/help.py` |

---

## 10. Prioridad sugerida (solo a efectos de lectura)

| Prioridad | Hallazgos | Motivo |
| :--- | :--- | :--- |
| **1. Dinero / integridad de datos** | S-01, S-02, S-03, S-07 | Pérdida o bloqueo de fondos y de eventos on-chain |
| **2. Abuso desde Internet** | S-04, F-12 | Eludir límites y superficie de suministro |
| **3. Funcionalidad visible rota** | S-05, S-06, F-01, F-02, F-04 | El usuario no puede completar lo que la UI promete |
| **4. Coherencia económica mostrada** | F-03, U-05 | Lo que se promete frente a lo que paga el contrato |
| **5. Mantenibilidad** | D-01 a D-06, H-*, 5.1 | Deuda con coste creciente |
| **6. Rendimiento** | R-01 a R-09 | Importante en el host objetivo de 512 MB |
| **7. Proceso** | F-10, F-11, T-01, T-04 | Evitar que todo lo anterior vuelva |

---

## 11. Fuera de alcance / no verificado

- **Auditoría del contrato FunC línea por línea:** se revisó su superficie pública, almacenamiento, códigos de salida y comportamiento en sandbox (22 comprobaciones), pero no se hizo un análisis formal de vulnerabilidades ni de invariantes del TVM.
- **Pruebas contra mainnet/testnet reales:** el indexador, `ton_proof` y los deeplinks se validaron con clientes falsos y con la suite local, no con transacciones reales firmadas.
- **`ton_proof` con wallets reales** (Tonkeeper/MyTonWallet): la verificación criptográfica está cubierta por tests, pero no se probó el handshake completo con un cliente TON Connect en dispositivo.
- **Carga y concurrencia:** no se hizo prueba de estrés; las observaciones de rendimiento (R-08, R-09) son estáticas y de plan de consultas.
- **Despliegue real:** no se ejecutó `deploy.js` contra la red (solo dry-run).
