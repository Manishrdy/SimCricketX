/* SimCricketX onboarding guide — tour content.
 *
 * Pure data, read by scx_guide.js. Each tour:
 *   id        unique, [a-z0-9_] (stored server-side in users.guide_state)
 *   page      guide page id from utils/guide_state.GUIDE_PAGES, or '*' for any
 *   main      the tour the "?" button replays on this page
 *   auto(ctx) when the tour may auto-start (default: always, once)
 *   priority  higher wins when several could auto-start (one per page load)
 *   waitFor   selector — start only once this becomes visible (wizard steps)
 *   kicker    small label above the title (string or ctx => string)
 *   onEnd(ctx, status, api)
 *   steps: [{ target, title, body, placement, when(ctx), cta, ctaDismiss,
 *             fallback: 'center' }]
 *
 * target = CSS selector (or list, first visible wins). No target = centred
 * card. A step whose target is missing is dropped, so keep every tour
 * readable with any subset of its steps.
 *
 * `body` is HTML we author. Anything user-provided (ctx.name) must go through
 * ctx.esc(). tests/test_guide.py checks the #id selectors below still exist in
 * their templates — update it when you rename one.
 */
(function () {
    'use strict';

    const journeyKicker = ctx => {
        if (!ctx.inJourney) return '';
        const n = { team1: 1, team2: 2, match: 3 }[ctx.journey.stage] || 1;
        return `Getting started · Step ${n} of 3`;
    };
    const squadRule = ctx =>
        `${ctx.rules.min}–${ctx.rules.max} players, with at least ${ctx.rules.wk} wicketkeeper and ${ctx.rules.bowl} bowlers or all-rounders`;

    const T = {};
    const add = tour => { T[tour.id] = tour; };

    // ── New-user journey ──────────────────────────────────────────────────
    add({
        id: 'journey_home',
        page: 'home',
        priority: 10,
        auto: ctx => ctx.inJourney,
        kicker: 'Getting started',
        replay: false,
        steps: [
            {
                title: ctx => `Welcome to SimCricketX, ${ctx.esc(ctx.name)}!`,
                body: `<p>You build cricket teams, then simulate matches between them ball by ball.</p>
                       <p>Your first match is three steps away:</p>
                       <ul><li><strong>Create a team</strong> from the player pool</li>
                       <li><strong>Create a second team</strong> to play against</li>
                       <li><strong>Simulate a match</strong> and watch it unfold</li></ul>`,
            },
            {
                target: '[data-guide="team-create"]',
                title: 'Step 1: Create a team',
                body: ctx => `<p>Name your team, then pick a squad of ${squadRule(ctx)}.</p>
                              <span class="scxg-tip">You'll do this twice, once for each side.</span>`,
                placement: 'bottom',
            },
            {
                target: '[data-guide="match-setup"]',
                title: 'Step 3: Simulate a match',
                body: `<p>Once you have two teams, start here. Choose the teams, the format, the pitch and the weather, and the match plays out ball by ball.</p>`,
                placement: 'right',
            },
            {
                target: '#scxg-journey-mount .scxg-journey',
                title: 'Your checklist',
                body: `<p>Each step ticks itself off as you finish it. The checklist follows you to every page until your first match is done.</p>`,
                placement: 'top',
            },
            {
                target: '[data-guide-replay]',
                title: 'Stuck? Press ?',
                body: `<p>Every page has its own short guide. Press <strong>?</strong> in the top bar to replay it at any time.</p>`,
                placement: 'bottom',
                cta: ctx => ctx.stage('match')
                    ? { label: 'Set up the match', href: ctx.urls.matchSetup }
                    : { label: ctx.stage('team2') ? 'Create team #2' : 'Create my first team', href: ctx.urls.createTeam },
                ctaDismiss: "I'll look around first",
            },
        ],
    });

    // Shown on whichever page the user lands on after a stage flips (normally
    // /teams/manage, where saving a team redirects).
    add({
        id: 'journey_team2',
        page: '*',
        priority: 15,
        auto: ctx => ctx.stage('team2') && ['team_create', 'match_detail'].indexOf(ctx.page) === -1,
        kicker: 'Getting started · Step 2 of 3',
        replay: false,
        steps: [
            {
                target: ['.teams-grid .glass-card', '[data-guide="teams-manage"]'],
                fallback: 'center',
                title: 'Team one is ready',
                body: `<p>Your first team is saved and ready to play. A match needs two sides, so next you'll build an opponent the same way.</p>`,
            },
            {
                target: ['.nav-actions .create-btn', '[data-guide="team-create"]'],
                fallback: 'center',
                title: 'Build your second team',
                body: `<p>Give it a different name and short code. You can use players already in your first team.</p>`,
                cta: ctx => ({ label: 'Create team #2', href: ctx.urls.createTeam }),
                ctaDismiss: 'Later',
            },
        ],
    });

    add({
        id: 'journey_match',
        page: '*',
        priority: 15,
        auto: ctx => ctx.stage('match') && ['match_setup', 'match_detail'].indexOf(ctx.page) === -1,
        kicker: 'Getting started · Step 3 of 3',
        replay: false,
        steps: [
            {
                target: ['[data-guide="match-setup"]'],
                fallback: 'center',
                title: 'Two teams. Time to play.',
                body: `<p>Both sides are ready. Set up your first match: pick the two teams, choose the conditions, confirm the XIs and press <strong>Simulate</strong>.</p>`,
                cta: ctx => ({ label: 'Set up the match', href: ctx.urls.matchSetup }),
                ctaDismiss: 'Later',
            },
        ],
    });

    add({
        id: 'journey_complete',
        page: '*',
        priority: 20,
        auto: ctx => ctx.stage('complete'),
        kicker: 'Getting started · Complete',
        replay: false,
        onEnd: (ctx, status, api) => api.setJourney('done'),
        steps: [
            {
                title: 'Your first match is in the books!',
                body: `<p>You've built two teams and simulated a full match. Here's what to try next:</p>
                       <ul><li><strong>Tournaments</strong>: run a league, a knockout or a multi-format tour</li>
                       <li><strong>Statistics</strong>: career records build up with every match</li>
                       <li><strong>My Matches</strong>: every scorecard, saved for you</li></ul>`,
                cta: ctx => ctx.page === 'home'
                    ? { label: 'Tour the dashboard', tour: 'home' }
                    : { label: 'Tour the dashboard', href: `${ctx.urls.home}?guide=home` },
                ctaDismiss: "I'm good",
            },
        ],
    });

    // ── Home ──────────────────────────────────────────────────────────────
    add({
        id: 'home',
        page: 'home',
        main: true,
        auto: ctx => !ctx.inJourney && !ctx.stage('complete'),
        kicker: 'Dashboard tour',
        steps: [
            {
                title: ctx => `Welcome back, ${ctx.esc(ctx.name)}`,
                body: `<p>Here's a quick look at everything on your dashboard: what each area does and where to start.</p>
                       <p>It takes about a minute.<span class="scxg-kbd-hint"> Use <kbd>←</kbd> <kbd>→</kbd> to move, <kbd>Esc</kbd> to close.</span></p>`,
            },
            {
                target: '[data-guide="match-setup"]',
                title: 'Simulate a match',
                body: `<p>Pick two of your teams, choose a format (<strong>T20, T10, List A or First-Class</strong>), set the pitch and weather, then watch it play out ball by ball.</p>`,
                placement: 'right',
            },
            {
                target: '[data-guide="team-create"]',
                title: 'Create a team',
                body: ctx => `<p>Give your side a name, home ground and colours, then pick a squad from the player pool: ${squadRule(ctx)}.</p>`,
            },
            {
                target: '[data-guide="teams-manage"]',
                title: 'Manage teams',
                body: `<p>All your teams in one place. Edit squads, finish drafts, change captains, or delete teams you no longer need.</p>`,
            },
            {
                target: '[data-guide="tournaments"]',
                title: 'Tournaments & tours',
                body: `<p>Run a <strong>league</strong>, a <strong>knockout</strong> or a series, or a multi-format <strong>tour</strong> between two teams. Fixtures, the points table and leaders update after every match.</p>`,
            },
            {
                target: '[data-guide="player-pool"]',
                title: 'Player pool',
                body: `<p>Every player you can pick for your squads. Adjust a player's ratings with your own version, or add custom players of your own.</p>`,
            },
            {
                target: '[data-guide="ground-conditions"]',
                title: 'Ground conditions',
                body: `<p>Fine-tune how each pitch behaves and pick a game mode for each format.</p>
                       <span class="scxg-tip">Optional. The defaults are already tuned, so you can skip this until you want to experiment.</span>`,
            },
            {
                target: '[data-guide="my-matches"]',
                title: 'My matches',
                body: `<p>Every match you've simulated, with full scorecards. Search, filter, and download match archives.</p>`,
            },
            {
                target: '[data-guide="statistics"]',
                title: 'Statistics',
                body: `<p>Batting, bowling and fielding records across all your matches or a single tournament. Compare players and check head-to-head records.</p>`,
            },
            {
                target: '[data-guide="community"]',
                title: 'Community',
                body: `<p>Ask a question, report a bug or suggest an idea. Upvote posts you agree with; the most-voted ones get worked on first.</p>`,
            },
            {
                target: ['#profileBtn'],
                title: 'Your account',
                body: `<p>Matches still in progress show up here so you can jump back in. You'll also find your analytics and account settings.</p>`,
                placement: 'bottom',
            },
            {
                target: '[data-guide-replay]',
                title: 'Help on every page',
                body: `<p>Press <strong>?</strong> on any page to see its guide again.</p>`,
                placement: 'bottom',
            },
        ],
    });

    // ── Create team ───────────────────────────────────────────────────────
    add({
        id: 'team_create',
        page: 'team_create',
        main: true,
        kicker: ctx => journeyKicker(ctx) || 'Create a team',
        steps: [
            {
                target: '.tc-identity',
                title: ctx => ctx.stage('team2') ? 'Your opponent: team identity' : 'Team identity',
                body: `<p>Give the team a <strong>name</strong>, a short <strong>code</strong> (like MUM), a <strong>home ground</strong>, the pitch that ground favours, and a colour.</p>`,
            },
            {
                target: '.tc-tabs',
                title: 'One squad per format',
                body: `<p>A team keeps a separate squad for each format. <strong>You only need one to play</strong>, so T20 is a good place to start.</p>
                       <p>Once one squad is built, the others can copy it.</p>`,
            },
            {
                target: '#tc-pool-list',
                title: 'Pick players from the pool',
                body: `<p>Search by name or filter by role, then press <strong>Add</strong> (or drag the grip) to move a player into your squad.</p>`,
                placement: 'right',
            },
            {
                target: '#tc-roster-list',
                title: 'Your squad',
                body: ctx => `<p>A squad needs ${squadRule(ctx)}.</p><p>Use the arrows to reorder players and <strong>Remove</strong> to drop one.</p>`,
                placement: 'left',
            },
            {
                target: '.tc-leaders',
                title: 'Captain & wicketkeeper',
                body: `<p>Choose both from your squad. The wicketkeeper must be a player with the Wicketkeeper role.</p>`,
            },
            {
                target: '#tc-validation',
                title: "What's still missing",
                body: `<p>This list tells you exactly what's left to fix. When it's clear, <strong>Save</strong> unlocks.</p>`,
                placement: 'top',
            },
            {
                target: '#tc-save',
                title: 'Save the team',
                body: ctx => ctx.inJourney
                    ? `<p>Save when you're ready. Your work is kept in this browser as a draft, so nothing is lost if you leave.</p>
                       <span class="scxg-tip">After saving you'll land on your teams list, and we'll pick up from there.</span>`
                    : `<p>Your work is kept in this browser as a draft while you build, so nothing is lost if you leave. <strong>Clear Draft</strong> starts over.</p>`,
                placement: 'top',
            },
        ],
    });

    // ── Manage teams ──────────────────────────────────────────────────────
    add({
        id: 'manage_teams',
        page: 'manage_teams',
        main: true,
        kicker: 'Manage teams',
        steps: [
            {
                target: '.stats-grid',
                title: 'Your teams at a glance',
                body: `<p>How many teams you have, their average squad size and the total number of players across them.</p>`,
            },
            {
                target: '#team-search',
                title: 'Find a team fast',
                body: `<p>Search by team name, short code, captain or ground.</p>`,
            },
            {
                target: '.teams-grid .empty-state',
                title: 'No teams yet',
                body: `<p>Your teams will appear here as cards once you create them.</p>`,
            },
            {
                target: '.teams-grid .glass-card',
                title: 'Each card is a team',
                body: `<p>The coloured badges show which formats have a squad and how many players are in each. A team can only play formats it has a squad for.</p>`,
            },
            {
                target: '.teams-grid .card-actions',
                title: 'Edit or delete',
                body: `<p><strong>Edit</strong> changes the team's details and squads. <strong>Delete</strong> removes it for good. Teams used in a tournament are protected.</p>`,
                placement: 'left',
            },
            {
                target: ['.nav-actions .create-btn', '.empty-state .create-btn'],
                title: 'Add another team',
                body: `<p>Build a new team at any time from here.</p>`,
                placement: 'bottom',
            },
        ],
    });

    // ── Squad builder (per-format squad of an existing team) ──────────────
    add({
        id: 'team_squad',
        page: 'team_squad',
        main: true,
        kicker: 'Squad builder',
        steps: [
            {
                target: '.sb-composition',
                title: 'Squad balance',
                body: ctx => `<p>A live count of your squad by role. A playable squad has ${squadRule(ctx)}.</p>`,
            },
            {
                target: '#poolPanel',
                title: 'Player pool',
                body: `<p>Search, filter by role, and add players to the squad.</p>`,
                placement: 'right',
            },
            {
                target: '#squadPanel',
                title: 'Your squad',
                body: `<p>Players you've picked for this format. Reorder or remove them here.</p>`,
                placement: 'left',
            },
            {
                target: '.sb-hero-actions',
                title: 'Draft or publish',
                body: `<p><strong>Save Draft</strong> keeps your progress without making the team playable. <strong>Publish Team</strong> checks the squad rules and makes it available for matches.</p>`,
            },
        ],
    });

    // ── Match setup (3-step wizard) ───────────────────────────────────────
    add({
        id: 'match_setup',
        page: 'match_setup',
        main: true,
        kicker: ctx => journeyKicker(ctx) || 'Match setup',
        steps: [
            {
                target: '.ms-stepper',
                title: 'Three quick steps',
                body: `<p><strong>Teams</strong> → <strong>Match</strong> conditions → <strong>Squads</strong>. Then you press Simulate.</p>`,
            },
            {
                target: ['.teams-grid', '.ms-empty-state'],
                title: 'Pick two teams',
                body: `<p>Click two teams. The <strong>first one you pick is the home side</strong> and hosts the match at its ground.</p>
                       <p>The badges show which formats each team has a squad for.</p>`,
            },
            {
                target: '.ms-ticket',
                title: 'Your match ticket',
                body: `<p>A live summary of the match you're building. It fills in as you make your choices.</p>`,
                placement: 'left',
            },
            {
                target: '#next-btn',
                title: 'Next',
                body: `<p>Moves to the next step once two teams are picked. On the last step it becomes <strong>Simulate</strong>.</p>`,
                placement: 'top',
            },
        ],
    });

    add({
        id: 'match_setup_conditions',
        page: 'match_setup',
        waitFor: '#step-2.active',
        kicker: ctx => journeyKicker(ctx) || 'Match conditions',
        steps: [
            {
                target: '#pitch-select',
                title: 'Pitch',
                body: `<p>The surface shifts the balance between bat and ball. <strong>Flat</strong> and <strong>Dead</strong> pitches suit batters; <strong>Green</strong> and <strong>Dry</strong> help the bowlers.</p>`,
            },
            {
                target: ['.format-selector', '.format-locked-display'],
                title: 'Format',
                body: `<p><strong>T20</strong>, <strong>T10</strong>, <strong>List A</strong> (40 or 50 overs) or <strong>First-Class</strong> (a 4 or 5-day match). Both teams need a squad in that format.</p>`,
            },
            {
                target: '#simulation-mode-row',
                title: 'Auto or manual',
                body: `<p><strong>Auto</strong> plays the whole match by itself. <strong>Manual</strong> pauses so you can choose the next batter and bowler.</p>
                       <span class="scxg-tip">You can switch between them during the match too.</span>`,
            },
            {
                target: '#weather-forecast-select',
                title: 'Weather & extras',
                body: `<p>Rain can shorten a match and bring in a revised target. A day/night match adds dew later in the game. <strong>Make Match Interesting</strong> sets up a dramatic finish.</p>`,
            },
        ],
    });

    add({
        id: 'match_setup_squads',
        page: 'match_setup',
        waitFor: '#step-3.active',
        kicker: ctx => journeyKicker(ctx) || 'Playing XIs',
        steps: [
            {
                target: '#home-pane',
                title: 'Pick each playing XI',
                body: `<p>Choose <strong>11 players</strong> for each side, including a wicketkeeper and at least <strong>5 bowling options</strong>. The order you set is the batting order.</p>
                       <p>Use <strong>Add to XI</strong> and the Up/Down buttons, or drag players between zones.</p>`,
            },
            {
                target: '#away-pane',
                title: 'Same for the away side',
                body: `<p>The counters turn complete when each XI is valid.</p>`,
            },
            {
                target: '#next-btn',
                title: 'Simulate!',
                body: `<p>When both XIs are ready, press <strong>Simulate</strong> to go to the live match.</p>`,
                placement: 'top',
            },
        ],
    });

    // ── Live match ────────────────────────────────────────────────────────
    add({
        id: 'match_detail',
        page: 'match_detail',
        main: true,
        kicker: ctx => journeyKicker(ctx) || 'Live match',
        steps: [
            {
                target: '.toss-section',
                title: 'It starts with the toss',
                body: `<p>Press <strong>Spin Coin</strong>. The toss result decides who bats first, and then the match begins.</p>`,
                placement: 'right',
            },
            {
                target: '.team-list-container',
                title: 'Playing XIs',
                body: `<p>Both line-ups in batting order. <strong>C</strong> marks the captain.</p>`,
                placement: 'right',
            },
            {
                target: '.score-banner',
                title: 'Live scoreboard',
                body: `<p>Score, overs, run rates, the batters at the crease, the current bowler, the partnership and every ball of this over.</p>`,
            },
            {
                target: ['.ctrl-pill-group:has(#pill-auto)', '#pill-auto'],
                title: 'Auto or manual',
                body: `<p><strong>Auto</strong> keeps bowling by itself. <strong>Manual</strong> pauses when a decision is needed, like the next batter or bowler, so you can make the call.</p>`,
                placement: 'bottom',
            },
            {
                target: ['.ctrl-pill-group:has(#pill-commentary)', '#pill-commentary'],
                title: 'Commentary or Match Center',
                body: `<p><strong>Commentary</strong> gives you ball-by-ball text. <strong>Match Center</strong> shows the wagon wheel, win probability and run-rate charts.</p>`,
                placement: 'bottom',
            },
            {
                target: '#save-dashboard-btn',
                title: 'Save the Match Center',
                body: `<p>Download an image of the Match Center to share.</p>`,
                placement: 'bottom',
            },
            {
                title: 'Enjoy the match!',
                body: `<p>A full scorecard appears at each innings break and at the end. Every completed match is saved to <strong>My Matches</strong> and counts toward your statistics.</p>
                              <span class="scxg-tip">If you leave mid-match, pick it up again from your profile menu (top right).</span>`,
            },
        ],
    });

    // ── Tournaments ───────────────────────────────────────────────────────
    add({
        id: 'tournaments',
        page: 'tournaments',
        main: true,
        kicker: 'Tours & tournaments',
        steps: [
            {
                target: '.ch-choice-tournament',
                title: 'Tournament',
                body: `<p>One format, as many teams as you like. Choose a <strong>league</strong>, a <strong>knockout</strong> or a <strong>series</strong>, and get fixtures, a points table and leaderboards.</p>`,
            },
            {
                target: '.ch-choice-tour',
                title: 'Tour',
                body: `<p>Two teams playing a run of series across different formats, like a Test series followed by one-dayers and T20s, with overall tour stats.</p>`,
            },
            {
                target: '.ch-library',
                title: 'Your competitions',
                body: `<p>Everything you've started lives here. Open one to play the next fixture.</p>`,
                placement: 'top',
            },
        ],
    });

    add({
        id: 'tournament_create',
        page: 'tournament_create',
        main: true,
        kicker: 'Create a tournament',
        steps: [
            {
                target: '#tournamentForm .form-section',
                title: 'Name & format',
                body: `<p>Name your tournament and choose its format. Every match in it is played in that format.</p>`,
            },
            {
                target: '#teamsGrid',
                title: 'Choose the teams',
                body: `<p>Only teams with a squad in the chosen format appear here.</p>`,
            },
            {
                target: '.no-teams-state',
                title: 'You need teams first',
                body: `<p>A tournament needs at least two teams with a squad in the chosen format. Create or finish your teams, then come back.</p>`,
            },
            {
                target: '#modeGrid',
                title: 'How it plays out',
                body: `<p>League, knockout, or both. Each option shows how many matches it takes. Playoff formats unlock with 4+ teams, and a custom series with exactly 2.</p>`,
            },
            {
                target: '#submitBtn',
                title: 'Create',
                body: `<p>Creates the fixtures. You'll go straight to the tournament dashboard to play the first match.</p>`,
                placement: 'top',
            },
        ],
    });

    add({
        id: 'tournament_dashboard',
        page: 'tournament_dashboard',
        main: true,
        kicker: 'Tournament',
        steps: [
            {
                target: '.fixtures-section',
                title: 'Fixtures & results',
                body: `<p>Press <strong>play</strong> on a fixture to set up that match. The next fixture due is highlighted. Results update the table automatically.</p>`,
            },
            {
                target: '.standings-section',
                title: 'Points table',
                body: `<p>Standings update after every result. Top teams qualify for the playoffs where the format has them.</p>`,
            },
            {
                target: '.leaders-section',
                title: 'Tournament leaders',
                body: `<p>Top run-scorers, wicket-takers and player-of-the-match awards for this tournament.</p>`,
                placement: 'top',
            },
        ],
    });

    add({
        id: 'tour_create',
        page: 'tour_create',
        main: true,
        kicker: 'Build a tour',
        steps: [
            {
                target: ['section:has(#teams-title)', '#teams-title'],
                title: '1. Name it and pick two teams',
                body: `<p>A tour is always between two teams.</p>`,
            },
            {
                target: ['section:has(#formats-title)', '#formats-title'],
                title: '2. Choose the matches',
                body: `<p>Set how many matches to play in each format. Only formats both teams have a squad for are available. Leave a format at 0 to skip it.</p>`,
            },
            {
                target: ['section:has(#order-title)', '#order-title'],
                title: '3. Set the playing order',
                body: `<p>Decide which series comes first, like Tests before the white-ball games.</p>`,
            },
            {
                target: ['section:has(#review-title)', '#review-title'],
                title: '4. Review & confirm',
                body: `<p>Check the schedule, tick the confirmation box, and create the tour.</p>`,
                placement: 'top',
            },
        ],
    });

    // ── Player pool ───────────────────────────────────────────────────────
    add({
        id: 'player_pool',
        page: 'player_pool',
        main: true,
        kicker: 'Player pool',
        steps: [
            {
                target: '.pp-hero',
                title: 'Every player you can pick',
                body: `<p>The pool mixes the global player list with your own versions of players and any custom players you've added. Only you see your changes.</p>`,
            },
            {
                target: '.pp-toolbar',
                title: 'Search & filter',
                body: `<p>Find a player by name or narrow the list down by role.</p>`,
            },
            {
                target: '.pp-table-wrap',
                title: 'Ratings & actions',
                body: `<p>Editing a global player creates <strong>your own version</strong> with your ratings. Revert it at any time to go back to the original.</p>`,
                placement: 'top',
            },
            {
                target: '.pp-actions',
                title: 'Add or import',
                body: `<p>Create a custom player, or import many at once from a file.</p>`,
            },
        ],
    });

    // ── Ground conditions ─────────────────────────────────────────────────
    add({
        id: 'ground_conditions',
        page: 'ground_conditions',
        main: true,
        kicker: 'Ground conditions',
        steps: [
            {
                title: 'For tinkerers',
                body: `<p>This page controls how the simulation engine treats pitches. <strong>You don't need to change anything to play</strong>: the defaults are already tuned for realistic scores.</p>`,
            },
            {
                target: '.gc-format-tabs',
                title: 'One setup per format',
                body: `<p>Each format has its own settings, so changing T20 won't affect List A or First-Class.</p>`,
            },
            {
                target: '#gcModesRow',
                title: 'Game mode',
                body: `<p>Sets the overall character of your matches: aggressive or defensive batting, or conditions that favour batters (Flat Track Bully) or bowlers (Bowler's Day).</p>`,
            },
            {
                target: '#gcPitchGrid',
                title: 'Pitch profiles',
                body: `<p>Open a pitch to adjust how it treats pace, spin and batting.</p>`,
            },
            {
                target: '#btnSaveAll',
                title: 'Save or reset',
                body: `<p>Save your changes, or go back to factory defaults at any time.</p>`,
                placement: 'top',
            },
        ],
    });

    // ── My matches ────────────────────────────────────────────────────────
    add({
        id: 'my_matches',
        page: 'my_matches',
        main: true,
        kicker: 'My matches',
        steps: [
            {
                target: '.tab-bar',
                title: 'History & archives',
                body: `<p><strong>History</strong> lists every match you've completed. <strong>Archives</strong> holds downloadable match files (commentary, scorecards and logs).</p>`,
            },
            {
                target: ['#search-box', '#filter-bar'],
                title: 'Search & filter',
                body: `<p>Find matches by team, and filter by format, tournament or date.</p>`,
            },
            {
                target: '#panel-history .empty-state',
                title: 'No matches yet',
                body: `<p>Every match you finish lands here automatically with its full scorecard.</p>`,
            },
            {
                target: ['#match-grid .match-card', '#match-grid'],
                title: 'Open a scorecard',
                body: `<p>Click a match to see its full scorecard.</p>`,
            },
            {
                target: '#manage-mode-btn',
                title: 'Tidy up',
                body: `<p>Select several matches at once to delete them. Deleting a match also removes it from your statistics.</p>`,
                placement: 'bottom',
            },
        ],
    });

    // ── Statistics ────────────────────────────────────────────────────────
    add({
        id: 'statistics',
        page: 'statistics',
        main: true,
        kicker: 'Statistics',
        steps: [
            {
                target: '.stats-bar',
                title: 'Choose what to look at',
                body: `<p>Look at <strong>overall</strong> records or a single <strong>tournament</strong>, in any format. Statistics are always kept separate per format.</p>`,
            },
            {
                target: '.empty-state',
                title: 'Nothing here yet',
                body: `<p>Records fill in by themselves as you complete matches, so there's nothing to set up.</p>`,
            },
            {
                target: '.kpi-strip',
                title: 'Headline numbers',
                body: `<p>Total runs, wickets and the top performer for the current selection.</p>`,
            },
            {
                target: '.stats-tabs-bar',
                title: 'Detailed tables',
                body: `<p>Batting, bowling, fielding and partnerships. Search, sort, and export any table to CSV.</p>`,
            },
            {
                target: 'a[href*="head-to-head"]',
                title: 'Head-to-head & compare',
                body: `<p>See how two teams have fared against each other, or put two players side by side.</p>`,
                placement: 'bottom',
            },
        ],
    });

    // ── Community ─────────────────────────────────────────────────────────
    add({
        id: 'community',
        page: 'community',
        main: true,
        kicker: 'Community',
        steps: [
            {
                target: '#cm-search',
                title: 'Search first',
                body: `<p>Someone may have asked already. If they have, <strong>upvote</strong> their post instead of posting again. Upvotes decide what gets fixed and built next.</p>`,
            },
            {
                target: '#cm-browse-controls',
                title: 'Sort & filter',
                body: `<p>Sort by recent activity, newest or top posts, and filter by type. You can also see just your own posts or ones that mention you.</p>`,
            },
            {
                target: '.cm-hero .cm-btn--primary',
                title: 'Start a post',
                body: `<p>Ask a question, report a bug or suggest an idea. Screenshots help a lot with bugs.</p>`,
                placement: 'bottom',
            },
            {
                target: '#cmNavBell',
                title: 'Notifications',
                body: `<p>Replies, mentions and updates on your posts show up here.</p>`,
                placement: 'bottom',
            },
        ],
    });

    // Fallback for the "?" button on a page without a tour.
    add({
        id: 'no_tour',
        page: '*',
        auto: () => false,
        replay: false,
        persist: false,
        steps: [
            {
                title: 'No guide for this page yet',
                body: ctx => `<p>The <a href="${ctx.esc(ctx.urls.home)}?guide=home">dashboard tour</a> covers every part of SimCricketX.</p>`,
            },
        ],
    });

    window.SCX_GUIDE_TOURS = T;
})();
