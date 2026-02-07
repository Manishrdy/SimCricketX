import pandas as pd
import os
import logging
from datetime import datetime

class StatsAggregator:
    REQUIRED_BATTING_COLS = {'Player Name', 'Team Name', 'Runs', 'Balls', 'Status',
                             '1s', '2s', '3s', 'Fours', 'Sixes', 'Dots'}
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

    def _merge_csv_files(self, file_paths, required_columns=None):
        if not file_paths:
            return pd.DataFrame()
        df_list = []
        for file_path in file_paths:
            try:
                df = pd.read_csv(file_path)
                # Validate required columns
                if required_columns:
                    missing = required_columns - set(df.columns)
                    if missing:
                        logging.error(f"Skipping {file_path}: missing required columns: {missing}")
                        continue
                df['match_id'] = os.path.basename(file_path).split('_')[0]
                df_list.append(df)
            except Exception as e:
                logging.error(f"Error reading {file_path}: {e}")
        return pd.concat(df_list, ignore_index=True) if df_list else pd.DataFrame()

    def _calculate_batting_stats(self):
        if self.batting_df.empty:
            return pd.DataFrame()

        num_cols = ['Runs', 'Balls', '1s', '2s', '3s', 'Fours', 'Sixes', 'Dots']
        self.batting_df[num_cols] = self.batting_df[num_cols].apply(pd.to_numeric, errors='coerce').fillna(0)

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
        ducks = self.batting_df[(self.batting_df['Runs'] == 0) & self.batting_df['Status'].notna() & (self.batting_df['Status'] != 'not out')].groupby(['Player Name', 'Team Name']).size().reset_index(name='Ducks')  # A11: exclude not-outs

        for df in [innings, not_outs, hs, fifties, hundreds, ducks]:
            player_stats = pd.merge(player_stats, df, on=['Player Name', 'Team Name'], how='left')
        
        outs = player_stats['Innings'].fillna(0) - player_stats['NOs'].fillna(0)
        player_stats['Average'] = (player_stats['Runs'] / outs.where(outs > 0, None)).fillna(0).round(2)
        player_stats['Strike Rate'] = (player_stats['Runs'] / player_stats['Balls'].where(player_stats['Balls'] > 0, None) * 100).fillna(0).round(2)

        dismissals = pd.crosstab(self.batting_df['Player Name'], self.batting_df['Status'])
        player_stats = pd.merge(player_stats, dismissals, on='Player Name', how='left')
        
        catches = self.batting_df[self.batting_df['Status'] == 'Caught'].groupby('Fielder Out').size().reset_index(name='Catches')
        run_outs = self.batting_df[self.batting_df['Status'] == 'Run out'].groupby('Fielder Out').size().reset_index(name='RunOuts_Made')

        player_stats = pd.merge(player_stats, catches, left_on='Player Name', right_on='Fielder Out', how='left').drop(columns=['Fielder Out'])
        player_stats = pd.merge(player_stats, run_outs, left_on='Player Name', right_on='Fielder Out', how='left').drop(columns=['Fielder Out'])
        
        player_stats.fillna(0, inplace=True)
        player_stats.rename(columns={'Player Name': 'Player', 'Team Name': 'Team', 'Fours': '4s', 'Sixes': '6s', 'Run out': 'RunOut', 'not out': 'NOs'}, inplace=True)
        
        ordered_cols = ['Player', 'Team', 'Matches', 'Innings', 'Runs', 'Balls', 'Strike Rate', 'Average', 'HS', 'NOs', '50s', '100s', '1s', '2s', '3s', '4s', '6s', 'Dots', 'Bowled', 'Caught', 'LBW', 'RunOut', 'Catches', 'RunOuts_Made']
        for col in ordered_cols:
            if col not in player_stats.columns:
                player_stats[col] = 0
        
        int_cols = [c for c in ordered_cols if c not in ['Player', 'Team', 'Strike Rate', 'Average']]
        for col in int_cols:
            if col in player_stats.columns:
                player_stats[col] = player_stats[col].astype(int)

        return player_stats[ordered_cols]

    def _calculate_bowling_stats(self):
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

        if not self.batting_df.empty:
            wicket_df = self.batting_df[self.batting_df['Bowler Out'].notna() & (self.batting_df['Status'].isin(['Caught', 'Bowled', 'LBW']))]
            if not wicket_df.empty:
                # Team Name in batting_df is the batting team, not the bowling team,
                # so we must NOT join on Team Name (they are opposite teams)
                wicket_types = wicket_df.groupby(['Bowler Out', 'Status']).size().unstack(fill_value=0).reset_index()
                wicket_types.rename(columns={'Bowler Out': 'Bowler Name'}, inplace=True)
                # Rename status columns to prefixed names
                for col in ['Caught', 'Bowled', 'LBW']:
                    if col in wicket_types.columns:
                        wicket_types.rename(columns={col: f'D_{col}'}, inplace=True)
                bowling_stats = pd.merge(bowling_stats, wicket_types, on=['Bowler Name'], how='left')

        bowling_stats.fillna(0, inplace=True)
        bowling_stats.rename(columns={'Bowler Name':'Player','Team Name':'Team','No Balls':'no balls','Maidens':'maidens'}, inplace=True)
        
        ordered_cols = ['Player', 'Team', 'Matches', 'Overs', 'Runs', 'Wickets', 'maidens', 'Best', 'Average', 'Economy', 'Strike Rate', '4w', 'Wides', 'no balls', 'Byes', 'Leg Byes', 'D_Caught', 'D_Bowled', 'D_LBW']
        for col in ordered_cols:
            if col not in bowling_stats.columns:
                bowling_stats[col] = 0
        
        int_cols = [c for c in ordered_cols if c not in ['Player', 'Team', 'Overs', 'Best', 'Average', 'Economy', 'Strike Rate']]
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
