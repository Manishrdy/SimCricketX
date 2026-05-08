# SimCricketX Gunicorn Configuration
#
# IMPORTANT: This app uses in-memory match state (MATCH_INSTANCES dict).
# Multiple workers each get their own memory space, causing match state
# to diverge across workers. Must use exactly 1 worker.
#
# worker_class GeventWebSocketWorker is required for flask-socketio WebSocket
# transport. The plain "gevent" worker only handles HTTP — it cannot complete
# WS handshakes and produces "Invalid websocket upgrade" errors. The
# geventwebsocket worker subclasses gevent's worker and adds the WS handler.
# gevent.monkey.patch_all() runs at the top of app.py so existing
# threading.Lock / threading.Thread code keeps working with green-thread
# semantics.

bind = "127.0.0.1:5000"
workers = 1
worker_class = "geventwebsocket.gunicorn.workers.GeventWebSocketWorker"
timeout = 600
