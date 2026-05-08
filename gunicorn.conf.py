# SimCricketX Gunicorn Configuration
#
# IMPORTANT: This app uses in-memory match state (MATCH_INSTANCES dict).
# Multiple workers each get their own memory space, causing match state
# to diverge across workers. Must use exactly 1 worker.
#
# worker_class = "gevent" is required for flask-socketio WebSocket transport.
# gevent provides cooperative green threads that let a single worker hold
# many long-lived WebSocket connections in a few KB each. app.py runs
# gevent.monkey.patch_all() at import time so existing threading.Lock /
# threading.Thread code keeps working with green-thread semantics.
# gevent-websocket package is also required (see requirements.txt) for the
# WS handshake handler.

bind = "127.0.0.1:5000"
workers = 1
worker_class = "gevent"
timeout = 600
