"""
Regression tests for the FC (First-Class) multi-innings archiver rewrite.

Two bugs are covered here:

1. Engine bug: Match._save_partnership() routed every FC partnership into
   first_innings_partnerships/second_innings_partnerships keyed off
   self.innings, which stays frozen at 1 for the entire FC match by design
   (see engine/match.py). Every partnership from every FC innings therefore
   landed in first_innings_partnerships, and second_innings_partnerships
   (and any innings beyond it) was silently always empty. Fixed by routing
   through fc_innings_partnerships, keyed by self.fc_innings.

2. Archiver bug: MatchArchiver._save_to_database() only understood a fixed
   2-innings shape (first_innings_*/second_innings_*). A shim in
   Match._fc_create_match_archive() worked around this by faking those two
   attributes from fc_innings_stats[0] (innings 1) and fc_innings_stats[-1]
   (the last innings) — so for a 3-4 innings FC match, the middle innings
   (innings 2, and innings 3 in a 4-innings match) were silently dropped:
   no MatchScorecard rows, no career-stat aggregation, no DB score columns.
   Fixed by MatchArchiver._build_innings_plan(), which reads
   fc_innings_stats/fc_innings_totals/fc_innings_partnerships directly and
   drives every DB write (scorecards, partnerships, Match row score columns)
   off however many real innings actually occurred.
"""
import copy
import os
import sys
import uuid
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import db
from database.models import Match as DBMatch, MatchScorecard, MatchPartnership, Player as DBPlayer
import engine.match as match_module
import match_archiver as match_archiver_module
from match_archiver import MatchArchiver


def _fc_squad(prefix):
    """Minimal valid FC playing XI — enough for Match.__init__ to construct
    cleanly. Ratings/technique fields are all read with .get(...) defaults
    elsewhere in the engine, so they're safe to omit here since these tests
    never simulate a ball."""
    return [{
        "name": f"{prefix}{i+1}",
        "role": "Bowler" if i < 5 else "Batsman",
        "batting_rating": 70, "bowling_rating": 70, "fielding_rating": 65,
        "batting_hand": "Right", "bowling_type": "Medium", "bowling_hand": "Right",
        "will_bowl": i < 5, "is_captain": i == 0, "is_wicketkeeper": i == 1,
    } for i in range(11)]


def _fc_match_data(user_id="tester"):
    return {
        "match_id": str(uuid.uuid4()), "created_by": user_id,
        "timestamp": "2026-08-29T12:00:00",
        "team_home": f"TW_{user_id}", "team_away": f"TC_{user_id}",
        "stadium": "Test Ground", "pitch": "Hard",
        "toss": "Heads", "toss_winner": "TW", "toss_decision": "Bat",
        "match_format": "FC", "days": 4, "simulation_mode": "auto",
        "playing_xi": {"home": _fc_squad("H_P"), "away": _fc_squad("A_P")},
        "substitutes": {"home": [], "away": []},
        "weather_forecast": "clear",
    }


# ---------------------------------------------------------------------------
# 1. Engine: partnership routing bug
# ---------------------------------------------------------------------------

def test_fc_save_partnership_routes_by_fc_innings_not_self_innings(app):
    m = match_module.Match(_fc_match_data())
    assert m.is_fc is True
    assert m.innings == 1  # frozen for the whole FC match, per design

    m.fc_innings = 3
    m.current_partnership_runs = 42
    m.current_partnership_balls = 55
    m.wickets = 2
    m._save_partnership()

    # Landed in the FC bucket, keyed by fc_innings (3) — not the T20/ListA
    # first_/second_innings_partnerships lists, which must stay untouched.
    assert m.first_innings_partnerships == []
    assert m.second_innings_partnerships == []
    assert 3 in m.fc_innings_partnerships
    assert len(m.fc_innings_partnerships[3]) == 1
    assert m.fc_innings_partnerships[3][0]["innings_number"] == 3
    assert m.fc_innings_partnerships[3][0]["runs"] == 42

    # A second partnership in innings 1 must land in its own bucket, not
    # get merged with innings 3's.
    m.fc_innings = 1
    m.current_partnership_runs = 10
    m.current_partnership_balls = 12
    m.wickets = 1
    m._save_partnership()
    assert 1 in m.fc_innings_partnerships
    assert len(m.fc_innings_partnerships[1]) == 1
    assert m.fc_innings_partnerships[1][0]["innings_number"] == 1
    assert len(m.fc_innings_partnerships[3]) == 1  # untouched by the innings-1 save


# ---------------------------------------------------------------------------
# 2. Archiver: every real innings (not just first + last) gets persisted
# ---------------------------------------------------------------------------

def _make_fc_match_with_four_innings(user_id):
    """Home bats innings 1 & 3 (a normal 2nd innings, no follow-on), away
    bats innings 2 & 4 (chasing) — the shape that most clearly exposes the
    old first+last shim, since it dropped innings 2 AND 3."""
    data = _fc_match_data(user_id)
    m = match_module.Match(data)
    m.result = "Test Warriors won by 25 runs."
    m.winner_is_home = True
    m.match_status = "completed"
    m.margin_type = "runs"
    m.margin_value = 25
    m.follow_on_enforced = False

    m.fc_innings_stats = [
        {
            "innings_number": 1, "batting_side": "home", "bowling_side": "away",
            "batting_stats": {"John Doe": {"runs": 80, "balls": 140, "fours": 8, "sixes": 0}},
            "bowling_stats": {"Champion 1": {"balls_bowled": 90, "runs": 60, "wickets": 3, "maidens": 5}},
        },
        {
            "innings_number": 2, "batting_side": "away", "bowling_side": "home",
            "batting_stats": {"Champ Bat 1": {"runs": 55, "balls": 100, "fours": 5, "sixes": 0}},
            "bowling_stats": {"Allrounder 1": {"balls_bowled": 84, "runs": 50, "wickets": 4, "maidens": 3}},
        },
        {
            "innings_number": 3, "batting_side": "home", "bowling_side": "away",
            "batting_stats": {"Batsman 1": {"runs": 45, "balls": 90, "fours": 4, "sixes": 1}},
            "bowling_stats": {"Champion 2": {"balls_bowled": 60, "runs": 40, "wickets": 2, "maidens": 2}},
        },
        {
            "innings_number": 4, "batting_side": "away", "bowling_side": "home",
            "batting_stats": {"Champ Bat 2": {"runs": 30, "balls": 70, "fours": 2, "sixes": 0}},
            "bowling_stats": {"Allrounder 2": {"balls_bowled": 66, "runs": 45, "wickets": 5, "maidens": 1}},
        },
    ]
    m.fc_innings_totals = {
        1: {"score": 180, "wickets": 10, "overs_str": "90.0", "side": "home"},
        2: {"score": 150, "wickets": 10, "overs_str": "84.0", "side": "away"},
        3: {"score": 145, "wickets": 6, "overs_str": "60.0", "side": "home"},
        4: {"score": 165, "wickets": 10, "overs_str": "66.0", "side": "away"},
    }
    m.fc_innings_partnerships = {
        1: [{
            "innings_number": 1, "wicket_number": 1,
            "batsman1_name": "John Doe", "batsman2_name": "Batsman 2",
            "runs": 60, "balls": 100,
            "batsman1_contribution": 40, "batsman2_contribution": 20,
            "start_over": 0.0, "end_over": 16.4,
        }],
        3: [{
            "innings_number": 3, "wicket_number": 1,
            "batsman1_name": "Batsman 1", "batsman2_name": "Batsman 3",
            "runs": 30, "balls": 55,
            "batsman1_contribution": 20, "batsman2_contribution": 10,
            "start_over": 0.0, "end_over": 9.1,
        }],
    }
    return m


def _archive(match):
    arch = MatchArchiver(match.match_data, match)
    arch.filenames = {"json": f"/tmp/{match.match_data['match_id']}.json"}
    ok = arch._save_to_database()
    db.session.commit()
    return ok, arch


def test_fc_day_night_archive_identifies_pink_ball(app):
    data = _fc_match_data("archive-viewer")
    data["is_day_night"] = True
    match = match_module.Match(data)
    archiver = MatchArchiver(match.match_data, match)
    assert archiver._match_type_label() == "Day/Night · Pink ball"
    assert "Match type: Day/Night · Pink ball" in archiver._generate_text_header()


def test_fc_weather_archive_uses_time_loss_language_and_never_dls(app):
    data = _fc_match_data("weather-archive")
    data["weather_forecast"] = "rain_around"
    match = match_module.Match(data)
    match.fc_weather_affected = True
    match.fc_day_gross_delay_minutes = 90
    match.fc_day_makeup_minutes = 60
    match.fc_day_net_lost_minutes = 30
    match.fc_weather_log = [{
        "day": 1, "cause": "rain", "delay_minutes": 90,
        "outcome": "resumed",
    }]

    archiver = MatchArchiver(match.match_data, match)
    archive_text = archiver._generate_text_header() + archiver._format_match_summary()
    assert "WEATHER AFFECTED" in archive_text
    assert "Gross delay: 90 minute(s)" in archive_text
    assert "Time recovered: 60 minute(s)" in archive_text
    assert "Net lost: 30 minute(s) (7 over(s))" in archive_text
    assert "DLS method applied" not in archive_text


def test_archiver_includes_all_named_fc_and_legacy_scorecard_images(monkeypatch, tmp_path):
    """The ZIP input list contains every card uploaded for this match only."""
    match = match_module.Match(_fc_match_data("viewer@example.com"))
    archiver = MatchArchiver(match.match_data, match)
    monkeypatch.setattr(match_archiver_module, "PROJECT_ROOT", Path(tmp_path))
    archiver.archive_path = tmp_path / "archive"
    archiver.archive_path.mkdir()

    temp_root = tmp_path / "data" / "temp_scorecard_images"
    user_dir = temp_root / "viewerexample.com"
    user_dir.mkdir(parents=True)
    match_id = match.match_data["match_id"]

    expected_labels = {
        "day_01_lunch_innings_2_scorecard",
        "day_01_tea_innings_2_scorecard",
        "day_01_stumps_innings_2_scorecard",
        "innings_2_end_scorecard",
    }
    for label in expected_labels:
        (user_dir / f"{match_id}_{label}.png").write_bytes(b"png")

    # Existing T20/List A names, and historical flat-directory uploads, are
    # still discovered after the FC extension.
    (temp_root / f"{match_id}_first_innings_scorecard.png").write_bytes(b"png")
    (user_dir / f"{match_id}_second_innings_scorecard.png").write_bytes(b"png")
    (user_dir / "another-match_innings_1_end_scorecard.png").write_bytes(b"other")

    archiver._include_scorecard_images()

    archived_names = {path.name for path in archiver.created_files}
    assert archived_names == {
        *(f"TW_vs_TC_{label}.png" for label in expected_labels),
        "TW_vs_TC_first_innings_scorecard.png",
        "TW_vs_TC_second_innings_scorecard.png",
    }
    assert all((archiver.archive_path / name).read_bytes() == b"png" for name in archived_names)

    zip_path = archiver._create_zip_archive()
    with match_archiver_module.zipfile.ZipFile(zip_path) as archive:
        assert set(archive.namelist()) == archived_names


def test_archiver_rebuild_preserves_existing_scorecards_and_new_capture(monkeypatch, tmp_path):
    """The final browser pass must not erase cards sealed by engine completion."""
    match = match_module.Match(_fc_match_data("viewer@example.com"))
    archiver = MatchArchiver(match.match_data, match)
    monkeypatch.setattr(match_archiver_module, "PROJECT_ROOT", Path(tmp_path))
    archiver.archive_path = tmp_path / "archive"
    archiver.archive_path.mkdir()

    zip_path = tmp_path / "data" / archiver.filenames["zip"]
    zip_path.parent.mkdir(parents=True)
    old_name = "TW_vs_TC_day_01_lunch_innings_1_scorecard.png"
    with match_archiver_module.zipfile.ZipFile(zip_path, "w") as existing:
        existing.writestr(old_name, b"old-card")
        existing.writestr("commentary.txt", b"not an image")
        existing.writestr("../unsafe_scorecard.png", b"unsafe")

    archiver._restore_scorecard_images_from_existing_archive()

    user_dir = tmp_path / "data" / "temp_scorecard_images" / "viewerexample.com"
    user_dir.mkdir(parents=True)
    match_id = match.match_data["match_id"]
    final_name = "TW_vs_TC_innings_4_end_scorecard.png"
    (user_dir / f"{match_id}_innings_4_end_scorecard.png").write_bytes(b"final-card")
    archiver._include_scorecard_images()

    assert {path.name for path in archiver.created_files} == {old_name, final_name}
    assert (archiver.archive_path / old_name).read_bytes() == b"old-card"
    assert (archiver.archive_path / final_name).read_bytes() == b"final-card"
    assert not (tmp_path / "unsafe_scorecard.png").exists()

    rebuilt_path = archiver._create_zip_archive()
    with match_archiver_module.zipfile.ZipFile(rebuilt_path) as rebuilt:
        assert set(rebuilt.namelist()) == {old_name, final_name}


def test_fc_build_innings_plan_has_all_four_innings(app, regular_user, test_team, test_team_2):
    with app.app_context():
        match = _make_fc_match_with_four_innings(regular_user.id)
        home_team, away_team = test_team, test_team_2
        arch = MatchArchiver(match.match_data, match)
        plan = arch._build_innings_plan(home_team, away_team)

        assert [e["innings_number"] for e in plan] == [1, 2, 3, 4]
        assert plan[0]["batting_team_id"] == home_team.id
        assert plan[1]["batting_team_id"] == away_team.id
        assert plan[2]["batting_team_id"] == home_team.id  # home's 2nd innings
        assert plan[3]["batting_team_id"] == away_team.id  # away's 2nd innings (chase)
        assert plan[2]["partnerships"][0]["batsman1_name"] == "Batsman 1"


def test_fc_archiver_persists_every_innings_scorecard(app, regular_user, test_team, test_team_2):
    with app.app_context():
        match = _make_fc_match_with_four_innings(regular_user.id)
        ok, _ = _archive(match)
        assert ok is not False

        match_id = match.match_data["match_id"]
        by_innings = {
            c.innings_number: c
            for c in MatchScorecard.query.filter_by(match_id=match_id, record_type="batting").all()
        }
        # All 4 innings persisted — under the old first+last shim, innings 2
        # (Champ Bat 1) and innings 3 (Batsman 1) would be missing entirely.
        assert set(by_innings.keys()) == {1, 2, 3, 4}
        assert by_innings[1].player_ref.name == "John Doe"
        assert by_innings[2].player_ref.name == "Champ Bat 1"
        assert by_innings[3].player_ref.name == "Batsman 1"
        assert by_innings[3].runs == 45
        assert by_innings[4].player_ref.name == "Champ Bat 2"

        # The innings-3 batter's runs reached career aggregates too — proof
        # the row isn't just written but actually counted.
        batsman1 = DBPlayer.query.filter_by(name="Batsman 1").first()
        assert batsman1.total_runs == 45
        assert batsman1.matches_played == 1


def test_fc_archiver_writes_second_innings_db_columns(app, regular_user, test_team, test_team_2):
    with app.app_context():
        match = _make_fc_match_with_four_innings(regular_user.id)
        _archive(match)

        db_match = DBMatch.query.get(match.match_data["match_id"])
        assert db_match.home_team_score == 180       # innings 1
        assert db_match.home_team_wickets == 10
        assert db_match.home_team_score_innings2 == 145  # innings 3 (home's 2nd)
        assert db_match.home_team_wickets_innings2 == 6

        assert db_match.away_team_score == 150        # innings 2
        assert db_match.away_team_score_innings2 == 165  # innings 4 (away's chase)

        assert db_match.days == 4
        assert db_match.follow_on_enforced is False


def test_fc_archiver_persists_partnerships_for_every_innings(app, regular_user, test_team, test_team_2):
    with app.app_context():
        match = _make_fc_match_with_four_innings(regular_user.id)
        _archive(match)

        match_id = match.match_data["match_id"]
        inn1 = MatchPartnership.query.filter_by(match_id=match_id, innings_number=1).all()
        inn3 = MatchPartnership.query.filter_by(match_id=match_id, innings_number=3).all()
        assert len(inn1) == 1 and inn1[0].runs == 60
        # Innings 3 (a "middle" innings the old shim dropped) is persisted.
        assert len(inn3) == 1 and inn3[0].runs == 30
