"""CSV import must use the same format IDs as database-backed statistics."""
import csv
import logging

import pandas as pd
import pytest

from engine.format_config import format_label
from engine.stats_aggregator import StatsAggregator
from match_archiver import MatchArchiver


@pytest.mark.parametrize('statuses,expected_nos,expected_average', [
    (['not out', 'Bowled'], 1, 40),
    (['Bowled', 'Caught'], 0, 20),
    (['not out', 'not out'], 2, 0),
])
def test_batting_csv_has_one_not_out_column(
    tmp_path, monkeypatch, statuses, expected_nos, expected_average
):
    monkeypatch.chdir(tmp_path)
    paths = []
    for index, status in enumerate(statuses):
        row = dict.fromkeys(StatsAggregator.REQUIRED_BATTING_COLS, 0)
        row.update({
            'Player Name': 'Batter', 'Team Name': 'Team',
            'Runs': 20, 'Balls': 10, 'Status': status,
        })
        path = tmp_path / f'match{index}_batting.csv'
        pd.DataFrame([row]).to_csv(path, index=False)
        paths.append(str(path))

    aggregator = StatsAggregator(paths, 'test')
    stats = aggregator._calculate_batting_stats()
    assert stats.columns.is_unique
    assert stats.loc[0, 'NOs'] == expected_nos
    assert stats.loc[0, 'Average'] == expected_average

    aggregator.process_and_save()
    output = next((tmp_path / 'data' / 'stats').glob('test_batting_*.csv'))
    with output.open(newline='') as stream:
        header = next(csv.reader(stream))
    assert len(header) == len(set(header))
    assert header.count('NOs') == 1
    assert pd.read_csv(output).loc[0, 'NOs'] == expected_nos


def test_archived_hundred_csv_economy_and_mixed_labels(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    archiver = MatchArchiver.__new__(MatchArchiver)
    archiver.archive_path = tmp_path
    archiver.match_data = {'match_format': 'Hundred', 'scheduled_overs': 20}
    archiver.format_label = format_label('Hundred', 20)
    archiver.created_files = []
    archiver.logger = logging.getLogger(__name__)
    path = tmp_path / 'match1_bowling.csv'
    archiver._create_bowling_csv(
        path.name, {'Bowler': {'balls_bowled': 20, 'runs': 25, 'wickets': 1}}, 'Team'
    )

    aggregator = StatsAggregator([str(path)], 'test')
    row = aggregator._calculate_bowling_stats().iloc[0]
    assert row['Match Format'] == 'Hundred'
    assert row['Balls'] == row['Overs'] == 20
    assert row['Economy'] == 125
    assert row['Economy Unit'] == 'runs/100 balls'

    # Canonical and display labels must contribute to one weighted total.
    canonical = pd.read_csv(path)
    canonical['Match Format'] = 'Hundred'
    canonical['Balls'] = 10
    canonical['Runs'] = 5
    second_path = tmp_path / 'match2_bowling.csv'
    canonical.to_csv(second_path, index=False)
    aggregator = StatsAggregator([str(path), str(second_path)], 'test')
    rows = aggregator._calculate_bowling_stats()
    assert len(rows) == 1
    row = rows.iloc[0]
    assert row['Matches'] == 2
    assert row['Balls'] == row['Overs'] == 30
    assert row['Runs'] == 30
    assert row['Economy'] == 100


@pytest.mark.parametrize('label,expected', [
    ('The Hundred', 'Hundred'), (' hundred ', 'Hundred'),
    ('List A · 40 overs', 'ListA'), ('List A · 50 overs', 'ListA'),
    ('ListA', 'ListA'), ('First-Class', 'FC'), ('FC', 'FC'),
    ('T20', 'T20'), ('T10', 'T10'), ('Custom', 'Custom'),
    (None, 'Legacy'), ('', 'Legacy'),
])
def test_import_normalizes_both_disciplines(tmp_path, monkeypatch, label, expected):
    monkeypatch.chdir(tmp_path)
    batting = dict.fromkeys(StatsAggregator.REQUIRED_BATTING_COLS, 0)
    batting.update({'Player Name': 'Batter', 'Team Name': 'Team', 'Status': 'Bowled'})
    bowling = dict.fromkeys(StatsAggregator.REQUIRED_BOWLING_COLS, 0)
    bowling.update({'Bowler Name': 'Bowler', 'Team Name': 'Team', 'Overs': 3.2, 'Runs': 25})
    paths = []
    for discipline, data in [('batting', batting), ('bowling', bowling)]:
        data['Match Format'] = label
        path = tmp_path / f'match1_{discipline}.csv'
        pd.DataFrame([data]).to_csv(path, index=False)
        paths.append(str(path))
    aggregator = StatsAggregator(paths, 'test')
    assert aggregator.batting_df['Match Format'].tolist() == [expected]
    assert aggregator.bowling_df['Match Format'].tolist() == [expected]
    row = aggregator._calculate_bowling_stats().iloc[0]
    assert row['Economy'] == (125 if expected == 'Hundred' else 7.5)


def test_same_name_players_on_different_teams_do_not_share_attributions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def _bat_row(**overrides):
        row = dict.fromkeys(StatsAggregator.REQUIRED_BATTING_COLS, 0)
        row.update({'Fielder Out': None, 'Bowler Out': None, 'Match Format': 'T20'})
        row.update(overrides)
        return row

    def _bowl_row(**overrides):
        row = dict.fromkeys(StatsAggregator.REQUIRED_BOWLING_COLS, 0)
        row.update({'Match Format': 'T20'})
        row.update(overrides)
        return row

    # Match 1: Team A vs Team B (both teams have a player named "Alex")
    m1_bat = pd.DataFrame([
        # Team A batting: Alex (Team A) is Bowled by Alex (Team B); Opener A Caught by Alex (Team B) off Alex (Team B); Batter A3 Run out by Alex (Team B)
        _bat_row(**{
            'Player Name': 'Alex', 'Team Name': 'Team A', 'Runs': 15, 'Balls': 10,
            'Status': 'Bowled', 'Bowler Out': 'Alex',
        }),
        _bat_row(**{
            'Player Name': 'Opener A', 'Team Name': 'Team A', 'Runs': 8, 'Balls': 6,
            'Status': 'Caught', 'Bowler Out': 'Alex', 'Fielder Out': 'Alex',
        }),
        _bat_row(**{
            'Player Name': 'Batter A3', 'Team Name': 'Team A', 'Runs': 4, 'Balls': 5,
            'Status': 'Run out', 'Fielder Out': 'Alex',
        }),
        # Team B batting: Alex (Team B) is LBW to Alex (Team A); Opener B Caught by Alex (Team A) off Pace A
        _bat_row(**{
            'Player Name': 'Alex', 'Team Name': 'Team B', 'Runs': 30, 'Balls': 20,
            'Status': 'LBW', 'Bowler Out': 'Alex',
        }),
        _bat_row(**{
            'Player Name': 'Opener B', 'Team Name': 'Team B', 'Runs': 12, 'Balls': 9,
            'Status': 'Caught', 'Bowler Out': 'Pace A', 'Fielder Out': 'Alex',
        }),
    ])
    m1_bowl = pd.DataFrame([
        _bowl_row(**{'Bowler Name': 'Alex', 'Team Name': 'Team B', 'Overs': 4.0, 'Runs': 20, 'Wickets': 2}),
        _bowl_row(**{'Bowler Name': 'Alex', 'Team Name': 'Team A', 'Overs': 4.0, 'Runs': 25, 'Wickets': 1}),
        _bowl_row(**{'Bowler Name': 'Pace A', 'Team Name': 'Team A', 'Overs': 2.0, 'Runs': 15, 'Wickets': 1}),
    ])

    # Match 2: Team C vs Team D (another player named "Alex" on Team D)
    m2_bat = pd.DataFrame([
        _bat_row(**{
            'Player Name': 'Batter C', 'Team Name': 'Team C', 'Runs': 10, 'Balls': 12,
            'Status': 'Bowled', 'Bowler Out': 'Alex',
        }),
        _bat_row(**{
            'Player Name': 'Alex', 'Team Name': 'Team D', 'Runs': 5, 'Balls': 4,
            'Status': 'Caught', 'Bowler Out': 'Bowler C', 'Fielder Out': 'Fielder C',
        }),
    ])
    m2_bowl = pd.DataFrame([
        _bowl_row(**{'Bowler Name': 'Alex', 'Team Name': 'Team D', 'Overs': 3.0, 'Runs': 10, 'Wickets': 1}),
        _bowl_row(**{'Bowler Name': 'Bowler C', 'Team Name': 'Team C', 'Overs': 2.0, 'Runs': 5, 'Wickets': 1}),
    ])

    paths = []
    for name, df in [
        ('match1_batting.csv', m1_bat),
        ('match1_bowling.csv', m1_bowl),
        ('match2_batting.csv', m2_bat),
        ('match2_bowling.csv', m2_bowl),
    ]:
        p = tmp_path / name
        df.to_csv(p, index=False)
        paths.append(str(p))

    agg = StatsAggregator(paths, 'test')
    bat = agg._calculate_batting_stats().set_index(['Player', 'Team'])
    bowl = agg._calculate_bowling_stats().set_index(['Player', 'Team'])

    # Batting dismissal & fielding assertions for each distinct (Alex, Team)
    assert bat.loc[('Alex', 'Team A'), 'Bowled'] == 1
    assert bat.loc[('Alex', 'Team A'), 'LBW'] == 0
    assert bat.loc[('Alex', 'Team A'), 'Caught'] == 0
    assert bat.loc[('Alex', 'Team A'), 'Catches'] == 1
    assert bat.loc[('Alex', 'Team A'), 'RunOuts_Made'] == 0

    assert bat.loc[('Alex', 'Team B'), 'Bowled'] == 0
    assert bat.loc[('Alex', 'Team B'), 'LBW'] == 1
    assert bat.loc[('Alex', 'Team B'), 'Caught'] == 0
    assert bat.loc[('Alex', 'Team B'), 'Catches'] == 1
    assert bat.loc[('Alex', 'Team B'), 'RunOuts_Made'] == 1

    assert bat.loc[('Alex', 'Team D'), 'Bowled'] == 0
    assert bat.loc[('Alex', 'Team D'), 'LBW'] == 0
    assert bat.loc[('Alex', 'Team D'), 'Caught'] == 1
    assert bat.loc[('Alex', 'Team D'), 'Catches'] == 0
    assert bat.loc[('Alex', 'Team D'), 'RunOuts_Made'] == 0

    # Bowling dismissal breakdown assertions for each distinct (Alex, Team)
    assert bowl.loc[('Alex', 'Team A'), 'D_LBW'] == 1
    assert bowl.loc[('Alex', 'Team A'), 'D_Bowled'] == 0
    assert bowl.loc[('Alex', 'Team A'), 'D_Caught'] == 0

    assert bowl.loc[('Alex', 'Team B'), 'D_LBW'] == 0
    assert bowl.loc[('Alex', 'Team B'), 'D_Bowled'] == 1
    assert bowl.loc[('Alex', 'Team B'), 'D_Caught'] == 1

    assert bowl.loc[('Alex', 'Team D'), 'D_LBW'] == 0
    assert bowl.loc[('Alex', 'Team D'), 'D_Bowled'] == 1
    assert bowl.loc[('Alex', 'Team D'), 'D_Caught'] == 0

