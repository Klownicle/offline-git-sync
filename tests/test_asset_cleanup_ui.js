// Refresh Assets UI tests. All API actions and DOM surfaces are isolated mocks.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'github_dashboard.py'), 'utf8').replace(/\r\n/g, '\n');
const script = source.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script);
const nodes = new Map();
function node(id) {
  if (!nodes.has(id)) nodes.set(id, {hidden:false,disabled:false,textContent:'',innerHTML:'',
    value:'',listeners:{},scrollIntoView(){},addEventListener(name,callback){this.listeners[name]=callback}});
  return nodes.get(id);
}
const context = vm.createContext({document:{getElementById:node},console});
const names = ['formatBytes','savedAssetPatterns','patternSignature','assetPatternsDirty',
  'clearAssetCleanupPreview','updateAssetCleanupAvailability','assetCleanupFileHTML',
  'assetCleanupPreviewHTML','showAssetCleanupPreview','previewAssetCleanup','applyAssetCleanup',
  'finishAssetCleanup','syncPreviewPatterns'];
vm.runInContext(script.split(/\r?\n/).filter(line => line.startsWith('const $=') ||
  names.some(name => line.startsWith(`function ${name}(`))).join('\n'), context);
vm.runInContext(`let data={rows:[]},selected='owner/repo',busy=false,assetCleanupPreview=null,assetPreview=null,preview={token:'release-cleanup-kept'};
let enteredPatterns=['*.zip'],actions=[],errors=[],messages=[],renderCalls=[];
function rawPatterns(){return enteredPatterns}
function runAction(action,fields,callback){actions.push({action,fields,callback})}
function modalError(error){errors.push(error)}
function notice(message,error,warning){messages.push({message,error,warning})}
function renderManager(row){renderCalls.push(row)}
function refresh(){return Promise.resolve()}
function updatePatternControls(){}
function clearReleaseRefreshPreview(){}
function updateReleaseRefreshAvailability(){}
function previewReleaseRefresh(){}
function clearDeleteSyncPreview(){}
function closeDeleteSync(){}
function openDeleteSync(){}
function clearAddSyncReview(){}
function policyEditorHTML(){return ""}
function bindPolicyEditor(){}
function renderAssetMatches(){}
function patternRowHTML(value){return '<input value="'+esc(value)+'">'}
`, context);
const run = code => vm.runInContext(code, context);
function reset() {
  run(`data={rows:[{repository:'owner/repo',config:{asset_patterns:['*.zip']}}]};selected='owner/repo';busy=false;assetCleanupPreview=null;assetPreview=null;enteredPatterns=['*.zip'];actions=[];errors=[];messages=[];renderCalls=[]`);
  node('assets-cleanup-preview').innerHTML='';
  node('assets-cleanup-preview').hidden=true;
}
const proof = {token:'approval-token-not-markup',count:2,bytes:3072,release_count:1,
  patterns:['*.zip'],assets:[{tag:'v1',folder:'v1',name:'old.exe',size:1024},{tag:'v1',folder:'v1',name:'old.msi',size:2048}],skipped:[]};
context.proof = proof;
let count=0;
function test(name,fn){reset();fn();count++;console.log(`PASS ${name}`)}

test('Refresh Assets previews only saved patterns and never sends drafts',()=>{
  run('previewAssetCleanup()');
  assert.equal(run('actions[0].action'),'assets-cleanup-preview');
  assert.equal(run('JSON.stringify(actions[0].fields)'),'{}');
  run('actions[0].callback(proof)');
  assert.match(node('assets-cleanup-preview').innerHTML,/Will delete 2 assets/);
  assert.match(node('assets-cleanup-preview').innerHTML,/3 KiB/);
  assert.match(node('assets-cleanup-preview').innerHTML,/Delete 2 assets/);
  assert.doesNotMatch(node('assets-cleanup-preview').innerHTML,/approval-token-not-markup/);
});
test('unsaved pattern changes disable preview and cannot bypass the guard',()=>{
  run(`enteredPatterns=['*.apk'];updateAssetCleanupAvailability();previewAssetCleanup()`);
  assert.equal(node('assets-cleanup-preview-button').disabled,true);
  assert.equal(node('assets-cleanup-save-note').hidden,false);
  assert.equal(run('actions.length'),0);
  assert.match(run('errors[0]'),/Save your asset pattern changes/);
});
test('backend normalization does not leave a successful save stuck dirty',()=>{
  run(`enteredPatterns=[' *.ZIP ','*.zip'];updateAssetCleanupAvailability()`);
  assert.equal(run('assetPatternsDirty()'),false);
  assert.equal(node('assets-cleanup-preview-button').disabled,false);
});
test('explicit delete uses the preview token and no client file paths',()=>{
  run(`showAssetCleanupPreview(proof,selected);applyAssetCleanup()`);
  assert.equal(run('actions[0].action'),'assets-cleanup');
  assert.equal(run('JSON.stringify(actions[0].fields)'),JSON.stringify({token:proof.token}));
});
test('zero candidates has no delete button and cannot trigger deletion',()=>{
  run(`showAssetCleanupPreview({...proof,count:0,bytes:0,release_count:0,assets:[]},selected);applyAssetCleanup()`);
  assert.match(node('assets-cleanup-preview').innerHTML,/Will delete 0 assets/);
  assert.doesNotMatch(node('assets-cleanup-preview').innerHTML,/confirm-assets-cleanup/);
  assert.equal(run('actions.length'),0);
});
test('all names, patterns and skipped reasons are escaped and full list is reviewable',()=>{
  const html=run(`assetCleanupPreviewHTML({count:6,bytes:6,release_count:1,patterns:['<script>'],assets:Array.from({length:6},(_,i)=>({tag:'<tag>',folder:'</li>',name:i===5?'last-asset.exe':'<img src=x>',size:1})),skipped:[{folder:'<folder>',name:'<file>',reason:'<unsafe>'}]})`);
  assert.match(html,/&lt;script&gt;/);
  assert.match(html,/&lt;img src=x&gt;/);
  assert.match(html,/&lt;unsafe&gt;/);
  assert.doesNotMatch(html,/<script>|<img|<unsafe>/);
  assert.match(html,/View all 6 assets to delete/);
  assert.match(html,/last-asset.exe/);
});
test('a late preview for another manager cannot become deletable',()=>{
  run(`selected='another/repo';showAssetCleanupPreview(proof,'owner/repo')`);
  assert.equal(run('assetCleanupPreview'),null);
  assert.equal(node('assets-cleanup-preview').hidden,true);
});
test('changed saved patterns invalidate the approval and require saving draft again',()=>{
  run(`showAssetCleanupPreview(proof,selected);data.rows[0].config.asset_patterns=['*.apk'];updateAssetCleanupAvailability()`);
  assert.equal(run('assetCleanupPreview'),null);
  assert.equal(node('assets-cleanup-preview').hidden,true);
  assert.equal(node('assets-cleanup-preview-button').disabled,true);
});
test('input invalidation keeps release cleanup and remote Asset Preview separate',()=>{
  run(`assetCleanupPreview=proof;assetPreview={prefix:'edit',payload:{kept:true}}`);
  const inputLine=script.split(/\r?\n/).find(line=>line.startsWith("$('manage-content').addEventListener('input'"));
  run(inputLine);
  node('manage-content').listeners.input();
  assert.equal(run('assetCleanupPreview'),null);
  assert.equal(run('preview.token'),'release-cleanup-kept');
  assert.equal(run('assetPreview.payload.kept'),true);
});
test('editing patterns inside Asset Preview invalidates local deletion approval',()=>{
  run(`assetCleanupPreview=proof;assetPreview={prefix:'edit'};enteredPatterns=['*.apk'];syncPreviewPatterns()`);
  assert.equal(run('assetCleanupPreview'),null);
  assert.equal(node('assets-cleanup-preview-button').disabled,true);
});
test('partial deletion shows actual server message and warning, then refreshes manager',()=>{
  run(`showAssetCleanupPreview(proof,selected);applyAssetCleanup();actions[0].callback({count:1,message:'Refresh Assets stopped after removing 1 asset.',warning:'One file was in use.',data:{rows:[]}})`);
  assert.equal(run('assetCleanupPreview'),null);
  assert.equal(run('renderCalls.length'),1);
  assert.equal(run('messages[0].message'),'Refresh Assets stopped after removing 1 asset.');
  assert.equal(run('messages[0].warning'),'One file was in use.');
});

// Render the real manager template for archived and source-only configurations.
const manager=script.match(/function renderManager\(r\)\{[\s\S]*?\n\}\nfunction openManager/)[0].replace(/\nfunction openManager$/,'');
run(`function inventoryOf(r){return r.inventory||{count:0,releases:[],skipped:[]}}function scheduleOf(){return {every_minutes:15}}function archived(r){return !!r.config.archived}function localText(){return '0 releases synced locally'}function skippedHTML(){return ''}function patternEditorHTML(){return '<div>Pattern editor retained</div>'}function bindPatternEditor(){}function readPatterns(){return enteredPatterns}`);
run(manager);
test('Refresh Assets is available for active, archived and source-only managers',()=>{
  for(const flags of [{archived:false,source_only:false},{archived:true,source_only:false},{archived:false,source_only:true},{archived:true,source_only:true}]){
    context.flags=flags;
    run(`data.rows[0].config={asset_patterns:['*.zip'],last:3,...flags};renderManager(data.rows[0])`);
    const html=node('manage-content').innerHTML;
    assert.match(html,/<h3>Refresh Assets<\/h3>/);
    assert.match(html,/id="assets-cleanup-preview-button"/);
    assert.match(html,/This action does not download anything/);
    assert.doesNotMatch(html,/>Cleanup assets</);
    if(flags.archived&&flags.source_only)assert.match(html,/after this sync is resumed and release syncing is enabled/);
  }
});
console.log(`${count} Refresh Assets UI tests passed; full dashboard JavaScript syntax valid.`);
