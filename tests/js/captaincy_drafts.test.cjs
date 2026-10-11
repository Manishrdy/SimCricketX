const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const CaptaincyControls = require('../../static/js/captaincy.js');
const source = fs.readFileSync('templates/team_squad.html', 'utf8');

test('populated squad offers the saved draft before writing server defaults', () => {
    const saved = {players:[{name:'Draft captain'}], captaincy_config:{composure:0,batting_aggression:75}};
    const context = {INITIAL_SQUAD:[{name:'Server captain',is_captain:true}],
        draft:{players:[]}, pendingRestore:null, loadDraftFromStorage:()=>saved,
        showRestoreBanner:value=>{context.pendingRestore=value},
        saveDraftToStorage:()=>{throw Error('Must preserve the saved draft')},
        renderSquad(){},fetchPool(){},setupDragDrop(){},setupSearchAndChips(){},
        setupPoolSlider(){},setupInfiniteScroll(){},updateValidation(){}};
    vm.createContext(context);
    const start=source.indexOf('(function boot()');
    vm.runInContext(source.slice(start, source.indexOf('})();',start)+5),context);
    assert.equal(context.pendingRestore,saved);
});

test('squad restore keeps zero ratings and updates the captaincy editor', () => {
    let rendered=0;
    const context = {window:{},CaptaincyControls,draft:{captaincy_config:CaptaincyControls.preset('Balanced')},
        pendingRestore:{players:[{name:'Captain'}],captain_name:'Captain',captaincy_config:{composure:0,batting_aggression:75}},
        captaincyEditor:{render(){rendered++}}, document:{getElementById:()=>({style:{}})},
        renderSquad(){},fetchPool(){},updateValidation(){},showFeedback(){}};
    vm.createContext(context);
    const start=source.indexOf('window.applyDraftRestore =');
    vm.runInContext(source.slice(start,source.indexOf('\n    };',start)+7),context);
    context.window.applyDraftRestore();
    assert.equal(context.draft.captaincy_config.composure,0);
    assert.equal(context.draft.captaincy_config.batting_aggression,75);
    assert.equal(rendered,1);
});
