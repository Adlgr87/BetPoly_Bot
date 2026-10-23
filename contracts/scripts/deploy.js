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
//: Pin de paridad con el bot; el despliegue no escribe nada si no coincide.
const CONTRACT_SYNC = path.resolve(ROOT, '..', 'tests', 'test_contract_sync.py');

function required(name) {
  const value = (process.env[name] || '').trim();
  if (!value) {
    console.error(`❌ Falta la variable de entorno ${name}`);
    process.exit(1);
  }
  return value;
}

/**
 * Hash del bytecode pineado en `tests/test_contract_sync.py`.
 *
 * Desplegar un bytecode distinto al que verifican los tests dejaría al bot
 * hablando otro protocolo: se comprueba antes de enviar nada.
 */
function pinnedCodeHash(file = CONTRACT_SYNC) {
  const source = fs.readFileSync(file, 'utf8');
  const match = source.match(/CODE_HASH\s*=\s*"([0-9a-f]+)"/);
  if (!match) {
    throw new Error(`no se encontró CODE_HASH en ${file}`);
  }
  return match[1];
}

function verifyCodeHash(actual, pinned) {
  if (actual !== pinned) {
    throw new Error(
      `el bytecode compilado (${actual}) no coincide con CODE_HASH (${pinned}); ` +
        'actualiza el pin, el README y el despliegue en el mismo commit',
    );
  }
  return true;
}

/**
 * Espera a que la cuenta esté `active`. Devuelve `false` si no lo consigue:
 * el llamador **no** debe escribir `deploy.json` en ese caso (F-13).
 */
async function waitForActive(client, address, { attempts = 30, delayMs = 4000 } = {}) {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, delayMs));
    const current = await client.getContractState(address);
    if (current.state === 'active') {
      return true;
    }
  }
  return false;
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
  const codeHash = code.hash().toString('hex');
  verifyCodeHash(codeHash, pinnedCodeHash());

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
    console.log(`   code hash : ${codeHash} (coincide con CODE_HASH)`);
    return 0;
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
  }

  // Confirmación explícita: sin `active` no hay despliegue que reportar.
  if (!(await waitForActive(client, contract.address))) {
    console.error('❌ El contrato no alcanzó el estado "active"; no se escribe deploy.json');
    return 1;
  }
  console.log('✅ Contrato activo');

  const output = {
    network,
    address: contract.address.toString(),
    addressRaw: `0:${contract.address.hash.toString('hex')}`,
    codeHash,
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
  return 0;
}

if (require.main === module) {
  main()
    .then((code) => {
      process.exitCode = code;
    })
    .catch((error) => {
      console.error('❌ ' + (error && error.message ? error.message : error));
      process.exitCode = 1;
    });
}

module.exports = { main, pinnedCodeHash, verifyCodeHash, waitForActive };
