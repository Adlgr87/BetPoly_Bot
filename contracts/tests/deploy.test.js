#!/usr/bin/env node
/**
 * Tests de `scripts/deploy.js` (F-13). No tocan la red: se prueba el
 * guardarraíl que impide reportar un despliegue que no ocurrió.
 */
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const { pinnedCodeHash, verifyCodeHash, waitForActive } = require('../scripts/deploy');

let passed = 0;

function check(name, fn) {
  fn();
  passed += 1;
  console.log(`  ✓ ${name}`);
}

async function checkAsync(name, fn) {
  await fn();
  passed += 1;
  console.log(`  ✓ ${name}`);
}

async function main() {
  console.log('\nDespliegue del contrato (Fase 7)\n');

  check('el pin real se lee de tests/test_contract_sync.py', () => {
    const pinned = pinnedCodeHash();
    assert.match(pinned, /^[0-9a-f]{64}$/);
  });

  check('un hash distinto al pin no despliega', () => {
    assert.throws(
      () => verifyCodeHash('00'.repeat(32), '11'.repeat(32)),
      /no coincide con CODE_HASH/,
    );
  });

  check('un pin sin CODE_HASH es un error explícito', () => {
    const file = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'betpoly-')), 'test_contract_sync.py');
    fs.writeFileSync(file, 'CODE_HASH_OLD = "abc"\n');
    assert.throws(() => pinnedCodeHash(file), /CODE_HASH/);
  });

  await checkAsync('el despliegue se da por bueno cuando la cuenta queda activa', async () => {
    const states = ['uninit', 'active'];
    const client = {
      getContractState: async () => ({ state: states.shift() }),
    };
    assert.strictEqual(await waitForActive(client, 'x', { attempts: 3, delayMs: 0 }), true);
  });

  await checkAsync('sin estado "active" no se confirma el despliegue', async () => {
    const client = { getContractState: async () => ({ state: 'uninit' }) };
    assert.strictEqual(await waitForActive(client, 'x', { attempts: 3, delayMs: 0 }), false);
  });

  console.log(`\n${passed} checks passed ✅\n`);
}

main().catch((error) => {
  console.error('\n❌ tests failed\n');
  console.error(error);
  process.exit(1);
});
