# Use threaded workers for SSE (Server-Sent Events) long-lived connections
# NOTE: gevent breaks Playwright Sync API, so we use gthread instead
worker_class = "gthread"
threads = 4

# Single worker to stay within Render free-tier memory limits
workers = 1

# 10-minute timeout — the automation process can take several minutes
timeout = 600

# Bind to all interfaces
bind = "0.0.0.0:10000"
