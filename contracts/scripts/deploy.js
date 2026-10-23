#!/usr/bin/env node
/**
 * Deploys contracts/escrow.fc to TON.
 *
 * The mnemonic below is the *operator's* wallet. It is used once, from your
 * laptop, to publish the contract. The BetPoly backend never needs it: the bot
 * only reads the chain and prepares unsigned transactions for the admin wallet.
 *
 * Usage:
 *   MNEMONIC="word word ... 24 words" \
 *   ADMIN_ADDRESS="EQ..." \
 *   INDEXER_ADDRESS="EQ..." \
 *   node scripts/deploy.js
 *
 * Optional environment:
 *   NETWORK=mainnet|testnet        (default: testnet)
 *   TON_API_KEY=...                (toncenter key, strongly recommended)
 *   FEE_BPS=200                    (2 % platform fee, max 1000)
 *   MIN_STAKE_NANO=100000000       (0.1 TON)
 *   DEPLOY_VALUE_NANO=100000000    (0.1 TON sent with the deploy message)
 */
const fs = require('fs');
const path = require('path');
const { mnemonicToPrivateKey } = require('@ton/crypto');
const { internal, toNano } = require('@ton/core');
const { TonClient, WalletContractV4 } = require('@ton/ton');
const { compile } = require('./build');

const ROOT = path.resolve(__dirname, '..');

function required(name) {
  const value = (process.env[name] || '').trim();
  if (!value) {
    console.error(`❌ Falta la variable de entorno ${name}`);
    process.exit(1);
  }
  return value;
}

async function main() {
  const dryRun = process.env.DRY_RUN === '1';
  const network = (process.env.NETWORK || 'testnet').toLowerCase();
  if (!['mainnet', 'testnet'].includes(network)) {
    throw new Error('NETWORK debe ser mainnet o testnet');
  }

  const mnemonic = dryRun ? [] : required('MNEMONIC').split(/\s+/);
  if (!dryRun && mnemonic.length !== 24) {
    throw new Error('MNEMONIC debe tener 24 palabras');
  }
  const adminAddress = required('ADMIN_ADDRESS');
  const indexerAddress = required('INDEXER_ADDRESS');
  const feeBps = Number(process.env.FEE_BPS || 200);
  const minStake = BigInt(process.env.MIN_STAKE_NANO || '100000000');
  const deployValue = BigInt(process.env.DEPLOY_VALUE_NANO || '100000000');

  if (!Number.isInteger(feeBps) || feeBps < 0 || feeBps > 1000) {
    throw new Error('FEE_BPS debe ser un entero entre 0 y 1000 (10 %)');
  }
  if (minStake <= 0n) {
    throw new Error('MIN_STAKE_NANO debe ser positivo');
  }

  const { Address, beginCell } = require('@ton/core');
  const admin = Address.parse(adminAddress);
  const indexer = Address.parse(indexerAddress);

  const code = await compile();

  const data = beginCell()
    .storeAddress(admin)
    .storeAddress(indexer)
    .storeUint(feeBps, 16)
    .storeCoins(minStake)
    .storeUint(0, 1) // paused
    .storeUint(0, 32) // seqno
    .storeCoins(0) // total_locked
    .storeCoins(0) // fees
    .storeDict(null) // markets
    .endCell();

  const address = require('@ton/core').contractAddress(0, { code, data });
  const contract = { address, init: { code, data } };

  if (dryRun) {
    console.log('🧪 DRY RUN — no se envía ninguna transacción');
    console.log(`   address   : ${address.toString()}`);
    console.log(`   code hash : ${code.hash().toString('hex')}`);
    return;
  }

  const endpoint =
    network === 'mainnet' ? 'https://toncenter.com/api/v2/jsonRPC' : 'https://testnet.toncenter.com/api/v2/jsonRPC';
  const client = new TonClient({
    endpoint,
    apiKey: process.env.TON_API_KEY || undefined,
  });

  const keyPair = await mnemonicToPrivateKey(mnemonic);
  const wallet = client.open(
    WalletContractV4.create({ workchain: 0, publicKey: keyPair.publicKey })
  );

  const state = await client.getContractState(contract.address);
  if (state.state !== 'uninit') {
    console.log(`ℹ️  El contrato ya está desplegado en ${contract.address.toString()}`);
  } else {
    console.log(`⏳ Desplegando en ${contract.address.toString()} (${network})...`);
    const seqno = await wallet.getSeqno();
    await wallet.sendTransfer({
      seqno,
      secretKey: keyPair.secretKey,
      messages: [
        internal({
          to: contract.address,
          value: deployValue,
          init: contract.init,
          body: beginCell().storeUint(0, 32).endCell(),
        }),
      ],
    });
    // Give the network a moment, then confirm.
    for (let attempt = 0; attempt < 30; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 4000));
      const current = await client.getContractState(contract.address);
      if (current.state === 'active') {
        console.log('✅ Contrato activo');
        break;
      }
    }
  }

  const output = {
    network,
    address: contract.address.toString(),
    addressRaw: `0:${contract.address.hash.toString('hex')}`,
    codeHash: code.hash().toString('hex'),
    admin: admin.toString(),
    indexer: indexer.toString(),
    feeBps,
    minStakeNano: minStake.toString(),
    deployedAt: new Date().toISOString(),
  };

  const outFile = path.join(ROOT, 'build', 'deploy.json');
  fs.mkdirSync(path.dirname(outFile), { recursive: true });
  fs.writeFileSync(outFile, JSON.stringify(output, null, 2) + '\n');

  console.log('\nConfigura en el backend:');
  console.log(`  ESCROW_ADDRESS=${output.address}`);
  console.log(`  ESCROW_ADMIN_ADDRESS=${output.admin}`);
  console.log(`  MIN_STAKE_NANO=${output.minStakeNano}`);
  console.log(`\nArtefacto: ${path.relative(process.cwd(), outFile)}`);
}

if (require.main === module) {
  main().catch((error) => {
    console.error('❌ ' + (error && error.message ? error.message : error));
    process.exit(1);
  });
}

module.exports = { main };
