"""Story mode routes: a gallery of legendary match arcs.

A story pack (engine/data/scenarios/*.json) defines the pressure beats of a
famous real-world match. Stories are team-agnostic — the gallery's call to
action links into the regular match setup flow with the story preselected,
where the user picks their own teams, venue, and pitch. The actual steering
is wired up by /match/setup (story_id in the POST payload).
"""

from flask import render_template
from flask_login import login_required

from engine.scenario_packs import list_scenario_packs


def register_scenario_routes(app):

    @app.route("/scenarios")
    @login_required
    def scenario_gallery():
        return render_template("scenarios.html", packs=list_scenario_packs())
