
from app import create_app
from database import db
from database.models import Team

app = create_app()

with app.app_context():
    teams = Team.query.filter_by(user_id='admin@projectx.com').all()
    print(f"Found {len(teams)} teams for admin@projectx.com")
    for t in teams:
        print(f"Team: {t.name} (ID: {t.id})")
        print(f"  - Home Ground: '{t.home_ground}'")
        print(f"  - Pitch Pref : '{t.pitch_preference}'")
        print("-" * 20)
