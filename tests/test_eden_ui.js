// Provider-specific display and link tests: no browser, network, or mutations.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'github_dashboard.py'), 'utf8').replace(/\r\n/g, '\n');
const script = source.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script);
const context = vm.createContext({document: {getElementById() {return {}}}});
const names = ['canonicalPublicHost','canonicalRepositoryPart','repositoryInfo','hostBadgeHTML','commitLink','commitSummaryHTML','recordAddedSync','newestProgress'];
vm.runInContext(script.split(/\r?\n/).filter(line => line.startsWith('const $=') ||
  line.startsWith('const activeFirst=') || names.some(name => line.startsWith(`function ${name}(`))).join('\n'), context);
vm.runInContext('let data={rows:[]}', context);
const run = expression => vm.runInContext(expression, context);
let count = 0;
function test(name, fn) {fn(); count++; console.log(`PASS ${name}`)}

test('Eden key has real owner and repository rather than treating host as owner', () => {
  assert.equal(run(`JSON.stringify(repositoryInfo({repository:'git.eden-emu.dev/eden-emu/eden'}))`),
    JSON.stringify({host:'git.eden-emu.dev',owner:'eden-emu',name:'eden',provider:'forgejo',web_url:'https://git.eden-emu.dev/eden-emu/eden'}));
});
test('GitHub and Eden mirrors keep distinct URLs and labels', () => {
  assert.equal(run(`repositoryInfo({repository:'eden-emu/eden'}).web_url`), 'https://github.com/eden-emu/eden');
  assert.match(run(`hostBadgeHTML({repository:'git.eden-emu.dev/eden-emu/eden'})`), /Forgejo \/ Gitea · git.eden-emu.dev/);
  assert.match(run(`hostBadgeHTML({repository:'eden-emu/eden'})`), />GitHub</);
});
test('commit and commit-summary links use the selected provider', () => {
  const eden = run(`commitLink({repository:'git.eden-emu.dev/eden-emu/eden'},'abcdef0123456789')`);
  assert.match(eden, /https:\/\/git.eden-emu.dev\/eden-emu\/eden\/commit\/abcdef0123456789/);
  const github = run(`commitSummaryHTML({repository:'eden-emu/eden'},'abcdef0 Fix release <metadata>')`);
  assert.match(github, /https:\/\/github.com\/eden-emu\/eden\/commit\/abcdef0/);
  assert.match(github, /&lt;metadata&gt;/);
  assert.doesNotMatch(run(`commitLink({repository:'owner/repo'},'javascript:alert(1)')`), /<a /);
});
test('untrusted row URL cannot change host or create an executable link', () => {
  assert.equal(run(`repositoryInfo({repository:'git.eden-emu.dev/owner/repo',web_url:'javascript:alert(1)'}).web_url`),
    'https://git.eden-emu.dev/owner/repo');
  assert.equal(run(`repositoryInfo({repository:'owner/repo',owner:'<script>'}).web_url`), 'https://github.com/owner/repo');
});
test('optimistic added Eden row preserves complete key and provider metadata', () => {
  run(`recordAddedSync({repository:'git.eden-emu.dev/eden-emu/eden',job:{asset_patterns:['*.apk']},first_sync:{status:'queued'}})`);
  assert.equal(run('data.rows[0].repository'), 'git.eden-emu.dev/eden-emu/eden');
  assert.equal(run('data.rows[0].owner'), 'eden-emu');
  assert.equal(run('data.rows[0].name'), 'eden');
  assert.equal(run('data.rows[0].host'), 'git.eden-emu.dev');
  assert.equal(run('data.rows[0].web_url'), 'https://git.eden-emu.dev/eden-emu/eden');
});
console.log(`${count} Eden UI logic tests passed; full dashboard JavaScript syntax valid.`);
