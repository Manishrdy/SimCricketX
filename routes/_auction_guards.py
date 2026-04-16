"""
Shared ownership/lookup guards for the League → Season → Auction route
tree. Before this module these helpers were re-defined (nearly identically)
in `league_routes.py`, `auction_routes.py`, and `auction_realtime.py` —
three copies meant three places to drift.

`make_guards(...)` captures the SQLAlchemy model refs once and returns an
object whose methods abort(404) if the current user doesn't own the row.
`current_user` is resolved from `flask_login` at call time, so the same
instance works across requests.
"""

from flask import abort
from flask_login import current_user


def make_guards(*, DBLeague, DBSeason, DBSeasonTeam=None,
                DBAuction=None, DBAuctionCategory=None, DBAuctionPlayer=None):
    """Return a guards namespace bound to the provided model classes.

    All models are passed explicitly so this module has no import dependency
    on `database.models`. Optional params default to None; any method that
    needs a model that wasn't provided raises AttributeError at call time,
    which is easier to spot than a late NoneType error.
    """

    class _Guards:
        def own_league(self, league_id):
            lg = DBLeague.query.get(league_id)
            if lg is None or lg.user_id != current_user.id:
                abort(404)
            return lg

        def own_season(self, season_id):
            """Return (season, league). Aborts 404 if season doesn't exist or
            the current user isn't the league owner."""
            s = DBSeason.query.get(season_id)
            if s is None:
                abort(404)
            lg = DBLeague.query.get(s.league_id)
            if lg is None or lg.user_id != current_user.id:
                abort(404)
            return s, lg

        def own_season_team(self, season_team_id):
            if DBSeasonTeam is None:
                raise AttributeError("own_season_team requires DBSeasonTeam")
            st = DBSeasonTeam.query.get(season_team_id)
            if st is None:
                abort(404)
            season, league = self.own_season(st.season_id)
            return st, season, league

        def own_auction(self, season_id):
            """Return (season, league, auction). Requires DBAuction."""
            if DBAuction is None:
                raise AttributeError("own_auction requires DBAuction")
            season, league = self.own_season(season_id)
            auction = DBAuction.query.filter_by(season_id=season.id).first()
            if auction is None:
                abort(404)
            return season, league, auction

        def own_category(self, cat_id):
            if DBAuctionCategory is None or DBAuction is None:
                raise AttributeError("own_category requires DBAuctionCategory + DBAuction")
            cat = DBAuctionCategory.query.get(cat_id)
            if cat is None:
                abort(404)
            auc = DBAuction.query.get(cat.auction_id)
            if auc is None:
                abort(404)
            season, _ = self.own_season(auc.season_id)
            return cat, auc, season

        def own_ap(self, ap_id):
            if DBAuctionPlayer is None or DBAuction is None:
                raise AttributeError("own_ap requires DBAuctionPlayer + DBAuction")
            ap = DBAuctionPlayer.query.get(ap_id)
            if ap is None:
                abort(404)
            auc = DBAuction.query.get(ap.auction_id)
            if auc is None:
                abort(404)
            season, _ = self.own_season(auc.season_id)
            return ap, auc, season

    return _Guards()
