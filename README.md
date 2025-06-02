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

This Hugging Face Space hosts my Flask‐based IPL cricket simulator. It uses Docker under the hood:

- **app.py**: Flask entrypoint (serves on 0.0.0.0:7860).  
- **Dockerfile**: builds a Python 3.9‐slim container, installs requirements, and runs `python app.py`.  
- **requirements.txt**: lists `Flask`, `PyYAML`, and any other dependencies.  

To update the Space, commit/push changes (no `.pyc` or `__pycache__`), and HF will rebuild automatically.
