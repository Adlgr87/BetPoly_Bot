"""Escrow indexer: idempotency, event parsing, bet/claim mirroring."""
from __future__ import annotations

import base64

import pytest
from pytoniq_core import begin_cell

from bot.chain import bodies
from bot.chain.addresses import normalize_address
from bot.chain.indexer import EscrowIndexer
from bot.chain.toncenter import ChainTransaction

ESCROW = "0:" + "ab" * 32
INDEXER_ADDR = "0:" + "99" * 32
ALICE = "0:" + "11" * 32
BOB = "0:" + "22" * 32
CAROL = "0:" + "33" * 32


def _b64(cell) -> str:
    return base64.b64encode(cell.to_boc()).decode()


def created_event(market_id: int, creator: str, title_hash: str, closes_at: int, count: int):
    return (
        begin_cell()
        .store_uint(bodies.EVENT_MARKET_CREATED, 32)
        .store_uint(market_id, 32)
        .store_address(bodies.parse_address(creator))
        .store_uint(int(title_hash, 16), 256)
        .store_uint(closes_at, 32)
        .store_uint(count, 8)
        .store_uint(200, 16)
        .end_cell()
    )


def resolved_event(market_id: int, winner: int, total: int, winning_pool: int):
    return (
        begin_cell()
        .store_uint(bodies.EVENT_MARKET_RESOLVED, 32)
        .store_uint(market_id, 32)
        .store_uint(winner, 8)
        .store_coins(total)
        .store_coins(winning_pool)
        .end_cell()
    )


def cancelled_event(market_id: int):
    return (
        begin_cell()
        .store_uint(bodies.EVENT_MARKET_CANCELLED, 32)
        .store_uint(market_id, 32)
        .end_cell()
    )


def out_msg(destination: str, body_cell, value: int = 10_000_000):
    """The contract sends events inside a carrier cell (a single ref)."""
    carrier = begin_cell().store_ref(body_cell).end_cell()
    return {
        "destination": destination,
        "value": str(value),
        "msg_data": {"@type": "msg.dataRaw", "body": _b64(carrier)},
    }


def make_tx(lt: int, *, inbound=None, body=None, success=True, outs=None, hash_suffix="00"):
    return ChainTransaction(
        tx_hash=f"{lt:016x}{hash_suffix}",
        lt=lt,
        utime=1_800_000_000 + lt,
        success=success,
        aborted=not success,
        inbound_source=normalize_address(inbound) if inbound else None,
        inbound_value=2_000_000_000,
        inbound_body=_b64(body) if body is not None else None,
        out_messages=outs or [],
    )


class FakeClient:
    def __init__(self, txs):
        self.txs = txs
        self.calls = 0

    async def get_transactions(self, address, *, lt=None, limit=50):
        self.calls += 1
        return sorted(self.txs, key=lambda t: -t.lt)


@pytest.fixture
def indexer_factory(db):
    def build(txs):
        return EscrowIndexer(
            client=FakeClient(txs),
            escrow_address=ESCROW,
            poll_interval=5,
            batch=50,
            min_stake_nano=100_000_000,
        )

    return build


class TestMarketLifecycle:
    async def test_create_event_registers_market_with_metadata(self, indexer_factory, db):
        title = "¿Lloverá mañana?"
        title_hash = bodies.title_to_hash(title)
        await db.create_pending_action(
            user_id=1,
            kind="create_market",
            match_a=ALICE,
            match_b=title_hash,
            payload={"title": title, "options": ["Sí", "No"], "closes_at": 1_800_003_600},
        )
        txs = [
            make_tx(
                100,
                inbound=ALICE,
                body=bodies.build_create_market(title_hash, 1_800_003_600, 2),
                outs=[out_msg(INDEXER_ADDR, created_event(0, ALICE, title_hash, 1_800_003_600, 2))],
            )
        ]
        result = await indexer_factory(txs).run_once()
        assert result.markets_created == 1
        market = await db.get_market(0)
        assert market["title"] == title
        assert market["option_count"] == 2
        assert market["status"] == "open"
        assert [o["label"] for o in market["options"]] == ["Sí", "No"]
        assert await db.find_pending_action("create_market", match_a=ALICE, match_b=title_hash) is None

    async def test_create_event_without_metadata_uses_placeholder(self, indexer_factory, db):
        title_hash = "ef" * 32
        txs = [
            make_tx(
                10,
                inbound=ALICE,
                outs=[out_msg(INDEXER_ADDR, created_event(5, ALICE, title_hash, 1_800_003_600, 3))],
            )
        ]
        await indexer_factory(txs).run_once()
        market = await db.get_market(5)
        assert market["title"] == "Mercado #5"
        assert len(market["options"]) == 3

    async def test_resolve_and_cancel_events(self, indexer_factory, db):
        await db.enrich_market_from_event(7, ALICE, "Mercado", 2, 1_800_000_100)
        txs = [
            make_tx(20, outs=[out_msg(INDEXER_ADDR, resolved_event(7, 1, 5_000, 3_000))]),
            make_tx(21, outs=[out_msg(INDEXER_ADDR, cancelled_event(7))]),
        ]
        result = await indexer_factory(txs).run_once()
        assert result.resolutions == 1
        assert result.cancellations == 1
        market = await db.get_market(7)
        assert market["status"] == "cancelled"
        assert market["winning_option"] == 1
        assert market["total_pool"] == 5_000


class TestBets:
    async def test_bet_is_mirrored_and_pending_intent_consumed(self, indexer_factory, db):
        await db.enrich_market_from_event(1, ALICE, "M", 2, 1_800_003_600)
        await db.create_pending_action(1, "bet", match_a=BOB, match_b="1", payload={"option": 0})
        txs = [
            make_tx(30, inbound=BOB, body=bodies.build_place_bet(1, 0, 1_500_000_000)),
        ]
        result = await indexer_factory(txs).run_once()
        assert result.bets == 1
        bet = await db.get_onchain_bet(1, BOB)
        assert bet["stake"] == 1_500_000_000
        assert bet["option"] == 0
        assert await db.find_pending_action("bet", match_a=BOB, match_b="1") is None

    async def test_rejected_bet_is_not_recorded(self, indexer_factory, db):
        await db.enrich_market_from_event(1, ALICE, "M", 2, 1_800_003_600)
        txs = [make_tx(31, inbound=BOB, body=bodies.build_place_bet(1, 0, 1_500_000_000), success=False)]
        result = await indexer_factory(txs).run_once()
        assert result.bets == 0
        assert await db.get_onchain_bet(1, BOB) is None

    async def test_bet_below_minimum_is_ignored(self, indexer_factory, db):
        await db.enrich_market_from_event(1, ALICE, "M", 2, 1_800_003_600)
        txs = [make_tx(32, inbound=BOB, body=bodies.build_place_bet(1, 0, 1))]
        await indexer_factory(txs).run_once()
        assert await db.get_onchain_bet(1, BOB) is None

    async def test_bet_out_of_range_is_ignored(self, indexer_factory, db):
        await db.enrich_market_from_event(1, ALICE, "M", 2, 1_800_003_600)
        txs = [make_tx(33, inbound=BOB, body=bodies.build_place_bet(1, 9, 1_500_000_000))]
        await indexer_factory(txs).run_once()
        assert await db.get_onchain_bet(1, BOB) is None

    async def test_unknown_market_creates_placeholder(self, indexer_factory, db):
        txs = [make_tx(34, inbound=CAROL, body=bodies.build_place_bet(99, 1, 2_000_000_000))]
        await indexer_factory(txs).run_once()
        market = await db.get_market(99)
        assert market is not None
        assert market["title"] == "Mercado #99"
        bet = await db.get_onchain_bet(99, CAROL)
        assert bet["stake"] == 2_000_000_000

    async def test_claim_marks_bet_as_paid(self, indexer_factory, db):
        await db.enrich_market_from_event(2, ALICE, "M", 2, 1_800_000_000)
        await db.record_onchain_bet(2, BOB, 0, 1_000_000_000, "abc", 1)
        txs = [
            make_tx(
                40,
                inbound=BOB,
                body=bodies.build_claim_payout(2),
                outs=[out_msg(BOB, begin_cell().end_cell(), value=1_470_000_000)],
            )
        ]
        result = await indexer_factory(txs).run_once()
        assert result.claims == 1
        bet = await db.get_onchain_bet(2, BOB)
        assert bet["claimed"] == 1
        assert bet["payout"] == 1_470_000_000


class TestIdempotency:
    async def test_same_transactions_are_applied_once(self, indexer_factory, db):
        await db.enrich_market_from_event(1, ALICE, "M", 2, 1_800_003_600)
        txs = [
            make_tx(50, inbound=BOB, body=bodies.build_place_bet(1, 0, 1_500_000_000)),
            make_tx(51, inbound=CAROL, body=bodies.build_place_bet(1, 1, 1_500_000_000)),
        ]
        indexer = indexer_factory(txs)
        first = await indexer.run_once()
        assert first.bets == 2
        second = await indexer.run_once()
        assert second.bets == 0
        async with db.connection() as conn:
            async with conn.execute("SELECT COUNT(*) AS n FROM onchain_bets") as cur:
                assert (await cur.fetchone())["n"] == 2

    async def test_cursor_advances(self, indexer_factory, db):
        txs = [make_tx(60), make_tx(61)]
        await indexer_factory(txs).run_once()
        lt, tx_hash = await db.get_cursor("escrow")
        assert lt == 61
        assert tx_hash.startswith(f"{61:016x}")

    async def test_new_transactions_after_cursor_are_processed(self, indexer_factory, db):
        await db.enrich_market_from_event(1, ALICE, "M", 2, 1_800_003_600)
        txs = [make_tx(70, inbound=BOB, body=bodies.build_place_bet(1, 0, 1_500_000_000))]
        indexer = indexer_factory(txs)
        await indexer.run_once()
        txs.append(make_tx(71, inbound=CAROL, body=bodies.build_place_bet(1, 1, 1_500_000_000)))
        result = await indexer.run_once()
        assert result.bets == 1
        assert await db.get_onchain_bet(1, CAROL) is not None
        assert await db.get_onchain_bet(1, BOB) is not None

    async def test_status_is_recorded(self, indexer_factory, db):
        await indexer_factory([make_tx(80)]).run_once()
        status = await db.get_indexer_status()
        assert status["last_lt"] == 80
        assert status["last_error"] is None


class TestFailureRecovery:
    """S-01: un fallo de persistencia no puede perder el evento para siempre."""

    async def test_cursor_stops_before_the_failed_transaction(self, indexer_factory, db, monkeypatch):
        title_hash = bodies.title_to_hash("Mercado tres")
        txs = [
            make_tx(
                10,
                inbound=ALICE,
                body=bodies.build_create_market(bodies.title_to_hash("Uno"), 1_800_003_600, 2),
                outs=[out_msg(INDEXER_ADDR, created_event(1, ALICE, bodies.title_to_hash("Uno"), 1_800_003_600, 2))],
            ),
            make_tx(20, inbound=BOB, body=bodies.build_place_bet(1, 0, 1_500_000_000)),
            make_tx(
                30,
                inbound=ALICE,
                body=bodies.build_create_market(title_hash, 1_800_003_600, 2),
                outs=[out_msg(INDEXER_ADDR, created_event(3, ALICE, title_hash, 1_800_003_600, 2))],
            ),
        ]
        indexer = indexer_factory(txs)

        async def explode(*_args, **_kwargs):
            raise RuntimeError("fallo simulado de persistencia")

        monkeypatch.setattr(indexer, "_apply_bet", explode)
        result = await indexer.run_once()

        assert result.errors, "el fallo debe quedar registrado en el resultado"
        cursor_lt, _cursor_hash = await db.get_cursor("escrow")
        assert cursor_lt == 10, "el cursor debe quedarse en la última tx aplicada"
        assert await db.get_market(3) is None, "no debe saltarse la tx fallida"
        assert await db.count_chain_tx_failures() == 1

        # Segundo ciclo sin el fallo: se reprocesa desde el último punto bueno.
        monkeypatch.undo()
        result = await indexer.run_once()
        assert result.errors == []
        assert await db.get_market(3) is not None
        assert await db.get_onchain_bet(1, BOB) is not None
        assert (await db.get_cursor("escrow"))[0] == 30
        assert await db.count_chain_tx_failures() == 0, "el éxito debe limpiar la dead-letter"

    async def test_poison_transaction_is_parked_and_the_stream_advances(
        self, indexer_factory, db, monkeypatch
    ):
        from bot.chain import indexer as indexer_module

        txs = [make_tx(40), make_tx(50)]
        indexer = indexer_factory(txs)

        async def explode(*_args, **_kwargs):
            raise RuntimeError("tx veneno")

        async def process_second_only(tx, result):
            if tx.lt == 40:
                raise RuntimeError("tx veneno")
            result.processed += 1

        monkeypatch.setattr(indexer, "_process_tx", process_second_only)

        parked_at = None
        for attempt in range(1, indexer_module.MAX_TX_ATTEMPTS + 1):
            result = await indexer.run_once()
            if result.parked:
                parked_at = attempt
                break
            assert (await db.get_cursor("escrow"))[0] == 0, "el cursor no debe avanzar mientras reintenta"

        assert parked_at == indexer_module.MAX_TX_ATTEMPTS
        failures = await db.list_chain_tx_failures()
        assert len(failures) == 1 and failures[0]["lt"] == 40
        assert (await db.get_cursor("escrow"))[0] == 50, "la tx veneno no puede detener el stream"

        # Cuando el backoff vence y el fallo desaparece, la tx se recupera sola.
        monkeypatch.undo()
        async with db.connection() as conn:
            await conn.execute("UPDATE chain_tx_failures SET next_retry_at = '2000-01-01T00:00:00+00:00'")
            await conn.commit()
        result = await indexer.run_once()
        assert result.errors == []
        assert await db.count_chain_tx_failures() == 0

    async def test_a_transient_failure_matches_the_clean_run(self, indexer_factory, db, monkeypatch):
        """Propiedad: con fallos transitorios el espejo acaba igual que sin ellos."""
        await db.enrich_market_from_event(1, ALICE, "M", 2, 1_800_003_600)
        txs = [
            make_tx(60, inbound=BOB, body=bodies.build_place_bet(1, 0, 1_000_000_000)),
            make_tx(61, inbound=CAROL, body=bodies.build_place_bet(1, 1, 2_000_000_000)),
        ]
        indexer = indexer_factory(txs)
        failing = {60}

        original = indexer._process_tx

        async def flaky(tx, result):
            if tx.lt in failing:
                failing.discard(tx.lt)
                raise RuntimeError("fallo transitorio")
            await original(tx, result)

        monkeypatch.setattr(indexer, "_process_tx", flaky)
        await indexer.run_once()
        await indexer.run_once()

        bob = await db.get_onchain_bet(1, BOB)
        carol = await db.get_onchain_bet(1, CAROL)
        assert bob is not None and bob["stake"] == 1_000_000_000
        assert carol is not None and carol["stake"] == 2_000_000_000
        assert await db.count_chain_tx_failures() == 0


class TestMarketMetadata:
    """S-02: el marcador nunca debe ganar a los datos del evento de creación."""

    async def test_bet_before_create_is_enriched_by_the_event(self, indexer_factory, db):
        title = "¿Llueve el sábado?"
        title_hash = bodies.title_to_hash(title)
        await db.create_pending_action(
            user_id=1,
            kind="create_market",
            match_a=ALICE,
            match_b=title_hash,
            payload={"title": title, "options": ["Sí", "No"], "closes_at": 1_800_003_600},
        )
        txs = [
            # La apuesta llega antes que el evento de creación (reordenación real).
            make_tx(10, inbound=BOB, body=bodies.build_place_bet(9, 1, 1_500_000_000)),
            make_tx(
                20,
                inbound=ALICE,
                body=bodies.build_create_market(title_hash, 1_800_003_600, 2),
                outs=[out_msg(INDEXER_ADDR, created_event(9, ALICE, title_hash, 1_800_003_600, 2))],
            ),
        ]
        indexer = indexer_factory(txs)
        await indexer.run_once()

        market = await db.get_market(9)
        assert market["title"] == title, "el título real debe sustituir al marcador"
        assert market["closes_at"] == 1_800_003_600
        assert market["fee_bps"] == 200
        assert market["option_count"] == 2
        assert market["metadata_source"] == "event"
        assert [o["label"] for o in market["options"]] == ["Sí", "No"]

    async def test_placeholder_uses_a_neutral_title_and_zero_close(self, db):
        created = await db.insert_market_placeholder(3, None, 2)
        assert created is True
        market = await db.get_market(3)
        assert market["title"] == "Mercado #3"
        assert market["closes_at"] == 0
        assert market["metadata_source"] == "placeholder"
        assert await db.insert_market_placeholder(3, None, 4) is False

    async def test_a_second_event_never_overwrites_real_metadata(self, indexer_factory, db):
        title = "Mercado original"
        title_hash = bodies.title_to_hash(title)
        await db.enrich_market_from_event(
            4, ALICE, title, 2, 1_800_000_000, title_hash=title_hash, fee_bps=100
        )
        txs = [
            make_tx(
                30,
                outs=[
                    out_msg(
                        INDEXER_ADDR,
                        created_event(4, ALICE, "aa" * 32, 1_900_000_000, 5),
                    )
                ],
            )
        ]
        await indexer_factory(txs).run_once()

        market = await db.get_market(4)
        assert market["title"] == title
        assert market["closes_at"] == 1_800_000_000
        assert market["fee_bps"] == 100
        assert market["option_count"] == 2

    async def test_enrichment_fills_only_empty_fields(self, db):
        await db.insert_market_placeholder(5, None, 2)
        created, enriched = await db.enrich_market_from_event(
            5, ALICE, "Título", 3, 1_800_000_500, title_hash="bb" * 32, fee_bps=250
        )
        assert (created, enriched) == (False, True)
        market = await db.get_market(5)
        assert market["creator_address"] == ALICE
        assert market["title_hash"] == "bb" * 32
        assert market["fee_bps"] == 250

        # Idempotente: un segundo enriquecimiento no reporta cambios.
        assert await db.enrich_market_from_event(
            5, ALICE, "Otro", 9, 1_800_000_900, fee_bps=900
        ) == (False, False)
