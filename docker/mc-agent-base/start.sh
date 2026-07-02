#!/bin/sh
# start.sh — startet poll.sh in einer tmux-Session
# PTY-Terminal kann sich via "tmux attach-session -t $AGENT_NAME" dran haengen
# Container bleibt am Leben via "tail -f /dev/null" als PID 1

SESSION="${AGENT_NAME:-agent}"

# poll.sh in detached tmux-Session starten (sh explizit — Agent-User hat /sbin/nologin)
tmux new-session -d -s "$SESSION" "sh /home/agent/poll.sh"

# Container am Leben halten (PID 1 bleibt stabil)
exec tail -f /dev/null
