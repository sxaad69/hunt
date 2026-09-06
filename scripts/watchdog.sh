#!/bin/bash
# Hunt paper-run watchdog: restarts the 7-day campaign if it dies.
# Runs detached (nohup); every 2 minutes. Deterministic, no AI needed.
cd /Users/user/Documents/Projects/hunt
while true; do
    sleep 120
    if ! pgrep -f hunt.paper.run > /dev/null; then
        REM=$(.venv/bin/python -c "
import json, time
try:
    m = json.load(open('state/run_meta.json'))
    print(max(int(m['duration_s'] - (time.time() - m['started_ts'])), 0))
except Exception:
    print(0)")
        if [ "$REM" -gt 1800 ]; then
            echo "$(date '+%F %T') watchdog: run dead, restarting with ${REM}s remaining" >> logs/watchdog.log
            nohup .venv/bin/python -m hunt.paper.run "$REM" >> logs/paper_week_run.log 2>&1 &
            echo "$(date '+%F %T') watchdog: restarted pid $!" >> logs/watchdog.log
        else
            echo "$(date '+%F %T') watchdog: run dead, remaining ${REM}s <= 1800 — campaign complete" >> logs/watchdog.log
        fi
    fi
done
