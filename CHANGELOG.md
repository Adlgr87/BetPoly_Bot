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
