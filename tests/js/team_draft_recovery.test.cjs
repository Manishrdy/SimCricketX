const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../../templates/team_create.html'),'utf8');
function extract(name){const start=source.indexOf(`  function ${name}(`);return source.slice(start,source.indexOf('\n  }',start)+4);}
function setup(){
 const saved=new Map(),nodes={};const c={IS_EDIT:false,LS_KEY:'team_create_draft_v1',VALID_FORMATS:['T20','ListA','FC'],state:{identity:{team_name:'Latest'},activeFmt:'FC',rosters:{T20:[],ListA:[],FC:[{name:'Keeper',role:'Wicketkeeper'}]},leaders:{FC:{captain:'Keeper',wicketkeeper:'Keeper'}}},saveTimer:1,lastSavedJson:'',clearTimeout(){c.cancelled=true},
 localStorage:{getItem:k=>saved.get(k),setItem:(k,v)=>saved.set(k,v)},document:{getElementById:id=>nodes[id]},console,
 colorSwatch:{style:{}},tabButtons:[],saveStatus:{},saveStatusLabel:{}};
 for(const key of ['nameInput','shortInput','groundInput','pitchSelect','colorInput'])c[key]={value:''};
 vm.createContext(c);for(const name of ['snapshot','setSaveStatus','saveDraftNow','loadDraft','loadSubmittedDraft'])vm.runInContext(extract(name),c);
 // Record the mode but still run the real indicator, so label assertions read the page's own text.
 const setSaveStatus=c.setSaveStatus;c.setSaveStatus=mode=>{c.status=mode;setSaveStatus(mode)};
 return {c,saved,nodes};
}
test('final draft snapshot is flushed synchronously before leaving, including latest order and leaders',()=>{
 const s=setup();s.c.saveDraftNow();const restored=JSON.parse(s.saved.get('team_create_draft_v1'));
 assert.equal(restored.activeFmt,'FC');assert.equal(restored.rosters.FC[0].name,'Keeper');assert.equal(restored.leaders.FC.captain,'Keeper');assert.equal(s.c.cancelled,true);
 assert.equal(source.includes("window.addEventListener('beforeunload'"),false);
});
test('server-rejected form restores all formats and identity even when storage is denied',()=>{
 const s=setup();s.c.localStorage.getItem=()=>{throw Error('denied')};
 const profiles={T20:{players:[{name:'T20'}],captain:'T20',wicketkeeper:'T20'},FC:{players:[{name:'Second'},{name:'First'}],captain:'First',wicketkeeper:'Second'}};
 s.nodes['tc-submitted-data']={textContent:JSON.stringify({team_name:"O'Brien XI",short_code:'TEST',home_ground:'Home',pitch_preference:'Flat',team_color:'#123456',active_format:'FC',profiles_payload:JSON.stringify(profiles)})};
 assert.equal(s.c.loadSubmittedDraft(),true);assert.equal(s.c.nameInput.value,"O'Brien XI");assert.equal(s.c.state.activeFmt,'FC');assert.deepEqual(Array.from(s.c.state.rosters.FC,p=>p.name),['Second','First']);assert.equal(s.c.state.leaders.FC.captain,'First');assert.equal(s.c.state.rosters.ListA.length,0);
});
test('failed storage writes never report a saved draft',()=>{
 const s=setup();s.c.localStorage.setItem=()=>{throw Error('quota')};s.c.saveDraftNow();assert.notEqual(s.c.status,'saved');assert.match(s.c.saveStatusLabel.textContent,/not saved/);
});
test('successful redirect cleanup removes the actual draft key; ordinary visits keep it',()=>{
 const manage=fs.readFileSync(path.join(__dirname,'../../templates/manage_teams.html'),'utf8');
 const start=manage.indexOf('        function clearTeamCreateDraftIfNeeded()');const fn=manage.slice(start,manage.indexOf('\n        }',start)+10);
 const removed=[];const c={shouldClearTeamDraft:false,localStorage:{removeItem:k=>removed.push(k)},URL,window:{location:{href:'https://example.test/teams/manage?clear_team_draft=1'},history:{replaceState(){}}},document:{title:'Teams'}};
 c.window.scxStorage=c.localStorage;
 vm.createContext(c);vm.runInContext(fn,c);c.clearTeamCreateDraftIfNeeded();assert.equal(removed.length,0);
 c.shouldClearTeamDraft=true;c.clearTeamCreateDraftIfNeeded();assert(removed.includes('team_create_draft_v1'));
});
