# SimCricketX Gunicorn Configuration
#
# IMPORTANT: This app uses in-memory match state (MATCH_INSTANCES dict).
# Multiple workers each get their own memory space, causing match state
# to diverge across workers. Must use exactly 1 worker.

bind = "127.0.0.1:5000"
workers = 1
threads = 4
timeout = 120
