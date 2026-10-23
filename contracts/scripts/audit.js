#!/usr/bin/env node
/**
 * `npm audit` con excepciones explícitas y caducables (Fase 7).
 *
 * Un advisory sin excepción falla el build. Una excepción caducada o una
 * excepción que ya no corresponde a ningún advisory también fallan: la lista
 * solo puede encogerse, igual que el baseline de `tests/test_wiring.py`.
 *
 * Uso:
 *   node scripts/audit.js                # audita y aplica audit-allowlist.json
 *   node scripts/audit.js --report-only  # informa sin fallar (diagnóstico local)
 */
const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const ROOT = path.join(__dirname, '..');
const ALLOWLIST_PATH = path.join(ROOT, 'audit-allowlist.json');

/** Severidades que bloquean el build (el resto solo se informa). */
const BLOCKING = new Set(['high', 'critical']);

const RANK = { info: 0, low: 1, moderate: 2, high: 3, critical: 4 };

/** Advisories reales del informe, indexados por id (GHSA-…/CVE-…). */
function advisories(report) {
  const found = new Map();
  for (const [name, vuln] of Object.entries(report.vulnerabilities || {})) {
    for (const via of vuln.via || []) {
      if (typeof via !== 'object' || !via.url) continue; // cadena transitiva, no advisory
      const id = String(via.url).split('/').pop();
      const candidate = {
        id,
        module: via.name || name,
        severity: vuln.severity || 'info',
        title: via.title || '',
        range: via.range || '',
      };
      const previous = found.get(id);
      if (!previous || RANK[candidate.severity] > RANK[previous.severity]) {
        found.set(id, candidate);
      }
    }
  }
  return found;
}

/**
 * Devuelve la lista de problemas. Lista vacía = build verde.
 *
 * @param {object} report informe de `npm audit --json`
 * @param {object} allowlist `{ "<id>": { module, severity, reason, expires } }`
 * @param {string} today fecha ISO (YYYY-MM-DD) para decidir caducidades
 */
function evaluate(report, allowlist, today) {
  const problems = [];
  const found = advisories(report);

  for (const advisory of [...found.values()].sort((a, b) => a.id.localeCompare(b.id))) {
    const exception = allowlist[advisory.id];
    if (!exception) {
      if (BLOCKING.has(advisory.severity)) {
        problems.push(
          `advisory sin excepción: ${advisory.id} (${advisory.severity}) en ` +
            `${advisory.module} — ${advisory.title}`,
        );
      }
      continue;
    }
    if (exception.expires && exception.expires < today) {
      problems.push(`excepción caducada: ${advisory.id} venció el ${exception.expires}`);
    }
    if (!exception.reason) {
      problems.push(`excepción sin justificación: ${advisory.id}`);
    }
  }

  for (const id of Object.keys(allowlist)) {
    if (!found.has(id)) {
      problems.push(`excepción obsoleta: ${id} ya no aparece en npm audit; elimínala`);
    }
  }
  return problems;
}

function auditReport() {
  try {
    return JSON.parse(
      execFileSync('npm', ['audit', '--json'], { cwd: ROOT, encoding: 'utf8', stdio: ['ignore', 'pipe', 'ignore'] }),
    );
  } catch (error) {
    // npm sale con código ≠ 0 cuando hay advisories: el JSON sigue siendo válido.
    if (error.stdout) return JSON.parse(error.stdout);
    throw error;
  }
}

function main(argv) {
  const allowlist = JSON.parse(fs.readFileSync(ALLOWLIST_PATH, 'utf8'));
  const today = new Date().toISOString().slice(0, 10);
  const problems = evaluate(auditReport(), allowlist, today);

  if (problems.length === 0) {
    console.log('✅ npm audit: sin advisories altos/críticos fuera de la lista de excepciones');
    return 0;
  }
  for (const problem of problems) console.error(`❌ ${problem}`);
  if (argv.includes('--report-only')) {
    console.error('(informe local: no falla el build)');
    return 0;
  }
  return 1;
}

module.exports = { advisories, evaluate, BLOCKING };

if (require.main === module) {
  process.exitCode = main(process.argv.slice(2));
}
