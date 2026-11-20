// Local DOM/API mocks only: no real downloads, file deletions, or scheduled tasks.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'github_dashboard.py'), 'utf8').replace(/\r\n/g, '\n');
const script = source.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script);
const nodes = new Map();
function node(id) {
  if (!nodes.has(id)) nodes.set(id, {value:'',checked:false,disabled:false,hidden:false,textContent:'',listeners:{},inputs:[],dataset:{},
    reportValidity(){return this.valid!==false},focus(){},scrollIntoView(){},showModal(){},close(){},
    addEventListener(name,callback){this.listeners[name]=callback},
    querySelectorAll(selector){return this.inputs.filter(input=>selector==='input'||selector==='[data-type-limit]'&&input.dataset.typeLimit||selector==='[data-asset-pattern]'&&input.assetPattern)},
    get innerHTML(){return this.html||''},
    set innerHTML(html){this.html=html;this.inputs=[];for(const match of html.matchAll(/<input\b[^>]*>/g)){
      const inputId=match[0].match(/\bid="([^"]+)"/)?.[1],input=node(inputId||'anonymous-'+nodes.size);
      input.value=match[0].match(/\bvalue="([^"]*)"/)?.[1]||'';input.checked=/\schecked(?:\s|>)/.test(match[0]);input.disabled=/\sdisabled(?:\s|>)/.test(match[0]);
      for(const attr of match[0].matchAll(/data-([a-z-]+)="([^"]*)"/g))input.dataset[attr[1].replace(/-([a-z])/g,(_,letter)=>letter.toUpperCase())]=attr[2];
      this.inputs.push(input);
    }}
  });
  return nodes.get(id);
}
const context=vm.createContext({document:{getElementById:node},console});
const names=['formatBytes','releaseChannelText','patternSignature','savedAssetPatterns','assetPatternsDirty',
  'clearAssetCleanupPreview','clearReleaseRefreshPreview','releaseRefreshSignature','releaseRefreshUnavailable',
  'updateReleaseRefreshAvailability','releaseRefreshPreviewHTML','showReleaseRefreshPreview','previewReleaseRefresh',
  'applyReleaseRefresh','openAssetPreview','openAddSync','firstSyncHTML','newestProgress','latestResultStatus','needs','defaultReleasePolicy','effectiveReleasePolicy','releaseTypeLabel','releaseSelectionText','releaseSettingsSignature','policyEditorHTML','readReleaseSettings','renderReleaseTypes','typeRuleHTML','renderTypeRules','policyEdited','bindPolicyEditor','discoverReleaseTypes','clearAddSyncReview','resetNewRepositoryPolicy','addSyncFields','addReviewAssetsHTML','addSyncReviewHTML','showAddSyncReview','reviewAddSync','confirmAddSync','patternMatchers','saveManagerSettings'];
vm.runInContext(script.split('\n').filter(line=>line.startsWith('const $=')||line.startsWith('const exact=')||
  line.startsWith('const activeFirst=')||line.startsWith('const archived=')||names.some(name=>line.startsWith(`function ${name}(`))).join('\n'),context);
vm.runInContext(`const management={token:'test-token'};let data={rows:[]},selected=null,busy=false,preview=null,assetPreview=null,assetCleanupPreview=null,releaseRefreshPreview=null,enteredPatterns=['*.zip'],actions=[],errors=[],messages=[],draws=0,closes=0,fetches=0,addSyncReview=null,policyEditors={new:null,edit:null},highlighted=null;
function saveUI(){}
function rawPatterns(){return enteredPatterns} function readPatterns(){return enteredPatterns}
function runAction(action,fields,callback){actions.push({action,fields,callback})}
function modalError(error){if(error)errors.push(error)} function notice(message){messages.push(message)}
function draw(){draws++} function closeManager(){closes++;selected=null}
function fetchAssetPreview(){fetches++} function patternEditorHTML(){return '<div>Pattern editor</div>'} function bindPatternEditor(){} function syncPreviewPatterns(){}
function clearDeleteSyncPreview(){} function closeDeleteSync(){} function openDeleteSync(){} function updateAssetCleanupAvailability(){} function previewAssetCleanup(){} function inventoryOf(row){return row.inventory||{count:0,skipped:[]}}
function scheduleOf(row){return {every_minutes:15}} function localText(){return '0 releases synced locally'} function skippedHTML(){return ''}
`,context);
const run=code=>vm.runInContext(code,context);
const manager=script.match(/function renderManager\(r\)\{[\s\S]*?\n\}\nfunction openManager/)[0].replace(/\nfunction openManager$/,'');
run(manager);
function reset(){
  nodes.clear();
  run(`data={rows:[{repository:'owner/repo',status:'success',config:{last:5,include_prereleases:false,asset_patterns:['*.zip']}}]};selected='owner/repo';busy=false;preview=null;assetPreview=null;assetCleanupPreview=null;releaseRefreshPreview=null;enteredPatterns=['*.zip'];actions=[];errors=[];messages=[];draws=0;closes=0;fetches=0;addSyncReview=null;policyEditors={new:null,edit:null};renderManager(data.rows[0]);actions=[];errors=[]`);
}
const proof={token:'opaque-proof',count:2,bytes:4096,last:5,include_prereleases:false,
  releases:[{tag:'nightly-1',folder:'nightly-1'},{tag:'nightly-2',folder:'nightly-2'}],
  selected:[{tag:'v1.0',prerelease:false}],skipped:[{folder:'older-unrecognized',reason:'No verified metadata'}]};
context.proof=proof;
let checks=0;
function test(name,fn){reset();fn();checks++;console.log(`PASS ${name}`)}

test('new sync defaults to all types and submit only requests a read-only review',()=>{
  run('openAddSync()');
  assert.equal(run('policyEditors.new.policy.mode'),'all');
  assert.equal(node('new-every-minutes').value,'240');
  assert.equal(node('new-release-policy-options').hidden,true);
  assert.match(node('new-release-policy-summary').textContent,/all release types/);
  node('new-repository').value='owner/new';node('new-every-minutes').value='15';node('new-release-limit').value='5';
  node('add-sync-form').listeners.submit({preventDefault(){}});
  assert.equal(run('actions[0].action'),'add-preview');
  assert.equal(run('actions[0].fields.release_policy.mode'),'all');
  assert.equal(run('actions.length'),1);
});
test('manager preserves legacy selection until policy is explicitly edited',()=>{
  run('delete data.rows[0].config.include_prereleases;renderManager(data.rows[0])');
  node('settings-form').listeners.submit({preventDefault(){}});
  assert.equal(run('actions[0].action'),'settings');
  assert.equal(run('actions[0].fields.include_prereleases'),true);
  assert.equal(run('actions[0].fields.every_minutes'),15);
  assert.equal(run("Object.hasOwn(actions[0].fields,'release_policy')"),false);
});
test('new and existing Asset Preview use current policy and limit',()=>{
  run('openAddSync()');node('new-repository').value='owner/new';node('new-release-limit').value='3';
  run("policyEditors.new.policy.mode='selected';policyEditors.new.policy.selected_types=['prerelease:rc'];openAssetPreview('new')");
  assert.equal(run('assetPreview.params.last'),3);
  assert.equal(run('assetPreview.params.release_policy.selected_types[0]'),'prerelease:rc');
  run("selected='owner/repo';renderManager(data.rows[0]);openAssetPreview('edit')");
  assert.equal(run('assetPreview.params.release_policy'),null);
  assert.equal(run('assetPreview.params.include_prereleases'),false);
  assert.equal(run('actions.length'),0);
});
test('refresh preview is non-destructive and shows exact scope before confirmation',()=>{
  run('previewReleaseRefresh();actions[0].callback(proof)');
  assert.equal(run('actions[0].action'),'refresh-preview');
  assert.equal(run('JSON.stringify(actions[0].fields)'),'{}');
  assert.equal(run('actions.length'),1);
  const html=node('release-refresh-preview').innerHTML;
  assert.match(html,/Will delete 2 local releases/);assert.match(html,/4 KiB/);
  assert.match(html,/nightly-1/);assert.match(html,/nightly-2/);assert.match(html,/older-unrecognized/);
  assert.match(html,/source checkout and Git history are not deleted/);
  assert.match(html,/cannot be undone/);assert.match(html,/deleted files are not restored automatically/);
  assert.match(html,/stable/);assert.match(html,/v1.0/);assert.doesNotMatch(html,/opaque-proof/);
});
test('confirmation sends only preview token and activates refresh progress',()=>{
  run('previewReleaseRefresh();actions[0].callback(proof);applyReleaseRefresh()');
  assert.equal(run('actions[1].action'),'refresh');
  assert.equal(run('JSON.stringify(actions[1].fields)'),JSON.stringify({token:'opaque-proof'}));
  run(`actions[1].callback({message:'Refresh queued.',first_sync:{id:'refresh',kind:'refresh_releases',status:'queued',updated_at:'2026-11-20T12:00:00Z'}})`);
  assert.equal(run('data.rows[0].first_sync.kind'),'refresh_releases');
  assert.equal(run('closes'),1);assert.equal(run('draws'),1);
});
test('unsaved channels, limits and patterns block destructive refresh',()=>{
  for(const change of ["policyEditors.edit.changed=true;policyEditors.edit.policy.mode='all'","$('release-limit').value='2'","enteredPatterns=['*.apk']"]){
    reset();run(`${change};updateReleaseRefreshAvailability();previewReleaseRefresh()`);
    assert.equal(node('release-refresh-preview-button').disabled,true);
    assert.equal(run('actions.length'),0);assert.match(run('errors[0]'),/Save your release selection/);
  }
});
test('archived and source-only entries cannot be resumed by refresh',()=>{
  for(const flag of ['archived','source_only']){
    reset();run(`data.rows[0].config.${flag}=true;renderManager(data.rows[0]);previewReleaseRefresh()`);
    assert.equal(node('release-refresh-preview-button').disabled,true);
    assert.equal(run('actions.length'),0);
    assert.match(node('release-refresh-note').textContent,flag==='archived'?/Resume it explicitly/:/source only/);
  }
});
test('running syncs and saved-setting changes invalidate refresh approval',()=>{
  run('previewReleaseRefresh();actions[0].callback(proof);data.rows[0].config.last=3;applyReleaseRefresh()');
  assert.equal(run('releaseRefreshPreview'),null);assert.equal(run('actions.length'),1);
  reset();run("data.rows[0].first_sync={status:'queued'};previewReleaseRefresh()");
  assert.equal(run('actions.length'),0);assert.match(run('errors[0]'),/current sync/);
});
test('editing the channel after preview cannot reuse its deletion approval',()=>{
  run('previewReleaseRefresh();actions[0].callback(proof)');
  run("policyEditors.edit.changed=true;policyEditors.edit.policy.mode='all'");
  run('applyReleaseRefresh()');
  assert.equal(run('releaseRefreshPreview'),null);assert.equal(run('actions.length'),1);
  assert.match(run('errors[0]'),/Save your release selection/);
});
test('late preview cannot target a different repository',()=>{
  run("previewReleaseRefresh();selected='another/repo';actions[0].callback(proof)");
  assert.equal(run('releaseRefreshPreview'),null);
});
test('untrusted tags and kept-item messages are escaped',()=>{
  const html=run(`releaseRefreshPreviewHTML({...proof,releases:[{tag:'<img>',folder:'<script>'}],selected:[{tag:'<tag>',prerelease:true}],skipped:[{folder:'<folder>',reason:'<reason>'}]})`);
  for(const tag of ['img','script','tag','folder','reason']){assert.match(html,new RegExp('&lt;'+tag+'&gt;'));assert.doesNotMatch(html,new RegExp('<'+tag+'>'))}
});
test('empty eligible selection cannot start a refresh',()=>{
  run('previewReleaseRefresh();actions[0].callback({...proof,selected:[]});applyReleaseRefresh()');
  assert.equal(run('actions.length'),1);
  assert.doesNotMatch(node('release-refresh-preview').innerHTML,/id="confirm-release-refresh"/);
});
test('refresh progress has its own label while legacy first sync retains its label',()=>{
  for(const status of ['queued','success','failed','interrupted']){
    context.status=status;
    const html=run("firstSyncHTML({kind:'refresh_releases',status})");
    assert.match(html,/Refresh releases/);assert.doesNotMatch(html,/First sync/);
  }
  assert.match(run("firstSyncHTML({status:'success'})"),/First sync complete/);
});
test('worker progress replaces the synthetic queue and stale snapshots cannot restart it',()=>{
  run(`data.rows[0].first_sync={id:'request',kind:'refresh_releases',status:'queued',queued_at:'2026-11-20T12:00:00Z'};
    data.rows[0].first_sync=newestProgress(data.rows[0].first_sync,{id:'worker',kind:'refresh_releases',status:'running',queued_at:'2026-11-20T12:00:01Z',updated_at:'2026-11-20T12:00:02Z'});
    data.rows[0].first_sync=newestProgress(data.rows[0].first_sync,{id:'worker',kind:'refresh_releases',status:'success',queued_at:'2026-11-20T12:00:01Z',updated_at:'2026-11-20T12:00:05Z'});
    data.rows[0].first_sync=newestProgress(data.rows[0].first_sync,{id:'request',kind:'refresh_releases',status:'queued',queued_at:'2026-11-20T12:00:00Z'});`);
  assert.equal(run('data.rows[0].first_sync.status'),'success');
  assert.match(run('firstSyncHTML(data.rows[0].first_sync)'),/Refresh releases complete/);
});
test('a refresh failure before reporting replaces old success and needs attention',()=>{
  run(`data.rows[0].finished_at='2026-11-20T12:00:00Z';data.rows[0].first_sync={kind:'refresh_releases',status:'failed',updated_at:'2026-11-20T12:01:00Z'};`);
  assert.equal(run('statusOf(data.rows[0])'),'failed');assert.equal(run('needs(data.rows[0])'),true);
  run("data.rows[0].first_sync.status='interrupted'");
  assert.equal(run('statusOf(data.rows[0])'),'interrupted');
});
test('a later normal sync supersedes prior refresh failure and archive suppresses attention',()=>{
  run(`data.rows[0].first_sync={kind:'refresh_releases',status:'failed',finished_at:'2026-11-20T12:01:00Z'};data.rows[0].started_at='2026-11-20T12:02:00Z';data.rows[0].status='running';`);
  assert.equal(run('statusOf(data.rows[0])'),'running');assert.equal(run('needs(data.rows[0])'),false);
  run("data.rows[0].status='success';data.rows[0].finished_at='2026-11-20T12:03:00Z'");
  assert.equal(run('statusOf(data.rows[0])'),'success');assert.equal(run('needs(data.rows[0])'),false);
  run("data.rows[0].config.archived=true;data.rows[0].finished_at='2026-11-20T12:00:00Z';delete data.rows[0].started_at");
  assert.equal(run('statusOf(data.rows[0])'),'archived');assert.equal(run('needs(data.rows[0])'),false);
});
const discoveredTypes=[{id:'stable:lts',label:'LTS (stable)',source:'tag',count:3,examples:['v2-lts']},{id:'prerelease:nightly',label:'Nightly',source:'tag',count:12,examples:['nightly-2026-11-20']}];
context.discoveredTypes=discoveredTypes;
test('repository discovery only shows actual descriptors and examples',()=>{
  run('openAddSync()');node('new-repository').value='owner/new';
  run("discoverReleaseTypes('new');actions[0].callback({release_types:discoveredTypes})");
  assert.equal(run('actions[0].action'),'release-types');
  assert.equal(node('new-release-policy-options').hidden,false);
  assert.match(node('new-release-types-list').innerHTML,/LTS \(stable\)|nightly-2026-11-20/);
  assert.doesNotMatch(node('new-release-types-list').innerHTML,/Release candidate|Beta/);
  assert.equal(run('policyEditors.new.policy.mode'),'all');
});
test('restricted selection leaves newly discovered types unchecked and keeps unavailable selections',()=>{
  run(`data.rows[0].config.release_policy={...defaultReleasePolicy(),mode:'selected',selected_types:['stable:lts']};data.rows[0].release_types=[discoveredTypes[0]];renderManager(data.rows[0]);discoverReleaseTypes('edit');actions[0].callback({release_types:discoveredTypes})`);
  const inputs=node('release-types-list').inputs;
  assert.equal(inputs.find(input=>input.dataset.releaseType==='stable:lts').checked,true);
  assert.equal(inputs.find(input=>input.dataset.releaseType==='prerelease:nightly').checked,false);
  run("discoverReleaseTypes('edit');actions[1].callback({release_types:[discoveredTypes[1]]})");
  const unavailable=node('release-types-list').inputs.find(input=>input.dataset.releaseType==='stable:lts');
  assert.equal(unavailable.checked,true);assert.equal(unavailable.disabled,false);
  assert.equal(run("policyEditors.edit.policy.selected_types.includes('stable:lts')"),true);
});
test('separate limits and automatic type mode reach previews together',()=>{
  run(`data.rows[0].config.release_policy={...defaultReleasePolicy(),limit_mode:'per_type',limits:{'stable:lts':1,'prerelease:nightly':5}};data.rows[0].release_types=discoveredTypes;renderManager(data.rows[0]);openAssetPreview('edit')`);
  assert.equal(run('assetPreview.params.release_policy.mode'),'all');
  assert.equal(run('assetPreview.params.release_policy.limits["stable:lts"]'),1);
  assert.equal(run('assetPreview.params.release_policy.limits["prerelease:nightly"]'),5);
  assert.match(node('asset-context').textContent,/LTS \(stable\) 1/);
  assert.match(node('asset-context').textContent,/Nightly 5/);
});
test('legacy stable selection includes discovered stable families until explicit conversion',()=>{
  run("data.rows[0].release_types=discoveredTypes;renderManager(data.rows[0])");
  assert.equal(run('policyEditors.edit.legacy'),true);
  assert.equal(run("policyEditors.edit.policy.selected_types.includes('stable:lts')"),true);
  assert.equal(run("readReleaseSettings('edit').release_policy"),null);
});
test('custom grouping rules keep immutable IDs and ordered patterns',()=>{
  run('openAddSync()');node('new-add-type-rule').listeners.click();
  const id=run('policyEditors.new.policy.type_rules[0].id');
  const input={value:'Long-term support',dataset:{},closest(){return {dataset:{typeRule:id}}},hasAttribute(name){return name==='data-rule-label'}};
  node('new-type-rules').listeners.input({target:input});
  input.value='lts-*, *-lts';input.hasAttribute=name=>name==='data-rule-patterns';
  node('new-type-rules').listeners.input({target:input});
  assert.equal(run('policyEditors.new.policy.type_rules[0].id'),id);
  assert.equal(run('policyEditors.new.policy.type_rules[0].label'),'Long-term support');
  assert.equal(run('JSON.stringify(policyEditors.new.policy.type_rules[0].patterns)'),JSON.stringify(['lts-*','*-lts']));
  assert.equal(run('policyEditors.new.rulesDirty'),true);
});
function prepareAddReview(){
  run('openAddSync()');node('new-repository').value='owner/new';node('new-branch').value='main';
  run(`reviewAddSync();actions[0].callback({repository:'owner/new',job:actions[0].fields,review_token:'review-secret',source_path:'C:/sync/owner/new',releases_path:'C:/sync/owner/new-releases',run_now:actions[0].fields.run_now,assets:{release_types:discoveredTypes,releases:[{tag:'v1',type_label:'LTS (stable)',assets:[{name:'app.zip',size:2048},{name:'app.exe',size:100}],source_archive:{name:'source-v1.zip'}}]}})`);
}
test('Add review shows selected assets and folders without creating a sync',()=>{
  prepareAddReview();
  assert.equal(run('actions.length'),1);assert.equal(run('actions[0].action'),'add-preview');
  assert.equal(node('add-sync-form').hidden,true);
  const html=node('add-sync-review').innerHTML;
  assert.match(html,/app.zip/);assert.doesNotMatch(html,/app.exe/);assert.match(html,/2 KiB/);
  assert.match(html,/source-v1.zip/);assert.match(html,/unknown size/);assert.match(html,/C:\/sync\/owner\/new-releases/);
  assert.match(html,/main/);assert.match(html,/independently of release type or tag selection/);
  assert.match(html,/Start the first sync immediately/);assert.doesNotMatch(html,/review-secret/);
});
test('only explicit final Add submits the captured review token',()=>{
  prepareAddReview();run('confirmAddSync()');
  assert.equal(run('actions[1].action'),'add');assert.equal(run('actions[1].fields.review_token'),'review-secret');
  assert.equal(run('actions[1].fields.release_policy.mode'),'all');
  assert.equal(run('actions[1].fields.branch'),'main');
});
test('draft changes and Back invalidate Add review approval',()=>{
  prepareAddReview();node('new-release-limit').value='2';run('confirmAddSync()');
  assert.equal(run('actions.length'),1);assert.equal(run('addSyncReview'),null);
  assert.match(run('errors.at(-1)'),/settings changed/);
  reset();prepareAddReview();node('edit-add-sync').listeners.click();
  assert.equal(run('addSyncReview'),null);assert.equal(node('add-sync-form').hidden,false);
  run('confirmAddSync()');assert.equal(run('actions.length'),1);
});
test('new repository input clears type choices from the previous repository',()=>{
  run('openAddSync()');node('new-repository').value='owner/other';
  run("policyEditors.new.policy.mode='selected';policyEditors.new.policy.selected_types=['prerelease:nightly'];policyEditors.new.catalog=discoveredTypes;resetNewRepositoryPolicy()");
  assert.equal(run('policyEditors.new.policy.mode'),'all');assert.equal(run('policyEditors.new.catalog.length'),0);
  assert.equal(node('new-release-policy-options').hidden,true);
});
test('archived type settings save without resuming, with a separate Resume action',()=>{
  run("data.rows[0].config.archived=true;data.rows[0].config.release_policy=defaultReleasePolicy();renderManager(data.rows[0])");
  node('settings-form').listeners.submit({preventDefault(){}});
  assert.equal(run('actions[0].action'),'settings');assert.equal(run('data.rows[0].config.archived'),true);
  node('resume-sync').listeners.click();assert.equal(run('actions[1].action'),'resume');
});
test('review content escapes repository and remote names and source-only excludes downloads',()=>{
  const html=run(`addSyncReviewHTML({repository:'<repo>',job:{last:5,release_policy:defaultReleasePolicy(),asset_patterns:['*.zip'],branch:'<branch>',source_only:true},source_path:'<path>',assets:{releases:[{tag:'<tag>',assets:[{name:'app.zip',size:2}]}]},run_now:false})`);
  assert.match(html,/&lt;repo&gt;/);assert.match(html,/&lt;branch&gt;/);assert.match(html,/&lt;path&gt;/);
  assert.doesNotMatch(html,/<repo>|app.zip/);assert.match(html,/Source-only sync/);assert.match(html,/Wait for the schedule/);
});
console.log(`${checks} release selection, Add review, and refresh UI tests passed; full dashboard JavaScript syntax valid.`);
