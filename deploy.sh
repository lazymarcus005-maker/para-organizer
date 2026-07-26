#!/bin/bash
#
# PARA Organizer — Docker Compose Deployment Script
# Usage: bash deploy.sh [up|down|logs|status|backup|restore]
#

set -e

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Print colored messages
log_info() {
    echo -e "${BLUE}ℹ${NC} $1"
}

log_success() {
    echo -e "${GREEN}✓${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}⚠${NC} $1"
}

log_error() {
    echo -e "${RED}✗${NC} $1"
}

# Check prerequisites
check_docker() {
    if ! command -v docker &> /dev/null; then
        log_error "Docker is not installed"
        exit 1
    fi
    if ! docker ps &> /dev/null; then
        log_error "Docker daemon is not running"
        exit 1
    fi
    log_success "Docker is ready"
}

# Check .env file
check_env() {
    if [ ! -f ".env" ]; then
        log_warn ".env file not found"
        log_info "Creating .env from .env.example..."
        cp .env.example .env
        log_warn "Please edit .env and set your API keys:"
        log_warn "  - OLLAMA_API_KEY"
        log_warn "  - TELEGRAM_BOT_TOKEN (optional)"
        log_warn "Then run: bash deploy.sh up"
        exit 0
    fi
    log_success ".env file exists"
}

# Deploy functions
cmd_up() {
    log_info "Starting PARA Organizer containers..."
    check_docker
    check_env
    
    docker compose up -d
    
    log_info "Waiting for services to be healthy..."
    sleep 5
    
    if docker compose ps | grep -q "para-organizer-app.*Up"; then
        log_success "Services started successfully"
        log_info "Access the application at: http://localhost:8731"
        log_info "View logs: bash deploy.sh logs"
        
        # Show status
        docker compose ps
    else
        log_error "Services failed to start"
        docker compose logs para-app
        exit 1
    fi
}

cmd_down() {
    log_info "Stopping PARA Organizer containers..."
    docker compose down
    log_success "Containers stopped"
}

cmd_logs() {
    log_info "Showing application logs (Ctrl+C to exit)..."
    docker compose logs -f para-app
}

cmd_status() {
    log_info "Container status:"
    docker compose ps
    
    log_info "\nService health:"
    if docker compose exec -T para-app curl -s http://localhost:8731/api/stats > /dev/null 2>&1; then
        log_success "API is responding"
    else
        log_error "API is not responding"
    fi
}

cmd_test() {
    log_info "Running tests..."
    check_docker
    
    # Test API
    log_info "Testing API endpoints..."
    
    if ! curl -s http://localhost:8731/api/stats | grep -q "total"; then
        log_error "Stats endpoint failed"
        return 1
    fi
    log_success "✓ Stats endpoint"
    
    # Test creating a note
    response=$(curl -s -X POST http://localhost:8731/api/notes \
        -H "Content-Type: application/json" \
        -d '{
            "title": "Test Note",
            "content": "This is a test note",
            "source": "manual"
        }')
    
    if echo "$response" | grep -q "title"; then
        log_success "✓ Create note endpoint"
    else
        log_error "Create note endpoint failed"
        return 1
    fi
    
    # Test search
    if curl -s "http://localhost:8731/api/search?q=test" | grep -q "results"; then
        log_success "✓ Search endpoint"
    else
        log_error "Search endpoint failed"
        return 1
    fi
    
    log_success "All tests passed!"
}

cmd_backup() {
    log_info "Creating database backup..."
    
    if [ ! -d "/var/lib/para-organizer/data" ]; then
        log_error "Data directory not found"
        exit 1
    fi
    
    backup_file="/var/lib/para-organizer/data/para.db.backup.$(date +%Y%m%d-%H%M%S)"
    docker compose exec -T para-app cp \
        /var/lib/para-organizer/data/para.db \
        "$backup_file"
    
    log_success "Database backed up to: $backup_file"
}

cmd_restore() {
    log_error "Restore functionality not implemented yet"
    exit 1
}

cmd_shell() {
    log_info "Opening shell to para-app container..."
    docker compose exec para-app /bin/bash
}

cmd_seed() {
    log_info "Seeding test data..."
    docker compose exec para-app python3 scripts/seed.py
    log_success "Test data seeded"
}

cmd_build() {
    log_info "Building Docker image..."
    docker compose build
    log_success "Build completed"
}

cmd_restart() {
    log_info "Restarting containers..."
    docker compose restart
    log_success "Containers restarted"
}

cmd_clean() {
    log_warn "This will remove all containers and volumes!"
    read -p "Are you sure? (y/N) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        docker compose down -v
        log_success "Cleaned up"
    else
        log_info "Cancelled"
    fi
}

# Show help
show_help() {
    cat <<EOF
PARA Organizer — Docker Deployment Script

Usage: bash deploy.sh [COMMAND]

Commands:
  up              Start containers
  down            Stop containers
  logs            View application logs
  status          Show container status & health check
  test            Run API tests
  backup          Backup database
  restore         Restore database from backup
  shell           Open bash shell in app container
  seed            Seed test data
  build           Build Docker image
  restart         Restart containers
  clean           Remove all containers and volumes (be careful!)
  help            Show this help message

Examples:
  bash deploy.sh up              # Start for the first time
  bash deploy.sh logs            # Watch logs in real-time
  bash deploy.sh status          # Check health
  bash deploy.sh test            # Test API endpoints
  bash deploy.sh backup          # Backup database

For more details, see DEPLOYMENT.md
EOF
}

# Main
case "${1:-help}" in
    up)      cmd_up ;;
    down)    cmd_down ;;
    logs)    cmd_logs ;;
    status)  cmd_status ;;
    test)    cmd_test ;;
    backup)  cmd_backup ;;
    restore) cmd_restore ;;
    shell)   cmd_shell ;;
    seed)    cmd_seed ;;
    build)   cmd_build ;;
    restart) cmd_restart ;;
    clean)   cmd_clean ;;
    help)    show_help ;;
    *)       log_error "Unknown command: $1"; show_help; exit 1 ;;
esac
