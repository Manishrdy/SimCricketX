import os

# Root directory
root_dir = "D://project_x"

# Directory structure
directories = [
    "templates",
    "static/css",
    "static/js",
    "static/assets",
    "data/teams",
    "data/matches",
    "logs/matches",
    "auth",
    "engine",
    "utils",
    "config"
]

# Files to create with relative paths
files = [
    "app.py",
    "templates/home.html",
    "templates/team.html",
    "templates/match.html",
    "data/commentary_bank.json",
    "data/ratings_config.json",
    "logs/execution.log",
    "auth/credentials.json",
    "auth/user_auth.py",
    "engine/player.py",
    "engine/team.py",
    "engine/match.py",
    "engine/ball_outcome.py",
    "engine/rain_dls.py",
    "engine/commentary.py",
    "utils/logger.py",
    "utils/helpers.py",
    "config/config.yaml",
    "requirements.txt",
    "Dockerfile",
    "launch.bat",
    "launch.sh"
]

def create_structure():
    for directory in directories:
        dir_path = os.path.join(root_dir, directory)
        os.makedirs(dir_path, exist_ok=True)
        print(f"Created directory: {dir_path}")

    for file in files:
        file_path = os.path.join(root_dir, file)
        # Only create if it doesn't exist
        if not os.path.exists(file_path):
            with open(file_path, 'w') as f:
                f.write("")  # Placeholder empty file
            print(f"Created file: {file_path}")

if __name__ == "__main__":
    create_structure()
