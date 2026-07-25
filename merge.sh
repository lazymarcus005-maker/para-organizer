#!/bin/bash
# Merge all 3 phase branches into main
# Run this AFTER all 3 agents complete their work

set -e
cd ~/workspace/PARA-organizer

echo "═══════════════════════════════════════════════════════════════"
echo "  PARA Organizer — Merge All Phases"
echo "═══════════════════════════════════════════════════════════════"
echo ""

# Check branches exist
for b in phase1-core phase2-mcp phase3-telegram; do
    if ! git show-ref --verify --quiet refs/heads/$b; then
        echo "❌ Branch $b not found!"
        exit 1
    fi
done

echo "Switching to main..."
git checkout main
echo ""

echo "Merging phase1-core (Core + Web UI)..."
git merge phase1-core --no-ff -m "Merge phase1: Core backend + Web UI (Claude Code)" || {
    echo ""
    echo "⚠️  Conflict detected! Resolving..."
    # Merge both sides for requirements.txt and main.py
    git checkout --theirs requirements.txt 2>/dev/null || true
    git checkout --theirs app/main.py 2>/dev/null || true
    git add -A
    git commit -m "Merge phase1: Core backend + Web UI (resolved conflicts)"
}
echo "  ✅ phase1 merged"
echo ""

echo "Merging phase2-mcp (MCP Server)..."
git merge phase2-mcp --no-ff -m "Merge phase2: MCP server + Hermes integration (OpenCode)" || {
    echo ""
    echo "⚠️  Conflict detected! Resolving..."
    git add -A
    git commit -m "Merge phase2: MCP server (resolved conflicts)"
}
echo "  ✅ phase2 merged"
echo ""

echo "Merging phase3-telegram (Telegram + Scheduler)..."
git merge phase3-telegram --no-ff -m "Merge phase3: Telegram bot + scheduler + notifier (Codex)" || {
    echo ""
    echo "⚠️  Conflict detected! Resolving..."
    git add -A
    git commit -m "Merge phase3: Telegram + scheduler (resolved conflicts)"
}
echo "  ✅ phase3 merged"
echo ""

echo "═══════════════════════════════════════════════════════════════"
echo "✅ All phases merged to main!"
echo ""
echo "Next steps:"
echo "  1. pip install -r requirements.txt"
echo "  2. python3 scripts/init_db.py"
echo "  3. python3 scripts/seed.py"
echo "  4. python3 -m pytest tests/ -v"
echo "  5. python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8731"
echo "  6. Open http://localhost:8731/"
echo "  7. git push origin main"
echo "═══════════════════════════════════════════════════════════════"