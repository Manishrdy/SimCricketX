"""CSV import must use the same format IDs as database-backed statistics."""
import logging

import pandas as pd
import pytest

from engine.format_config import format_label
from engine.stats_aggregator import StatsAggregator
from match_archiver import MatchArchiver


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
