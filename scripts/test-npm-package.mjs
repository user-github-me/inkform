/** Real npm tarball smoke test; explicit setup may download Python dependencies. */
import assert from 'node:assert/strict';
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { projectRoot } from '../bin/inkform.mjs';

const temporary = mkdtempSync(join(tmpdir(), 'inkform packed smoke '));
const metadata = JSON.parse(readFileSync(join(projectRoot, 'package.json'), 'utf8'));
const env = { ...process.env, INKFORM_CACHE_DIR: join(temporary, 'runtime'), INKFORM_DATA_DIR: join(temporary, 'private'), npm_config_userconfig: join(temporary, '.npmrc') };
// Local tarball installation needs no user registry credentials or global script allowances.
for (const key of Object.keys(env)) if (key.toLowerCase() === 'npm_config_allow_scripts') delete env[key];
writeFileSync(env.npm_config_userconfig, '');
function command(executable, args, cwd = temporary) {
  const result = spawnSync(executable, args, { cwd, env, encoding: 'utf8', maxBuffer: 8 * 1024 * 1024 });
  assert.equal(result.status, 0, `${executable} ${args.join(' ')}\n${result.stdout}\n${result.stderr}`);
  return result.stdout.trim();
}
try {
  const payload = JSON.parse(command('npm', ['pack', '--json', '--pack-destination', temporary], projectRoot));
  const packed = Array.isArray(payload) ? payload[0] : Object.values(payload)[0];
  assert.equal(packed.name, metadata.name);
  assert.equal(packed.version, metadata.version);
  const allowed = /^(?:package\.json|README\.md|LICENSE|pyproject\.toml|bin\/[^/]+\.mjs|src\/inkform\/[^/]+\.py|src\/inkform\/schemas\/[^/]+\.json|skills\/inkform\/SKILL\.md|skills\/inkform\/agents\/[^/]+\.yaml|skills\/inkform\/references\/[^/]+\.(?:md|json)|skills\/inkform\/scripts\/[^/]+\.py|examples\/[^/]+\.json)$/;
  for (const file of packed.files) assert.match(file.path, allowed, `Unexpected npm content: ${file.path}`);
  command('npm', ['install', '--ignore-scripts', '--no-audit', '--no-fund', '--prefix', temporary, join(temporary, packed.filename)]);
  const installedPackage = join(temporary, 'node_modules', ...metadata.name.split('/'));
  assert.equal(JSON.parse(readFileSync(join(installedPackage, 'package.json'), 'utf8')).name, metadata.name);
  const binary = join(temporary, 'node_modules', '.bin', 'inkform');
  assert.equal(command(binary, ['--version']), `Inkform ${metadata.version}`);
  command(binary, ['setup']);
  const examples = command(binary, ['examples']);
  const pdf = join(temporary, 'demo.pdf');
  const report = JSON.parse(command(binary, ['render', join(examples, 'math.json'), '--profile', join(examples, 'synthetic-profile.json'), '--output', pdf]));
  assert.equal(report.ok, true);
  assert.ok(existsSync(pdf));
  const check = JSON.parse(command(binary, ['check', pdf]));
  assert.equal(check.ok, true);
  assert.equal(check.handwriting_raster_objects, 0);
  assert.equal(check.font_text_operators, 0);
  assert.ok(check.vector_path_count > 0);
  const correctionsPdf = join(temporary, 'corrections.pdf');
  command(binary, ['render', join(examples, 'corrections.json'), '--profile', join(examples, 'synthetic-profile.json'), '--output', correctionsPdf]);
  const corrections = JSON.parse(readFileSync(join(temporary, 'corrections.build.json'), 'utf8'));
  assert.ok(corrections.corrections.some(edit => edit.kind === 'explicit'));
  assert.ok(corrections.visible_symbol_count > corrections.rendered_symbol_count);
  assert.equal(corrections.required_symbol_count, corrections.rendered_symbol_count);
  const installed = join(temporary, 'standalone skill');
  command(binary, ['skill', 'install', '--path', installed]);
  // Delete the npx/global-style package to verify that the installed skill survives it.
  rmSync(join(temporary, 'node_modules'), { recursive: true });
  const standalone = join(installed, 'runtime', 'bin', 'inkform.mjs');
  assert.equal(command(process.execPath, [standalone, '--version']), `Inkform ${metadata.version}`);
  command(process.execPath, [standalone, 'render', join(installed, 'runtime', 'examples', 'math.json'), '--profile', join(installed, 'runtime', 'examples', 'synthetic-profile.json'), '--output', join(temporary, 'from-skill.pdf')]);
  assert.ok(existsSync(join(temporary, 'from-skill.pdf')));
  assert.match(readFileSync(join(installed, 'SKILL.md'), 'utf8'), /training/);
  console.log(`Packed npm smoke passed: ${packed.files.length} public files, real vector PDF, independent installed skill.`);
} finally {
  if (process.env.INKFORM_KEEP_NPM_SMOKE === '1') console.log(`Kept smoke artifacts: ${temporary}`);
  else rmSync(temporary, { recursive: true, force: true });
}
