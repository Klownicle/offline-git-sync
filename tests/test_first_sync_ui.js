// Isolated logic tests: no browser, downloads, API calls, or scheduled tasks.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'github_dashboard.py'), 'utf8').replace(/\r\n/g, '\n');
const script = source.match(/<script>([\s\S]*?)<\/script>/)[1];
const lines = script.split(/\r?\n/);
const names = ['formatBytes', 'newestProgress', 'acceptSnapshot', 'firstSyncHTML',
  'patternMatchers', 'renderAssetMatches', 'recordAddedSync', 'repositoryInfo',
  'canonicalPublicHost', 'canonicalRepositoryPart'];
const dom = new Map();
const context = vm.createContext({
  document: {getElementById: id => {
    if (!dom.has(id)) dom.set(id, {innerHTML: '', textContent: '', hidden: false});
    return dom.get(id);
  }},
  console,
});
vm.runInContext(lines.filter(line => line.startsWith('const $=') ||
  line.startsWith('const date=') || line.startsWith('const exact=') ||
  line.startsWith('const activeFirst=') || names.some(name => line.startsWith(`function ${name}(`))).join('\n'), context);
vm.runInContext('let data={rows:[]}, assetPreview=null, enteredPatterns=[]; function rawPatterns(){return enteredPatterns} function updateAssetCleanupAvailability(){} function updateReleaseRefreshAvailability(){}', context);
const run = expression => vm.runInContext(expression, context);
let checks = 0;
function test(name, fn) { fn(); checks++; console.log(`PASS ${name}`); }

test('matching is union, case insensitive, and dot is literal', () => {
  assert.equal(run(`patternMatchers(['*WIN*.zip','tool-?.exe']).matchers.some(p=>p.test('Example-Win64.ZIP'))`), true);
  assert.equal(run(`patternMatchers(['*WIN*.zip','tool-?.exe']).matchers.some(p=>p.test('tool-A.EXE'))`), true);
  assert.equal(run(`patternMatchers(['tool.zip']).matchers[0].test('toolXzip')`), false);
  assert.equal(run(`patternMatchers(['tool-?.exe']).matchers[0].test('tool-AA.exe')`), false);
  assert.equal(run(`patternMatchers(['*.zip']).matchers[0].test('tool'+String.fromCharCode(10)+'portable.zip')`), true);
});
test('blank, regex syntax, and oversize patterns are not broad matches', () => {
  assert.equal(run(`JSON.stringify(patternMatchers(['','  ','[.*]','a'.repeat(201),'*.zip']).invalid)`), '[1,2,3,4]');
  assert.equal(run(`patternMatchers(['','[.*]']).matchers.length`), 0);
});
test('preview matches Python lowercase fnmatch Unicode and absolute-end rules', () => {
  assert.equal(run(`patternMatchers(['?.zip']).matchers[0].test(String.fromCodePoint(0x1f600)+'.zip')`), true);
  assert.equal(run(`patternMatchers(['??.zip']).matchers[0].test(String.fromCodePoint(0x1f600)+'.zip')`), false);
  assert.equal(run(`patternMatchers(['k*.zip']).matchers[0].test(String.fromCodePoint(0x212a)+'.zip')`), true);
  assert.equal(run(`patternMatchers(['i?.zip']).matchers[0].test(String.fromCodePoint(0x130)+'.zip')`), true);
  assert.equal(run(`patternMatchers(['i.zip']).matchers[0].test(String.fromCodePoint(0x130)+'.zip')`), false);
  assert.equal(run(`patternMatchers(['*.zip']).matchers[0].test('tool.zip'+String.fromCharCode(10))`), false);
  assert.equal(run(`patternMatchers(['*.zip?']).matchers[0].test('tool.zip'+String.fromCharCode(10))`), true);
  assert.equal(run(`patternMatchers(['*.zip*']).matchers[0].test('tool.zip'+String.fromCharCode(10))`), true);
});
test('older progress cannot replace newer progress or terminal state', () => {
  assert.equal(run(`newestProgress({id:'one',status:'running',updated_at:'2026-11-10T10:02:00Z'}, {id:'one',status:'queued',updated_at:'2026-11-10T10:01:00Z'}).status`), 'running');
  assert.equal(run(`newestProgress({id:'one',status:'success',updated_at:'2026-11-10T10:02:00Z'}, {id:'one',status:'running',updated_at:'2026-11-10T10:02:00Z'}).status`), 'success');
});
test('snapshot refresh retains progress newer than its inventory snapshot', () => {
  run(`data={rows:[{repository:'owner/repo',first_sync:{id:'x',status:'success',updated_at:'2026-11-10T10:02:00Z'}}]};acceptSnapshot({rows:[{repository:'owner/repo',first_sync:{id:'x',status:'running',updated_at:'2026-11-10T10:01:00Z'}}]})`);
  assert.equal(run('data.rows[0].first_sync.status'), 'success');
});
test('unknown source progress stays indeterminate without pretend bytes', () => {
  const html = run(`firstSyncHTML({status:'running',stage:'source',files_total:0,bytes_downloaded:0})`);
  assert.match(html, /<progress aria-label="First sync progress"><\/progress>/);
  assert.doesNotMatch(html, /0 B|value=/);
});
test('download progress shows measured file count and current file bytes', () => {
  const html = run(`firstSyncHTML({status:'running',stage:'downloads',files_total:4,files_completed:1,current_file:'large.zip',bytes_downloaded:512,bytes_total:1024})`);
  assert.match(html, /max="4" value="1"/);
  assert.match(html, /Current file download progress" max="1024" value="512"/);
  assert.match(html, /1 of 4 release files completed/);
  assert.match(html, /512 B of 1 KiB in current file/);
});
test('failed progress escapes errors and does not claim completion', () => {
  const html = run(`firstSyncHTML({status:'failed',stage:'finished',error:'<img src=x onerror=alert(1)>',bytes_downloaded:0})`);
  assert.match(html, /First sync failed/);
  assert.match(html, /&lt;img/);
  assert.doesNotMatch(html, /<img|<progress|0 B/);
});
test('committed add can display progress even when the full snapshot failed', () => {
  run(`data={rows:[]};recordAddedSync({repository:'owner/repo',job:{last:5},first_sync:{id:'x',status:'queued'}})`);
  assert.equal(run('data.rows.length'), 1);
  assert.equal(run('data.rows[0].first_sync.status'), 'queued');
  assert.equal(run('data.rows[0].inventory.count'), null);
});
test('all assets remain visible while union matches and source archive highlight', () => {
  run(`enteredPatterns=['*win*.zip','*.exe'];assetPreview={payload:{source_only:false,releases:[{tag:'v1',name:'<release>',assets:[{name:'win64.zip',size:1024},{name:'setup.exe',size:2048},{name:'linux.tar.gz',size:4096},{name:'<img src=x>',size:1}],source_archive:{name:'Source-code-v1.zip'}}]}};renderAssetMatches()`);
  const html = dom.get('asset-results').innerHTML;
  assert.match(html, /linux.tar.gz/);
  assert.match(html, /&lt;img src=x&gt;/);
  assert.doesNotMatch(html, /<img/);
  assert.equal((html.match(/asset-item match/g) || []).length, 2);
  assert.match(html, /Always included · source ZIP/);
  assert.match(dom.get('asset-summary').textContent, /2 of 4 assets match/);
});
test('pattern edits recalculate highlights without replacing metadata', () => {
  run(`enteredPatterns=['*.tar.gz',''];renderAssetMatches()`);
  assert.equal((dom.get('asset-results').innerHTML.match(/asset-item match/g) || []).length, 1);
  assert.equal(dom.get('asset-pattern-warning').hidden, false);
  assert.match(dom.get('asset-pattern-warning').textContent, /Pattern 2 is blank or invalid/);
});
test('source-only and no-release previews have clear empty states', () => {
  run(`assetPreview={payload:{source_only:true,releases:[],message:'Source only: release downloads skipped'}};renderAssetMatches()`);
  assert.match(dom.get('asset-summary').textContent, /release downloads skipped/);
  assert.equal(dom.get('asset-results').innerHTML, '');
  run(`assetPreview={payload:{source_only:false,releases:[]}};renderAssetMatches()`);
  assert.match(dom.get('asset-summary').textContent, /No published releases/);
});
console.log(`${checks} UI logic tests passed.`);
