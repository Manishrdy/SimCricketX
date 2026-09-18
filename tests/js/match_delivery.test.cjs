const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../../static/js/match_detail.js'),'utf8');
const flush=()=>new Promise(setImmediate);
function setup(){
 const requests=[],pending=[],processed=[],statuses=[],timers=new Map(),handlers={};let seq=0;
 const c={AbortController,URLSearchParams,encodeURIComponent,matchOver:false,ballInFlight:false,simTimerId:null,
  window:{location:{pathname:'/match/a',reload(){}}},matchData:{match_id:'a'},scorecardIsVisible:()=>false,
  setTimeout:(fn,ms)=>{const id=++seq;timers.set(id,{fn,ms});return id},clearTimeout:id=>timers.delete(id),
  fetch:(url,options)=>{requests.push({url,options});return new Promise((resolve,reject)=>pending.push({resolve,reject}))},
  _processBallResult:async data=>{processed.push(data);c.ballInFlight=false},
  io:()=>({on:(event,fn)=>handlers[event]=fn,emit:(event,data)=>requests.push({event,data})})
 };
 vm.createContext(c);
 vm.runInContext(source.slice(source.indexOf('let deliveryToken ='),source.indexOf('// These are scripted match comments')),c);
 const a=source.indexOf('function startMatch()');vm.runInContext(source.slice(a,source.indexOf('// --- Scorecard Logic ---',a)),c);
 c.showDeliveryStatus=(...args)=>statuses.push(args);
 const run=script=>vm.runInContext(script,c);
 const respond=(body,status=200)=>pending.at(-1).resolve({ok:status<300,status,json:async()=>body});
 return {c,requests,pending,processed,statuses,timers,handlers,run,respond};
}
const result={delivery_request:'one',delivery_token:'two',score:10};
test('first delivery gets a server token and uses it in the HTTP request',async()=>{
 const s=setup();s.c.startMatch();assert(s.requests[0].url.includes('live-state?delivery=1'));
 s.respond({status:'delivery_ready',delivery_token:'one'});await flush();
 assert.equal(JSON.parse(s.requests[1].options.body).delivery_token,'one');
 s.respond(result);await flush();assert.equal(s.processed.length,1);assert.equal(s.run('deliveryToken'),'two');
});
test('socket disconnect recovers the lost result without another ball request',async()=>{
 const s=setup();s.run("deliveryToken='one'");s.handlers.connect();s.c.startMatch();s.handlers.disconnect();
 assert(s.statuses.at(-1)[0].includes('interrupted'));
 const p=s.c.recoverDelivery();s.respond({status:'delivery_recovery',delivery:result});await p;
 assert.equal(s.requests.filter(r=>r.options?.method==='POST').length,0);assert.equal(s.processed.length,1);
 await s.handlers.ball_result(result);assert.equal(s.processed.length,1);
});
test('permanent socket outage retries the same token through HTTP after checking state',async()=>{
 const s=setup();s.run("deliveryToken='one'");s.handlers.connect();s.c.startMatch();s.handlers.ws_error();
 const p=s.c.recoverDelivery();s.respond({status:'delivery_ready',delivery_token:'one'});await p;
 assert.equal(JSON.parse(s.requests.at(-1).options.body).delivery_token,'one');
 s.respond(result);await flush();assert.equal(s.processed.length,1);
});
test('HTTP rejection and missing socket replies both trigger bounded recovery',async()=>{
 const s=setup();s.run("deliveryToken='one'");s.c.startMatch();s.respond({error:'unavailable'},503);await flush();
 assert(s.statuses.at(-1)[0].includes('interrupted'));
 for(let i=0;i<4;i++){
  s.run('clearTimeout(recoveryTimer); recoveryTimer=null');
  const p=s.c.recoverDelivery();s.pending.at(-1).reject(Error('offline'));await p;
 }
 assert.match(s.statuses.at(-1)[0],/Match paused/);
 assert.equal(s.requests.filter(r=>r.options?.method==='POST').length,1);
 const t=setup();t.run("deliveryToken='one'");t.handlers.connect();t.c.startMatch();
 [...t.timers.values()].find(t=>t.ms===15000).fn();assert(t.statuses.length);
});
test('instance changes require reload and never automatically advance',async()=>{
 const s=setup();s.run("pendingDelivery='old'");const p=s.c.recoverDelivery();s.respond({reload_required:true},409);await p;
 assert.equal(s.run('recoveryBlocked'),true);assert.equal(s.statuses.at(-1)[1],true);
 s.c.startMatch();assert.equal(s.requests.length,1);
});
test('late socket result wins over a stale state lookup without duplicate processing',async()=>{
 const s=setup();s.run("pendingDelivery='one'");const p=s.c.recoverDelivery();
 await s.c.acceptDelivery(result);assert.equal(s.run('recoveryRunning'),false);
 s.respond({status:'delivery_ready',delivery_token:'one'});await p;
 assert.equal(s.processed.length,1);assert.equal(s.requests.length,1);assert.equal(s.run('deliveryToken'),'two');
});
test('an in-flight ball or visible scorecard blocks a second simulation loop',()=>{
 const s=setup();s.c.ballInFlight=true;s.c.startMatch();assert.equal(s.requests.length,0);
 s.c.ballInFlight=false;s.c.scorecardIsVisible=()=>true;s.c.startMatch();assert.equal(s.requests.length,0);
});
