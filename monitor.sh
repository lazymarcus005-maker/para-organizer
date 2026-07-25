#!/bin/bash
# Monitor all 3 PARA Organizer agents

while true; do
    clear
    echo "═══════════════════════════════════════════════════════════════"
    echo "  PARA Organizer — Agent Monitor  $(date '+%H:%M:%S')"
    echo "═══════════════════════════════════════════════════════════════"
    echo ""
    
    for s in para-claude para-opencode para-codex; do
        echo "┌─── $s ───────────────────────────────────────────────────"
        tmux capture-pane -t $s -p -S -20 2>/dev/null || echo "  [session not found]"
        echo "└──────────────────────────────────────────────────────────"
        echo ""
    done
    
    echo "Press Ctrl+C to exit. Auto-refresh in 10s."
    sleep 10
done