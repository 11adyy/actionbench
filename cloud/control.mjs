import { readFile } from 'node:fs/promises';

const action = process.argv[2] || 'status';
if (!['status', 'report', 'prepare', 'run', 'resume'].includes(action)) {
  console.error('Usage: node cloud/control.mjs status|report|prepare|run|resume');
  process.exit(2);
}
let local = {};
try { local = JSON.parse(await readFile('.vercel-control.json', 'utf8')); }
catch (error) { if (error.code !== 'ENOENT') throw error; }
const url = process.env.ACTIONBENCH_CONTROL_URL || local.url;
const token = process.env.ACTIONBENCH_CONTROL_TOKEN || local.token;
if (!url || !token) {
  console.error('Set ACTIONBENCH_CONTROL_URL and ACTIONBENCH_CONTROL_TOKEN, or create a private .vercel-control.json');
  process.exit(2);
}
const endpoint = `${url.replace(/\/$/, '')}/api/control${action === 'report' ? '?report=1' : ''}`;
const response = await fetch(endpoint, {
  method: ['status', 'report'].includes(action) ? 'GET' : 'POST',
  headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
  body: ['status', 'report'].includes(action) ? undefined : JSON.stringify({ action }),
});
const text = await response.text();
let value;
try { value = JSON.parse(text); }
catch { value = { response: text }; }
console.log(JSON.stringify(value, null, 2));
if (!response.ok) process.exit(1);
