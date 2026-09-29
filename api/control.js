import { timingSafeEqual } from 'node:crypto';
import { Sandbox } from '@vercel/sandbox';

const ROOT = '/vercel/actionbench';
const STATE = `${ROOT}/.cloud-state`;
const REPO = 'https://github.com/11adyy/actionbench.git';
const SESSION_MS = 44 * 60 * 1000;

function json(value, status = 200) {
  return Response.json(value, { status, headers: { 'Cache-Control': 'no-store' } });
}

function authorized(request) {
  const expected = process.env.ACTIONBENCH_CONTROL_TOKEN;
  if (!expected || expected.length < 32) return false;
  const header = request.headers.get('authorization') || '';
  const received = header.startsWith('Bearer ') ? header.slice(7) : '';
  const a = Buffer.from(expected);
  const b = Buffer.from(received);
  return a.length === b.length && timingSafeEqual(a, b);
}

function sandboxName() {
  const name = process.env.ACTIONBENCH_SANDBOX_NAME || 'actionbench-pilot-001';
  if (!/^[a-z0-9][a-z0-9-]{2,62}$/.test(name)) throw new Error('Invalid ACTIONBENCH_SANDBOX_NAME');
  return name;
}

async function existingSandbox() {
  try { return await Sandbox.get({ name: sandboxName() }); }
  catch (error) {
    if (error?.response?.status === 404) return null;
    throw error;
  }
}

async function read(sandbox, path, limit = 12000) {
  const contents = await sandbox.readFileToBuffer({ path });
  return contents ? contents.toString('utf8').slice(-limit) : null;
}

async function currentState(sandbox) {
  const raw = await read(sandbox, `${STATE}/state.json`, 16000);
  let state = null;
  if (raw) {
    try { state = JSON.parse(raw); }
    catch { state = { phase: 'invalid_state_file' }; }
  }
  return {
    sandbox: sandbox.name,
    session: sandbox.status,
    state,
    sourceCommit: (await read(sandbox, `${STATE}/source-sha`, 100))?.trim() || null,
    logTail: await read(sandbox, `${STATE}/worker.log`),
  };
}

async function launch(sandbox, action) {
  const env = {};
  if (action !== 'prepare') {
    for (const name of ['OPENAI_API_KEY', 'AB_MODEL', 'AB_INPUT_USD_PER_MILLION', 'AB_OUTPUT_USD_PER_MILLION']) {
      if (!process.env[name]) throw new Error(`Configure ${name} in Vercel before running evaluations`);
      env[name] = process.env[name];
    }
    for (const name of ['AB_CACHED_INPUT_USD_PER_MILLION', 'AB_BUDGET_USD', 'AB_CAMPAIGN']) {
      if (process.env[name]) env[name] = process.env[name];
    }
  }
  const command = await sandbox.runCommand({
    cmd: 'bash', args: ['cloud/worker.sh', action], cwd: ROOT,
    env, detached: true, timeoutMs: SESSION_MS,
  });
  return { sandbox: sandbox.name, action, commandId: command.cmdId };
}

async function handle(request) {
  if (!process.env.ACTIONBENCH_CONTROL_TOKEN || process.env.ACTIONBENCH_CONTROL_TOKEN.length < 32) {
    return json({ error: 'Set ACTIONBENCH_CONTROL_TOKEN in Vercel project settings' }, 503);
  }
  if (!authorized(request)) return json({ error: 'Unauthorized' }, 401);
  if (request.method === 'GET') {
    const sandbox = await existingSandbox();
    if (!sandbox) return json({ sandbox: sandboxName(), state: { phase: 'not_created' } });
    if (new URL(request.url).searchParams.get('report') === '1') {
      const raw = await read(sandbox, `${ROOT}/artifacts/report.json`, 2_000_000);
      return raw ? json(JSON.parse(raw)) : json({ error: 'Report not available yet' }, 404);
    }
    return json(await currentState(sandbox));
  }
  if (request.method !== 'POST') return json({ error: 'Method not allowed' }, 405);
  let body;
  try { body = await request.json(); }
  catch { return json({ error: 'Expected JSON body' }, 400); }
  const action = body?.action;
  if (!['prepare', 'run', 'resume'].includes(action)) return json({ error: 'Unknown action' }, 400);
  if (action === 'prepare') {
    const source = { type: 'git', url: REPO };
    if (process.env.VERCEL_GIT_COMMIT_SHA) source.revision = process.env.VERCEL_GIT_COMMIT_SHA;
    const sandbox = await Sandbox.getOrCreate({
      name: sandboxName(), source,
      image: 'vercel/sandbox/universal:latest',
      timeout: 45 * 60 * 1000,
      resources: { vcpus: 4 }, persistent: true,
      keepLastSnapshots: { count: 1 },
    });
    const state = await currentState(sandbox);
    if (state.state?.phase === 'ready' || state.state?.phase === 'complete') return json(state);
    return json(await launch(sandbox, action), 202);
  }
  const sandbox = await existingSandbox();
  if (!sandbox) return json({ error: 'Run prepare first' }, 409);
  if (!(await read(sandbox, `${STATE}/ready`, 32))) return json({ error: 'Preparation has not passed the real Docker smoke test' }, 409);
  return json(await launch(sandbox, action), 202);
}

export default {
  async fetch(request) {
    try { return await handle(request); }
    catch (error) {
      console.error('ActionBench control error:', error);
      return json({ error: error?.message || 'Controller error' }, 500);
    }
  },
};
