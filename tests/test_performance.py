"""Fase 6: rendimiento medible, no «parece más rápido» (R-01…R-05).

Cada test fija una propiedad que se puede romper con una edición inocente:

* los planes de consulta no hacen ``SCAN`` ni montan un B-TREE temporal;
* el número de consultas **no depende** del número de filas (adiós N+1);
* los agregados del perfil coinciden exactamente con el cálculo en Python.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest

from bot import database

WALLET = "0:" + "11" * 32


@pytest.fixture
def query_log(db, monkeypatch):
    """Registra cada ``execute`` de aiosqlite mientras dure el test."""
    calls: list[str] = []
    original = aiosqlite.Connection.execute

    def counting(self, sql, *args, **kwargs):
        # ``aiosqlite.Connection.execute`` es síncrona y devuelve un ``Result``
        # que luego se espera o se usa como context manager.
        calls.append(" ".join(str(sql).split()))
        return original(self, sql, *args, **kwargs)

    monkeypatch.setattr(aiosqlite.Connection, "execute", counting)
    return calls


async def _prediction(db, title: str = "¿Llueve?") -> int:
    return await db.create_prediction(
        creator_id=1,
        chat_id=1,
        title=title,
        options=["Sí", "No"],
        closes_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )


async def _market(db, market_id: int, title: str = "Mercado") -> None:
    await db.enrich_market_from_event(
        market_id, WALLET, title, 2, int(time.time()) + 3600, fee_bps=200
    )
    await db.set_market_options(market_id, ["Sí", "No"])


class TestQueryPlans:
    """R-04/R-05: índice correcto o el test falla."""

    async def _plan(self, db, sql: str, params=()) -> str:
        conn = await database._get_conn()
        async with conn.execute("EXPLAIN QUERY PLAN " + sql, params) as cur:
            rows = await cur.fetchall()
        return " | ".join(str(tuple(row)) for row in rows)

    async def test_market_list_does_not_sort_in_temporary_btree(self, db):
        plan = await self._plan(
            db,
            "SELECT * FROM markets WHERE status = ?"
            " ORDER BY closes_at ASC, market_id DESC LIMIT 25 OFFSET 0",
            ("open",),
        )
        assert "SCAN markets" not in plan
        assert "TEMP B-TREE" not in plan

    async def test_onchain_bets_by_bettor_uses_the_index(self, db):
        plan = await self._plan(
            db,
            "SELECT * FROM onchain_bets WHERE bettor = ? ORDER BY id DESC LIMIT 50",
            (WALLET,),
        )
        assert "SCAN onchain_bets" not in plan
        assert "idx_onchain_bets_bettor" in plan

    async def test_option_pools_group_without_temporary_btree(self, db):
        plan = await self._plan(
            db,
            "SELECT option, COALESCE(SUM(stake),0) FROM onchain_bets"
            " WHERE market_id = ? GROUP BY option",
            (1,),
        )
        assert "SCAN onchain_bets" not in plan
        assert "TEMP B-TREE" not in plan


class TestNoNPlusOne:
    """R-01: el coste de una página no crece con el número de filas."""

    async def test_market_page_uses_a_constant_number_of_queries(self, db, query_log):
        await _market(db, 1, "Uno")
        query_log.clear()
        await db.list_markets(status="open", limit=25)
        few = len(query_log)

        for market_id in range(2, 12):
            await _market(db, market_id, f"Mercado {market_id}")
            await db.record_onchain_bet(market_id, WALLET, 0, 1_000_000, f"t{market_id}", market_id)
        query_log.clear()
        page = await db.list_markets(status="open", limit=25)
        many = len(query_log)

        assert len(page) == 11
        assert few == 3, query_log
        assert many == few, f"la página hizo {many} consultas con 11 mercados (antes {few})"

    async def test_market_page_still_returns_pools_and_labels(self, db):
        await _market(db, 1, "Uno")
        await db.record_onchain_bet(1, WALLET, 0, 1_000_000, "t1", 1)
        page = await db.list_markets(status="open", limit=25)
        assert page[0]["pool"] == 1_000_000
        assert page[0]["bets"] == 1
        assert page[0]["option_pools"] == {"0": 1_000_000}
        assert [o["label"] for o in page[0]["options"]] == ["Sí", "No"]

    async def test_market_without_labels_falls_back_to_numbered_options(self, db):
        await db.enrich_market_from_event(5, WALLET, "Sin etiquetas", 3, int(time.time()) + 60)
        page = await db.list_markets(status="open", limit=25)
        assert [o["label"] for o in page[0]["options"]] == ["Opción 1", "Opción 2", "Opción 3"]

    async def test_predictions_list_is_one_query_with_the_pool(self, db, query_log):
        pred_id = await _prediction(db)
        await db.upsert_user(1, "alice", "Alice")
        options = await db.get_options(pred_id)
        await db.place_bet(1, pred_id, options[0]["id"], 250)

        query_log.clear()
        rows = await db.get_open_predictions(chat_id=1)
        assert len(query_log) == 1
        assert rows[0]["total_pool"] == 250

    async def test_empty_page_does_not_query_children(self, db, query_log):
        query_log.clear()
        assert await db.list_markets(status="open") == []
        assert len(query_log) == 1


class TestUserStats:
    """R-02/R-03: los agregados equivalen al cálculo anterior."""

    async def _history(self, db):
        await db.upsert_user(1, "alice", "Alice")
        await db.upsert_user(2, "bob", "Bob")
        won = await _prediction(db, "Ganada")
        lost = await _prediction(db, "Perdida")
        refunded = await _prediction(db, "Reembolsada")
        for pred_id, option_index in ((won, 0), (lost, 1), (refunded, 0)):
            options = await db.get_options(pred_id)
            await db.place_bet(1, pred_id, options[option_index]["id"], 100)
        await db.place_bet(2, won, (await db.get_options(won))[1]["id"], 300)

        await db.settle_prediction(won, (await db.get_options(won))[0]["id"])
        await db.settle_prediction(lost, (await db.get_options(lost))[0]["id"])
        await db.settle_prediction(refunded, (await db.get_options(refunded))[0]["id"])
        return won, lost, refunded

    async def test_stats_match_the_python_calculation(self, db):
        await self._history(db)
        stats = await db.get_user_stats(1)

        bets = await db.get_user_bets(1, limit=500)
        expected_wins = sum(1 for b in bets if b["result"] == "win")
        expected_losses = sum(1 for b in bets if b["result"] == "lose")
        expected_refunds = sum(1 for b in bets if b["result"] == "refund")
        expected_net = sum(int(b["payout"] or 0) - int(b["amount"]) for b in bets if b["result"])

        assert stats["total"] == len(bets)
        assert stats["wins"] == expected_wins
        assert stats["losses"] == expected_losses
        assert stats["refunds"] == expected_refunds
        assert stats["pending"] == 0
        assert stats["net"] == expected_net
        assert stats["staked"] == sum(int(b["amount"]) for b in bets)
        decided = expected_wins + expected_losses
        assert stats["accuracy"] == pytest.approx(expected_wins / decided * 100)

    async def test_stats_of_a_new_user(self, db):
        await db.upsert_user(1, "alice", "Alice")
        stats = await db.get_user_stats(1)
        assert stats["total"] == 0
        assert stats["accuracy"] is None
        assert stats["net"] == 0

    async def test_ranking_position_counts_people_ahead(self, db):
        await db.upsert_user(1, "alice", "Alice")
        await db.upsert_user(2, "bob", "Bob")
        await db.upsert_user(3, "carol", "Carol")
        await db.adjust_points(2, 500)
        await db.adjust_points(3, -500)
        assert await db.ranking_position(1) == 2
        assert await db.ranking_position(2) == 1
        assert await db.ranking_position(3) == 3
        assert await db.ranking_position(999) is None

    async def test_ranking_position_is_a_tie_break_by_user_id(self, db):
        await db.upsert_user(1, "alice", "Alice")
        await db.upsert_user(2, "bob", "Bob")
        assert await db.ranking_position(1) == 1
        assert await db.ranking_position(2) == 2
