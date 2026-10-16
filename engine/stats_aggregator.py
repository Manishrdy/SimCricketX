import pandas as pd
import os
import logging
from datetime import datetime

from engine.format_catalog import FORMAT_CATALOG
from utils.exception_tracker import log_exception

class StatsAggregator:
    REQUIRED_BATTING_COLS = {'Player Name', 'Team Name', 'Runs', 'Balls', 'Status',
                             '1s', '2s', '3s', 'Fours', 'Sixes', 'Dots'}
    OPTIONAL_BATTING_COLS = {'Fielder Out', 'Bowler Out'}
    REQUIRED_BOWLING_COLS = {'Bowler Name', 'Team Name', 'Overs', 'Maidens', 'Runs',
                             'Wickets', 'Wides', 'No Balls', 'Byes', 'Leg Byes'}

    def __init__(self, uploaded_files, user_id):
        self.uploaded_files = uploaded_files
        self.user_id = user_id
        self.stats_dir = os.path.join('data', 'stats')
        os.makedirs(self.stats_dir, exist_ok=True)

        self.batting_files = [f for f in uploaded_files if 'batting' in os.path.basename(f).lower()]
        self.bowling_files = [f for f in uploaded_files if 'bowling' in os.path.basename(f).lower()]

        self.batting_df = self._merge_csv_files(self.batting_files, self.REQUIRED_BATTING_COLS)
        self.bowling_df = self._merge_csv_files(self.bowling_files, self.REQUIRED_BOWLING_COLS)

    @staticmethod
    def _normalize_format(value):
        """Accept canonical IDs and archive display labels at the CSV boundary."""
        if pd.isna(value) or not str(value).strip():
            return 'Legacy'
        label = str(value).strip()
        # List A archives include the scheduled length in their display label.
        base_label = label.split('·', 1)[0].strip().casefold()
        for format_id, spec in FORMAT_CATALOG.items():
            if base_label in (format_id.casefold(), spec['label'].casefold()):
                return format_id
        # Preserve custom formats rather than silently assigning them to T20.
        return label

    def _merge_csv_files(self, file_paths, required_columns=None):
        if not file_paths:
            return pd.DataFrame()
        df_list = []
        for file_path in file_paths:
            try:
                df = pd.read_csv(file_path)
                if 'Match Format' not in df:
                    df['Match Format'] = 'Legacy'
                df['Match Format'] = df['Match Format'].map(self._normalize_format)
                if 'Balls' in df and 'Overs' not in df and required_columns == self.REQUIRED_BOWLING_COLS:
                    # Legacy aggregation works in six-ball notation internally.
                    df['Overs'] = df['Balls'].apply(lambda b: int(b) // 6 + (int(b) % 6) / 10)
                # Validate required columns
                if required_columns:
                    missing = required_columns - set(df.columns)
                    if missing:
                        logging.error(f"Skipping {file_path}: missing required columns: {missing}")
                        continue
                # Ensure optional attribution columns exist so downstream groupby
                # logic never crashes when providers omit them.
                if required_columns == self.REQUIRED_BATTING_COLS:
                    for col in self.OPTIONAL_BATTING_COLS:
                        if col not in df.columns:
                            df[col] = None
                df['match_id'] = os.path.basename(file_path).split('_')[0]
                df_list.append(df)
            except Exception as e:
                log_exception(e)
                logging.error(f"Error reading {file_path}: {e}")
        return pd.concat(df_list, ignore_index=True) if df_list else pd.DataFrame()

    def _by_format(self, discipline):
        original_bat, original_bowl = self.batting_df, self.bowling_df
        source = original_bat if discipline == 'batting' else original_bowl
        if source.empty:
            return pd.DataFrame()
        results = []
        try:
            for fmt in source['Match Format'].unique():
                self.batting_df = original_bat[original_bat['Match Format'] == fmt].copy() if not original_bat.empty else original_bat
                self.bowling_df = original_bowl[original_bowl['Match Format'] == fmt].copy() if not original_bowl.empty else original_bowl
                result = getattr(self, '_legacy_' + discipline + '_stats')()
                result['Match Format'] = fmt
                if discipline == 'bowling':
                    result['Balls'] = result['Overs'].astype(float).apply(lambda x: int(x) * 6 + round((x % 1) * 10))
                    result['Economy Unit'] = 'runs/100 balls' if fmt == 'Hundred' else 'runs/over'
                    if fmt == 'Hundred':
                        result['Economy'] = (result['Runs'] * 100 / result['Balls'].where(result['Balls'] > 0)).round(2)
                        result['Overs'] = result['Balls']
                results.append(result)
        finally:
            self.batting_df, self.bowling_df = original_bat, original_bowl
        return pd.concat(results, ignore_index=True)

    def _calculate_batting_stats(self):
        return self._by_format('batting')

    def _calculate_bowling_stats(self):
        return self._by_format('bowling')

    def _attribute_opposing_team(self, rows, actor_col, discipline='batting'):
        """Resolve the fielding/bowling team for each dismissal row using match context."""
        if rows.empty or actor_col not in rows.columns:
            return pd.DataFrame()

        subset = rows[rows[actor_col].notna()].copy()
        if subset.empty:
            return pd.DataFrame()
        subset[actor_col] = subset[actor_col].astype(str).str.strip()
        subset = subset[subset[actor_col] != '']
        if subset.empty:
            return pd.DataFrame()

        bat_match_player_teams = {}
        bowl_match_player_teams = {}
        all_match_player_teams = {}
        match_teams = {}

        if not self.batting_df.empty:
            for _, r in self.batting_df[['match_id', 'Player Name', 'Team Name']].dropna().iterrows():
                m_id = r['match_id']
                p_name = str(r['Player Name']).strip()
                t_name = r['Team Name']
                bat_match_player_teams.setdefault((m_id, p_name), set()).add(t_name)
                all_match_player_teams.setdefault((m_id, p_name), set()).add(t_name)
                match_teams.setdefault(m_id, set()).add(t_name)

        if not self.bowling_df.empty:
            for _, r in self.bowling_df[['match_id', 'Bowler Name', 'Team Name']].dropna().iterrows():
                m_id = r['match_id']
                p_name = str(r['Bowler Name']).strip()
                t_name = r['Team Name']
                bowl_match_player_teams.setdefault((m_id, p_name), set()).add(t_name)
                all_match_player_teams.setdefault((m_id, p_name), set()).add(t_name)
                match_teams.setdefault(m_id, set()).add(t_name)

        primary_match_player = (
            bowl_match_player_teams if discipline == 'bowling' else bat_match_player_teams
        )

        def _resolve(row):
            m_id = row.get('match_id')
            bat_team = row.get('Team Name')
            actor = row[actor_col]
            for candidates in (
                primary_match_player.get((m_id, actor), set()) - {bat_team},
                all_match_player_teams.get((m_id, actor), set()) - {bat_team},
                match_teams.get(m_id, set()) - {bat_team},
            ):
                if len(candidates) == 1:
                    return next(iter(candidates))
            return None

        subset['Opposing Team'] = subset.apply(_resolve, axis=1)
        return subset[subset['Opposing Team'].notna()]

    def _legacy_batting_stats(self):
        if self.batting_df.empty:
            return pd.DataFrame()

        num_cols = ['Runs', 'Balls', '1s', '2s', '3s', 'Fours', 'Sixes', 'Dots']
        self.batting_df[num_cols] = self.batting_df[num_cols].apply(pd.to_numeric, errors='coerce').fillna(0)

        # Normalize dismissal values to canonical casing before any filter/groupby.
        # The engine writes "Run Out" (capital O); archive CSVs may use "Run out".
        # All other dismissal types are also normalised here so the crosstab columns
        # and the rename map further below always match, regardless of source.
        _STATUS_CANON = {
            'run out': 'Run out',
            'caught':  'Caught',
            'bowled':  'Bowled',
            'lbw':     'LBW',
            'not out': 'not out',
        }
        self.batting_df['Status'] = (
            self.batting_df['Status']
            .where(self.batting_df['Status'].isna(),
                   self.batting_df['Status'].str.strip().apply(
                       lambda s: _STATUS_CANON.get(s.lower(), s) if isinstance(s, str) else s
                   ))
        )

        agg_funcs = {
            'Matches': ('match_id', 'nunique'), 'Runs': ('Runs', 'sum'), 'Balls': ('Balls', 'sum'),
            '1s': ('1s', 'sum'), '2s': ('2s', 'sum'), '3s': ('3s', 'sum'),
            'Fours': ('Fours', 'sum'), 'Sixes': ('Sixes', 'sum'), 'Dots': ('Dots', 'sum')
        }
        player_stats = self.batting_df.groupby(['Player Name', 'Team Name']).agg(**agg_funcs).reset_index()

        innings = self.batting_df.dropna(subset=['Status']).groupby(['Player Name', 'Team Name']).size().reset_index(name='Innings')
        not_outs = self.batting_df[self.batting_df['Status'] == 'not out'].groupby(['Player Name', 'Team Name']).size().reset_index(name='NOs')
        hs = self.batting_df.groupby(['Player Name', 'Team Name'])['Runs'].max().reset_index(name='HS')
        fifties = self.batting_df[(self.batting_df['Runs'] >= 50) & (self.batting_df['Runs'] < 100)].groupby(['Player Name', 'Team Name']).size().reset_index(name='50s')  # A10: exclude hundreds
        hundreds = self.batting_df[self.batting_df['Runs'] >= 100].groupby(['Player Name', 'Team Name']).size().reset_index(name='100s')
        double_hundreds = self.batting_df[(self.batting_df['Runs'] >= 200) & (self.batting_df['Runs'] < 300)].groupby(['Player Name', 'Team Name']).size().reset_index(name='200s')
        triple_hundreds = self.batting_df[self.batting_df['Runs'] >= 300].groupby(['Player Name', 'Team Name']).size().reset_index(name='300s')
        ducks = self.batting_df[(self.batting_df['Runs'] == 0) & self.batting_df['Status'].notna() & (self.batting_df['Status'] != 'not out')].groupby(['Player Name', 'Team Name']).size().reset_index(name='Ducks')  # A11: exclude not-outs

        for df in [innings, not_outs, hs, fifties, hundreds, double_hundreds, triple_hundreds, ducks]:
            player_stats = pd.merge(player_stats, df, on=['Player Name', 'Team Name'], how='left')
        
        outs = player_stats['Innings'].fillna(0) - player_stats['NOs'].fillna(0)
        player_stats['Average'] = (player_stats['Runs'] / outs.where(outs > 0, None)).fillna(0).round(2)
        player_stats['Strike Rate'] = (player_stats['Runs'] / player_stats['Balls'].where(player_stats['Balls'] > 0, None) * 100).fillna(0).round(2)

        # Not-outs are already counted per player/team above; they are not dismissals.
        dismissals = pd.crosstab(
            [self.batting_df['Player Name'], self.batting_df['Team Name']],
            self.batting_df['Status'],
        ).drop(columns=['not out'], errors='ignore')
        if not dismissals.empty:
            player_stats = pd.merge(
                player_stats, dismissals.reset_index(),
                on=['Player Name', 'Team Name'], how='left',
            )

        caught_df = self._attribute_opposing_team(
            self.batting_df[self.batting_df['Status'] == 'Caught'],
            'Fielder Out',
            discipline='batting',
        )
        if not caught_df.empty:
            catches = (
                caught_df.groupby(['Fielder Out', 'Opposing Team'])
                .size()
                .reset_index(name='Catches')
                .rename(columns={'Fielder Out': 'Player Name', 'Opposing Team': 'Team Name'})
            )
            player_stats = pd.merge(player_stats, catches, on=['Player Name', 'Team Name'], how='left')

        run_out_df = self._attribute_opposing_team(
            self.batting_df[self.batting_df['Status'] == 'Run out'],
            'Fielder Out',
            discipline='batting',
        )
        if not run_out_df.empty:
            run_outs = (
                run_out_df.groupby(['Fielder Out', 'Opposing Team'])
                .size()
                .reset_index(name='RunOuts_Made')
                .rename(columns={'Fielder Out': 'Player Name', 'Opposing Team': 'Team Name'})
            )
            player_stats = pd.merge(player_stats, run_outs, on=['Player Name', 'Team Name'], how='left')
        
        player_stats.fillna(0, inplace=True)
        player_stats.rename(columns={'Player Name': 'Player', 'Team Name': 'Team', 'Fours': '4s', 'Sixes': '6s', 'Run out': 'RunOut'}, inplace=True)
        
        ordered_cols = ['Player', 'Team', 'Matches', 'Innings', 'Runs', 'Balls', 'Strike Rate', 'Average', 'HS', 'NOs', '50s', '100s', '200s', '300s', '1s', '2s', '3s', '4s', '6s', 'Dots', 'Bowled', 'Caught', 'LBW', 'RunOut', 'Catches', 'RunOuts_Made']
        for col in ordered_cols:
            if col not in player_stats.columns:
                player_stats[col] = 0
        
        int_cols = [c for c in ordered_cols if c not in ['Player', 'Team', 'Strike Rate', 'Average']]
        for col in int_cols:
            if col in player_stats.columns:
                player_stats[col] = player_stats[col].astype(int)

        return player_stats[ordered_cols]

    def _legacy_bowling_stats(self):
        if self.bowling_df.empty:
            return pd.DataFrame()

        self.bowling_df['Balls'] = self.bowling_df['Overs'].apply(lambda x: int(x) * 6 + round((x - int(x)) * 10))
        agg_funcs = {'Balls': 'sum', 'Maidens': 'sum', 'Runs': 'sum', 'Wickets': 'sum', 'Wides': 'sum', 'No Balls': 'sum', 'Byes': 'sum', 'Leg Byes': 'sum', 'match_id': 'nunique'}
        
        bowling_stats = self.bowling_df.groupby(['Bowler Name', 'Team Name']).agg(agg_funcs).rename(columns={'match_id': 'Matches'}).reset_index()
        
        bowling_stats['Overs'] = bowling_stats['Balls'].apply(lambda b: f"{b // 6}.{b % 6}")
        bowling_stats['Economy'] = (bowling_stats['Runs'] / (bowling_stats['Balls'] / 6).where(bowling_stats['Balls'] > 0, None)).fillna(0).round(2)
        bowling_stats['Average'] = (bowling_stats['Runs'] / bowling_stats['Wickets'].where(bowling_stats['Wickets'] > 0, None)).fillna(0).round(2)
        bowling_stats['Strike Rate'] = (bowling_stats['Balls'] / bowling_stats['Wickets'].where(bowling_stats['Wickets'] > 0, None)).fillna(0).round(2)

        best_sorted = self.bowling_df.sort_values(['Wickets', 'Runs'], ascending=[False, True])
        best_idx = best_sorted.groupby(['Bowler Name', 'Team Name']).first().reset_index()
        best_df = best_idx.set_index(['Bowler Name', 'Team Name'])
        bowling_stats['Best'] = bowling_stats.set_index(['Bowler Name', 'Team Name']).index.map(best_df.apply(lambda row: f"{int(row['Wickets'])}/{int(row['Runs'])}", axis=1))
        
        fours_df = self.bowling_df[self.bowling_df['Wickets'] >= 4].groupby(['Bowler Name', 'Team Name']).size().reset_index(name='4w')
        bowling_stats = pd.merge(bowling_stats, fours_df, on=['Bowler Name', 'Team Name'], how='left')
        five_df = self.bowling_df[self.bowling_df['Wickets'] >= 5].groupby(['Bowler Name', 'Team Name']).size().reset_index(name='5w')
        bowling_stats = pd.merge(bowling_stats, five_df, on=['Bowler Name', 'Team Name'], how='left')

        match_figures = self.bowling_df.groupby(
            ['Bowler Name', 'Team Name', 'match_id'], as_index=False
        ).agg({'Wickets': 'sum', 'Runs': 'sum'})
        best_match = match_figures.sort_values(
            ['Wickets', 'Runs'], ascending=[False, True]
        ).groupby(['Bowler Name', 'Team Name']).first().reset_index()
        best_match['BBM'] = best_match.apply(
            lambda row: f"{int(row['Wickets'])}/{int(row['Runs'])}", axis=1
        )
        ten_match = match_figures[match_figures['Wickets'] >= 10].groupby(
            ['Bowler Name', 'Team Name']
        ).size().reset_index(name='10wm')
        bowling_stats = pd.merge(
            bowling_stats,
            best_match[['Bowler Name', 'Team Name', 'BBM']],
            on=['Bowler Name', 'Team Name'], how='left',
        )
        bowling_stats = pd.merge(
            bowling_stats, ten_match,
            on=['Bowler Name', 'Team Name'], how='left',
        )

        if not self.batting_df.empty:
            wicket_df = self._attribute_opposing_team(
                self.batting_df[
                    self.batting_df['Bowler Out'].notna()
                    & (self.batting_df['Status'].isin(['Caught', 'Bowled', 'LBW']))
                ],
                'Bowler Out',
                discipline='bowling',
            )
            if not wicket_df.empty:
                wicket_types = (
                    wicket_df.groupby(['Bowler Out', 'Opposing Team', 'Status'])
                    .size()
                    .unstack(fill_value=0)
                    .reset_index()
                    .rename(columns={'Bowler Out': 'Bowler Name', 'Opposing Team': 'Team Name'})
                )
                # Rename status columns to prefixed names
                for col in ['Caught', 'Bowled', 'LBW']:
                    if col in wicket_types.columns:
                        wicket_types.rename(columns={col: f'D_{col}'}, inplace=True)
                bowling_stats = pd.merge(
                    bowling_stats, wicket_types,
                    on=['Bowler Name', 'Team Name'], how='left',
                )

        bowling_stats.fillna(0, inplace=True)
        bowling_stats.rename(columns={'Bowler Name':'Player','Team Name':'Team','No Balls':'no balls','Maidens':'maidens'}, inplace=True)
        
        ordered_cols = ['Player', 'Team', 'Matches', 'Overs', 'Runs', 'Wickets', 'maidens', 'Best', 'BBM', 'Average', 'Economy', 'Strike Rate', '4w', '5w', '10wm', 'Wides', 'no balls', 'Byes', 'Leg Byes', 'D_Caught', 'D_Bowled', 'D_LBW']
        for col in ordered_cols:
            if col not in bowling_stats.columns:
                bowling_stats[col] = 0
        
        int_cols = [c for c in ordered_cols if c not in ['Player', 'Team', 'Overs', 'Best', 'BBM', 'Average', 'Economy', 'Strike Rate']]
        for col in int_cols:
            if col in bowling_stats.columns:
                bowling_stats[col] = bowling_stats[col].astype(int)

        return bowling_stats[ordered_cols]

    def process_and_save(self):
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        batting_stats = self._calculate_batting_stats()
        bowling_stats = self._calculate_bowling_stats()

        if not batting_stats.empty:
            batting_stats.to_csv(os.path.join(self.stats_dir, f"{self.user_id}_batting_{timestamp}.csv"), index=False)
        if not bowling_stats.empty:
            bowling_stats.to_csv(os.path.join(self.stats_dir, f"{self.user_id}_bowling_{timestamp}.csv"), index=False)
