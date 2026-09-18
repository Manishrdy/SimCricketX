const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../../templates/match_setup.html'),'utf8');
function extract(name){const start=source.indexOf(`    function ${name}(`);return source.slice(start,source.indexOf('\n    }',start)+6);}
function setup(){
 const nodes={},counts=[];let validations=0;
 for(const side of ['home','away'])for(const suffix of ['selected','available','substitutes']){
  const zone={id:`${side}-${suffix}`,children:[],appendChild(p){this.insertBefore(p,null)},insertBefore(p,other){
   if(p.parentElement)p.parentElement.children.splice(p.parentElement.children.indexOf(p),1);
   this.children.splice(other?this.children.indexOf(other):this.children.length,0,p);p.parentElement=this;
  }};nodes[zone.id]=zone;
 }
 nodes['squad-announce']={};
 const c={document:{getElementById:id=>nodes[id]},validateSquads:()=>validations++,updateCounts:side=>{counts.push(side);c.updateLineupControls(side)}};
 vm.createContext(c);for(const name of ['cyclePlayerZone','movePlayerToZone','reorderLineupPlayer','updateLineupControls'])vm.runInContext(extract(name),c);
 function player(name,side='home'){
  const controls=Object.fromEntries(['move','up','down'].map(action=>[action,{setAttribute(k,v){this[k]=v}}]));
  return {dataset:{name,side},parentElement:null,focus(){this.focused=true},querySelector:s=>controls[s.match(/="(.*?)"/)[1]],controls,
   get previousElementSibling(){const a=this.parentElement.children;return a[a.indexOf(this)-1]},
   get nextElementSibling(){const a=this.parentElement.children;return a[a.indexOf(this)+1]}};
 }
 return {c,nodes,player,counts,get validations(){return validations}};
}
test('tap action adds/removes players, updates controls/counts, and preserves bowling choice',()=>{
 const s=setup(),p=s.player('Keeper');p.willBowl=false;s.nodes['home-available'].appendChild(p);s.c.updateLineupControls('home');
 assert.equal(p.controls.move.textContent,'Add to XI');assert.equal(p.controls.up.hidden,true);
 s.c.cyclePlayerZone(p,'home');assert.equal(p.parentElement.id,'home-selected');assert.equal(p.controls.move.textContent,'Move to reserve');assert.equal(p.controls.up.disabled,true);
 s.c.cyclePlayerZone(p,'home');assert.equal(p.parentElement.id,'home-available');assert.equal(p.willBowl,false);assert.equal(s.validations,2);assert.equal(p.focused,true);
});
test('reordering changes the submitted DOM order and stops at boundaries',()=>{
 const s=setup(),a=s.player('A'),b=s.player('B'),c=s.player('C'),zone=s.nodes['home-selected'];[a,b,c].forEach(p=>zone.appendChild(p));
 s.c.reorderLineupPlayer(c,'home',-1);assert.deepEqual(zone.children.map(p=>p.dataset.name),['A','C','B']);
 s.c.reorderLineupPlayer(c,'home',-1);assert.deepEqual(zone.children.map(p=>p.dataset.name),['C','A','B']);assert.equal(c.controls.up.disabled,true);
 s.c.reorderLineupPlayer(c,'home',-1);assert.equal(zone.children[0],c);
 s.c.reorderLineupPlayer(c,'home',1);assert.deepEqual(zone.children.map(p=>p.dataset.name),['A','C','B']);assert.match(s.nodes['squad-announce'].textContent,/number 2/);
});
test('controls cannot move a player across teams or reorder reserve players',()=>{
 const s=setup(),p=s.player('A');s.nodes['home-available'].appendChild(p);
 s.c.movePlayerToZone(p,'away','selected','Playing XI');assert.equal(p.parentElement.id,'home-available');
 s.c.reorderLineupPlayer(p,'home',1);assert.equal(p.parentElement.id,'home-available');
});
test('Enter/Space on nested buttons and bowling checkbox keep their native behavior',()=>{
 const handler=source.match(/div.onkeydown = e => \{([\s\S]*?)\n        \};/)[1];let moves=0;
 const div={};for(const target of [{type:'checkbox'},{type:'button'}])vm.runInNewContext(handler,{e:{key:' ',target,preventDefault(){throw Error('must not intercept')}},div,side:'home',cyclePlayerZone:()=>moves++});
 vm.runInNewContext(handler,{e:{key:'Enter',target:div,preventDefault(){}},div,side:'home',cyclePlayerZone:()=>moves++});assert.equal(moves,1);
});
