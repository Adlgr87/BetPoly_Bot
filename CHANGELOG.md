# Changelog

Registro de cambios por fase. Cada entrada responde a tres preguntas: **qué se
arregló**, **qué se decidió** y **qué quedó pendiente**. El plan completo vive en
`PLAN_IMPLEMENTACION.md` y los hallazgos originales en `AUDITORIA.md`.

---

## Fase 0 — Red de seguridad

**Fecha:** 2026-10-23

### Arreglado
- **F-10 · No había CI.** `.github/workflows/ci.yml` con cuatro jobs: `lint`
  (ruff), `test` (pytest + cobertura con umbral), `contract` (compilación,
  verificación de hash reproducible, sandbox TVM y `npm audit`) y `security`
  (`pip-audit`).
- **F-11 · Los tests no eran instalables.** `requirements-dev.txt` separa las
  dependencias de desarrollo de las de runtime.
- **T-04 · Artefactos sin ignorar.** `.gitignore` cubre `.coverage`, `htmlcov/`,
  `coverage.xml`, `.mypy_cache/` y `.pytest_cache/`.
- **R-10 (parcial) · Reglas de ruff sin fijar.** `pyproject.toml` define
  explícitamente `select`/`ignore`, así el lint no depende de los defaults de la
  versión del linter. Se corrigieron los 14 hallazgos restantes (imports
  desordenados, `TimeoutError` moderno, `contextlib.suppress`, formato `%`).

### Añadido
- `tests/test_architecture.py`: guardarraíl de capas (P7). Verifica que la API no
  importe Telegram, que `bot/chain` no conozca la mensajería, que los handlers no
  toquen los clientes de cadena y que la persistencia no dependa de presentación.
  Las violaciones preexistentes son un trinquete documentado.
- `tests/test_wiring.py`: guardarraíl de conectividad. Detecta rutas de API sin
  consumidor, `callback_data` sin handler, handlers sin botón que los emita,
  builders de mensajes on-chain inalcanzables y comandos prometidos en `/help`
  que no existen. Los huérfanos conocidos están en un baseline con su fase de
  resolución, y el baseline solo puede encogerse.
- `tests/test_dev_server.py`: ejecuta el sembrado del preview local, que hasta
  ahora no tenía ninguna prueba (y por eso llamaba a una función ya retirada).

### Decidido
- El umbral de cobertura se fija en el valor actual (56 %) y **solo puede subir**
  en las siguientes fases.
- El lint arranca con un conjunto moderado de reglas (`E4,E7,E9,F,I,B,C4,SIM,UP`)
  en lugar de `ALL`, para que sea bloqueante desde el primer día.

### Pendiente
- Activar más reglas de ruff y normalizar el tipado legacy (`UP006/UP045`) en la
  Fase 7.
- Protección de rama en `main` (acción manual en GitHub; el workflow ya existe).

---

## Fase 1 — Integridad del dinero on-chain

**Fecha:** 2026-10-23

### Arreglado
- **S-01 · El indexador perdía eventos para siempre.** El cursor solo avanza
  sobre transacciones aplicadas con éxito; un fallo se reintenta en el acto y, si
  persiste, la transacción se aparca en `chain_tx_failures` (dead-letter) con
  backoff exponencial para que una transacción envenenada no detenga el flujo.
  El estado se expone en `/api/indexer` y en el panel de administración.
- **S-02 · El marcador ganaba a los datos reales.** `upsert_market` se separó en
  `insert_market_placeholder` y `enrich_market_from_event`. El enriquecimiento
  solo rellena campos vacíos o de marcador y nunca sobrescribe un mercado que ya
  tiene metadatos de evento (`metadata_source`).
- **S-07 · Desvincular una wallet la reservaba para siempre.** `wallet_links` se
  reconstruye con clave subrogada e índices únicos parciales (`address` y
  `user_id` solo cuando `is_active = 1`). La migración es automática y conserva
  el historial; una dirección desvinculada puede vincularse desde otra cuenta.
- **F-05 · Se podía cancelar un mercado vivo por error.** `prepare_cancel` exige
  `force=True` si el mercado sigue abierto y ya tiene apuestas. El bot pide una
  confirmación explícita; la API HTTP nunca envía `force`.
- **F-06 · Carrera entre resolución y apuestas.** Preparar una resolución o una
  cancelación marca `markets.pending_resolve_at`: durante 15 minutos se rechazan
  apuestas nuevas (`market_resolving`) y la Mini App oculta el formulario. La
  marca se limpia cuando el evento on-chain llega.

### Decidido
- **ADR-02 aplicado:** dead-letter en lugar de outbox completa. Reintento en el
  mismo ciclo (2 reintentos), 5 fallos acumulados antes de aparcar y reintento
  programado con retroceso 60 s → 5 min → 15 min → 1 h.
- **Reutilización en vez de código nuevo:** `database.market_pool` (antes sin
  uso) alimenta la política de cancelación.
- Los reintentos son seguros por construcción: `chain_txs` garantiza aplicación
  *at-most-once* y cada reintento libera su marca antes de volver a intentarlo.

### Añadido
- Tabla `chain_tx_failures` + funciones de registro, listado, programación y
  limpieza, con índice por `next_retry_at`.
- Columnas `markets.metadata_source` y `markets.pending_resolve_at` con migración
  para bases existentes.
- 45 pruebas nuevas (263 → 308 en total): recuperación tras fallo, transacción envenenada, propiedad
  «con fallos transitorios el espejo acaba igual», marcador vs. evento, migración
  del esquema antiguo de wallets, política de cancelación y ventana de resolución.

### Pendiente
- Reconciliación manual desde el panel de admin (hoy es automática por backoff).
- Fase 2: operaciones de administración del contrato (`withdraw_fees`,
  `set_paused`, `set_fee`, `change_admin`), que siguen sin interfaz.

---

## Fase 2 — Administración del contrato (S-03, D-03)

**Fecha:** 2026-10-23

### Arreglado
- **S-03 · Cuatro operaciones de administración sin interfaz.**
  `withdraw_fees`, `set_fee`, `set_paused` y `change_admin` existían en el
  contrato y en `bot/chain/bodies.py`, pero ningún flujo dejaba firmarlas.
  Ahora se preparan sin firmar en `bot/markets.py`
  (`prepare_withdraw_fees`, `prepare_set_fee`, `prepare_set_paused`,
  `prepare_change_admin`) y se firman siempre en la wallet del administrador,
  desde el bot o desde la Mini App.
- **D-03 · Estado del contrato invisible.** `bot/chain/toncenter.py` gana
  `run_get_method` (con normalización del stack: hex/decimal → `int`) y
  `bot/chain/escrow_state.py` compone los seis getters en un `EscrowState`.
- **`ESCROW_ADMIN_ADDRESS` no se usaba.** Ahora se compara con la wallet
  vinculada del operador y se rechaza la preparación (`wallet_not_admin`) antes
  de firmar cuando no coincide. Sigue siendo informativo: los permisos los
  decide el contrato, no la configuración.

### Añadido
- `EscrowStateCache`: TTL de 30 s con *stale-while-error*; si la cadena falla se
  sirve la última lectura buena marcada como `stale` en lugar de ocultar el
  estado. `reset()` y `force=True` para pruebas y botón «Actualizar».
- Panel **⚙️ Contrato escrow** en `/admin` con el estado on-chain, aviso si la
  wallet no es la administradora y confirmación explícita antes de pausar o
  cambiar de administrador (el valor vive en el estado de la conversación, no
  en `callback_data`, que tiene un límite de 64 bytes).
- Endpoints `GET /api/admin/contract` y
  `POST /api/admin/contract/{withdraw,set-fee,set-paused,change-admin}/prepare`,
  más la tarjeta equivalente en la pestaña *Admin* de la Mini App.
- `bot/parsing.py`: `parse_nano` y `parse_fee_bps` como única fuente de parseo
  monetario (la API los reutiliza en lugar de su copia local) y
  `markets.normalize_admin_address` para validar `change_admin`.
- `MessageBody.admin_fee_bps` y `MessageBody.paused_flag` para que las pruebas
  lean los payloads de administración sin reinterpretar `amount`/`flag`.
- 67 pruebas nuevas (308 → 375): `runGetMethod`, snapshot y caché, los cuatro
  preparadores con sus validaciones previas a la firma, los endpoints HTTP y un
  caso de extremo a extremo en el sandbox TVM que transfiere la administración
  y comprueba que el admin anterior pierde el control (22 → 24 checks).

### Decidido
- **El bonus de "doble confirmación" se resuelve con dos barreras:** el botón
  «Confirmar» en el bot o `confirm()` en la Mini App, y después la firma en la
  wallet. El backend nunca difunde nada por su cuenta.
- **`withdraw_fees` se valida contra `fees` leídas on-chain** (`0 < importe ≤
  fees`), no contra saldos del espejo: los fondos de los usuarios no son
  retirables ni siquiera si el operador se equivoca.
- **El flag de pausa se lee siempre de la cadena** antes de invertirlo, para que
  dos operadores no se pisen con acciones contrarias.
- **Vulture entra en CI** (`--min-confidence 80`) después de retirar cuatro
  parámetros `edit` muertos en los handlers; el umbral de cobertura sube a 59 %.

### Pendiente
- Fase 3 (seguridad y aislamiento de la API) antes de la Fase 4.
- El panel del contrato no muestra aún el historial de transacciones de
  administración; hoy se consulta en el explorador con la dirección del escrow.

---

## Fase 3 — Endurecimiento de la API (S-04, S-05, D-01, D-02)

**Fecha:** 2026-10-23

### Arreglado
- **S-04 · El limitador de tasa se eludía falsificando `X-Forwarded-For`.**
  `bot/api.py:_client_ip` ya no confía en la cabecera: solo se honra con
  `TRUST_PROXY=true` **y** si la conexión llega desde una red de
  `TRUSTED_PROXIES`; entonces se recorre la lista de derecha a izquierda y se
  devuelve el primer salto que no sea un proxy de confianza. La prueba de la
  auditoría (40 `POST /api/session` rotando la cabecera) ahora termina en `429`.
- **S-04 · El diccionario del limitador crecía sin control.** `RateLimiter` usa
  ahora `OrderedDict` con **evicción LRU real** (`max_keys`, 10 000 por defecto)
  en lugar de borrar solo las cubetas vacías. Las respuestas `429` incluyen
  `Retry-After` en segundos.
- **S-05 · El botón «Reintentar» no hacía nada.** Era un `onclick` inline que la
  propia CSP del servidor bloquea. Pasa a `data-reload="1"` con delegación de
  eventos (el patrón que ya usa el resto de la UI).
- **D-02 · Dos endpoints sin consumidor.** La Mini App incorpora «Desvincular
  wallet» (`DELETE /api/wallet`) y «Cancelar y reembolsar» por mercado
  (`POST /api/admin/markets/{id}/cancel/prepare`), ambos con confirmación. El
  baseline de `test_wiring` queda solo con `/api/health`, justificado.

### Añadido
- **Backstop global por endpoint** (300/min en `/api/session` y
  `/api/wallet/nonce`, 120/min en `/api/wallet/verify`, `/api/markets/prepare` y
  `/api/wallet`): la clave es la ruta, no el cliente, así que rotar IPs o
  sesiones no lo esquiva. Los limitadores se crean por aplicación
  (`create_app(backstop_limits=…)`) para que una app de prueba no herede
  contadores ajenas.
- **Configuración que no puede quedar muda:** `TRUST_PROXY` y `TRUSTED_PROXIES`
  se validan al arrancar; confiar sin lista (o declarar lista sin confiar)
  lanza `ConfigError` en vez de no hacer nada, que es la lección de D-03.
- **D-01 · Menos duplicación en la API:** `_user_payload`, `_market_id` y
  `_require_admin` sustituyen los clones de `/api/session` ↔ `/api/me` y de los
  cuatro handlers con `market_id`/chequeo de admin; el mapeo de errores de
  dominio (`forbidden`→403, `chain_unavailable`→503) vive en un único sitio
  (`_MARKET_ERROR_STATUS`), lo que además elimina `_escrow_admin_error`. En los
  handlers del bot, `_show_cancel_prepared` unifica las dos cancelaciones.
- Pruebas nuevas: `tests/test_config.py` (proxies y validaciones de arranque) y
  `tests/test_web_assets.py` (cero manejadores inline + la CSP que se publica de
  verdad), más los casos de IP real, backstop, `Retry-After`, desvincular wallet
  y cancelar desde la Mini App.

### Decidido
- **La cabecera no se honra por defecto.** Un despliegue directo a Internet no
  necesita saber nada de proxies; detrás de uno, se declara explícitamente. El
  valor por defecto seguro evita el fallo silencioso.
- **No borrar endpoints, cablearlos.** `DELETE /api/wallet` y el `cancel/prepare`
  de admin se exponen en la Mini App; `/api/health` se documenta como endpoint de
  sondas y queda como único huérfano justificado.
- **Un techo por endpoint, no por IP.** El backstop es una segunda barrera
  independiente de la identidad; no sustituye al límite por usuario, que sigue
  dando mensajes más precisos en el uso normal.

### Pendiente
- Fase 4: economía única y coherencia de lo mostrado (F-03, H-*, U-05).
- El backstop es en memoria y por proceso: con varias réplicas cada una lleva su
  propia cuenta (aceptable en el host único de 512 MB; ver ADR-05).

---

## Fase 4 — Economía única y coherencia de lo mostrado (F-03, H-*, U-05)

**Fecha:** 2026-10-23

### Arreglado
- **F-03 · Lo que se muestra y lo que se paga salían de fórmulas distintas.** La
  fórmula pari-mutuel vive ahora **solo** en `bot/economics.py`
  (`distributable`, `fee_of`, `payout`, `multiplier`, `preview_payout`). La
  consumen `bot/database.py` (liquidación de puntos), `bot/markets.py`
  (`estimate_payout`, `market_view.multiplier`, `quote_payout`) y
  `bot/handlers/markets.py`. Con un pozo de 4 TON repartido 3:1 y 2 % de comisión
  el bot ya muestra `3.92x` / `1.307x` en vez de `4.00x` / `1.333x`.
- **F-03 · La Mini App calculaba el «Pago potencial» por su cuenta.** Ahora pide
  `GET /api/markets/{id}/quote?option=N&stake_ton=X` (con *debounce* de 250 ms y
  guarda de secuencia para descartar respuestas viejas) y pinta el
  `payout_ton` del servidor; mientras responde, «Calculando…», y si falla, «—».
  El frontend no calcula ningún importe. `market_detail` publica además `fee_bps`
  y `fee_percent` por mercado.
- **U-05 · «Pago por punto apostado».** La confirmación de una apuesta de puntos
  ya no mezcla puntos con formato de multiplicador: dice «N puntos por punto
  apostado» y, cuando nadie acertó, «sin ganadores (se reembolsa todo)».

### Añadido
- **H-* · Los importes son configuración, no literales.** `Settings` gana
  `MAX_STAKE_NANO` (5000 TON), `BET_GAS_NANO`, `CREATE_GAS_NANO`,
  `ADMIN_GAS_NANO`, `CLAIM_GAS_NANO`, `MIN_MARKET_DURATION_MIN` (15),
  `MAX_MARKET_DURATION_DAYS` (365) y `TX_TTL_SECONDS` (300), con dos
  validaciones de arranque (tope ≥ mínimo, duración mínima < máxima).
  `.env.example` y `README.md` los documentan.
- **`/api/config` es la única copia para la UI:** publica `max_stake_ton`,
  `min_market_duration_min`, `max_market_duration_days`, `bet_gas_ton`,
  `create_gas_ton`, `admin_gas_ton` y `claim_gas_ton`. `help.py`, el formulario de
  creación y la hoja de apuesta leen de ahí; `web/index.html` deja de traer
  `0.05` y `app.js` deja de exigir `+900 s`.
- **`contracts/vectors/economics.json`:** vectores compartidos que ejecutan
  `tests/test_economics.py` y `contracts/tests/escrow.test.js` (25 comprobaciones
  en el sandbox TVM). Si Python o el contrato se desvían, falla el otro.
- **Guardarraíl contra la reincidencia:** `tests/test_economics.py::TestNoMoneyLiterals`
  falla si `30_000_000`, `50_000_000`, `20_000_000`, `5_000 * NANO` o `0.05`
  reaparecen en `bot/`, `web/`, `scripts/` o `main.py`, y
  `tests/test_web_assets.py::TestNoLocalEconomics` falla si `app.js` vuelve a
  mencionar la fórmula o deja de pedir el *quote*.
- Pruebas nuevas: `tests/test_economics.py` (aritmética, truncado, pozo vacío,
  vectores compartidos, literales), `/quote` en `tests/test_api.py` y
  `tests/test_markets_service.py`, límites publicados y valores por defecto en
  `tests/test_config.py`.

### Decidido
- **El backend es el único que calcula; si no hay *quote*, no hay promesa.** El
  plan permitía un respaldo en JS con `fee_bps`; se descarta porque duplicaría la
  fórmula (P6) y porque `tests/test_architecture.py` prohíbe `fee_bps` en
  `app.js`. Un «—» honesto es mejor que un número inventado.
- **Los vectores son el contrato ejecutable.** Mantener la fórmula en dos
  lenguajes es inevitable (FunC y Python); lo que se elimina es el tercer y
  cuarto sitio, y un JSON compartido detecta la deriva en lugar de confiar en la
  revisión manual.
- **El redondeo sigue siendo `floor` + resto al mayor apostador.** Los pagos suman
  exactamente el pozo distribuible; el test lo verifica fila a fila.
- **Se mantiene `RESOLVE_WINDOW_SECONDS` (15 min) como constante de dominio**, no
  de configuración: es una regla del producto (no se apuesta en el último cuarto
  de hora), no un parámetro de despliegue.

### Pendiente
- Fase 5: capa Telegram (F-01, F-14, F-02, F-04, S-06, U-01…U-04, U-06).
- F-04 (notificaciones), F-05 (`prepare_cancel` en mercado abierto) y F-06
  (bloqueo optimista de resolución) siguen abiertos, como marca el plan.

---

## Fase 5 — Capa Telegram: navegación, entrada y notificaciones

**Fecha:** 2026-10-23

### Arreglado
- **F-01 · El botón «🔧 Admin» desaparecía al navegar.** Todas las pantallas
  resuelven el rol con `common.menu_for(update)` / `common.back_keyboard(update)`
  y `back_to_menu_keyboard(is_admin)` deja de ignorar su parámetro. `/help` y el
  ranking reciben el mismo tratamiento. Un test recorre 13 pantallas con un
  admin y con un usuario normal.
- **F-14 · El menú de bienvenida se construía sin rol.** Mismo helper: el primer
  `/start` ya muestra u oculta la fila de administración según quién escribe.
- **S-06 · El importe apostado se corrompía al parsearlo.** `bot/parsing.py`
  gana `parse_points`: los separadores de miles (`1.000`, `1,000`, `1 000`) son
  miles, y un decimal (`10.5`) es un **error explícito** («los puntos son
  enteros: si querías 10, escribe 10») en vez de convertirse en `105` o `15`.
  Cero, negativos y cantidades por encima del saldo también se rechazan. La
  confirmación muestra el pago potencial con `economics.preview_payout`, así que
  el número que se promete es el que liquida el reparto.
- **F-02 · El modo grupo estaba anunciado pero no funcionaba** (opción A del
  plan). La conversación de creación pasa a `per_chat=True` y el paso del
  importe acepta mensajes de grupo **solo** si son respuesta al mensaje del bot
  (`amount_prompt_id`). Un mensaje suelto en el grupo se ignora en silencio.
- **F-04 · Nadie avisaba de una resolución.** Tabla de salida `notifications`
  con `UNIQUE (user_id, kind, ref_id)`, productores transaccionales
  (`settle_prediction`, `update_market_settled`, `update_market_cancelled`) y
  repartidor `job_dispatch_notifications` cada 30 s en lotes de 20 con backoff
  exponencial (60 s → 1 h, 5 intentos). Se avisa de lo accionable: victoria,
  reembolso o pago pendiente en el contrato; una apuesta perdida no genera ruido.
- **U-01 · `/start` mostraba la wallet en `0:hex`.** Ahora en formato *friendly*.
- **U-02 · El ejemplo de fecha de cierre era `25/12/2025`,** una fecha ya pasada
  que el propio validador rechazaría. El ejemplo es dinámico (`+7 días`).
- **U-03 · El estado vacío de `/mercados` remitía a `/mercados`.** Ahora invita a
  la Mini App o a `/predicciones`.
- **U-04 · El panel de admin listaba `predictions[:8]`** mientras el teclado
  renderizaba todas: se listan todas las que el panel ofrece.
- **U-06 · `/help` prometía `/admin` a todo el mundo.** El texto depende del rol.

### Añadido
- `tests/telegram_fakes.py`: dobles de `Update`/`Message`/`CallbackQuery`/`Bot`
  que registran texto y teclados. Es lo que permite probar 1 900 líneas de
  handlers sin red.
- `tests/test_handlers_navigation.py` (rol en cada pantalla),
  `tests/test_handlers_flows.py` (apuestas, creación, perfil, wallet, admin,
  contrato), `tests/test_parsing.py` (tabla S-06) y `tests/test_notifications.py`
  (dedupe, backoff, «Telegram caído no bloquea la cadena», escape de HTML).
- CI: nuevo umbral de cobertura **global 80 %** (antes 60) y otro específico de
  **70 %** para `bot.handlers` + `bot.keyboards`.
- `example_close()` en `predictions.py`: una única fuente para el ejemplo de
  fecha del formulario y de los mensajes de error.

### Decidido
- **Avisar solo de lo accionable.** Notificar cada derrota convertiría el bot en
  ruido; `/misbets` sigue siendo la fuente de verdad del historial. El outbox
  guarda el resultado completo por si más adelante se quiere un resumen diario.
- **El aviso se escribe en la transacción del resultado.** Si la transacción
  falla, no hay aviso; si se repite, no hay duplicado. Es la única forma de
  garantizar «ni de más ni de menos» sin un segundo proceso que reconcilie.
- **El indexador no sabe nada de Telegram.** Escribe filas; el repartidor las
  envía. Así un `403` de un usuario que bloqueó el bot no puede frenar la
  indexación (hay un test que lo fija).
- **En grupo, silencio ante lo ambiguo.** Si el mensaje no responde al prompt, no
  se responde nada: intentar adivinar cuál de los tres «100» del grupo es una
  apuesta sería peor que pedir una respuesta explícita.
- **Se corrigió un test intermitente** (`test_tampered_token`): reemplazaba los
  dos últimos caracteres de la firma, que solo llevan 12 bits significativos, y
  de vez en cuando decodificaba a los mismos bytes. Ahora altera el primero.

### Pendiente
- Fase 6: rendimiento y activos (R-01…R-09), incluido el icono de 921 KB.
- Fase 7: mantenibilidad, dependencias y tooling del contrato.
- `main.py` sigue por debajo del 50 % de cobertura: `post_init`,
  `build_application` y `run` necesitan un `Application` real o un doble más
  profundo; se aborda en la Fase 7 junto con la limpieza de código muerto.

---

## Fase 6 — Rendimiento y activos

**Fecha:** 2026-10-23

### Arreglado
- **R-01 · N+1 en las listas.** `get_open_predictions()` trae el pozo con un
  `LEFT JOIN` + `GROUP BY` en vez de una consulta por fila. `list_markets()`
  pide opciones y pozos de toda la página en dos consultas: la página del
  listado hace **3 consultas con 1 mercado o con 50** (antes, 1 + 3·N).
- **R-02 · El perfil agregaba en Python.** `get_user_stats(user_id)` calcula con
  `SUM`/`COUNT` totales, ganadas, perdidas, reembolsos, pendientes y neto. Se
  elimina la consulta de 500 filas que solo servía para contar.
- **R-03 · «Tu posición» mostraba los puntos, no el puesto.** `ranking_position()`
  cuenta cuántos usuarios están por delante (desempate por `user_id`) y el
  mensaje dice el puesto real.
- **R-04 · `SCAN` en `onchain_bets`.** Índice `idx_onchain_bets_bettor(bettor, id DESC)`.
- **R-05 · Dos `TEMP B-TREE`.** Índices `idx_markets_status_close(status, closes_at,
  market_id DESC)` e `idx_onchain_bets_mkt_opt(market_id, option)`.
- **R-06 · Icono de 921 KB.** `web/static/icon.png` pasa a 512×512 con paleta de
  64 colores: **79 408 B** (−91 %). `scripts/build_icon.py` lo regenera y falla
  si supera los 80 KB.
- **R-08 · Sondeo fijo cada 25 s.** Backoff 15/30/60 s que se reinicia con la
  actividad del usuario o un cambio real; `/api/markets` responde `ETag` y `304`
  con cuerpo vacío.
- **R-09 · Sin caché ni compresión.** Los activos salen versionados con
  `?v=<huella>` y `Cache-Control: public, max-age=31536000, immutable`; JS/CSS
  se sirven con gzip memoizado por proceso y `ETag` para el resto.

### Añadido
- `tests/test_performance.py`: planes de consulta (`EXPLAIN QUERY PLAN` sin
  `SCAN` ni `TEMP B-TREE`), conteo de consultas con `aiosqlite` instrumentado y
  equivalencia de los agregados del perfil con el cálculo anterior.
- `tests/test_assets_caching.py`: caché inmutable, gzip, `304`, rechazo de rutas
  fuera del directorio estático, `ETag` de `/api/markets` y presupuesto de
  arranque (**93.3 KB** comprimidos frente a 350 KB) como test.
- `scripts/build_icon.py`: generación reproducible del icono con tope de tamaño.
- CI: el umbral de cobertura global sube de 80 % a **83 %** (medido: 84.8 %).

### Arreglado (CI)
- **`npm audit` rompía el build por un advisory sin parche.** Un aviso nuevo
  contra `zod` (<4.6.6), arrastrado por `@ton/ton` (devDependency que solo usa
  `scripts/deploy.js`), dejó el job del contrato en rojo sin que el código
  cambiara. En vez de bajar el nivel de exigencia, `contracts/scripts/audit.js`
  aplica `npm audit` con `contracts/audit-allowlist.json`: una excepción lleva
  motivo, versión afectada y **caducidad**; una excepción caducada, sin motivo o
  que ya no aparece falla el build, y cualquier advisory alto/crítico no listado
  también. `contracts/tests/audit.test.js` (9 checks) fija esas reglas.

### Decidido
- **El `ETag` va sobre el contenido, no sobre la hora.** Dos peticiones iguales
  devuelven el mismo `304` incluso en otro proceso; con `Last-Modified` una
  republicación sin cambios invalidaría la caché de todos.
- **Gzip en memoria, una vez por proceso.** Comprimir en cada petición gastaría
  CPU en el host de 512 MB; tres activos caben de sobra en RAM.
- **El presupuesto de activos es un test, no una convención.** Un PNG de 900 KB
  vuelve a fallar el CI aunque nadie mire el tamaño.
- **Sondeo con `setTimeout` encadenado.** `setInterval` encola refrescos si la
  red va lenta; encadenar garantiza una petición en vuelo como máximo.

### Pendiente
- Fase 7: mantenibilidad, dependencias y tooling del contrato.
- R-07 (complejidad de `verify_ton_proof` y `settle_prediction`) sigue abierto:
  no se tocó en esta fase para no mezclar refactor con rendimiento.
