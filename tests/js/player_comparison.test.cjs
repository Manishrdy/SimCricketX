const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function setup() {
    const nodes = {}, pending = [], timers = new Map(), charts = [];
    let timerId = 0;
    function element() {return {value:'',checked:true,disabled:false,hidden:false,dataset:{},style:{},classList:{toggle(){}},
        innerHTML:'',textContent:'',children:[],addEventListener(type,fn){this[type]=fn;},
        replaceChildren(){this.children=[];},append(child){this.children.push(child);},
        setAttribute(){},querySelector(){return {parentElement:{}};}};}
    const formats=['T20','ListA','FC'].map(value=>Object.assign(element(),{value}));
    const picker=['master:1','user:2','player:3','player:4','player:5','player:6','player:7'].map(value=>Object.assign(element(),{value,type:'checkbox'}));
    const document={documentElement:{},getElementById(id){return nodes[id] ||= element();},
        createElement:element,querySelectorAll(selector){
            if(selector.includes('name="cmp-format"'))return formats;
            if(selector==='#cmp-picker input')return picker;
            return [];
        },querySelector(selector){
            if(selector.includes('name="cmp-format"'))return selector.includes('[value=')?formats.find(f=>selector.includes(`"${f.value}"`)):formats.find(f=>f.checked);
            return element();
        }};
    document.getElementById('cmp-tournament').selectedOptions=[{value:'',textContent:'All',dataset:{}}];
    document.getElementById('cmp-metric').value='batting.runs';
    document.getElementById('cmp-discipline').value='batting';
    document.getElementById('cmp-composition-mode').value='total';
    class Chart {constructor(canvas,config){Object.assign(this,config);charts.push(this);}destroy(){this.destroyed=true;}update(){}}
    vm.runInNewContext(fs.readFileSync('static/js/player_comparison.js','utf8'),{
        document,window:{Chart,CRICKET_FORMAT_LABELS:{T20:'T20',T10:'T10',ListA:'List A',FC:'First-Class'}},Chart,withListALength: url => url, URLSearchParams,AbortController,
        getComputedStyle:()=>({color:'rgb(240, 240, 240)'}),matchMedia:()=>({matches:true}),
        MutationObserver:class{observe(){}},
        setTimeout(fn){timers.set(++timerId,fn);return timerId;},clearTimeout(id){timers.delete(id);},
        fetch:(url,options)=>new Promise((resolve,reject)=>pending.push({url,options,resolve:data=>resolve({ok:true,json:async()=>data}),reject}))
    });
    return {nodes,formats,pending,charts,select(index,checked=true){const box=picker[index];box.checked=checked;nodes['cmp-picker'].change({target:box});},
        dispatch(){for(const fn of timers.values())fn();timers.clear();}};
}
function response(){
    return {success:true,data:{formats:['T20','ListA','FC'],players:['master:1','user:2'].map((id,i)=>({id,name:'Player '+i,formats:Object.fromEntries(['T20','ListA','FC'].map(fmt=>[fmt,{
        matches:1,has_data:true,incomplete:false,batting:{runs:10,average:10,strike_rate:100,innings:1,balls:10},bowling:{},fielding:{catches:1,run_outs:0,stumpings:0,total_dismissals:1},
        recent:{batting:[{runs:10,balls:10,is_out:false,date:'2026-01-01',match_id:'m',innings_number:1}],bowling:[]},composition:{fours:4,sixes:0,other:6,boundary_percentage:40},
        insights:{strengths:[],weaknesses:[]},sample:{batting:false,bowling:false}
    }]))}))}};
}
const flush=()=>new Promise(setImmediate);

test('automatic compare renders all chart families and format toggles are local',async()=>{
    const s=setup();s.select(0);s.select(1);s.dispatch();assert.equal(s.pending.length,1);
    s.pending[0].resolve(response());await flush();
    assert.equal(s.nodes['cmp-results'].hidden,false);assert.equal(s.charts.length,15);
    assert.match(s.nodes['cmp-heatmap'].innerHTML,/T20/);
    s.formats[0].checked=false;s.formats[0].change();
    assert.equal(s.pending.length,1);assert.equal(s.charts.filter(c=>!c.destroyed).length,10);
    assert.ok(s.charts.every(c=>c.options.animation===false));
    assert.match(s.nodes['cmp-recent'].children[0].innerHTML,/View chart data/);
});

test('superseded response cannot replace newest selection during debounce',async()=>{
    const s=setup();s.select(0);s.select(1);s.dispatch();
    s.select(2);assert.equal(s.pending[0].options.signal.aborted,true);
    s.pending[0].resolve(response());await flush();
    assert.equal(s.nodes['cmp-results'].hidden,true);
    assert.equal(s.charts.length,0);
});

test('clear cancels pending work and stale errors cannot overwrite status',async()=>{
    const s=setup();s.select(0);s.select(1);s.dispatch();s.nodes['cmp-clear'].click();
    s.pending[0].reject(Error('old failure'));await flush();
    assert.equal(s.nodes['cmp-status'].textContent,'Select at least two players to begin.');
    assert.equal(s.nodes['cmp-refresh'].disabled,true);
});

test('network failure exposes retry and manual refresh recovers',async()=>{
    const s=setup();s.select(0);s.select(1);s.dispatch();s.pending[0].reject(Error('offline'));await flush();
    assert.match(s.nodes['cmp-status'].textContent,/Refresh to retry/);
    s.nodes['cmp-refresh'].click();s.pending[1].resolve(response());await flush();
    assert.equal(s.nodes['cmp-results'].hidden,false);
});

test('tournament restricts format and sends its scope to API',()=>{
    const s=setup();s.select(0);s.select(1);
    s.nodes['cmp-tournament'].value='7';
    s.nodes['cmp-tournament'].selectedOptions=[{value:'7',textContent:'FC Cup',dataset:{format:'FC'}}];
    s.nodes['cmp-tournament'].change();s.dispatch();
    assert.ok(s.formats.every(f=>f.disabled));assert.deepEqual(s.formats.map(f=>f.checked),[false,false,true]);
    assert.match(s.pending[0].url,/tournament_id=7/);
});


test('six-player limit rejects a seventh and keyboard-native change removes selection',()=>{
    const s=setup();for(let i=0;i<6;i++)s.select(i);
    assert.equal(s.nodes['cmp-count'].textContent,'6 / 6');
    s.select(6);assert.match(s.nodes['cmp-status'].textContent,/up to six/);
    assert.equal(s.nodes['cmp-count'].textContent,'6 / 6');
    s.select(2,false);assert.equal(s.nodes['cmp-count'].textContent,'5 / 6');
});

test('last visible format cannot be deselected',()=>{
    const s=setup();s.formats[0].checked=false;s.formats[1].checked=false;
    s.formats[2].checked=false;s.formats[2].change();
    assert.equal(s.formats[2].checked,true);
});
