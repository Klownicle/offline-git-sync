// Local UI regression tests. No real server, clipboard access, or task mutations.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'github_dashboard.py'), 'utf8').replace(/\r\n/g, '\n');
const script = source.match(/<script>([\s\S]*?)<\/script>/)[1];
const names = ['taskDiagnosticsText', 'modalError', 'copyTaskDetails', 'request', 'runAction'];
const nodes = new Map();
function node(id) {
  if (!nodes.has(id)) nodes.set(id, {textContent: '', hidden: false, open: false,
    value: '', focused: false, selected: false, scrollIntoView() {},
    focus() {this.focused = true}, select() {this.selected = true},
    set innerHTML(value) {throw new Error(`Unexpected HTML mutation: ${id}`)}});
  return nodes.get(id);
}
const diagnostics = {
  task_name: 'Git Sync - owner-repo', repository: 'owner/repo',
  help: 'Nothing was changed. Review this task’s Actions before retrying.',
  expected: {command: 'C:\\Python\\pythonw.exe', arguments: 'C:\\Git\\github_sync.py run-saved owner/repo'},
  actions: [{command: '<img src=x onerror=alert(1)>', arguments: '"legacy.py" --last 5', reason: 'Unexpected executable'}],
  token: 'must-not-copy-an-extra-field',
};
const result = {error: 'Asset patterns were not saved.', code: 'task_action_conflict', task_diagnostics: diagnostics};
let copyCalls = [], fetchResult = result;
const navigator = {clipboard: {writeText: async text => copyCalls.push(text)}};
const context = vm.createContext({document: {getElementById: node}, navigator, console,
  fetch: async () => ({ok: false, status: 409, json: async () => fetchResult})});
vm.runInContext(script.split(/\r?\n/).filter(line => line.startsWith('const $=') ||
  names.some(name => line.startsWith(`function ${name}(`) || line.startsWith(`async function ${name}(`))).join('\n'), context);
vm.runInContext(`const management={token:'page-auth-token-must-not-copy'};let busy=false,dataVersion=0,selected='owner/repo',assetPreview={draft:'kept'};function setBusy(value){busy=value}function pollProgress(){}function clearAssetCleanupPreview(){}function updateAssetCleanupAvailability(){}function draw(){throw new Error('Conflict must not redraw forms')}`, context);
const run = code => vm.runInContext(code, context);
let checks = 0;
async function test(name, fn) {await fn(); checks++; console.log(`PASS ${name}`)}
(async () => {
  await test('request preserves typed conflict diagnostics', async () => {
    const error = await run(`request('patterns','owner/repo',{asset_patterns:['*.zip']}).catch(error=>error)`);
    assert.equal(error.message, result.error);
    assert.equal(error.code, result.code);
    assert.deepEqual(error.taskDiagnostics, diagnostics);
    context.conflictError = error;
  });
  await test('diagnostics render as literal text and keep draft editor state', () => {
    node('edit-patterns').value = 'unsaved *.zip';
    node('release-limit').value = '17';
    run('modalError(conflictError)');
    assert.equal(node('manage-task-diagnostics').hidden, false);
    assert.equal(node('manage-task-diagnostics').open, false);
    assert.match(node('manage-task-details').textContent, /<img src=x onerror=alert\(1\)>/);
    assert.match(node('manage-task-details').textContent, /Expected sync action/);
    assert.equal(node('edit-patterns').value, 'unsaved *.zip');
    assert.equal(node('release-limit').value, '17');
    assert.equal(run('assetPreview.draft'), 'kept');
  });
  await test('copy uses only selected diagnostic fields, excluding page auth', async () => {
    await run('copyTaskDetails()');
    assert.equal(copyCalls.length, 1);
    assert.match(copyCalls[0], /Git Sync - owner-repo/);
    assert.doesNotMatch(copyCalls[0], /page-auth-token|must-not-copy-an-extra-field/);
    assert.equal(node('manage-copy-status').textContent, 'Task details copied.');
    assert.equal(node('manage-copy-fallback').hidden, true);
  });
  await test('clipboard refusal selects readonly fallback without discarding edits', async () => {
    navigator.clipboard.writeText = async () => {throw new Error('clipboard blocked')};
    await run('copyTaskDetails()');
    assert.equal(node('manage-copy-fallback').hidden, false);
    assert.equal(node('manage-copy-fallback').focused, true);
    assert.equal(node('manage-copy-fallback').selected, true);
    assert.equal(node('manage-copy-fallback').value, node('manage-task-details').textContent);
    assert.match(node('manage-copy-status').textContent, /Ctrl\+C/);
    assert.equal(node('edit-patterns').value, 'unsaved *.zip');
  });
  await test('new action clears obsolete diagnostics and copied values', () => {
    run(`modalError('')`);
    assert.equal(node('manage-error').hidden, true);
    assert.equal(node('manage-task-diagnostics').hidden, true);
    assert.equal(node('manage-task-details').textContent, '');
    assert.equal(node('manage-copy-fallback').value, '');
    assert.equal(node('manage-copy-fallback').hidden, true);
  });
  await test('failed save preserves drafts and displays conflict without success redraw', async () => {
    await run(`runAction('patterns',{asset_patterns:['*.zip']},()=>{throw new Error('Must not report success')})`);
    assert.equal(run('busy'), false);
    assert.equal(node('manage-task-diagnostics').hidden, false);
    assert.equal(node('edit-patterns').value, 'unsaved *.zip');
    assert.equal(node('release-limit').value, '17');
    assert.equal(run('assetPreview.draft'), 'kept');
  });
  await test('ordinary errors have no task diagnostics', async () => {
    fetchResult = {error: 'Invalid pattern'};
    await run(`runAction('patterns',{asset_patterns:['bad']})`);
    assert.equal(node('manage-error').textContent, 'Invalid pattern');
    assert.equal(node('manage-task-diagnostics').hidden, true);
  });
  console.log(`${checks} task diagnostic UI tests passed.`);
})().catch(error => {console.error(error); process.exitCode = 1});
