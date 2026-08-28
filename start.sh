#!/bin/bash
source .venv/bin/activate
nohup python -m uvicorn app:app --host 0.0.0.0 --port 8081 > /tmp/wc_app.log 2>&1 &
echo "Started on PID=$!"
echo "Available at: http://0.0.0.0:8081/login"