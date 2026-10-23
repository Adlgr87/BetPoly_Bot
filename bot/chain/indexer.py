"""Read-only indexer for the non-custodial escrow contract.

How it works
------------
* **Bets and claims** happen on-chain as internal messages to the escrow
  contract.  The indexer reads them from a public API and stores a mirror.
  Nothing is emitted by the contract for these, so indexing costs no extra gas.
* **Market lifecycle** (create / resolve / cancel) is confirmed through the
  small event messages the contract sends to its configured indexer address,
  because only the contract knows the assigned ``market_id``.
* Every transaction is applied **at most once** (``chain_txs`` primary key) and
  the cursor is only advanced after the batch is committed, so restarts and
  duplicate polls are safe.
* The mirror is never authoritative for balances: the contract remains the only
  judge.  A missing row is repaired on the next poll.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from bot import database
from bot.chain.addresses import AddressError, normalize_address, same_address
from bot.chain.bodies import (
    EVENT_MARKET_CANCELLED,
    EVENT_MARKET_CREATED,
    EVENT_MARKET_RESOLVED,
    OP_CANCEL_MARKET,
    OP_CLAIM_PAYOUT,
    OP_PLACE_BET,
    OP_RESOLVE_MARKET,
    BodyError,
    parse_event,
    parse_message_body,
)
from bot.chain.toncenter import ChainTransaction, TonApiError, TonCenterClient

logger = logging.getLogger(__name__)

CURSOR_NAME = "escrow"

# A transaction is retried inline before the poll gives up on it.  Retrying is
# always safe: ``chain_txs`` guarantees at-most-once application, so a retry
# only re-applies what is missing.
MAX_INLINE_RETRIES = 2
# Total failures tolerated before the transaction is parked in the dead-letter
# table and the cursor is allowed to move past it (so one poison transaction
# cannot stall the whole stream).
MAX_TX_ATTEMPTS = 5
RETRY_BACKOFF_SECONDS = (60, 300, 900, 3600, 3600)


@dataclass
class IndexResult:
    fetched: int = 0
    processed: int = 0
    markets_created: int = 0
    markets_enriched: int = 0
    bets: int = 0
    resolutions: int = 0
    cancellations: int = 0
    claims: int = 0
    skipped: int = 0
    parked: int = 0
    errors: List[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(
            self.markets_created
            or self.markets_enriched
            or self.bets
            or self.resolutions
            or self.cancellations
            or self.claims
        )


def _out_msg_body(msg: Dict[str, Any]) -> Optional[str]:
    data = msg.get("msg_data") or {}
    body = data.get("body")
    return body if isinstance(body, str) and body else None


def _out_msg_destination(msg: Dict[str, Any]) -> Optional[str]:
    dest = msg.get("destination")
    if not dest:
        return None
    try:
        return normalize_address(dest)
    except AddressError:
        return None


def _out_msg_value(msg: Dict[str, Any]) -> int:
    try:
        return int(str(msg.get("value") or 0))
    except ValueError:
        return 0


@dataclass
class _Ordered:
    lt: int
    tx: ChainTransaction


class EscrowIndexer:
    """Polls the escrow contract and mirrors it into SQLite."""

    def __init__(
        self,
        client: TonCenterClient,
        escrow_address: str,
        *,
        poll_interval: int = 20,
        batch: int = 50,
        backfill_pages: int = 5,
        min_stake_nano: int = 1,
    ) -> None:
        self.client = client
        self.escrow_address = normalize_address(escrow_address)
        self.poll_interval = max(5, int(poll_interval))
        self.batch = max(1, min(int(batch), 100))
        self.backfill_pages = max(0, int(backfill_pages))
        self.min_stake_nano = max(0, int(min_stake_nano))
        self._task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()
        self.last_result = IndexResult()

    # ------------------------------------------------------------------
    # polling
    # ------------------------------------------------------------------
    async def run_once(self) -> IndexResult:
        result = IndexResult()
        try:
            await self._poll(result)
        except (TonApiError, AddressError) as exc:
            logger.warning("Indexer poll failed: %s", exc)
            result.errors.append(str(exc))
            await database.record_indexer_run(None, str(exc), result.processed)
        except Exception as exc:  # noqa: BLE001 - never kill the loop
            logger.exception("Unexpected indexer failure")
            result.errors.append(str(exc))
            await database.record_indexer_run(None, f"unexpected: {exc}", result.processed)
        self.last_result = result
        return result

    async def _poll(self, result: IndexResult) -> None:
        await self._rewind_for_retries()
        cursor_lt, cursor_hash = await database.get_cursor(CURSOR_NAME)
        txs = await self._fetch(cursor_lt, cursor_hash)
        result.fetched = len(txs)
        if not txs:
            await database.record_indexer_run(cursor_lt, None, 0)
            return

        pending = {row["tx_hash"] for row in await database.list_chain_tx_failures(limit=200)}
        # The cursor only moves over transactions that were applied for real.
        last_good_lt, last_good_hash = cursor_lt, cursor_hash

        for tx in txs:
            if cursor_lt and (tx.lt < cursor_lt or (tx.lt == cursor_lt and tx.tx_hash == cursor_hash)):
                continue

            error = await self._process_with_retries(tx, result)
            if error is None:
                result.processed += 1
                if tx.tx_hash in pending:
                    await database.clear_chain_tx_failure(tx.tx_hash, tx.lt)
                    pending.discard(tx.tx_hash)
                last_good_lt, last_good_hash = tx.lt, tx.tx_hash
                continue

            attempts = await database.register_chain_tx_failure(
                tx.tx_hash, tx.lt, self._tx_op(tx), error
            )
            result.errors.append(f"{tx.tx_hash[:12]}: {error}")
            logger.warning(
                "Could not index tx %s (attempt %d/%d): %s",
                tx.tx_hash[:12], attempts, MAX_TX_ATTEMPTS, error,
            )
            if attempts < MAX_TX_ATTEMPTS:
                # Keep the cursor here: the next poll retries this transaction.
                break
            # Poison transaction: park it and let the stream move on.
            result.parked += 1
            delay = RETRY_BACKOFF_SECONDS[min(attempts, len(RETRY_BACKOFF_SECONDS)) - 1]
            await database.schedule_chain_tx_retry(tx.tx_hash, tx.lt, delay)
            logger.error(
                "Parking tx %s after %d failures; retry in %ds", tx.tx_hash[:12], attempts, delay
            )
            last_good_lt, last_good_hash = tx.lt, tx.tx_hash

        if last_good_lt:
            await database.set_cursor(CURSOR_NAME, last_good_lt, last_good_hash)
        await database.record_indexer_run(last_good_lt or None, None, result.processed)
        if result.changed:
            logger.info(
                "Indexer: +%d markets, +%d bets, %d resolutions, %d claims",
                result.markets_created,
                result.bets,
                result.resolutions,
                result.claims,
            )

    async def _process_with_retries(
        self, tx: ChainTransaction, result: IndexResult
    ) -> Optional[str]:
        """Apply a transaction, retrying inline. Returns the last error or None.

        A failure always releases the ``chain_txs`` marker, otherwise the next
        attempt would consider the transaction already applied and silently
        drop the work that did not complete.
        """
        last_error: Optional[str] = None
        for attempt in range(MAX_INLINE_RETRIES + 1):
            try:
                await self._process_tx(tx, result)
                return None
            except Exception as exc:  # noqa: BLE001 - the caller decides what to do
                last_error = str(exc) or exc.__class__.__name__
                await database.unmark_tx_processed(tx.tx_hash, tx.lt)
                if attempt < MAX_INLINE_RETRIES:
                    logger.debug(
                        "Retrying tx %s after error (%d/%d): %s",
                        tx.tx_hash[:12], attempt + 1, MAX_INLINE_RETRIES, exc,
                    )
        return last_error

    @staticmethod
    def _tx_op(tx: ChainTransaction) -> Optional[int]:
        if not (tx.is_internal_inbound and tx.inbound_body):
            return None
        try:
            return parse_message_body(tx.inbound_body).op
        except (BodyError, AddressError):
            return None

    async def _rewind_for_retries(self) -> None:
        """Re-open the stream once the backoff of a parked transaction expires.

        Everything before the parked transaction is already in ``chain_txs``, so
        replaying the range is cheap and idempotent.
        """
        due = await database.list_chain_tx_failures(limit=1, due_only=True)
        if not due:
            return
        row = due[0]
        cursor_lt, _ = await database.get_cursor(CURSOR_NAME)
        target = max(0, int(row["lt"]) - 1)
        if target >= cursor_lt:
            return
        delay = RETRY_BACKOFF_SECONDS[
            min(int(row.get("attempts") or 1), len(RETRY_BACKOFF_SECONDS)) - 1
        ]
        await database.schedule_chain_tx_retry(row["tx_hash"], row["lt"], delay)
        logger.warning(
            "Rewinding cursor %s -> %s to retry parked tx %s (next attempt in %ds)",
            cursor_lt, target, row["tx_hash"][:12], delay,
        )
        await database.set_cursor(CURSOR_NAME, target, None)

    async def _fetch(self, cursor_lt: int, cursor_hash: Optional[str]) -> List[ChainTransaction]:
        """Fetch every transaction newer than the cursor (ascending order)."""
        pages = self.backfill_pages if not cursor_lt else 20
        lt: Optional[int] = None
        collected: List[ChainTransaction] = []
        seen_hashes: set[tuple[str, int]] = set()

        for _ in range(max(1, pages)):
            page = await self.client.get_transactions(self.escrow_address, lt=lt, limit=self.batch)
            if not page:
                break
            reached_cursor = False
            for tx in page:  # newest -> oldest
                if cursor_lt and (
                    tx.lt < cursor_lt or (tx.lt == cursor_lt and tx.tx_hash == cursor_hash)
                ):
                    reached_cursor = True
                    break
                key = (tx.tx_hash, tx.lt)
                if key not in seen_hashes:
                    seen_hashes.add(key)
                    collected.append(tx)
            if reached_cursor or (not cursor_lt and len(page) < self.batch):
                break
            if not cursor_lt and len(collected) >= self.batch * self.backfill_pages:
                break
            next_lt = min((tx.lt for tx in page), default=0)
            if next_lt == lt:
                break
            lt = next_lt
            if len(page) < self.batch:
                break

        collected.sort(key=lambda t: (t.lt, t.tx_hash))
        return collected

    # ------------------------------------------------------------------
    # transaction application
    # ------------------------------------------------------------------
    async def _process_tx(self, tx: ChainTransaction, result: IndexResult) -> None:
        parsed = None
        if tx.is_internal_inbound and tx.inbound_body:
            try:
                parsed = parse_message_body(tx.inbound_body)
            except (BodyError, AddressError) as exc:
                logger.debug("Skipping unparsable inbound body in %s: %s", tx.tx_hash[:12], exc)
                parsed = None

        op = parsed.op if parsed else None
        first_time = await database.mark_tx_processed(tx.tx_hash, tx.lt, self.escrow_address, op)
        if not first_time:
            result.skipped += 1
            return

        # 1) lifecycle events emitted to the indexer address
        for msg in tx.out_messages or []:
            body = _out_msg_body(msg)
            if not body:
                continue
            event = parse_event(body)
            if event is None:
                continue
            if event.op == EVENT_MARKET_CREATED:
                await self._apply_market_created(event, tx, result)
            elif event.op == EVENT_MARKET_RESOLVED:
                await database.update_market_settled(
                    event.market_id,
                    int(event.winning_option or 0),
                    int(event.total_pool or 0),
                    int(event.winning_pool or 0),
                )
                result.resolutions += 1
            elif event.op == EVENT_MARKET_CANCELLED:
                await database.update_market_cancelled(event.market_id)
                result.cancellations += 1

        # 2) bets / claims from the inbound message
        if parsed is None or not tx.is_internal_inbound:
            return

        if op == OP_PLACE_BET:
            if not tx.success:
                logger.info(
                    "Bet rejected on-chain (market=%s option=%s): exit code != 0",
                    parsed.market_id,
                    parsed.option,
                )
                return
            await self._apply_bet(parsed, tx, result)
        elif op == OP_CLAIM_PAYOUT:
            if not tx.success:
                return
            payout = self._claim_payout(tx)
            await database.mark_onchain_bet_claimed(
                int(parsed.market_id or 0), tx.inbound_source or "", payout
            )
            result.claims += 1
        elif op in (OP_RESOLVE_MARKET, OP_CANCEL_MARKET):
            if not tx.success:
                logger.info(
                    "Admin action %s rejected for market %s", parsed.name, parsed.market_id
                )

    def _claim_payout(self, tx: ChainTransaction) -> Optional[int]:
        best = None
        for msg in tx.out_messages or []:
            dest = _out_msg_destination(msg)
            if dest and tx.inbound_source and same_address(dest, tx.inbound_source):
                value = _out_msg_value(msg)
                if best is None or value > best:
                    best = value
        return best

    async def _apply_market_created(self, event: Any, tx: ChainTransaction, result: IndexResult) -> None:
        meta = await database.find_pending_action(
            "create_market", match_a=event.creator, match_b=event.title_hash
        )
        payload = (meta or {}).get("payload") or {}
        title = payload.get("title") or f"Mercado #{event.market_id}"
        created, enriched = await database.enrich_market_from_event(
            market_id=int(event.market_id),
            creator_address=event.creator,
            title=title,
            option_count=int(event.option_count or 0),
            closes_at=int(event.closes_at or 0),
            title_hash=event.title_hash,
            created_lt=tx.lt,
            created_tx=tx.tx_hash,
            fee_bps=event.fee_bps,
        )
        options = payload.get("options") or []
        if options:
            await database.set_market_options(int(event.market_id), options)
        if created:
            result.markets_created += 1
        if enriched:
            result.markets_enriched += 1
        if meta:
            await database.consume_pending_action(int(meta["id"]))

    async def _apply_bet(self, parsed: Any, tx: ChainTransaction, result: IndexResult) -> None:
        bettor = tx.inbound_source
        if not bettor:
            return
        market_id = int(parsed.market_id or 0)
        option = int(parsed.option or 0)
        stake = int(parsed.stake or 0)
        if stake < self.min_stake_nano:
            logger.info("Ignoring on-chain bet below min stake: %s", stake)
            return

        market = await database.get_market(market_id)
        if market is None:
            # The create event was not indexed (e.g. the market predates the
            # indexer). Keep the bet but flag the market as incomplete.
            logger.warning("Bet for unknown market %s; creating placeholder row", market_id)
            await database.insert_market_placeholder(
                market_id=market_id,
                creator_address=None,
                option_count=max(option + 1, 2),
                created_lt=tx.lt,
                created_tx=tx.tx_hash,
            )
            market = await database.get_market(market_id)
        elif int(market.get("option_count") or 0) <= option:
            logger.warning(
                "Bet option %s out of range for market %s (options=%s)",
                option,
                market_id,
                market.get("option_count"),
            )
            return

        inserted = await database.record_onchain_bet(
            market_id=market_id,
            bettor=bettor,
            option=option,
            stake=stake,
            tx_hash=tx.tx_hash,
            tx_lt=tx.lt,
        )
        if inserted:
            result.bets += 1
            meta = await database.find_pending_action(
                "bet", match_a=bettor, match_b=str(market_id)
            )
            if meta:
                await database.consume_pending_action(int(meta["id"]))

    # ------------------------------------------------------------------
    # background lifecycle
    # ------------------------------------------------------------------
    def start(self) -> asyncio.Task:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._loop(), name="escrow-indexer")
        return self._task

    async def _loop(self) -> None:
        logger.info(
            "Escrow indexer started for %s (every %ss)", self.escrow_address, self.poll_interval
        )
        failures = 0
        while not self._stopping.is_set():
            result = await self.run_once()
            if result.errors:
                failures += 1
            else:
                failures = 0
            delay = self.poll_interval if failures == 0 else min(self.poll_interval * (2 ** failures), 300)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)
            except TimeoutError:
                continue
        logger.info("Escrow indexer stopped")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=self.poll_interval + 15)
            except (TimeoutError, asyncio.CancelledError):  # pragma: no cover
                self._task.cancel()
            self._task = None


__all__ = ["EscrowIndexer", "IndexResult", "CURSOR_NAME"]
