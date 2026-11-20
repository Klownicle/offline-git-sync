// Delete UI regression tests use only mock DOM/API surfaces. No filesystem deletion.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'..','github_dashboard.py'),'utf8').replace(/\r\n/g,'\n');
const script=source.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script);
const nodes=new Map();
function node(id){if(!nodes.has(id))nodes.set(id,{hidden:false,checked:false,innerHTML:'',textContent:'',listeners:{},scrollIntoView(){},addEventListener(name,callback){this.listeners[name]=callback}});return nodes.get(id)}
const names=['formatBytes','clearDeleteSyncPreview','closeDeleteSync','openDeleteSync','deleteSyncPreviewHTML','showDeleteSyncPreview','previewDeleteSync','deleteSyncOutcomeHTML','finishDeleteSync','applyDeleteSync'];
const context=vm.createContext({document:{getElementById:node},console});
vm.runInContext(script.split('\n').filter(line=>line.startsWith('const $=')||names.some(name=>line.startsWith(`function ${name}(`))).join('\n'),context);
vm.runInContext(`let data={rows:[]},selected=null,busy=false,deleteSyncPreview=null,deleteSyncRepository=null,actions=[],errors=[],notices=[],renders=[],draws=0,closed=0;
function runAction(action,fields,callback){actions.push({action,fields,callback})}
function modalError(error){errors.push(error)}
function clearAssetCleanupPreview(){} function clearReleaseRefreshPreview(){}
function draw(){draws++} function closeManager(){closed++;closeDeleteSync();selected=null}
function notice(message,error,warning){notices.push({message,error,warning})}
function renderManager(row){renders.push(row);closeDeleteSync()}
`,context);
const run=code=>vm.runInContext(code,context);
function reset(){nodes.clear();run(`data={rows:[{repository:'owner/repo',config:{archived:false}},{repository:'owner/other',config:{archived:false}}]};selected='owner/repo';busy=false;deleteSyncPreview=null;deleteSyncRepository=null;actions=[];errors=[];notices=[];renders=[];draws=0;closed=0;openDeleteSync()`)}
const proof={repository:'owner/repo',token:'opaque-delete-proof',delete_files:true,bytes:3072,file_count:3,directory_count:4,paths:[
  {kind:'source',path:'C:/sync/owner/repo',exists:true,scanned:true,bytes:2048,file_count:2,directory_count:2},
  {kind:'releases',path:'C:/sync/owner/repo-releases',exists:true,scanned:true,bytes:1024,file_count:1,directory_count:2}
]};
const keepProof={...proof,delete_files:false,bytes:null,file_count:null,paths:proof.paths.map(item=>({...item,exists:null,scanned:false,bytes:null,file_count:null}))};
context.proof=proof;context.keepProof=keepProof;
let checks=0;
function test(name,fn){reset();fn();checks++;console.log(`PASS ${name}`)}

test('opening Delete defaults to keeping local files and does not call the API',()=>{
  assert.equal(node('delete-local-files').checked,false);
  assert.equal(run('actions.length'),0);
  assert.match(node('delete-sync-panel').innerHTML,/Also delete local files/);
  assert.match(node('delete-sync-panel').innerHTML,/entire source checkout and Git history/);
  assert.match(node('delete-sync-panel').innerHTML,/unrecognized release folders/);
});
test('keep-files preview is read-only and names both preserved paths',()=>{
  run('previewDeleteSync();actions[0].callback(keepProof)');
  assert.equal(run('actions[0].action'),'delete-preview');
  assert.equal(run('JSON.stringify(actions[0].fields)'),JSON.stringify({delete_files:false}));
  assert.equal(run('actions.length'),1);
  const html=node('delete-sync-preview').innerHTML;
  assert.match(html,/Local files will be kept/);assert.match(html,/C:\/sync\/owner\/repo-releases/);
  assert.match(html,/contents were not scanned/);assert.match(html,/Delete sync \(keep files\)/);
  assert.doesNotMatch(html,/opaque-delete-proof|NaN/);
});
test('file deletion preview shows exact scope, counts, size and permanent-deletion warning',()=>{
  node('delete-local-files').checked=true;
  run('previewDeleteSync();actions[0].callback(proof)');
  assert.equal(run('actions.length'),1);
  const html=node('delete-sync-preview').innerHTML;
  assert.match(html,/3 files/);assert.match(html,/3 KiB/);assert.match(html,/3,072 bytes/);
  assert.match(html,/Present · 2 files/);assert.match(html,/Permanent deletion/);
  assert.match(html,/local edits/);assert.match(html,/manually added files/);assert.match(html,/cannot be undone/);
  assert.match(html,/Delete sync and files/);
});
test('explicit confirmation submits token and deletion choice, never client paths',()=>{
  node('delete-local-files').checked=true;
  run('previewDeleteSync();actions[0].callback(proof);applyDeleteSync()');
  assert.equal(run('actions[1].action'),'delete');
  assert.equal(run('JSON.stringify(actions[1].fields)'),JSON.stringify({delete_files:true,token:proof.token}));
});
test('changing the checkbox invalidates approval and cannot bypass preview',()=>{
  run('previewDeleteSync();actions[0].callback(keepProof)');
  node('delete-local-files').checked=true;
  run('applyDeleteSync()');
  assert.equal(run('deleteSyncPreview'),null);assert.equal(run('actions.length'),1);
  assert.match(run('errors[0]'),/choice changed/);
  run('previewDeleteSync();actions[1].callback(proof)');
  node('delete-local-files').checked=false;node('delete-local-files').listeners.input();
  assert.equal(run('deleteSyncPreview'),null);assert.equal(node('delete-sync-preview').hidden,true);
});
test('reopening or changing repository always resets file deletion to unchecked',()=>{
  node('delete-local-files').checked=true;run('previewDeleteSync();actions[0].callback(proof);openDeleteSync()');
  assert.equal(node('delete-local-files').checked,false);assert.equal(run('deleteSyncPreview'),null);
  node('delete-local-files').checked=true;run("selected='owner/other';openDeleteSync()");
  assert.equal(node('delete-local-files').checked,false);assert.equal(run('deleteSyncRepository'),'owner/other');
});
test('a late preview cannot authorize deletion for another repository',()=>{
  run("previewDeleteSync();selected='owner/other';openDeleteSync();actions[0].callback(keepProof);applyDeleteSync()");
  assert.equal(run('deleteSyncPreview'),null);assert.equal(run('actions.length'),1);
});
test('missing paths are clearly identified and incomplete previews have no approval',()=>{
  node('delete-local-files').checked=true;
  run('showDeleteSyncPreview({...proof,paths:proof.paths.map(item=>({...item,exists:false,bytes:0,file_count:0})),bytes:0,file_count:0},selected,true)');
  assert.match(node('delete-sync-preview').innerHTML,/Not present · nothing to delete/);
  run('showDeleteSyncPreview({...proof,paths:proof.paths.slice(0,1)},selected,true);applyDeleteSync()');
  assert.equal(run('deleteSyncPreview'),null);assert.equal(run('actions.length'),0);
  assert.match(run('errors.at(-1)'),/incomplete deletion preview/);
});
test('partial failure keeps an archived row and reports deleted, affected and remaining paths',()=>{
  node('delete-local-files').checked=true;
  run(`previewDeleteSync();actions[0].callback(proof);applyDeleteSync();actions[1].callback({removed:false,archived:true,partial:true,files_deleted:[proof.paths[0].path],affected_path:proof.paths[1].path,remaining_paths:[proof.paths[1].path],warning:'A file is locked.'})`);
  assert.equal(run('data.rows.length'),2);assert.equal(run('data.rows[0].config.archived'),true);
  assert.equal(run('closed'),0);assert.equal(run('notices[0].error'),true);
  const html=node('delete-sync-result').innerHTML;
  assert.match(html,/Deletion incomplete/);assert.match(html,/remains archived/);assert.match(html,/A file is locked/);
  assert.match(html,/Deleted folders/);assert.match(html,/Possibly partly deleted/);assert.match(html,/Remaining paths/);
  run('openDeleteSync()');assert.equal(node('delete-local-files').checked,false);
});
test('only an explicit removed result removes the dashboard row',()=>{
  run('previewDeleteSync();actions[0].callback(keepProof);applyDeleteSync();actions[1].callback({removed:true,message:"Sync deleted. Files kept."})');
  assert.equal(run('data.rows.length'),1);assert.equal(run('data.rows[0].repository'),'owner/other');
  assert.equal(run('closed'),1);assert.equal(run('notices[0].error'),false);
});
test('paths and partial-failure messages are escaped as text',()=>{
  const html=run(`deleteSyncPreviewHTML({...proof,paths:proof.paths.map(item=>({...item,path:'<script>'})),warning:'<img src=x>'})`);
  assert.match(html,/&lt;script&gt;/);assert.match(html,/&lt;img src=x&gt;/);assert.doesNotMatch(html,/<script>|<img/);
  const outcome=run(`deleteSyncOutcomeHTML({archived:true,warning:'<warning>',affected_path:'<path>',files_deleted:['<deleted>']},proof)`);
  assert.match(outcome,/&lt;warning&gt;/);assert.match(outcome,/&lt;path&gt;/);assert.match(outcome,/&lt;deleted&gt;/);
});
test('Cancel clears the token and hides the deletion panel',()=>{
  run('previewDeleteSync();actions[0].callback(keepProof)');node('cancel-delete-preview').listeners.click();
  assert.equal(run('deleteSyncPreview'),null);assert.equal(run('deleteSyncRepository'),null);
  assert.equal(node('delete-sync-panel').hidden,true);run('applyDeleteSync()');assert.equal(run('actions.length'),1);
});
console.log(`${checks} Delete sync UI tests passed; full dashboard JavaScript syntax valid.`);
