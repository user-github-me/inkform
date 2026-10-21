import assert from 'node:assert/strict';
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { spawnSync } from 'node:child_process';
import test from 'node:test';
import { engineFingerprint, projectRoot, runtimeLocation } from '../../bin/inkform.mjs';

const cli = join(projectRoot, 'bin', 'inkform.mjs');
const metadata = JSON.parse(readFileSync(join(projectRoot, 'package.json'), 'utf8'));
function sandbox(t) {
  const path = mkdtempSync(join(tmpdir(), 'inkform npm test '));
  t.after(() => rmSync(path, { recursive: true, force: true }));
  return path;
}
function run(args, env = {}, cwd = projectRoot) {
  return spawnSync(process.execPath, [cli, ...args], { cwd, env: { ...process.env, ...env }, encoding: 'utf8' });
}

test('help and setup guidance work without preparing or downloading a runtime', t => {
  const temporary = sandbox(t);
  const env = { INKFORM_CACHE_DIR: join(temporary, 'cache') };
  const help = run(['--help'], env);
  assert.equal(help.status, 0, help.stderr);
  assert.match(help.stdout, /inkform setup/);
  const engine = run(['profile', 'list'], env);
  assert.equal(engine.status, 1);
  assert.match(engine.stderr, /Run `inkform setup` once/);
  const setupHelp = run(['setup', '--help'], env);
  assert.equal(setupHelp.status, 0, setupHelp.stderr);
  assert.match(setupHelp.stdout, /Requires network/);
  assert.equal(existsSync(env.INKFORM_CACHE_DIR), false);
});

test('CLI executes through an npm-style symlink', t => {
  const temporary = sandbox(t);
  const link = join(temporary, 'inkform');
  symlinkSync(cli, link);
  const result = spawnSync(process.execPath, [link, '--version'], { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout, `Inkform ${metadata.version}\n`);
});

test('engine forwarding preserves arguments, cwd, spaces, metacharacters and failure status', t => {
  const temporary = sandbox(t);
  const env = { INKFORM_CACHE_DIR: join(temporary, 'cache') };
  const runtime = runtimeLocation(env);
  mkdirSync(dirname(runtime.python), { recursive: true });
  writeFileSync(runtime.python, '#!/usr/bin/env node\nconsole.log(JSON.stringify({ args: process.argv.slice(2), cwd: process.cwd() })); process.exitCode = process.argv.includes("--fail") ? 19 : 0;\n');
  chmodSync(runtime.python, 0o755);
  writeFileSync(join(runtime.directory, 'ready.json'), JSON.stringify({ fingerprint: runtime.fingerprint }));
  const args = ['--data-dir', 'my data', 'document', '--text-file', '$(touch unintended) `echo nope`.txt'];
  const result = run(args, env, temporary);
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), { args: ['-m', 'inkform', ...args], cwd: realpathSync(temporary) });
  assert.equal(existsSync(join(temporary, 'unintended')), false);
  assert.equal(run(['--fail'], env).status, 19);
});

test('skill installation is self-contained and refuses unrelated overwrites', t => {
  const temporary = sandbox(t);
  const target = join(temporary, 'installed skill');
  const result = run(['skill', 'install', '--path', target]);
  assert.equal(result.status, 0, result.stderr);
  for (const file of ['SKILL.md', 'scripts/handwrite.py', 'references/installation.md', 'runtime/bin/inkform.mjs', 'runtime/src/inkform/training.py', 'runtime/src/inkform/schemas/profile.schema.json', 'runtime/LICENSE']) {
    assert.ok(existsSync(join(target, file)), file);
  }
  assert.equal(engineFingerprint(join(target, 'runtime')), engineFingerprint());
  const standalone = spawnSync(process.execPath, [join(target, 'runtime', 'bin', 'inkform.mjs'), '--version'], { cwd: temporary, encoding: 'utf8' });
  assert.equal(standalone.status, 0, standalone.stderr);
  assert.equal(standalone.stdout, `Inkform ${metadata.version}\n`);
  assert.equal(run(['skill', 'install', '--path', target]).status, 1);
  assert.equal(run(['skill', 'install', '--path', target, '--force']).status, 0);
  const unrelated = join(temporary, 'my existing files');
  mkdirSync(unrelated);
  writeFileSync(join(unrelated, 'keep.txt'), 'keep');
  assert.equal(run(['skill', 'install', '--path', unrelated, '--force']).status, 1);
  assert.equal(readFileSync(join(unrelated, 'keep.txt'), 'utf8'), 'keep');
  writeFileSync(join(unrelated, '.inkform-install.json'), '{}');
  assert.equal(run(['skill', 'install', '--path', unrelated, '--force']).status, 1);
  assert.equal(readFileSync(join(unrelated, 'keep.txt'), 'utf8'), 'keep');
});

test('scoped skill upgrades a valid legacy unscoped installation', t => {
  const temporary = sandbox(t);
  const target = join(temporary, 'legacy skill');
  assert.equal(run(['skill', 'install', '--path', target]).status, 0);
  const markerPath = join(target, '.inkform-install.json');
  const runtimePackagePath = join(target, 'runtime', 'package.json');
  const marker = JSON.parse(readFileSync(markerPath, 'utf8'));
  const runtimePackage = JSON.parse(readFileSync(runtimePackagePath, 'utf8'));
  marker.name = runtimePackage.name = 'inkform';
  writeFileSync(markerPath, JSON.stringify(marker));
  writeFileSync(runtimePackagePath, JSON.stringify(runtimePackage));
  assert.equal(run(['skill', 'install', '--path', target, '--force']).status, 0);
  assert.equal(JSON.parse(readFileSync(markerPath, 'utf8')).name, '@nazifbd/inkform');
  assert.equal(JSON.parse(readFileSync(runtimePackagePath, 'utf8')).name, '@nazifbd/inkform');
});

test('skill destinations cannot recurse into source trees, including through symlinks', t => {
  const temporary = sandbox(t);
  for (const directory of ['bin', 'src', 'examples', 'skills']) {
    const nested = join(projectRoot, directory, 'forbidden-install');
    assert.equal(run(['skill', 'install', '--path', nested]).status, 1);
    assert.equal(existsSync(nested), false);
  }
  const alias = join(temporary, 'source alias');
  symlinkSync(join(projectRoot, 'src'), alias, 'dir');
  assert.equal(run(['skill', 'install', '--path', join(alias, 'forbidden-install')]).status, 1);
  assert.equal(existsSync(join(projectRoot, 'src', 'forbidden-install')), false);
});

test('invalid npm options fail without side effects', t => {
  const temporary = sandbox(t);
  const env = { INKFORM_CACHE_DIR: join(temporary, 'cache') };
  for (const args of [['setup', '--unknown'], ['setup', '--python'], ['skill', 'install', '--path'], ['examples', 'extra']]) {
    assert.equal(run(args, env).status, 1, args.join(' '));
  }
  assert.equal(existsSync(env.INKFORM_CACHE_DIR), false);
});

test('an incomplete or changed environment is never treated as ready', t => {
  const temporary = sandbox(t);
  const env = { INKFORM_CACHE_DIR: join(temporary, 'cache') };
  const runtime = runtimeLocation(env);
  mkdirSync(dirname(runtime.python), { recursive: true });
  writeFileSync(runtime.python, 'incomplete');
  assert.equal(run(['profile', 'list'], env).status, 1);
  writeFileSync(join(runtime.directory, 'ready.json'), JSON.stringify({ fingerprint: 'stale' }));
  assert.equal(run(['profile', 'list'], env).status, 1);
});
