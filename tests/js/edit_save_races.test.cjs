const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const read=file=>fs.readFileSync(path.join(__dirname,'../../',file),'utf8');
const extract=(src,name,indent='  ')=>{const a=src.indexOf(`${indent}function ${name}(`);return src.slice(a,src.indexOf(`\n${indent}}`,a)+indent.length+2)};
function ground(){
 const source=read('templates/ground_conditions.html'),a=source.indexOf('        window.saveAll = async function () {');
 const save=source.slice(a,source.indexOf('\n        };',a)+11);
 let value=10,resolve,sent,draft='10',requests=0;
 const button={disabled:false};
 const c={window:{},document:{getElementById:()=>button},MATCH_FORMAT:'T20',hasUnsavedChanges:true,committedSnapshot:'0',
  buildConfig:()=>({value}),captureDraft:()=>String(value),clearGroundDraft:()=>{draft=null;c.hasUnsavedChanges=false},persistDraftNow:()=>{draft=String(value)},showToast(){},
  fetch:(_url,opts)=>{requests++;sent=JSON.parse(opts.body);return new Promise(r=>resolve=r)}};
 vm.createContext(c);vm.runInContext(save,c);
 return {c,edit:v=>value=v,resolve:ok=>resolve({ok,json:async()=>({message:'Saved'})}),draft:()=>draft,sent:()=>sent,requests:()=>requests};
}
test('edits during saving stay dirty and prevent Save & Switch navigation',async()=>{
 const s=ground(),p=s.c.window.saveAll();s.edit(20);s.resolve(true);
 assert.equal(await p,false);assert.equal(s.sent().value,10);assert.equal(s.c.committedSnapshot,'10');assert(s.c.hasUnsavedChanges);assert.equal(s.draft(),'20');
});
test('unchanged successful save clears draft; duplicate in-flight save is ignored',async()=>{
 const s=ground(),p=s.c.window.saveAll();assert.equal(await s.c.window.saveAll(),false);assert.equal(s.requests(),1);s.resolve(true);
 assert.equal(await p,true);assert.equal(s.draft(),null);assert.equal(s.c.hasUnsavedChanges,false);
});
test('failed ground save retains draft and dirty state',async()=>{
 const s=ground(),p=s.c.window.saveAll();s.resolve(false);assert.equal(await p,false);assert(s.c.hasUnsavedChanges);assert.equal(s.draft(),'10');
});
test('clean team visits and reverting edits do not leave drafts; real edits persist',()=>{
 const source=read('templates/team_create.html'),storage=new Map();
 const c={IS_EDIT:true,LS_KEY:'team_edit_draft_v1:TEST',saveTimer:null,lastSavedJson:'',editBaseline:'',clearTimeout(){},setSaveStatus(){},saveStatus:{},saveStatusLabel:{},
 state:{identity:{team_name:'Saved'},activeFmt:'T20',rosters:{},leaders:{}},localStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)}};
 vm.createContext(c);for(const name of ['snapshot','comparableSnapshot','clearDraft','saveDraftNow'])vm.runInContext(extract(source,name),c);
 c.editBaseline=c.comparableSnapshot(c.snapshot());c.saveDraftNow();assert.equal(storage.size,0);
 c.state.activeFmt='FC';c.saveDraftNow();assert.equal(storage.size,0);
 c.state.identity.team_name='Edited';c.saveDraftNow();assert.equal(JSON.parse(storage.get(c.LS_KEY)).identity.team_name,'Edited');
 c.state.identity.team_name='Saved';c.saveDraftNow();assert.equal(storage.size,0);
});
test('successful edit cleanup removes only the original team draft, even after renaming',()=>{
 const source=read('templates/manage_teams.html'),removed=[];
 const c={URL,window:{location:{href:'https://example.test/teams/manage?clear_team_edit_draft=OLD&search=keep'},history:{replaceState:(_,__,url)=>c.url=url}},document:{title:'Teams'},localStorage:{removeItem:k=>removed.push(k)}};
 c.window.scxStorage=c.localStorage;
 vm.createContext(c);vm.runInContext(extract(source,'clearTeamEditDraftIfNeeded','        '),c);c.clearTeamEditDraftIfNeeded();
 assert.deepEqual(removed,['team_edit_draft_v1:OLD']);assert.equal(c.url,'/teams/manage?search=keep');
});
