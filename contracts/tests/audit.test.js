#!/usr/bin/env node
/**
 * Tests de `scripts/audit.js`: la lista de excepciones no puede crecer por
 * descuido ni quedarse con entradas muertas (Fase 7).
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const { evaluate, advisories } = require('../scripts/audit');

const ROOT = path.join(__dirname, '..');
const REAL_ALLOWLIST = path.join(ROOT, 'audit-allowlist.json');

const GHSA = 'GHSA-mrhx-c7xc-m3w5';

function report(vulnerabilities) {
  return { vulnerabilities };
}

const ZOD = report({
  zod: {
    severity: 'high',
    via: [
      {
        name: 'zod',
        title: 'DoS al validar colecciones',
        url: `https://github.com/advisories/${GHSA}`,
        range: '<4.6.6',
      },
    ],
  },
  '@ton/ton': { severity: 'high', via: ['zod'] },
});

let passed = 0;

function check(name, fn) {
  fn();
  passed += 1;
  console.log(`  ✓ ${name}`);
}

console.log('\nAuditoría de dependencias del contrato (Fase 7)\n');

check('un advisory alto sin excepción falla', () => {
  const problems = evaluate(ZOD, {}, '2026-10-23');
  assert.strictEqual(problems.length, 1);
  assert.match(problems[0], /sin excepción/);
});

check('una cadena transitiva no cuenta como advisory propio', () => {
  const found = advisories(ZOD);
  assert.deepStrictEqual([...found.keys()], [GHSA]);
});

check('una excepción vigente y justificada deja el build verde', () => {
  const allowlist = { [GHSA]: { module: 'zod', reason: 'sin parche', expires: '2026-12-31' } };
  assert.deepStrictEqual(evaluate(ZOD, allowlist, '2026-10-23'), []);
});

check('una excepción caducada vuelve a fallar', () => {
  const allowlist = { [GHSA]: { module: 'zod', reason: 'sin parche', expires: '2026-01-01' } };
  const problems = evaluate(ZOD, allowlist, '2026-10-23');
  assert.strictEqual(problems.length, 1);
  assert.match(problems[0], /caducada/);
});

check('una excepción sin justificación falla', () => {
  const allowlist = { [GHSA]: { module: 'zod', expires: '2026-12-31' } };
  assert.match(evaluate(ZOD, allowlist, '2026-10-23')[0], /sin justificación/);
});

check('una excepción obsoleta falla para que la lista solo encoja', () => {
  const allowlist = { [GHSA]: { module: 'zod', reason: 'x', expires: '2026-12-31' }, 'GHSA-vieja': { module: 'y', reason: 'x', expires: '2026-12-31' } };
  const problems = evaluate(ZOD, allowlist, '2026-10-23');
  assert.strictEqual(problems.length, 1);
  assert.match(problems[0], /obsoleta/);
});

check('un advisory moderado no bloquea', () => {
  const moderate = report({
    left: { severity: 'moderate', via: [{ name: 'left', title: 't', url: 'https://github.com/advisories/GHSA-x' }] },
  });
  assert.deepStrictEqual(evaluate(moderate, {}, '2026-10-23'), []);
});

check('la lista real es JSON válido con motivo y caducidad', () => {
  const allowlist = JSON.parse(fs.readFileSync(REAL_ALLOWLIST, 'utf8'));
  for (const [id, entry] of Object.entries(allowlist)) {
    assert.match(id, /^(GHSA|CVE)-/);
    assert.ok(entry.reason && entry.reason.length > 20, `${id} necesita un motivo real`);
    assert.match(entry.expires, /^\d{4}-\d{2}-\d{2}$/, `${id} necesita caducidad ISO`);
  }
});

check('la lista real no tiene entradas caducadas', () => {
  const allowlist = JSON.parse(fs.readFileSync(REAL_ALLOWLIST, 'utf8'));
  const today = new Date().toISOString().slice(0, 10);
  for (const [id, entry] of Object.entries(allowlist)) {
    assert.ok(entry.expires >= today, `${id} caducó el ${entry.expires}`);
  }
});

console.log(`\n${passed} checks passed ✅\n`);
