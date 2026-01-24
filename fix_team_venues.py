
from app import create_app
from database import db
from database.models import Team

# Map standardized team names to Venues and Pitch preferences
TEAM_DEFAULTS = {
    "Chennai Super Kings": {"venue": "M. A. Chidambaram Stadium", "pitch": "Dry"},
    "Delhi Capitals": {"venue": "Arun Jaitley Stadium", "pitch": "Flat"},
    "Gujarat Titans": {"venue": "Narendra Modi Stadium", "pitch": "Hard"},
    "Kolkata Knight Riders": {"venue": "Eden Gardens", "pitch": "Green"},
    "Lucknow Super Giants": {"venue": "BRSABV Ekana Cricket Stadium", "pitch": "Dry"},
    "Mumbai Indians": {"venue": "Wankhede Stadium", "pitch": "Hard"},
    "Punjab Kings": {"venue": "IS Bindra Stadium", "pitch": "Green"},
    "Royal Challengers Bangalore": {"venue": "M. Chinnaswamy Stadium", "pitch": "Flat"},
    "Rajasthan Royals": {"venue": "Sawai Mansingh Stadium", "pitch": "Dry"},
    "Sunrisers Hyderabad": {"venue": "Rajiv Gandhi International Stadium", "pitch": "Flat"}
}

app = create_app()

with app.app_context():
    teams = Team.query.filter_by(user_id='admin@projectx.com').all()
    count = 0
    for t in teams:
        if t.name in TEAM_DEFAULTS:
            defaults = TEAM_DEFAULTS[t.name]
            # Only update if missing
            if not t.home_ground or t.home_ground == 'None':
                t.home_ground = defaults['venue']
            if not t.pitch_preference or t.pitch_preference == 'None':
                t.pitch_preference = defaults['pitch']
            count += 1
            print(f"Updated {t.name}: {t.home_ground} ({t.pitch_preference})")
    
    if count > 0:
        db.session.commit()
        print(f"Successfully updated {count} teams.")
    else:
        print("No teams needed updating.")
