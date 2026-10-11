const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('templates/match_setup.html', 'utf8');
const pitches = source.slice(source.indexOf('    function populatePitchOptions('), source.indexOf('    const STADIUM_FIXTURE_ID'));
const overrides = source.slice(source.indexOf('    function stadiumOverrides()'), source.indexOf('    function initializeStadium('));
const preview = source.slice(source.indexOf('    function updateStadiumPreview()'), source.indexOf('    // --- STEP 2: CONDITIONS ---'));
function setup() {
    const pitch = {value:'Green', options:[], appendChild(option) {this.options.push(option);}, set innerHTML(v) {this.options=[];}};
    const nodes = {'pitch-select':pitch, 'stadium-select':{value:'a'}, 'match-format-value':{value:'T20'}, 'stadium-preview':{textContent:''}};
    const stadium = (id, t20, fc) => ({id, pitch_defaults:{T20:t20,FC:fc}, dimensions_m:{straight_a:65,straight_b:70,square_a:60,square_b:62}, outfield_speed:1.04, average_temperature_c:30});
    const context = {document:{getElementById(id){return nodes[id];},createElement(){return {};}},
        TEAMS:[{pitch_preference:'Hard'}],selectedTeamIndices:[0],
        STADIUM_DATA:[stadium('a','Flat','Dry'),stadium('b','Green','Hard')],
        PITCH_CONDITIONS:{T20:['Green','Hard','Flat','Dry'].map(key=>({key})), FC:['Green','Hard','Flat','Dry'].map(key=>({key}))}};
    vm.createContext(context);
    vm.runInContext(pitches+overrides+preview,context);
    return {context,nodes,pitch};
}
test('format and stadium changes replace a previous pitch override with the correct default', () => {
    const {context,nodes,pitch}=setup();
    context.populatePitchOptions('T20'); assert.equal(pitch.value,'Flat');
    pitch.value='Green'; nodes['match-format-value'].value='FC';
    context.populatePitchOptions('FC'); assert.equal(pitch.value,'Dry');
    nodes['stadium-select'].value='b'; context.populatePitchOptions('FC');
    assert.equal(pitch.value,'Hard');
    assert.equal(pitch.options.some(option=>option.value===''),false);
});
test('the payload contains only the effective pitch override and the preview uses fixed values', () => {
    const {context,nodes,pitch}=setup();
    context.populatePitchOptions('T20');
    assert.deepEqual(JSON.parse(JSON.stringify(context.stadiumOverrides())),{pitch:'Flat'});
    assert.match(nodes['stadium-preview'].textContent,/65\/70 m straight, 60\/62 m square/);
    assert.match(nodes['stadium-preview'].textContent,/30°C/);
    nodes['stadium-select'].value=''; assert.deepEqual(JSON.parse(JSON.stringify(context.stadiumOverrides())),{});
});
test('legacy setup keeps its chosen pitch across format option refresh',()=>{
    const {context,nodes,pitch}=setup(); nodes['stadium-select'].value='';
    pitch.value='Green';context.populatePitchOptions('FC');assert.equal(pitch.value,'Green');
});
