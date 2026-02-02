#!/bin/bash

echo "Searching for and terminating Monitoring App related processes..."

# 1. Search for the main app (launcher.py) process
# [l]auncher.py is a trick to exclude the grep command itself.
APP_PIDS=$(ps aux | grep '[l]auncher.py' | awk '{print $2}')

if [ -z "$APP_PIDS" ]; then
  echo "No main app (launcher.py) is running."
else
  echo "Main app PID(s): $APP_PIDS ... Force terminating (kill -9)"
  kill -9 $APP_PIDS
fi

# 2. Search for CAEN worker processes (can sometimes remain if the main app dies)
WORKER_PIDS=$(ps aux | grep '[c]aen_process' | awk '{print $2}')

if [ -z "$WORKER_PIDS" ]; then
  echo "No remaining worker processes found."
else
  echo "Remaining worker PID(s): $WORKER_PIDS ... Force terminating (kill -9)"
  kill -9 $WORKER_PIDS
fi

echo "Process cleanup complete."
