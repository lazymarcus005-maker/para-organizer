#!/bin/bash
# Stop all PARA Organizer agent sessions

echo "Stopping all agent sessions..."

for s in para-claude para-opencode para-codex; do
    tmux kill-session -t $s 2>/dev/null && echo "  ✅ Killed $s" || echo "  ⏭  $s not running"
done

echo ""
echo "All stopped."