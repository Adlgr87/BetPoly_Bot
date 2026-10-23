#!/usr/bin/env node
/**
 * Compiles contracts/escrow.fc and prints the code hash + base64 BoC.
 *
 * Usage: npm run build
 */
const fs = require('fs');
const path = require('path');
const { compileFunc } = require('@ton-community/func-js');
const { Cell } = require('@ton/core');

const ROOT = path.resolve(__dirname, '..');

async function compile() {
  const target = path.join(ROOT, 'escrow.fc');
  const result = await compileFunc({
    targets: [target],
    sources: (file) => {
      try {
        return fs.readFileSync(path.resolve(file), 'utf8');
      } catch (error) {
        return undefined;
      }
    },
  });

  if (result.status !== 'ok') {
    console.error('❌ FunC compilation failed:\n' + result.message);
    process.exit(1);
  }

  const cell = Cell.fromBoc(Buffer.from(result.codeBoc, 'base64'))[0];
  const outDir = path.join(ROOT, 'build');
  fs.mkdirSync(outDir, { recursive: true });
  fs.writeFileSync(path.join(outDir, 'escrow.code.boc'), cell.toBoc());
  fs.writeFileSync(path.join(outDir, 'escrow.fif'), cell.toString());

  console.log('✅ escrow.fc compiled');
  console.log('   code hash : ' + cell.hash().toString('hex'));
  console.log('   code bits : ' + cell.bits.length);
  console.log('   artifact  : contracts/build/escrow.code.boc');
  return cell;
}

if (require.main === module) {
  compile().catch((error) => {
    console.error(error);
    process.exit(1);
  });
}

module.exports = { compile };
