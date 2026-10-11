const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname,'../../static/js/stadium_catalogue.js'),'utf8');
function setup(fail = false) {
  const calls = [], handlers = {}, button = {disabled:false}, status = {classList:{remove(){}},textContent:''};
  const globalStatus = {classList:{add(){},remove(){}},textContent:''};
  const form = {matches:s=>s==='.ground-add-form',dataset:{adoptUrl:'/api/stadiums/official-id/adopt'},
    querySelector:s=>s==='button'?button:status};
  const collection = {innerHTML:'old',setAttribute(){},removeAttribute(){},hasAttribute(){return false},
    querySelector:s=>s==='.grounds-update-status'?globalStatus:{textContent:'2 stadiums'},querySelectorAll:()=>[],
    addEventListener:(name,fn)=>handlers[name]=fn};
  const location = {href:'https://example.test/stadiums?kind=official',pathname:'/stadiums',origin:'https://example.test'};
  const context = {document:{querySelector:()=>collection,activeElement:null,getElementById(){return null}},
    location,history:{state:{},replaceState(){},pushState(){}},window:{scrollY:350,scrollTo(){},addEventListener(){}},
    URL,URLSearchParams,AbortController,
    FormData:class {get(key){return key==='revision'?'4':'qa-csrf'}},
    DOMParser:class {parseFromString(){return {querySelector:()=>({innerHTML:'new catalogue'})}}},
    fetch:async(url,options)=>{calls.push({url,options});return options.method==='POST'
      ? {ok:!fail,json:async()=>fail?{error:'This stadium changed. Reload before adopting.'}:{id:'private-id'}}
      : {ok:true,text:async()=>'<section></section>'}}};
  vm.createContext(context);vm.runInContext(source,context);
  return {calls,button,status,location,collection,submit:()=>handlers.submit({target:form,preventDefault(){}})};
}
test('card action adds directly and refreshes collection without visiting detail',async()=>{
  const s=setup();await s.submit();
  assert.equal(s.calls[0].url,'/api/stadiums/official-id/adopt');
  assert.equal(s.calls[0].options.method,'POST');
  assert.deepEqual(JSON.parse(s.calls[0].options.body),{revision:4});
  assert.equal(s.calls[0].options.headers['X-CSRFToken'],'qa-csrf');
  assert.equal(s.calls[1].url,s.location.href);
  assert.equal(s.location.href,'https://example.test/stadiums?kind=official');
  assert.equal(s.button.textContent,'Added to My stadiums');
  assert.equal(s.collection.innerHTML,'new catalogue');
  await s.submit();assert.equal(s.calls.length,2);
});
test('failed card adoption shows error and allows retry without navigation',async()=>{
  const s=setup(true);await s.submit();
  assert.equal(s.calls.length,1);assert.equal(s.button.disabled,false);
  assert.match(s.status.textContent,/changed/);assert.equal(s.collection.innerHTML,'old');
  await s.submit();assert.equal(s.calls.length,2);
});
