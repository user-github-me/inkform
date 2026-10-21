#!/usr/bin/env node
/** npm entry point. Installation never runs Python or downloads dependencies. */
import { createHash } from 'node:crypto';
import { cpSync, existsSync, mkdirSync, readFileSync, readdirSync, realpathSync, rmSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { basename, dirname, join, resolve, sep } from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

export const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const metadata = JSON.parse(readFileSync(join(projectRoot, 'package.json'), 'utf8'));

export function engineFingerprint(root = projectRoot) {
  const hash = createHash('sha256');
  function add(path, relative) {
    for (const entry of readdirSync(path, { withFileTypes: true }).sort((a, b) => a.name.localeCompare(b.name))) {
      const child = join(path, entry.name);
      const key = `${relative}/${entry.name}`;
      if (entry.isDirectory() && entry.name !== '__pycache__') add(child, key);
      else if (entry.isFile() && /\.(py|json)$/.test(entry.name)) hash.update(key).update('\0').update(readFileSync(child));
    }
  }
  hash.update(readFileSync(join(root, 'pyproject.toml')));
  add(join(root, 'src', 'inkform'), 'src/inkform');
  return hash.digest('hex');
}

export function runtimeLocation(env = process.env, platform = process.platform, home = homedir()) {
  const base = env.INKFORM_CACHE_DIR ? resolve(env.INKFORM_CACHE_DIR)
    : platform === 'darwin' ? join(home, 'Library', 'Caches', 'Inkform', 'runtime')
      : platform === 'win32' ? join(env.LOCALAPPDATA || join(home, 'AppData', 'Local'), 'Inkform', 'runtime')
        : join(env.XDG_CACHE_HOME || join(home, '.cache'), 'inkform', 'runtime');
  const fingerprint = engineFingerprint();
  const directory = join(base, `${metadata.version}-${fingerprint.slice(0, 16)}`);
  return { directory, fingerprint, python: join(directory, 'venv', platform === 'win32' ? 'Scripts/python.exe' : 'bin/python') };
}

function ready(runtime) {
  try {
    const marker = JSON.parse(readFileSync(join(runtime.directory, 'ready.json'), 'utf8'));
    return marker.fingerprint === runtime.fingerprint && existsSync(runtime.python);
  } catch { return false; }
}

function invoke(command, args, options = {}) {
  const result = spawnSync(command, args, { stdio: 'inherit', ...options });
  if (result.error) throw new Error(`Cannot run ${command}: ${result.error.message}`);
  if (result.signal) throw new Error(`${command} stopped by ${result.signal}.`);
  if (result.status !== 0) throw new Error(`${command} exited with status ${result.status}.`);
}

function available(command) {
  const result = spawnSync(command, ['--version'], { stdio: 'ignore' });
  return !result.error && result.status === 0;
}

function findPython(requested) {
  const explicit = requested && /^3\.\d+$/.test(requested) ? `python${requested}` : requested;
  for (const command of explicit ? [explicit] : ['python3.12', 'python3.13', 'python3.11', 'python3', 'python']) {
    const result = spawnSync(command, ['-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'], { stdio: 'ignore' });
    if (!result.error && result.status === 0) return command;
  }
  throw new Error('Python 3.11+ is required. Install Python or uv, then run `inkform setup` again. You may select an interpreter with `inkform setup --python /path/to/python`.');
}

export function parseOptions(args, allowed) {
  const options = {};
  for (let i = 0; i < args.length; i++) {
    const flag = args[i];
    if (!Object.hasOwn(allowed, flag)) throw new Error(`Unknown option: ${flag}`);
    if (allowed[flag] === 'value') {
      if (!args[i + 1] || args[i + 1].startsWith('--')) throw new Error(`${flag} requires a value.`);
      options[flag] = args[++i];
    } else options[flag] = true;
  }
  return options;
}

export function setup(args) {
  const options = parseOptions(args, { '--python': 'value', '--help': 'flag', '-h': 'flag' });
  if (options['--help'] || options['-h']) {
    console.log('Usage: inkform setup [--python VERSION_OR_PATH]\n\nInstalls the bundled Python engine and dependencies in a private cache.\nUses uv when available (default Python 3.12, downloaded if needed),\notherwise an installed Python 3.11+ with venv and pip. Requires network\naccess on the first run. No handwriting samples are uploaded.\nINKFORM_CACHE_DIR overrides the runtime cache directory.');
    return 0;
  }
  const runtime = runtimeLocation();
  if (ready(runtime)) {
    console.log(`Inkform ${metadata.version} is ready.\nRuntime: ${runtime.python}`);
    return 0;
  }
  mkdirSync(runtime.directory, { recursive: true });
  const lock = join(runtime.directory, 'setup.lock');
  try { mkdirSync(lock); } catch (error) {
    if (error.code === 'EEXIST') throw new Error(`Another setup may be running. If it has stopped, remove only this lock directory and retry: ${lock}`);
    throw error;
  }
  try {
    console.log(`Preparing Inkform ${metadata.version} in ${runtime.directory}`);
    const venv = join(runtime.directory, 'venv');
    if (available('uv')) {
      if (!existsSync(runtime.python)) invoke('uv', ['venv', '--python', options['--python'] || '3.12', venv]);
      invoke('uv', ['pip', 'install', '--python', runtime.python, projectRoot]);
    } else {
      const python = findPython(options['--python']);
      if (!existsSync(runtime.python)) invoke(python, ['-m', 'venv', venv]);
      invoke(runtime.python, ['-m', 'pip', 'install', projectRoot]);
    }
    invoke(runtime.python, ['-m', 'inkform', '--version']);
    writeFileSync(join(runtime.directory, 'ready.json'), `${JSON.stringify({ version: metadata.version, fingerprint: runtime.fingerprint }, null, 2)}\n`);
    console.log('Ready. Run `inkform --help` or `inkform skill install`.');
    return 0;
  } finally { rmSync(lock, { recursive: true, force: true }); }
}

export function installSkill(args) {
  const options = parseOptions(args, { '--path': 'value', '--force': 'flag', '--help': 'flag', '-h': 'flag' });
  if (options['--help'] || options['-h']) {
    console.log('Usage: inkform skill install [--path DIRECTORY] [--force]\n\nCopies the skill and its runtime into ~/.agents/skills/inkform by default.\n--path is the final skill directory, not its parent. An existing directory\nis preserved unless --force is supplied. This command does not download\ndependencies; run `inkform setup` before using the skill.');
    return 0;
  }
  const destination = canonicalPath(resolve(options['--path'] || join(homedir(), '.agents', 'skills', 'inkform')));
  const source = join(projectRoot, 'skills', 'inkform');
  const canonicalRoot = realpathSync(projectRoot);
  const within = (path, parent) => path === parent || path.startsWith(`${parent}${sep}`);
  if (within(canonicalRoot, destination) || ['bin', 'src', 'examples', 'skills'].some(item => within(destination, join(canonicalRoot, item)))) {
    throw new Error('The skill destination must not contain the package or be inside any of its source directories (bin, src, examples, skills).');
  }
  if (existsSync(destination)) {
    if (!options['--force']) throw new Error(`The skill destination already exists: ${destination}. Choose another --path or use --force to replace an Inkform installation.`);
    // Force replaces only a directory carrying our marker, never arbitrary user files.
    let previous;
    try {
      previous = JSON.parse(readFileSync(join(destination, '.inkform-install.json'), 'utf8'));
      const runtimePackage = JSON.parse(readFileSync(join(destination, 'runtime', 'package.json'), 'utf8'));
      const knownNames = new Set([metadata.name, 'inkform']);
      if (!knownNames.has(previous.name) || runtimePackage.name !== previous.name || runtimePackage.version !== previous.version || !/^\d+\.\d+\.\d+(?:[-+].*)?$/.test(previous.version) || !/^[a-f0-9]{64}$/.test(previous.fingerprint) || !existsSync(join(destination, 'SKILL.md')) || engineFingerprint(join(destination, 'runtime')) !== previous.fingerprint) previous = null;
    } catch { previous = null; }
    if (!previous) throw new Error('Refusing to replace a directory without a valid Inkform installation marker and runtime. Choose an empty destination.');
    rmSync(destination, { recursive: true });
  }
  mkdirSync(destination, { recursive: true });
  cpSync(source, destination, { recursive: true, filter: path => !path.includes('__pycache__') && !path.endsWith('.pyc') });
  const bundled = join(destination, 'runtime');
  mkdirSync(bundled);
  for (const item of ['package.json', 'pyproject.toml', 'README.md', 'LICENSE', 'bin', 'src', 'examples', 'skills/inkform']) {
    const from = join(projectRoot, item);
    if (existsSync(from)) cpSync(from, join(bundled, item), { recursive: true, filter: path => !path.includes('__pycache__') && !path.includes('.egg-info') && !path.endsWith('.pyc') });
  }
  writeFileSync(join(destination, '.inkform-install.json'), `${JSON.stringify({ name: metadata.name, version: metadata.version, fingerprint: engineFingerprint() }, null, 2)}\n`);
  console.log(`Installed Inkform skill: ${destination}\nRestart or reload your agent to discover it.\nTo set up its bundled runtime: node "${join(bundled, 'bin', 'inkform.mjs')}" setup`);
  return 0;
}

function canonicalPath(path) {
  const missing = [];
  let ancestor = path;
  while (!existsSync(ancestor)) {
    missing.unshift(basename(ancestor));
    const parent = dirname(ancestor);
    if (parent === ancestor) throw new Error(`Cannot resolve destination: ${path}`);
    ancestor = parent;
  }
  return join(realpathSync(ancestor), ...missing);
}

function help() {
  console.log(`Inkform ${metadata.version} — private handwriting profiles and vector PDFs\n\nInstall: npm install -g ${metadata.name}\n\nStart here:\n  inkform setup                    Install the Python engine and dependencies\n  inkform skill install            Install the bundled agent skill\n  inkform examples                 Show the public example directory\n\nHandwriting workflow (after setup):\n  inkform train --id WRITER --samples FOLDER\n  inkform profile coverage WRITER --document note.json\n  inkform render note.json --profile WRITER --output note.pdf\n\nCommands: train, profile, document, render, check, compare\nRun a command with --help for its options.\nTraining requires reviewed observations; importing photos alone does not\ncomplete a handwriting profile. Files stay local.\n\nSetup: inkform setup --help\nSkill: inkform skill install --help\nDocs:  https://github.com/user-github-me/inkform`);
}

export function main(args = process.argv.slice(2)) {
  if (!args.length || (args.length === 1 && ['--help', '-h'].includes(args[0]))) { help(); return 0; }
  if (args.length === 1 && args[0] === '--version') { console.log(`Inkform ${metadata.version}`); return 0; }
  if (args[0] === 'setup') return setup(args.slice(1));
  if (args[0] === 'examples') {
    if (args.length > 1) throw new Error('Usage: inkform examples');
    console.log(join(projectRoot, 'examples')); return 0;
  }
  if (args[0] === 'skill') {
    if (args[1] === 'install') return installSkill(args.slice(2));
    if (args[1] === 'path' && args.length === 2) { console.log(join(projectRoot, 'skills', 'inkform')); return 0; }
    throw new Error('Usage: inkform skill install [--path DIRECTORY] [--force], or inkform skill path');
  }
  const runtime = runtimeLocation();
  if (!ready(runtime)) throw new Error('Inkform needs its Python engine. Run `inkform setup` once, then repeat this command.');
  const result = spawnSync(runtime.python, ['-m', 'inkform', ...args], { stdio: 'inherit' });
  if (result.error) throw new Error(`Cannot launch the engine: ${result.error.message}`);
  if (result.signal) throw new Error(`The engine stopped by ${result.signal}.`);
  return result.status ?? 1;
}

if (process.argv[1] && realpathSync(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { process.exitCode = main(); }
  catch (error) { console.error(`Inkform: ${error.message}`); process.exitCode = 1; }
}
