const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/js/prematch_broadcast.js', 'utf8');
function setup(rendererFails = false) {
    const elements = new Map();
    const element = id => {
        if (!elements.has(id)) elements.set(id, {style:{}, focus(){}, textContent:''});
        return elements.get(id);
    };
    let started = 0;
    const uploads = [];
    const context = {document:{getElementById:element}, matchData:{team_home:'HOME_owner'},
        requestAnimationFrame:callback => callback(),
        html2canvas: async () => {if(rendererFails) throw Error('Failed'); return {toBlob: cb => cb({image:true})};},
        withScorecardTimeout:promise => promise,
        uploadNamedScorecardImage:async (blob,label) => uploads.push(label),
        appendLog(){}, startMatch:() => started++};
    vm.createContext(context); vm.runInContext(source, context);
    return {context, element, uploads, started:() => started};
}
for (const decision of ['Bat', 'Bowl']) test(`toss ${decision} holds play until close and saves presentation`, async () => {
    const s = setup();
    s.context.showPrematchPresentation({toss_winner:'HOME',toss_decision:decision});
    assert.equal(s.started(),0);
    assert.equal(s.element('pm-order-home').textContent,decision === 'Bat' ? 'BATTING FIRST' : 'BOWLING FIRST');
    await Promise.all([s.element('prematch-close').onclick(),s.element('prematch-close').onclick()]);
    assert.equal(s.started(),1);
    assert.deepEqual(s.uploads,['prematch_presentation']);
    assert.equal(s.element('prematch-overlay').style.display,'none');
});
test('renderer failure is visible and does not trap the match', async () => {
    const s = setup(true);
    s.context.showPrematchPresentation({toss_winner:'AWAY',toss_decision:'Bat'});
    await s.element('prematch-close').onclick();
    assert.equal(s.started(),1);
    assert.match(s.element('pm-save-status').textContent,/could not be saved/);
});
