/* Team captaincy editor. Independent format values are supplied by the host. */
(function (root) {
    'use strict';
    const descriptions = {
        match_awareness: 'Read the score, conditions, and urgency.',
        batting_management: 'Choose when to rebuild or accelerate.',
        bowling_management: 'Manage matchups, combinations, and spells.',
        adaptability: 'Recognize when a plan needs changing.',
        composure: 'Avoid panic changes under pressure.',
        batting_aggression: 'Conservative → attacking. More risk can cost wickets.',
        bowling_aggression: 'Containment → wicket hunting. Attacking can concede runs.'
    };
    const keys = Object.keys(descriptions);
    function normalize(value) {
        const config = {schema_version: 1};
        keys.forEach(key => { config[key] = Number.isInteger(value?.[key]) && value[key] >= 0 && value[key] <= 100 ? value[key] : 50; });
        return config;
    }
    function preset(name) {
        const config = normalize();
        const aggression = name === 'Attacking' ? 75 : name === 'Conservative' ? 25 : 50;
        config.batting_aggression = config.bowling_aggression = aggression;
        return config;
    }
    function nameOf(value) {
        const config = normalize(value);
        return ['Balanced', 'Attacking', 'Conservative'].find(name => keys.every(key => config[key] === preset(name)[key])) || 'Custom';
    }
    function summary(value) {
        const config = normalize(value);
        return `${nameOf(config)} · Bat aggression ${config.batting_aggression} · Bowl aggression ${config.bowling_aggression}`;
    }
    function mountStudio(element, getConfig, onChange, options) {
        function node(tag, className, text) {
            const result = document.createElement(tag);
            if (className) result.className = className;
            if (text) result.textContent = text;
            return result;
        }
        const studio = node('div', 'captaincy-studio');
        const header = node('div', 'captaincy-studio-header');
        const title = node('div');
        title.append(node('span', 'captaincy-eyebrow', 'Match approach'), node('h2', '', 'Captaincy style'));
        const format = node('span', 'captaincy-format');
        header.append(title, format);
        const intro = node('p', 'captaincy-intro', 'Start with a style. Adjust the risk to suit your squad.');
        const choices = node('div', 'captaincy-presets');
        choices.setAttribute('role', 'group');
        choices.setAttribute('aria-label', 'Captaincy style preset');
        const buttons = {};
        const styles = {
            Conservative: ['fa-shield-halved', 'Build patiently. Keep the pressure on.'],
            Balanced: ['fa-scale-balanced', 'Balance scoring and wicket-taking risk.'],
            Attacking: ['fa-bolt', 'Push the pace. Hunt for wickets.']
        };
        // Put the default first; descriptions explain intent rather than skill.
        ['Balanced', 'Attacking', 'Conservative'].forEach(name => {
            const button = node('button', 'captaincy-preset captaincy-preset-' + name.toLowerCase());
            button.type = 'button';
            const mark = node('span', 'captaincy-preset-mark');
            mark.setAttribute('aria-hidden', 'true');
            mark.append(node('i', 'fa-solid ' + styles[name][0]));
            button.append(mark,
                node('strong', '', name), node('span', 'captaincy-preset-description', styles[name][1]));
            button.setAttribute('aria-label', name + ' captaincy style');
            button.addEventListener('click', () => { onChange(preset(name)); render(); });
            choices.append(button);
            buttons[name] = button;
        });
        const status = node('p', 'captaincy-style-status');
        status.setAttribute('role', 'status');
        const risk = node('div', 'captaincy-risk');
        const advanced = node('details', 'captaincy-advanced');
        const advancedHeading = node('summary');
        advancedHeading.append(node('span', '', 'Decision-making attributes'), node('span', 'captaincy-advanced-action', 'Fine-tune'));
        advanced.append(advancedHeading, node('p', 'captaincy-advanced-note', 'These attributes affect decision quality. They are separate from how much risk the team takes.'));
        const attributes = node('div', 'captaincy-attributes');
        advanced.append(attributes);
        const inputs = {};
        function addRating(key, parent, titleText, hintText, endpoints) {
            const row = node('div', 'captaincy-studio-rating');
            const top = node('div', 'captaincy-rating-heading');
            const label = node('label', '', titleText);
            const input = node('input');
            input.type = 'range'; input.min = '0'; input.max = '100'; input.step = '1';
            input.id = `${element.id}-${key}`; label.htmlFor = input.id;
            const output = node('output'); output.htmlFor = input.id;
            top.append(label, output);
            const hint = node('p', 'captaincy-rating-hint', hintText);
            hint.id = input.id + '-hint'; input.setAttribute('aria-describedby', hint.id);
            row.append(top, hint, input);
            if (endpoints) {
                const scale = node('div', 'captaincy-scale');
                scale.setAttribute('aria-hidden', 'true');
                scale.append(node('span', '', endpoints[0]), node('span', '', endpoints[1]));
                row.append(scale);
            }
            parent.append(row);
            inputs[key] = {input, output};
            input.addEventListener('input', () => {
                const config = normalize(getConfig());
                config[key] = Number(input.value); onChange(config); render();
            });
        }
        addRating('batting_aggression', risk, 'Batting aggression', 'More attacking play can score faster, with greater wicket risk.', ['Patient', 'Attacking']);
        addRating('bowling_aggression', risk, 'Bowling aggression', 'Hunt for wickets, with a greater risk of conceding runs.', ['Containment', 'Wicket hunting']);
        keys.filter(key => !key.endsWith('_aggression')).forEach(key => {
            const label = key.replaceAll('_', ' ').replace(/^./, c => c.toUpperCase());
            addRating(key, attributes, label, descriptions[key]);
        });
        studio.append(header, intro, choices, status, risk, advanced);
        element.replaceChildren(studio);
        function render() {
            const config = normalize(getConfig());
            const current = nameOf(config);
            format.textContent = options.getFormat ? options.getFormat() : '';
            format.hidden = !format.textContent;
            Object.entries(buttons).forEach(([name, button]) => button.setAttribute('aria-pressed', String(name === current)));
            status.textContent = current === 'Custom' ? 'Custom style · Your individual adjustments are active.' : current + ' style · Adjust any attribute to make it your own.';
            keys.forEach(key => {
                inputs[key].input.value = config[key];
                inputs[key].input.style.setProperty('--rating-progress', config[key] + '%');
                inputs[key].output.textContent = config[key];
            });
        }
        render();
        return {render};
    }
    function mount(element, getConfig, onChange, options = {}) {
        if (options.layout === 'studio') return mountStudio(element, getConfig, onChange, options);
        const details = document.createElement('details');
        details.className = 'captaincy-editor';
        const heading = document.createElement('summary');
        heading.textContent = 'Captaincy';
        details.append(heading);
        const explanation = document.createElement('p');
        explanation.textContent = 'Team settings for this format. Changing the captain keeps these settings. Quality improves decisions; aggression sets the preferred risk.';
        details.append(explanation);
        const selectLabel = document.createElement('label');
        selectLabel.textContent = 'Style preset ';
        const select = document.createElement('select');
        select.setAttribute('aria-label', 'Captaincy style preset');
        ['Balanced', 'Attacking', 'Conservative', 'Custom'].forEach(name => select.add(new Option(name, name)));
        selectLabel.append(select);
        details.append(selectLabel);
        const inputs = {};
        keys.forEach(key => {
            const row = document.createElement('div');
            row.className = 'captaincy-rating';
            const label = document.createElement('label');
            const title = key.replaceAll('_', ' ').replace(/^./, c => c.toUpperCase());
            label.textContent = title;
            const input = document.createElement('input');
            input.type = 'range'; input.min = '0'; input.max = '100'; input.step = '1';
            input.id = `${element.id}-${key}`; label.htmlFor = input.id;
            const output = document.createElement('output');
            output.htmlFor = input.id;
            const hint = document.createElement('small'); hint.textContent = descriptions[key];
            row.append(label, output, input, hint); details.append(row);
            inputs[key] = {input, output};
            input.addEventListener('input', () => {
                const config = normalize(getConfig());
                config[key] = Number(input.value);
                onChange(config); render();
            });
        });
        select.addEventListener('change', () => {
            if (select.value !== 'Custom') { onChange(preset(select.value)); render(); }
        });
        element.replaceChildren(details);
        function render() {
            const config = normalize(getConfig());
            heading.textContent = 'Captaincy · ' + summary(config);
            select.value = nameOf(config);
            keys.forEach(key => {
                inputs[key].input.value = config[key];
                inputs[key].output.textContent = config[key];
            });
        }
        render();
        return {render};
    }
    root.CaptaincyControls = {normalize, preset, nameOf, summary, mount};
    if (typeof module !== 'undefined' && module.exports) module.exports = root.CaptaincyControls;
})(typeof window !== 'undefined' ? window : globalThis);
