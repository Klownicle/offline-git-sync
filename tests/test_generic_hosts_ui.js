// Generic provider UI checks. No browser, network calls, config writes, or tasks.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'github_dashboard.py'), 'utf8').replace(/\r\n/g, '\n');
const script = source.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script);
const names = ['canonicalPublicHost','canonicalRepositoryPart','repositoryInfo','hostBadgeHTML',
  'repositoryLinkHTML','commitLink','commitSummaryHTML','recordAddedSync','newestProgress'];
const context = vm.createContext({document:{getElementById(){return {}}}});
vm.runInContext(script.split(/\r?\n/).filter(line=>line.startsWith('const $=')||
  line.startsWith('const activeFirst=')||names.some(name=>line.startsWith(`function ${name}(`))).join('\n'),context);
vm.runInContext('let data={rows:[]}',context);
const run=code=>vm.runInContext(code,context);
let count=0;
function test(name,fn){fn();count++;console.log(`PASS ${name}`)}

test('MagicBean identity and links preserve canonical owner and repository',()=>{
  const identity=JSON.parse(run(`JSON.stringify(repositoryInfo({repository:'git.magicbean.dev/MagicBeanDev/Longshot'}))`));
  assert.deepEqual(identity,{host:'git.magicbean.dev',owner:'MagicBeanDev',name:'Longshot',provider:'forgejo',web_url:'https://git.magicbean.dev/MagicBeanDev/Longshot'});
  assert.match(run(`hostBadgeHTML({repository:'git.magicbean.dev/MagicBeanDev/Longshot'})`),/Forgejo \/ Gitea · git.magicbean.dev/);
  assert.match(run(`repositoryLinkHTML({repository:'git.magicbean.dev/MagicBeanDev/Longshot'})`),/href="https:\/\/git.magicbean.dev\/MagicBeanDev\/Longshot"/);
});
test('an additional public host works without a frontend allowlist',()=>{
  assert.equal(run(`repositoryInfo({repository:'codeberg.org/team/project'}).web_url`),'https://codeberg.org/team/project');
  assert.match(run(`commitSummaryHTML({repository:'codeberg.org/team/project'},'abcdef0 Fix <release>')`),/https:\/\/codeberg.org\/team\/project\/commit\/abcdef0/);
  assert.match(run(`commitSummaryHTML({repository:'codeberg.org/team/project'},'abcdef0 Fix <release>')`),/&lt;release&gt;/);
});
test('GitHub and existing Eden retain their established URLs',()=>{
  assert.equal(run(`repositoryInfo({repository:'owner/repo'}).web_url`),'https://github.com/owner/repo');
  assert.equal(run(`repositoryInfo({repository:'git.eden-emu.dev/eden-emu/eden'}).web_url`),'https://git.eden-emu.dev/eden-emu/eden');
  assert.equal(run(`repositoryInfo({repository:'git.eden-emu.dev/eden-emu/eden'}).provider`),'forgejo');
});
test('row metadata cannot redirect links or change displayed canonical identity',()=>{
  const identity=JSON.parse(run(`JSON.stringify(repositoryInfo({repository:'git.magicbean.dev/MagicBeanDev/Longshot',web_url:'javascript:alert(1)',host:'attacker.example.com',owner:'<img src=x>',name:'other',provider:'github'}))`));
  assert.equal(identity.web_url,'https://git.magicbean.dev/MagicBeanDev/Longshot');
  assert.equal(identity.owner,'MagicBeanDev');
  assert.equal(identity.name,'Longshot');
  assert.equal(identity.provider,'forgejo');
});
test('malformed or private-looking canonical keys never create links',()=>{
  const keys=['https://git.magicbean.dev/owner/repo','javascript:alert(1)/owner/repo',
    'git.magicbean.dev@attacker.com/owner/repo','git.magicbean.dev:443/owner/repo',
    'git.magicbean.dev./owner/repo','Git.MagicBean.dev/owner/repo','127.0.0.1/owner/repo',
    'localhost/owner/repo','git.localhost/owner/repo','git.internal/owner/repo',
    'git.home.arpa/owner/repo','con.example.com/owner/repo','github.com/owner/repo',
    'git.magicbean.dev/../repo','git.magicbean.dev/owner/CON.txt',
    'git.magicbean.dev/owner/repo?x','git.magicbean.dev/owner/repo#x',
    'git.magicbean.dev/owner/repo/extra','git.magicbean.dev/owner/repo\n',
    'git.magicbean.dev\n/owner/repo','owner/<img src=x onerror=alert(1)>'];
  for(const key of keys){context.badKey=key;assert.equal(run('repositoryInfo({repository:badKey}).web_url'),'');
    assert.doesNotMatch(run('repositoryLinkHTML({repository:badKey})'),/<a |<img/);
    assert.doesNotMatch(run("commitLink({repository:badKey},'abcdef012345')"),/<a /);}
});
test('optimistic add keeps complete generic key and leaves other hosts distinct',()=>{
  run(`recordAddedSync({repository:'git.magicbean.dev/MagicBeanDev/Longshot',job:{last:3},first_sync:{status:'queued'}});recordAddedSync({repository:'MagicBeanDev/Longshot',job:{last:3}})`);
  assert.equal(run('data.rows.length'),2);
  assert.equal(run('data.rows[0].repository'),'git.magicbean.dev/MagicBeanDev/Longshot');
  assert.equal(run('data.rows[0].host'),'git.magicbean.dev');
  assert.equal(run('data.rows[1].host'),'github.com');
});
test('form describes automatic platform check while keeping manual controls',()=>{
  assert.ok(script.includes('Forgejo / Gitea compatibility is checked automatically'));
  assert.ok(script.includes('Use owner/repository for GitHub'));
  assert.ok(script.includes("patternEditorHTML('new',['*windows*x64*.zip'])"));
  assert.ok(script.includes('<code>*.apk</code> or <code>*.zip</code>'));
  assert.ok(script.includes('<h3>Refresh Assets</h3>'));
  assert.ok(source.includes('Scheduled task details'));
  assert.ok(!script.includes('For Eden Android builds'));
});
console.log(`${count} generic provider UI tests passed; full dashboard JavaScript syntax valid.`);
