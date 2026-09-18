const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const widget=fs.readFileSync(path.join(__dirname,'../../static/js/support_widget.js'),'utf8');
function fn(source,name){const a=source.indexOf(`    function ${name}(`);return source.slice(a,source.indexOf('\n    }',a)+6)}
const flush=()=>new Promise(setImmediate);
function setup(){
 const requests=[],pending=[],timers=new Map();let seq=0;
 const c={outbox:null,sending:false,cooldownUntil:0,cooldownTimer:null,lockedUntilAdminReply:false,currentStatus:'open',conversationId:null,
  input:{value:'please help',disabled:false},sendBtn:{disabled:false},statusEl:{textContent:''},
  setStatus(text){c.statusEl.textContent=text},renderConversationStatus(){},renderMessage(){},applyConversation(){},markRead(){},
  panel:{getAttribute:()=>''},window:{location:{href:'http://local/home'},crypto:{randomUUID:()=>`nonce-${++seq}`},
   setTimeout:fn=>{const id=++seq;timers.set(id,fn);return id},clearTimeout:id=>timers.delete(id),setInterval:()=>99,clearInterval(){}},
  fetch:(url,options)=>{requests.push({url,options});return new Promise((resolve,reject)=>pending.push({resolve,reject}))}
 };
 vm.createContext(c);
 for(const name of ['formatRemaining','clearCooldown','clearRateLock','startCooldown','applyRateState','makeNonce','sendViaHttp','updateSendAvailability','sendMessage'])vm.runInContext(fn(widget,name),c);
 const respond=(status,payload)=>pending.at(-1).resolve({status,json:async()=>payload});
 const delivered={message:{id:1},conversation:{id:'a'},rate:{allowed:true}};
 return {c,pending,requests,timers,respond,delivered};
}
test('support keeps text while pending, rejects repeat sends, clears only on acknowledgement',async()=>{
 const s=setup();const p=s.c.sendMessage(s.c.input.value);s.c.sendMessage('repeat');
 assert.equal(s.requests.length,1);assert.equal(s.c.input.value,'please help');assert(s.c.sendBtn.disabled);
 s.respond(201,s.delivered);await p;assert.equal(s.c.input.value,'');assert.equal(s.c.outbox,null);assert(!s.c.sendBtn.disabled);
});
test('offline retry preserves the message and reuses its nonce',async()=>{
 const s=setup();const p=s.c.sendMessage(s.c.input.value);s.pending[0].reject(Error('offline'));await p;
 assert.equal(s.c.input.value,'please help');assert.match(s.c.statusEl.textContent,/retry/);
 const retry=s.c.sendMessage(s.c.input.value);
 assert.equal(JSON.parse(s.requests[0].options.body).client_nonce,JSON.parse(s.requests[1].options.body).client_nonce);
 s.respond(201,s.delivered);await retry;
});
test('lost acknowledgement times out and can retry even after realtime announces a rate lock',async()=>{
 const s=setup();const first=s.c.sendMessage(s.c.input.value);
 s.c.applyRateState({allowed:false,blocked_until_admin_reply:true});
 [...s.timers.values()][0]();await first;
 assert(!s.c.sendBtn.disabled);assert.equal(s.c.input.value,'please help');
 const retry=s.c.sendMessage(s.c.input.value);s.respond(201,{...s.delivered,rate:{allowed:false,blocked_until_admin_reply:true}});await retry;
 assert.equal(s.c.input.value,'');assert(s.c.sendBtn.disabled);
 s.pending[0].resolve({status:201,json:async()=>s.delivered});await flush();assert(s.c.sendBtn.disabled);
});
test('rate rejection retains draft and keeps sending blocked until unlocked',async()=>{
 const s=setup();const p=s.c.sendMessage(s.c.input.value);s.respond(429,{rate:{allowed:false,blocked_until_admin_reply:true}});await p;
 assert.equal(s.c.input.value,'please help');assert(s.c.sendBtn.disabled);
 s.c.applyRateState({allowed:true});assert(!s.c.sendBtn.disabled);
});
test('invalid acknowledgement never clears the draft',async()=>{
 const s=setup();const p=s.c.sendMessage(s.c.input.value);s.respond(200,{});await p;
 assert.equal(s.c.input.value,'please help');assert(s.c.outbox.uncertain);
});
