const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'), path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../../static/js/admin_support.js'),'utf8');
function extract(name){const start=source.indexOf(`    function ${name}(`);return source.slice(start,source.indexOf('\n    }',start)+6);}
function setup(){
 const pending=[],rendered=[],requests=[];
 const node=()=>({value:'',textContent:'',innerHTML:'',disabled:true,style:{},classList:{add(){},remove(){}},focus(){}});
 const c={selectedId:null,conversationRequest:0,conversationReady:false,composerDrafts:{},outbox:{},sendStatus:null,pendingThreadMessages:[],conversations:{},socket:null,seenMessageIds:{},renderedMessages:[],
  window:{setTimeout,clearTimeout,crypto:null,confirm:()=>true,alert(){}},renderList(){},currentConversationRows:()=>[],renderMessages:messages=>{rendered.length=0;rendered.push(...messages)},renderMessage:m=>rendered.push(m),renderContext(){},markRead(){},autosize(){},statusInfo:()=>({key:'open',label:'Open'}),initials:()=>'',fmt:()=>'',setStatusFilter(){},
  requestJson:(url,options)=>{requests.push({url,options});return new Promise((resolve,reject)=>pending.push({resolve,reject}))}
 };
 for(const name of ['input','sendBtn','closeBtn','reopenBtn','deleteBtn','titleEl','subtitleEl','threadAvatarEl','threadStatusEl','messagesEl','contextEl'])c[name]=node();
 vm.createContext(c);for(const name of ['resetThread','selectConversation','updateHeader','upsertConversation','updateSendState','sendMessage','mutateConversation','deleteConversation'])vm.runInContext(extract(name),c);
 return {c,pending,rendered,requests};
}
function response(id){return {conversation:{id,user_label:`User ${id}`,status:'open'},messages:[{id,body:`Message ${id}`}],context:{}};}
async function open(s,id){const promise=s.c.selectConversation(id);s.pending.at(-1).resolve(response(id));await promise;}
test('out-of-order conversation responses cannot change the displayed recipient',async()=>{
 const s=setup(),a=s.c.selectConversation(1),b=s.c.selectConversation(2);
 s.pending[1].resolve(response(2));await b;s.pending[0].resolve(response(1));await a;
 assert.equal(s.c.selectedId,2);assert.equal(s.c.titleEl.textContent,'User 2');assert.equal(s.rendered[0].id,2);
 const send=s.c.sendMessage('reply');assert(s.requests.at(-1).url.endsWith('/2/messages'));
 s.pending.at(-1).resolve({message:{id:50}});await send;
});
test('loading and failed loads block replies and stale errors do not replace the active thread',async()=>{
 const s=setup();await open(s,1);s.c.input.value='draft for one';
 const old=s.c.selectConversation(2);assert.equal(s.c.input.disabled,true);assert.equal(s.c.sendBtn.disabled,true);
 s.c.sendMessage('must not send');assert.equal(s.requests.length,2);
 s.c.upsertConversation(response(2).conversation);assert.equal(s.c.input.disabled,true);
 const current=s.c.selectConversation(3);s.pending[2].resolve(response(3));await current;
 s.pending[1].reject(new Error('offline'));await old;assert.equal(s.c.titleEl.textContent,'User 3');
 const failed=s.c.selectConversation(4);s.pending[3].reject(new Error('offline'));await failed;
 assert.equal(s.c.conversationReady,false);assert.equal(s.c.input.disabled,true);s.c.sendMessage('blocked');assert.equal(s.requests.length,4);
 await open(s,1);assert.equal(s.c.input.value,'draft for one');
});
test('A to B to A uses request generations rather than ID alone',async()=>{
 const s=setup(),old=s.c.selectConversation(1),b=s.c.selectConversation(2),latest=s.c.selectConversation(1);
 s.pending[2].resolve(response(1));await latest;s.pending[0].resolve({...response(1),conversation:{id:1,user_label:'Stale',status:'closed'}});await old;
 s.pending[1].resolve(response(2));await b;assert.equal(s.c.titleEl.textContent,'User 1');assert.equal(s.c.input.disabled,false);
});
test('a mismatched server response never enables sending',async()=>{
 const s=setup(),p=s.c.selectConversation(1);s.pending[0].resolve(response(2));await p;
 assert.equal(s.c.conversationReady,false);assert.equal(s.c.sendBtn.disabled,true);
});
test('late send success and failure cannot insert text into another conversation',async()=>{
 const s=setup();await open(s,1);s.c.sendMessage('for one');const sent=s.pending.at(-1);await open(s,2);
 sent.resolve({conversation:response(1).conversation,message:{id:99}});await new Promise(setImmediate);
 assert.deepEqual(s.rendered.map(m=>m.id),[2]);assert.equal(s.c.titleEl.textContent,'User 2');
 await open(s,1);s.c.sendMessage('failed for one');const failed=s.pending.at(-1);await open(s,2);s.c.input.value='for two';
 failed.reject(new Error('offline'));await new Promise(setImmediate);assert.equal(s.c.input.value,'for two');await open(s,1);assert.equal(s.c.input.value,'failed for one');
});
test('late close and delete responses do not reset a different conversation',async()=>{
 const s=setup();await open(s,1);const close=s.c.mutateConversation('close');const closing=s.pending.at(-1);await open(s,2);
 closing.resolve({conversation:{...response(1).conversation,status:'closed'}});await close;assert.equal(s.c.titleEl.textContent,'User 2');
 await open(s,1);const del=s.c.deleteConversation();const deleting=s.pending.at(-1);await open(s,2);deleting.resolve({});await del;
 assert.equal(s.c.selectedId,2);assert.equal(s.c.conversationReady,true);
});
test('realtime messages during loading wait for the matching view, and reset invalidates fetches',async()=>{
 const s=setup(),handlers={};s.c.io=()=>({on:(event,fn)=>handlers[event]=fn});s.c.maybeShowNotification=()=>{};
 vm.runInContext(extract('initSocket'),s.c);s.c.initSocket();
 const loading=s.c.selectConversation(1);
 handlers['support:message:new']({conversation:response(1).conversation,message:{id:20,body:'arrived during fetch'}});
 assert.equal(s.c.input.disabled,true);assert.equal(s.rendered.length,0);
 s.pending[0].resolve(response(1));await loading;assert.deepEqual(s.rendered.map(m=>m.id),[1,20]);
 const stale=s.c.selectConversation(2);s.c.resetThread();s.pending[1].resolve(response(2));await stale;
 assert.equal(s.c.selectedId,null);assert.equal(s.c.conversationReady,false);assert.equal(s.c.titleEl.textContent,'Select a conversation');
});
test('admin keeps a pending reply and retries a failed send with the same nonce',async()=>{
 const s=setup();await open(s,1);s.c.input.value='saved reply';
 const send=s.c.sendMessage('saved reply');assert(s.c.input.disabled);assert.equal(s.c.input.value,'saved reply');
 s.pending.at(-1).reject(Error('offline'));await send;
 assert(!s.c.input.disabled);assert.equal(s.c.input.value,'saved reply');
 const first=JSON.parse(s.requests.at(-1).options.body).client_nonce;
 const retry=s.c.sendMessage('saved reply');assert.equal(JSON.parse(s.requests.at(-1).options.body).client_nonce,first);
 s.pending.at(-1).resolve({message:{id:51}});await retry;assert.equal(s.c.input.value,'');assert.equal(s.c.outbox[1],undefined);
});
test('admin timeout retains the original recipient and ignores a late acknowledgement',async()=>{
 const s=setup();await open(s,1);s.c.input.value='for one';let timeout;
 s.c.window.setTimeout=fn=>{timeout=fn;return 1};s.c.window.clearTimeout=()=>{};
 const p=s.c.sendMessage('for one');const lost=s.pending.at(-1);await open(s,2);
 timeout();await p;assert.equal(s.c.composerDrafts[1],'for one');assert.equal(s.c.input.value,'');
 lost.resolve({message:{id:52}});await new Promise(setImmediate);
 await open(s,1);assert.equal(s.c.input.value,'for one');assert(!s.c.input.disabled);
});
