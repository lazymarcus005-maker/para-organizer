#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# PARA Organizer — Start 3 Agents in Parallel
# Run this script tomorrow to kick off all 3 coding agents
# ═══════════════════════════════════════════════════════════════

set -e
cd ~/workspace/PARA-organizer

echo "🧠 PARA Organizer — Starting 3 agents..."
echo ""

# ─── Agent 1: Claude Code (opus-4.8) — Phase 1: Core + Web UI ───
echo "1/3  Starting Claude Code → phase1-core..."
tmux new-session -d -s para-claude -x 200 -y 50
tmux send-keys -t para-claude 'cd ~/workspace/PARA-organizer && git checkout phase1-core && claude --dangerously-skip-permissions' Enter
sleep 8
# Trust dialog → press Enter (default: Yes)
tmux send-keys -t para-claude Enter
sleep 4
# Bypass permissions → Down then Enter (default is No, need Yes)
tmux send-keys -t para-claude Down
sleep 0.5
tmux send-keys -t para-claude Enter
sleep 5
# Send task
tmux send-keys -t para-claude 'Read spec.md thoroughly, then implement Phase 1 — Core Backend + Web UI. Follow spec.md sections 1-17 exactly. Create all files listed in spec section 12 project structure. Work on branch phase1-core. Run init_db.py and start server to verify. Commit when done with message "Phase 1: Core backend + Web UI".' Enter
echo "   ✅ Claude Code started (tmux: para-claude)"
echo ""

# ─── Agent 2: OpenCode (qwen3.8-preview) — Phase 2: MCP Server ───
echo "2/3  Starting OpenCode → phase2-mcp..."
tmux new-session -d -s para-opencode -x 200 -y 50
tmux send-keys -t para-opencode 'cd ~/workspace/PARA-organizer && git checkout phase2-mcp && opencode' Enter
sleep 6
tmux send-keys -t para-opencode 'Read spec.md thoroughly, then implement Phase 2 — MCP Server for Hermes. Follow spec.md section 8 exactly. Create app/mcp/mcp_server.py with 10 MCP tools (para_add_note, para_search, para_list, para_get, para_move, para_archive, para_stats, para_deadlines, para_digest, para_add_link). Use stdio transport. Create tests/test_mcp.py. Work on branch phase2-mcp. Commit when done with message "Phase 2: MCP server + Hermes integration".' Enter
echo "   ✅ OpenCode started (tmux: para-opencode)"
echo ""

# ─── Agent 3: Codex (gpt5.6-sole) — Phase 3: Telegram + Scheduler ───
echo "3/3  Starting Codex → phase3-telegram..."
tmux new-session -d -s para-codex -x 200 -y 50
tmux send-keys -t para-codex 'cd ~/workspace/PARA-organizer && git checkout phase3-telegram && codex' Enter
sleep 6
tmux send-keys -t para-codex 'Read spec.md thoroughly, then implement Phase 3 — Telegram Bot + Scheduler + Notifier + Cron Webhook. Follow spec.md sections 9-10 exactly. Create app/integrations/telegram_bot.py, app/routes/telegram_webhook.py, app/routes/cron_webhook.py, app/scheduler.py, app/notifier.py, tests/test_telegram.py, tests/test_scheduler.py, tests/conftest.py. Work on branch phase3-telegram. Commit when done with message "Phase 3: Telegram bot + scheduler + notifier + cron webhook".' Enter
echo "   ✅ Codex started (tmux: para-codex)"
echo ""

echo "═══════════════════════════════════════════════════════════════"
echo "🚀 All 3 agents are running!"
echo ""
echo "Monitor:"
echo "  ./monitor.sh          # check all 3"
echo "  tmux attach -t para-claude      # watch Claude Code"
echo "  tmux attach -t para-opencode    # watch OpenCode"
echo "  tmux attach -t para-codex       # watch Codex"
echo ""
echo "Stop all:"
echo "  ./stop-all.sh"
echo ""
echo "Merge when done:"
echo "  ./merge.sh"
echo "═══════════════════════════════════════════════════════════════"