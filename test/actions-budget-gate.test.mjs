import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';

const yaml = readFileSync(new URL('../.github/workflows/actions-budget-gate.yml', import.meta.url), 'utf8').replace(/\r\n/g, '\n');
const script = yaml.split('          script: |\n')[1].split('\n').map(line => line.replace(/^            /, '')).join('\n');
const run = new (Object.getPrototypeOf(async function() {}).constructor)('github', 'core', 'process', 'Date', 'fetch', 'AbortSignal', script);
const clock = class extends Date { constructor(...args) { super(...(args.length ? args : ['2026-10-08T12:00:00Z'])); } };
const row = (quantity, sku = 'Actions Linux', extra = {}) => ({product: 'actions', unitType: 'Minutes', date: '2026-10-01T00:00:00Z', sku, quantity, ...extra});
async function fixture({rows = [row(100)], usage, legacy, modernStatus, vars = {}, env = {}, variableError, notifyStatuses = [], conflictBody} = {}) {
  const calls = [], notices = [], outputs = {}, infos = [];
  const github = {request: async (route, args) => {
    calls.push({route, args});
    if (route.includes('/settings/billing/usage')) {
      if (modernStatus) throw Object.assign(new Error('billing unavailable'), {status: modernStatus});
      return {data: usage ?? {usageItems: rows}};
    }
    if (route.includes('/settings/billing/actions')) return {data: legacy};
    if (route === 'GET /orgs/{org}') return {data: {plan: {name: 'team'}}};
    if (route.startsWith('GET ') && route.includes('/variables/')) {
      if (variableError) throw Object.assign(new Error('variable denied'), {status: variableError});
      if (!(args.name in vars)) throw Object.assign(new Error('missing'), {status: 404});
      return {data: {name: args.name, value: vars[args.name]}};
    }
    if (/^(POST|PATCH) /.test(route)) {vars[args.name] = args.value; return {data: {}};}
    throw new Error('Unexpected route: ' + route);
  }};
  const core = {info: value => infos.push(value), warning: value => infos.push(value), setOutput: (key, value) => outputs[key] = value};
  const config = {BUDGET_ORG: 'meshent', BUDGET_SOFT: '70', BUDGET_HARD: '90', BUDGET_INCLUDED: '0', BUDGET_DRY_RUN: 'false', MERRYN_API_URL: '', MERRYN_TOKEN: '', MERRYN_DOMAIN: 'coordinator', ...env};
  const fetch = async (url, options) => {notices.push({url, options, body: JSON.parse(options.body)}); const status = notifyStatuses.shift() ?? 200; return {ok: status >= 200 && status < 300, status, json: async () => conflictBody ?? {code:'exists',key:'q-actions-budget-meshent-2026-10-hard'}};};
  let error; try {await run(github, core, {env: config}, clock, fetch, AbortSignal);} catch (e) {error = e;}
  return {calls, notices, outputs, infos, vars, error};
}
test('threshold boundaries and current actual Linux receipt', async () => {
  for (const [minutes, state] of [[2099,'ok'],[2100,'soft'],[2699,'soft'],[2700,'hard'],[3305,'hard']]) {
    const f = await fixture({rows:[row(minutes)]}); assert.ifError(f.error); assert.equal(f.vars.ACTIONS_BUDGET_STATE, state);
  }
  const f = await fixture({rows:[row(3305)]}); assert.equal(f.outputs['used-percent'], '110.17'); assert.match(f.infos[0], /24 days left/);
});
test('enhanced physical SKU minutes weighted once; other products/storage excluded', async () => {
  const f = await fixture({rows:[row(10),row(10,'actions_windows'),row(10,'actions_macos'),row(999,'storage',{unitType:'GigabyteHours'}),row(999,'linux',{product:'packages'})]});
  assert.ifError(f.error); assert.match(f.infos[0], /130 \/ 3000/);
});
test('only absent/moved enhanced API falls back to already weighted legacy total', async () => {
  for (const modernStatus of [404,410]) {const f=await fixture({modernStatus,legacy:{total_minutes_used:1800,included_minutes:2000}}); assert.ifError(f.error);assert.equal(f.outputs.state,'hard');}
  for (const modernStatus of [403,500]) {const f=await fixture({modernStatus});assert.ok(f.error);assert.equal(f.calls.length,1);assert.deepEqual(f.vars,{});}
});
test('explicit included-minute override is retained for legacy fallback', async () => {
  const f=await fixture({modernStatus:404,legacy:{total_minutes_used:1800,included_minutes:2000},env:{BUDGET_INCLUDED:'10000'}});assert.ifError(f.error);assert.equal(f.outputs.state,'ok');assert.equal(f.outputs['used-percent'],'18.00');
});
test('missing/bad/stale/unsupported usage never resets existing hard to a false zero', async () => {
  for (const config of [{usage:{}},{rows:[row(-1)]},{rows:[row('3')]},{rows:[row(1,'unknown runner')]},{rows:[row(10,'Actions Linux',{date:'2026-09-30'})]},{rows:[row(3305,'Actions Linux',{product:undefined})]},{rows:[row(3305,'Actions Linux',{unitType:undefined})]}]) {
    const f=await fixture({...config,vars:{ACTIONS_BUDGET_STATE:'hard'}});assert.ok(f.error);assert.equal(f.vars.ACTIONS_BUDGET_STATE,'hard');assert.ok(!f.calls.some(c=>/^(POST|PATCH)/.test(c.route)));
  }
});
test('dry run reports forced states without mutating any variables or posting alerts', async () => {
  const f=await fixture({rows:[row(100)],env:{BUDGET_SOFT:'1',BUDGET_HARD:'2',BUDGET_DRY_RUN:'true',MERRYN_API_URL:'https://example.test/api/projects/meshnet',MERRYN_TOKEN:'fixture'}});
  assert.ifError(f.error);assert.equal(f.outputs.state,'hard');assert.deepEqual(f.vars,{});assert.equal(f.notices.length,0);
});
test('variable permission failure refuses mutation', async () => {const f=await fixture({variableError:403});assert.ok(f.error);assert.deepEqual(f.vars,{});});
test('state changes create journal and hard question; acknowledged unchanged state stays quiet', async () => {
  const env={MERRYN_API_URL:'https://example.test/api/projects/meshnet',MERRYN_TOKEN:'fixture'};
  const vars={ACTIONS_BUDGET_STATE:'soft',ACTIONS_BUDGET_NOTIFIED_STATE:'soft'};
  const first=await fixture({rows:[row(2900)],env,vars});assert.ifError(first.error);assert.equal(first.notices.length,2);assert.equal(first.notices[1].body.kind,'question');assert.equal(vars.ACTIONS_BUDGET_NOTIFIED_STATE,'hard');
  const second=await fixture({rows:[row(2900)],env,vars});assert.ifError(second.error);assert.equal(second.notices.length,0);
});
test('failed notification remains owed even when gate state was already saved', async () => {
  const vars={ACTIONS_BUDGET_NOTIFIED_STATE:'ok'};const env={MERRYN_API_URL:'https://example.test/api/projects/meshnet',MERRYN_TOKEN:'fixture'};
  const failed=await fixture({rows:[row(2200)],env,vars,notifyStatuses:[500]});assert.ok(failed.error);assert.equal(vars.ACTIONS_BUDGET_STATE,'soft');assert.equal(vars.ACTIONS_BUDGET_NOTIFIED_STATE,'ok');
  const retry=await fixture({rows:[row(2200)],env,vars});assert.ifError(retry.error);assert.equal(retry.notices.length,1);assert.equal(vars.ACTIONS_BUDGET_NOTIFIED_STATE,'soft');
});
test('new-month zero usage reopens previous hard gate', async () => {const f=await fixture({rows:[],vars:{ACTIONS_BUDGET_STATE:'hard'}});assert.ifError(f.error);assert.equal(f.vars.ACTIONS_BUDGET_STATE,'ok');});
test('previously delivered hard question can be acknowledged after an interrupted ack', async () => {
  const vars={ACTIONS_BUDGET_STATE:'hard',ACTIONS_BUDGET_NOTIFIED_STATE:'soft'};
  const env={MERRYN_API_URL:'https://example.test/api/v1',MERRYN_TOKEN:'fixture'};
  const f=await fixture({rows:[row(2900)],vars,env,notifyStatuses:[200,409]});assert.ifError(f.error);assert.equal(vars.ACTIONS_BUDGET_NOTIFIED_STATE,'hard');
});
test('other conflicts and wrong question ids are not treated as delivered notifications', async () => {
  for (const conflictBody of [{code:'conflict',key:'q-actions-budget-meshent-2026-10-hard'},{code:'exists',key:'another-id'}]) {
    const f=await fixture({rows:[row(2900)],env:{MERRYN_API_URL:'https://example.test/api/v1',MERRYN_TOKEN:'fixture'},notifyStatuses:[200,409],conflictBody});assert.ok(f.error);assert.ok(!f.vars.ACTIONS_BUDGET_NOTIFIED_STATE);
  }
});
test('invalid thresholds and allowance are refused before writes', async () => {
  for (const env of [{BUDGET_SOFT:'90',BUDGET_HARD:'70'},{BUDGET_HARD:'101'},{BUDGET_INCLUDED:'-1'}]){const f=await fixture({env});assert.ok(f.error);assert.deepEqual(f.vars,{});}
});
