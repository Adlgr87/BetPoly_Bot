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
