#!/usr/bin/env node
/**
 * End-to-end tests for contracts/escrow.fc, executed on the TVM sandbox
 * (no network required).
 *
 * The tests double as executable documentation of the wire protocol shared with
 * bot/chain/bodies.py and bot/chain/indexer.py.
 */
const path = require('path');
const assert = require('assert');
const { Blockchain } = require('@ton/sandbox');
const { beginCell, contractAddress, toNano } = require('@ton/core');
const { compile } = require('../scripts/build');

const OP = {
  createMarket: 0x1a0a0001,
  placeBet: 0x1a0a0002,
  resolveMarket: 0x1a0a0003,
  cancelMarket: 0x1a0a0004,
  claimPayout: 0x1a0a0005,
  setPaused: 0x1a0a0101,
  setFee: 0x1a0a0102,
  withdrawFees: 0x1a0a0104,
};

const EV = {
  created: 0xe0a00001,
  resolved: 0xe0a00002,
  cancelled: 0xe0a00003,
};

let passed = 0;
function step(name) {
  passed += 1;
  console.log('  ✓ ' + name);
}

function dataCell({ admin, indexer, feeBps = 200, minStake = toNano('0.1'), paused = 0 }) {
  return beginCell()
    .storeAddress(admin)
    .storeAddress(indexer)
    .storeUint(feeBps, 16)
    .storeCoins(minStake)
    .storeUint(paused, 1)
    .storeUint(0, 32) // seqno
    .storeCoins(0) // total_locked
    .storeCoins(0) // fees
    .storeDict(null) // markets
    .endCell();
}

const body = {
  createMarket: (titleHash, closesAt, optionCount) =>
    beginCell()
      .storeUint(OP.createMarket, 32)
      .storeUint(titleHash, 256)
      .storeUint(closesAt, 32)
      .storeUint(optionCount, 8)
      .endCell(),
  placeBet: (marketId, option, stake) =>
    beginCell()
      .storeUint(OP.placeBet, 32)
      .storeUint(marketId, 32)
      .storeUint(option, 8)
      .storeCoins(stake)
      .endCell(),
  resolveMarket: (marketId, winner) =>
    beginCell()
      .storeUint(OP.resolveMarket, 32)
      .storeUint(marketId, 32)
      .storeUint(winner, 8)
      .endCell(),
  cancelMarket: (marketId) =>
    beginCell().storeUint(OP.cancelMarket, 32).storeUint(marketId, 32).endCell(),
  claim: (marketId) =>
    beginCell().storeUint(OP.claimPayout, 32).storeUint(marketId, 32).endCell(),
  setPaused: (value) => beginCell().storeUint(OP.setPaused, 32).storeUint(value, 8).endCell(),
  setFee: (bps) => beginCell().storeUint(OP.setFee, 32).storeUint(bps, 16).endCell(),
  withdrawFees: (amount) =>
    beginCell().storeUint(OP.withdrawFees, 32).storeCoins(amount).endCell(),
};

function unwrap(cell) {
  let current = cell;
  for (let i = 0; i < 8; i += 1) {
    const slice = current.beginParse();
    if (slice.remainingBits === 0 && slice.remainingRefs === 1) {
      current = slice.loadRef();
      continue;
    }
    return current;
  }
  return current;
}

// Failed actions bounce, so the interesting transaction is the one delivered to
// the escrow contract (not the sender's wallet nor the bounced message).
let CONTRACT_ADDRESS = null;

function exitCodeOf(result) {
  for (const tx of result.transactions) {
    if (tx.description.type !== 'generic') continue;
    const dest = tx.inMessage && tx.inMessage.info && tx.inMessage.info.dest;
    if (CONTRACT_ADDRESS && dest && dest.equals(CONTRACT_ADDRESS)) {
      const compute = tx.description.computePhase;
      return compute && compute.type === 'vm' ? compute.exitCode : null;
    }
  }
  return null;
}

function okExit(result) {
  const code = exitCodeOf(result);
  assert.strictEqual(code, 0, 'expected successful compute phase, got exit code ' + code);
}

function failedWith(result, code) {
  assert.strictEqual(exitCodeOf(result), code, 'expected exit code ' + code);
}

async function getNumber(blockchain, address, method, args = []) {
  const res = await blockchain.runGetMethod(address, method, args);
  return Number(res.stackReader.readBigNumber());
}

async function getMarket(blockchain, address, marketId) {
  const res = await blockchain.runGetMethod(address, 'get_market_data', [
    { type: 'int', value: BigInt(marketId) },
  ]);
  const reader = res.stackReader;
  return {
    status: Number(reader.readBigNumber()),
    winning: Number(reader.readBigNumber()),
    optionCount: Number(reader.readBigNumber()),
    closesAt: Number(reader.readBigNumber()),
    feeBps: Number(reader.readBigNumber()),
    totalPool: Number(reader.readBigNumber()),
    winningPool: Number(reader.readBigNumber()),
  };
}

async function main() {
  console.log('\nBetPoly escrow · TVM sandbox tests\n');

  const code = await compile();
  const blockchain = await Blockchain.create();
  blockchain.now = 1_800_000_000;

  const deployer = await blockchain.treasury('deployer');
  const admin = await blockchain.treasury('admin');
  const indexer = await blockchain.treasury('indexer');
  const alice = await blockchain.treasury('alice');
  const bob = await blockchain.treasury('bob');
  const carol = await blockchain.treasury('carol');
  const dave = await blockchain.treasury('dave');

  const data = dataCell({ admin: admin.address, indexer: indexer.address });
  const address = contractAddress(0, { code, data });
  CONTRACT_ADDRESS = address;

  const deploy = await deployer.send({
    to: address,
    value: toNano('1'),
    init: { code, data },
    bounce: false,
  });
  okExit(deploy);

  console.log('\n[1] Deployment & storage');
  assert.strictEqual(await getNumber(blockchain, address, 'get_market_count'), 0);
  assert.strictEqual(await getNumber(blockchain, address, 'get_fee_bps'), 200);
  assert.strictEqual(await getNumber(blockchain, address, 'get_paused'), 0);
  assert.strictEqual(
    (await getNumber(blockchain, address, 'get_total_locked')),
    0,
  );
  step('getters expose a clean initial state');

  // ------------------------------------------------------------ create market
  const closesAt = blockchain.now + 3600;
  const titleHash = BigInt('0x' + 'ab'.repeat(32));
  const createResult = await alice.send({
    to: address,
    value: toNano('0.05'),
    body: body.createMarket(titleHash, closesAt, 3),
    bounce: true,
  });
  okExit(createResult);

  assert.strictEqual(await getNumber(blockchain, address, 'get_market_count'), 1);
  const market = await getMarket(blockchain, address, 0);
  assert.strictEqual(market.status, 0);
  assert.strictEqual(market.optionCount, 3);
  assert.strictEqual(market.closesAt, closesAt);
  assert.strictEqual(market.totalPool, 0);
  step('market #0 created (open, 3 options)');

  const createdEvent = createResult.transactions
    .flatMap((tx) => [...tx.outMessages.values()])
    .find((msg) => {
      try {
        return unwrap(msg.body).beginParse().loadUint(32) === EV.created;
      } catch (error) {
        return false;
      }
    });
  assert.ok(createdEvent, 'market creation event must be emitted');
  assert.ok(createdEvent.info.dest.equals(indexer.address), 'event goes to the indexer');
  const evSlice = unwrap(createdEvent.body).beginParse();
  assert.strictEqual(evSlice.loadUint(32), EV.created);
  assert.strictEqual(Number(evSlice.loadUint(32)), 0);
  assert.ok(evSlice.loadAddress().equals(alice.address));
  assert.strictEqual(evSlice.loadUintBig(256), titleHash);
  assert.strictEqual(Number(evSlice.loadUint(32)), closesAt);
  assert.strictEqual(Number(evSlice.loadUint(8)), 3);
  step('lifecycle event carries market id, creator, hash and deadline');

  failedWith(
    await alice.send({
      to: address,
      value: toNano('0.05'),
      body: body.createMarket(titleHash, blockchain.now - 10, 2),
    }),
    405,
  );
  failedWith(
    await alice.send({
      to: address,
      value: toNano('0.05'),
      body: body.createMarket(titleHash, closesAt, 20),
    }),
    404,
  );
  failedWith(
    await alice.send({
      to: address,
      value: toNano('0.01'),
      body: body.createMarket(titleHash, closesAt, 2),
    }),
    406,
  );
  step('bad deadline / option count / value rejected (405, 404, 406)');

  // ------------------------------------------------------------------- bets
  const bet = async (wallet, option, stake, marketId = 0) =>
    wallet.send({
      to: address,
      value: stake + toNano('0.03'),
      body: body.placeBet(marketId, option, stake),
    });

  okExit(await bet(bob, 0, toNano('1')));
  okExit(await bet(carol, 1, toNano('2')));
  okExit(await bet(dave, 0, toNano('3')));

  const afterBets = await getMarket(blockchain, address, 0);
  assert.strictEqual(afterBets.totalPool, Number(toNano('6')));
  assert.strictEqual(await getNumber(blockchain, address, 'get_total_locked'), Number(toNano('6')));
  step('three bets recorded, 6 TON locked');

  failedWith(await bet(bob, 1, toNano('1')), 410);
  step('duplicate bet from the same wallet rejected (410)');

  failedWith(await bet(alice, 5, toNano('1')), 409);
  step('out-of-range option rejected (409)');

  failedWith(
    await alice.send({
      to: address,
      value: toNano('1.005'),
      body: body.placeBet(0, 0, toNano('1')),
    }),
    406,
  );
  step('stake without enough gas rejected (406)');

  // ----------------------------------------------------- resolve before close
  failedWith(
    await admin.send({
      to: address,
      value: toNano('0.05'),
      body: body.resolveMarket(0, 0),
    }),
    408,
  );
  step('cannot resolve before the deadline (408)');

  // ------------------------------------------------------------- paused mode
  okExit(
    await admin.send({ to: address, value: toNano('0.01'), body: body.setPaused(1) }),
  );
  failedWith(await bet(carol, 0, toNano('1')), 403);
  okExit(
    await admin.send({ to: address, value: toNano('0.01'), body: body.setPaused(0) }),
  );
  step('pause blocks new bets and can be lifted');

  // ------------------------------------------------------------- after close
  blockchain.now = closesAt + 1;
  failedWith(await bet(carol, 0, toNano('1')), 408);
  step('bets after the deadline rejected (408)');

  // ----------------------------------------------------------------- resolve
  failedWith(
    await bob.send({
      to: address,
      value: toNano('0.05'),
      body: body.resolveMarket(0, 0),
    }),
    401,
  );
  step('non-admin cannot resolve (401)');

  const resolveResult = await admin.send({
    to: address,
    value: toNano('0.05'),
    body: body.resolveMarket(0, 0),
  });
  okExit(resolveResult);

  const resolved = await getMarket(blockchain, address, 0);
  assert.strictEqual(resolved.status, 1);
  assert.strictEqual(resolved.winning, 0);
  assert.strictEqual(resolved.winningPool, Number(toNano('4')));
  const expectedFee = (toNano('6') * 200n) / 10000n; // 0.12 TON
  assert.strictEqual(
    await getNumber(blockchain, address, 'get_total_locked'),
    Number(toNano('6') - expectedFee),
  );
  step('market resolved by admin, fee taken from the pool');

  failedWith(
    await admin.send({
      to: address,
      value: toNano('0.05'),
      body: body.resolveMarket(0, 1),
    }),
    411,
  );
  step('double resolution rejected (411)');

  // ------------------------------------------------------------------ claims
  const bobBefore = await bob.getBalance();
  const bobClaim = await bob.send({
    to: address,
    value: toNano('0.02'),
    body: body.claim(0),
  });
  okExit(bobClaim);
  const bobAfter = await bob.getBalance();
  const bobPayout = toNano('1') * (toNano('6') - expectedFee) / toNano('4');
  const bobDelta = bobAfter - bobBefore;
  // The winner pays 0.02 in gas and gets the payout; transaction fees are tiny.
  assert.ok(
    bobDelta > bobPayout - toNano('0.03') && bobDelta < bobPayout,
    `bob payout mismatch: ${bobDelta} vs ${bobPayout}`,
  );
  console.log(`  ✓ winner payout ${Number(bobPayout) / 1e9} TON lands in the wallet`);

  failedWith(
    await bob.send({ to: address, value: toNano('0.02'), body: body.claim(0) }),
    415,
  );
  step('double claim rejected (415)');

  failedWith(
    await carol.send({ to: address, value: toNano('0.02'), body: body.claim(0) }),
    414,
  );
  step('losing option cannot claim (414)');

  okExit(await dave.send({ to: address, value: toNano('0.02'), body: body.claim(0) }));
  const locked = await getNumber(blockchain, address, 'get_total_locked');
  assert.strictEqual(locked, 0, 'all user funds must be claimable/released');
  step('locked funds are zero after every winner claimed');

  // ------------------------------------------------------------------- fees
  const fees = await getNumber(blockchain, address, 'get_fees');
  assert.ok(fees > expectedFee, 'fees must include gas buffers and the rake');
  const adminBefore = await admin.getBalance();
  okExit(
    await admin.send({
      to: address,
      value: toNano('0.01'),
      body: body.withdrawFees(BigInt(fees) - 1_000_000n),
    }),
  );
  const adminAfter = await admin.getBalance();
  assert.ok(adminAfter > adminBefore, 'admin receives withdrawn fees');
  step('fees can be withdrawn without touching locked funds');

  failedWith(
    await admin.send({
      to: address,
      value: toNano('0.01'),
      body: body.withdrawFees(
        BigInt(await getNumber(blockchain, address, 'get_fees')) + toNano('1'),
      ),
    }),
    406,
  );
  step('cannot withdraw more than the accrued fees');

  failedWith(
    await admin.send({ to: address, value: toNano('0.01'), body: body.setFee(5000) }),
    416,
  );
  okExit(
    await admin.send({ to: address, value: toNano('0.01'), body: body.setFee(100) }),
  );
  assert.strictEqual(await getNumber(blockchain, address, 'get_fee_bps'), 100);
  step('fee is capped at 10% and configurable below it');

  // ----------------------------------------------------------------- cancel
  blockchain.now = 1_800_100_000;
  okExit(
    await alice.send({
      to: address,
      value: toNano('0.05'),
      body: body.createMarket(titleHash, blockchain.now + 3600, 2),
    }),
  );
  okExit(await bet(carol, 1, toNano('2'), 1));
  const cancelResult = await admin.send({
    to: address,
    value: toNano('0.05'),
    body: body.cancelMarket(1),
  });
  okExit(cancelResult);
  const cancelled = await getMarket(blockchain, address, 1);
  assert.strictEqual(cancelled.status, 2);
  assert.ok(
    cancelResult.transactions
      .flatMap((tx) => [...tx.outMessages.values()])
      .some((msg) => {
        try {
          return unwrap(msg.body).beginParse().loadUint(32) === EV.cancelled;
        } catch (error) {
          return false;
        }
      }),
    'cancel event emitted',
  );

  const carolBefore = await carol.getBalance();
  okExit(await carol.send({ to: address, value: toNano('0.02'), body: body.claim(1) }));
  const carolDelta = (await carol.getBalance()) - carolBefore;
  assert.ok(
    carolDelta > toNano('1.95') && carolDelta < toNano('2'),
    `refund mismatch: ${carolDelta}`,
  );
  step('cancelled market refunds the full stake');

  // --------------------------------------------------------- safety invariants
  const lockedFinal = await getNumber(blockchain, address, 'get_total_locked');
  assert.strictEqual(lockedFinal, 0);
  const contractState = await blockchain.getContract(address);
  const balance = contractState.balance;
  const feesFinal = await getNumber(blockchain, address, 'get_fees');
  assert.ok(
    balance >= feesFinal,
    'balance must always cover accrued fees',
  );
  step('accounting invariant holds (locked = 0, balance ≥ fees)');

  console.log(`\n${passed} checks passed ✅\n`);
}

main().catch((error) => {
  console.error('\n❌ tests failed\n');
  console.error(error);
  process.exit(1);
});
