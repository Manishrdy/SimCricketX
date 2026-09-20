/* Cross-format dashboard. No persisted state; format/chart filters are local. */
(function () {
    'use strict';
    const $ = id => document.getElementById(id);
    const COLORS = ['#7161ef', '#0598a6', '#df6b36', '#d2478b', '#50862c', '#3376d5'];
    const FORMATS = window.CRICKET_FORMAT_LABELS;
    const METRICS = {
        'batting.runs': ['Batting · Runs', false], 'batting.average': ['Batting · Average', false],
        'batting.strike_rate': ['Batting · Strike rate', false], 'bowling.wickets': ['Bowling · Wickets', false],
        'bowling.average': ['Bowling · Average', true], 'bowling.economy': ['Bowling · Economy', true],
        'bowling.strike_rate': ['Bowling · Strike rate', true], 'fielding.total_dismissals': ['Fielding · Dismissals', false]
    };
    const selected = new Set();
    let payload = null, charts = [], timer, controller, generation = 0, focus = null;
    const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const number = value => value == null ? '—' : typeof value === 'number' ? value.toLocaleString(undefined, {maximumFractionDigits: 2}) : esc(value);
    const metricValue = (data, metric) => { const [section, key] = metric.split('.'); return data?.[section]?.[key] ?? null; };
    const formats = () => payload.formats.filter(fmt => document.querySelector(`input[name="cmp-format"][value="${fmt}"]`).checked);
    const color = index => COLORS[index % COLORS.length];
    const displayName = p => p.linked === false ? `${p.name} · #${p.player_ids[0]}` : p.name;
    const label = (p, index) => `<span class="cmp-player-label" style="--player-color:${color(index)}">${esc(displayName(p))}</span>`;
    const row = (cells, identity) => `<tr${identity ? ` data-player="${esc(identity)}"` : ''}>${cells.map(c => `<td>${c}</td>`).join('')}</tr>`;
    const table = (headers, rows) => `<div class="cmp-table-wrap"><table><thead><tr>${headers.map(h => `<th scope="col">${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.join('')}</tbody></table></div>`;
    const fallback = (headers, rows) => `<details><summary>View chart data</summary>${table(headers, rows)}</details>`;
    function panel(container, fmt, title, dataHTML) {
        const element = document.createElement('div'); element.className = 'cmp-panel';
        element.innerHTML = `<h3>${FORMATS[fmt]}${title ? ' · ' + esc(title) : ''}</h3><div class="cmp-canvas"><canvas role="img" aria-label="${esc(FORMATS[fmt] + ' ' + title)}"></canvas></div>${dataHTML}`;
        $(container).append(element); return element.querySelector('canvas');
    }
    function chart(canvas, type, data, options = {}) {
        if (!window.Chart) { canvas.parentElement.innerHTML = '<p>Chart library unavailable. Use the data table below.</p>'; return; }
        const hasValues = data.datasets.some(dataset => dataset.data.some(value => value != null));
        if (!hasValues) {
            canvas.parentElement.innerHTML = '<p class="cmp-empty-chart">No defined values for this view.<br>See the data table for missing statistics.</p>';
            return;
        }
        const fg = getComputedStyle(document.querySelector('.cmp-page')).color;
        const plugins = {legend: {display: false}, ...options.plugins};
        plugins.legend.labels = {...plugins.legend.labels, color: fg};
        const scales = options.scales || {x: {}, y: {beginAtZero: true}};
        Object.values(scales).forEach(axis => {
            axis.ticks = {color: fg, ...axis.ticks};
            axis.grid = {color: fg === 'rgb(0, 0, 0)' ? '#88888820' : '#88888825'};
            if (axis.title) axis.title.color = fg;
        });
        const instance = new Chart(canvas, {type, data, options: {
            responsive: true, maintainAspectRatio: false, color: fg,
            animation: matchMedia('(prefers-reduced-motion: reduce)').matches ? false : {duration: 250},
            ...options,
            plugins,
            scales
        }});
        charts.push(instance);
    }
    function overview() {
        $('cmp-overview').innerHTML = formats().map(fmt => `<div class="cmp-panel"><h3>${FORMATS[fmt]}</h3>${table(
            ['Player', 'Runs', 'Bat avg', 'Bat SR', 'Wkts', 'Bowl avg', 'Econ'],
            payload.players.map((p, i) => { const d = p.formats[fmt]; return row([label(p, i), ...['batting.runs','batting.average','batting.strike_rate','bowling.wickets','bowling.average','bowling.economy'].map(m => number(metricValue(d, m)))], p.id); }))}
            ${payload.players.map((p, i) => { const d = p.formats[fmt]; return `<p class="cmp-stat-line">${label(p, i)} · ${esc((p.teams || []).join(' / '))} · ${d.matches} matches · ${d.batting.innings || 0} batting innings / ${d.batting.balls || 0} balls · ${d.bowling.innings || 0} bowling innings / ${d.bowling.balls || 0} balls${!d.has_data ? ' · No recorded data' : ''}${d.incomplete ? ' · Incomplete archived statistics' : ''}</p>`; }).join('')}</div>`).join('');
    }
    function explorer() {
        const metric = $('cmp-metric').value, [name, lower] = METRICS[metric], fmts = formats();
        $('cmp-heatmap').innerHTML = table(['Player', ...fmts.map(f => FORMATS[f])], payload.players.map((p,i) => row([label(p,i), ...fmts.map(fmt => {
            const value = metricValue(p.formats[fmt], metric);
            const values = payload.players.map(p => metricValue(p.formats[fmt], metric)).filter(v => v != null);
            const min = Math.min(...values), max = Math.max(...values);
            const rank = max === min ? .5 : lower ? (max-value)/(max-min) : (value-min)/(max-min);
            return value == null ? '—' : `<span style="display:block;border-radius:6px;padding:.35rem;background:rgba(113,97,239,${.06 + rank*.26})">${number(value)}</span>`;
        })], p.id)));
        fmts.forEach(fmt => {
            const values = payload.players.map(p => metricValue(p.formats[fmt], metric));
            chart(panel('cmp-bars', fmt, name, fallback(['Player', name], payload.players.map((p,i) => row([label(p,i), number(values[i])], p.id)))), 'bar', {
                labels: payload.players.map(p => displayName(p)), datasets: [{label: name, data: values, backgroundColor: payload.players.map((_,i) => color(i)), borderRadius: 5}]
            }, {indexAxis:'y', scales:{x:{beginAtZero:true},y:{}}});
        });
    }
    function landscapes() {
        const discipline = $('cmp-discipline').value, batting = discipline === 'batting';
        $('cmp-bubble-note').textContent = batting ? 'Average × strike rate. Bubble area represents innings; larger values on both axes indicate higher recorded rates.' : 'Economy × bowling strike rate. Bubble area represents legal balls; lower values on both axes indicate better recorded rates.';
        formats().forEach(fmt => {
            const keys = batting ? ['average','strike_rate'] : ['economy','strike_rate'];
            const headers = batting ? ['Player','Average','Strike rate','Innings'] : ['Player','Economy','Strike rate','Balls'];
            const maxSample = Math.max(1, ...payload.players.map(p => p.formats[fmt][discipline][batting ? 'innings' : 'balls'] || 0));
            chart(panel('cmp-bubbles', fmt, batting ? 'Batting' : 'Bowling', fallback(headers, payload.players.map((p,i) => {const d=p.formats[fmt][discipline]; return row([label(p,i), number(d[keys[0]]),number(d[keys[1]]),number(d[batting?'innings':'balls'])],p.id);}))), 'bubble', {
                datasets: payload.players.map((p,i) => {const d=p.formats[fmt][discipline]; return {label:displayName(p), playerId:p.id, backgroundColor:color(i)+'aa', borderColor:color(i), data:d[keys[0]] == null || d[keys[1]] == null ? [] : [{x:d[keys[0]],y:d[keys[1]],r:Math.max(3,24*Math.sqrt(d[batting?'innings':'balls']/maxSample))}]};})
            }, {scales:{x:{title:{display:true,text:batting?'Batting average':'Economy'},beginAtZero:true}, y:{title:{display:true,text:batting?'Batting strike rate':'Bowling strike rate'},beginAtZero:true}}});
            const dataRows = [];
            payload.players.forEach((p,i) => p.formats[fmt].recent[discipline].forEach((d,j) => dataRows.push(row([label(p,i),j+1,esc(d.date?.slice(0,10) || 'Unknown'),esc(d.match_id),d.innings_number,batting?number(d.runs)+(!d.is_out?'*':''):number(d.wickets),number(d.balls)],p.id))));
            chart(panel('cmp-recent', fmt, 'Recent '+discipline, fallback(['Player','Sequence','Date','Match','Innings','Value','Balls'],dataRows)), 'line', {
                datasets:payload.players.map((p,i) => ({label:displayName(p),playerId:p.id,borderColor:color(i),backgroundColor:color(i),tension:.2,pointRadius:4,
                    pointStyle:p.formats[fmt].recent[discipline].map(d => batting&&!d.is_out?'star':'circle'),
                    data:p.formats[fmt].recent[discipline].map((d,j) => ({x:j+1,y:batting?d.runs:d.wickets,...d}))}))
            }, {scales:{x:{type:'linear',min:1,max:10,ticks:{stepSize:1},title:{display:true,text:'Individual innings sequence'}},y:{beginAtZero:true,title:{display:true,text:batting?'Runs':'Wickets'}}}, plugins:{tooltip:{callbacks:{afterLabel:ctx => {const d=ctx.raw; return `${d.date?.slice(0,10)||'Unknown date'} · Match ${d.match_id}\nInnings ${d.innings_number}${batting&&!d.is_out?' · Not out':''}`;}}}}});
        });
    }
    function stacked() {
        const percent = $('cmp-composition-mode').value === 'percent';
        formats().forEach(fmt => {
            const keys=['fours','sixes','other'], names=['From fours','From sixes','Other runs'];
            const values=payload.players.map(p => {const d=p.formats[fmt]; return keys.map(k => d.composition ? percent ? (d.batting.runs ? d.composition[k]*100/d.batting.runs : null) : d.composition[k] : null);});
            chart(panel('cmp-composition',fmt,percent?'Scoring share (%)':'Scoring runs',fallback(['Player',...names.map(n=>n+(percent?' (%)':''))],payload.players.map((p,i)=>row([label(p,i),...values[i].map(number)],p.id)))), 'bar', {
                labels:payload.players.map(p=>displayName(p)), datasets:keys.map((k,j)=>({label:names[j],data:values.map(v=>v[j]),backgroundColor:payload.players.map((_,i)=>color(i)+['ff','aa','55'][j])}))
            },{indexAxis:'y',scales:{x:{stacked:true,beginAtZero:true,...(percent?{max:100}:{})},y:{stacked:true}},plugins:{legend:{display:true}}});
            const fkeys=['catches','run_outs','stumpings'], fnames=['Catches','Run-outs','Stumpings'];
            chart(panel('cmp-fielding',fmt,'Dismissals',fallback(['Player',...fnames],payload.players.map((p,i)=>row([label(p,i),...fkeys.map(k=>number(p.formats[fmt].fielding[k]))],p.id)))), 'bar', {
                labels:payload.players.map(p=>displayName(p)),datasets:fkeys.map((k,j)=>({label:fnames[j],data:payload.players.map(p=>p.formats[fmt].fielding[k]??null),backgroundColor:payload.players.map((_,i)=>color(i)+['ff','aa','55'][j])}))
            },{indexAxis:'y',scales:{x:{stacked:true,beginAtZero:true,ticks:{precision:0}},y:{stacked:true}},plugins:{legend:{display:true}}});
        });
    }
    function insights() {
        $('cmp-insights').innerHTML=formats().map(fmt=>`<div class="cmp-panel"><h3>${FORMATS[fmt]}</h3>${payload.players.map((p,i)=>{const d=p.formats[fmt];return `<div class="cmp-insight" data-player="${esc(p.id)}">${label(p,i)}${['strengths','weaknesses'].map(kind=>d.insights[kind].map(s=>`<p><span class="cmp-tag">${kind==='strengths'?'Relative strength':'Relative gap'} · ${esc(s.label)}</span><br>${esc(s.direction)} ${esc(s.metric.replaceAll('_',' '))}: <b>${number(s.value)}</b>; eligible median ${number(s.median)} (${s.eligible_players} players). ${s.innings} innings / ${s.balls} balls.</p>`).join('')).join('')}${!d.insights.strengths.length&&!d.insights.weaknesses.length?'<p class="cmp-muted">No qualifying relative strengths or gaps.</p>':''}${!d.sample.batting?'<p class="cmp-warning">Batting sample below the insight threshold.</p>':''}${!d.sample.bowling?'<p class="cmp-warning">Bowling sample below the insight threshold.</p>':''}${d.incomplete?'<p class="cmp-warning">Incomplete archived statistics; insights suppressed.</p>':''}${d.composition?.boundary_percentage!=null?`<p>Boundary scoring share: ${number(d.composition.boundary_percentage)}% of runs.</p>`:''}</div>`;}).join('')}</div>`).join('');
    }
    function details() {
        const fields = {
            batting:['innings','balls','runs','not_outs','average','strike_rate','high_score','fours','sixes','fifties','hundreds'],
            bowling:['innings','balls','overs','wickets','runs','average','economy','strike_rate','best_figures'],
            fielding:['catches','run_outs','stumpings','total_dismissals']
        };
        $('cmp-details').innerHTML=formats().map(fmt=>`<details open><summary>${FORMATS[fmt]}</summary>${Object.entries(fields).map(([section,base])=>{const keys=[...base];if(fmt==='FC')keys.push(...(section==='batting'?['double_centuries','triple_centuries']:section==='bowling'?['best_match_figures','five_wicket_hauls','ten_wicket_matches']:[]));return `<h3>${section[0].toUpperCase()+section.slice(1)}</h3>${table(['Player',...keys.map(k=>k.replaceAll('_',' '))],payload.players.map((p,i)=>row([label(p,i),...keys.map(k=>number(p.formats[fmt][section][k]))],p.id)))}`;}).join('')}</details>`).join('');
    }
    function highlight() {
        document.querySelectorAll('[data-player]').forEach(el=>el.classList.toggle('cmp-dim',!!focus&&el.dataset.player!==focus));
        document.querySelectorAll('[data-focus]').forEach(el=>el.setAttribute('aria-pressed',String(el.dataset.focus===focus)));
        charts.forEach(c=>{c.data.datasets.forEach(ds=>{if(ds.playerId){ds.borderWidth=focus===ds.playerId?4:2;ds.backgroundColor=color(payload.players.findIndex(p=>p.id===ds.playerId))+(focus&&focus!==ds.playerId?'33':'aa');}else if(Array.isArray(ds.backgroundColor)){ds.backgroundColor=ds.backgroundColor.map((old,i)=>old.slice(0,7)+(focus&&payload.players[i].id!==focus?'25':(ds._originalAlpha||(ds._originalAlpha=old.slice(7)||'ff'))));}});c.update('none');});
    }
    function render() {
        charts.forEach(c=>c.destroy()); charts=[];
        ['cmp-bars','cmp-bubbles','cmp-recent','cmp-composition','cmp-fielding'].forEach(id=>$(id).replaceChildren());
        if(!payload)return;
        $('cmp-results').hidden=false;
        $('cmp-legend').innerHTML=payload.players.map((p,i)=>`<button type="button" data-focus="${esc(p.id)}" style="--player-color:${color(i)}" aria-pressed="false">${esc(displayName(p))}</button>`).join('');
        overview();explorer();landscapes();stacked();insights();details();highlight();
    }
    function scope() {
        const option=$('cmp-tournament').selectedOptions[0];
        document.querySelectorAll('input[name="cmp-format"]').forEach(box=>{box.disabled=!!option.value;if(option.value)box.checked=box.value===option.dataset.format;else box.checked=true;});
        $('cmp-scope').textContent=option.value?`${option.textContent} · ${FORMATS[option.dataset.format]} only · Saved matches`:'All three formats · Saved matches only';
    }
    function cancel() {clearTimeout(timer);generation++;controller?.abort();}
    function schedule() {
        cancel(); if (!selected.has(focus)) focus=null; payload=null;charts.forEach(c=>c.destroy());charts=[];$('cmp-results').hidden=true;
        $('cmp-count').textContent=`${selected.size} / 6`;$('cmp-refresh').disabled=selected.size<2;
        status(selected.size<2?'Select at least two players to begin.':'Loading comparison…');
        if(selected.size>=2)timer=setTimeout(load,300);
    }
    function status(message,error=false){$('cmp-status').textContent=message;$('cmp-status').dataset.error=String(error);}
    async function load(){
        cancel(); if(selected.size<2)return;
        const version=generation;controller=new AbortController();$('cmp-results').hidden=true;
        status('Loading comparison…');
        const params=new URLSearchParams({mode:'cross-format',identity_ids:[...selected].join(',')});
        if($('cmp-tournament').value)params.set('tournament_id',$('cmp-tournament').value);
        try{
            const response=await fetch(withListALength(`/api/compare-players?${params}`),{credentials:'same-origin',signal:controller.signal});
            const result=await response.json();if(version!==generation)return;
            if(!response.ok||!result.success)throw new Error(result.error||'Unable to load comparison');
            payload=result.data;render();status('Comparison ready. Highlight a player using the legend.');
            $('cmp-updated').textContent=`Loaded ${new Date().toLocaleTimeString()}`;
        }catch(error){if(error.name==='AbortError'||version!==generation)return;payload=null;status(`${error.message}. Use Refresh to retry.`,true);}
    }
    $('cmp-metric').innerHTML=Object.entries(METRICS).map(([key,[name]])=>`<option value="${key}">${esc(name)}</option>`).join('');
    $('cmp-picker').addEventListener('change',event=>{const box=event.target;if(box.type!=='checkbox')return;if(box.checked&&selected.size===6){box.checked=false;status('You can compare up to six players.',true);return;}box.checked?selected.add(box.value):selected.delete(box.value);schedule();});
    $('cmp-search').addEventListener('input',()=>{let count=0;const query=$('cmp-search').value.trim().toLowerCase();document.querySelectorAll('.cmp-choice').forEach(el=>{el.hidden=!el.dataset.search.includes(query);if(!el.hidden)count++;});$('cmp-search-empty').hidden=count>0;});
    $('cmp-clear').addEventListener('click',()=>{selected.clear();focus=null;document.querySelectorAll('#cmp-picker input').forEach(b=>b.checked=false);schedule();});
    $('cmp-refresh').addEventListener('click',load);
    $('cmp-tournament').addEventListener('change',()=>{scope();schedule();});
    document.querySelectorAll('input[name="cmp-format"]').forEach(box=>box.addEventListener('change',()=>{if(!document.querySelector('input[name="cmp-format"]:checked')){box.checked=true;status('Keep at least one format visible.');}if(payload)render();}));
    ['cmp-metric','cmp-discipline','cmp-composition-mode'].forEach(id=>$(id).addEventListener('change',()=>{if(payload)render();}));
    $('cmp-legend').addEventListener('click',event=>{const button=event.target.closest('[data-focus]');if(!button)return;focus=focus===button.dataset.focus?null:button.dataset.focus;highlight();});
    new MutationObserver(()=>{if(payload)render();}).observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});
    scope();
})();
