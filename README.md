---
title: SimCricketX
emoji: "🏏"
colorFrom: blue
colorTo: green
sdk: docker
sdk_version: "latest"
app_file: app.py
pinned: false
---

# SimCricketX

This Space hosts my Flask-based IPL cricket simulator in a Docker container.

- **app.py** → Flask entry point (listening on 0.0.0.0:7860)  
- **Dockerfile** → Builds a Python 3.9-slim image, installs `requirements.txt`, and runs `python app.py`  
- **requirements.txt** → Lists Flask, PyYAML, etc.

To update: commit changes (no `__pycache__` or `.pyc`), then `git push hf-space deploy:main --force`. HF will rebuild automatically.