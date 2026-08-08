/**
 * Kanban board functionality — load projects and enable drag-drop across status columns.
 * Integrates with SortableJS for smooth drag-drop UX.
 */

const statusColumns = {
    active: 'Active',
    completed: 'Completed',
    archived: 'Archived'
};

function createProjectCard(note) {
    const card = document.createElement('div');
    card.className = 'group rounded-lg border border-slate-200 bg-white p-3 shadow-sm transition hover:shadow-md cursor-move active:opacity-75';
    card.draggable = true;
    card.dataset.id = note.id;
    card.dataset.status = note.status;
    
    const priorityColors = {
        urgent: 'bg-red-100 text-red-700',
        high: 'bg-orange-100 text-orange-700',
        medium: 'bg-amber-100 text-amber-700',
        low: 'bg-emerald-100 text-emerald-700'
    };
    
    const statusBadgeColors = {
        active: 'bg-emerald-100 text-emerald-700',
        completed: 'bg-blue-100 text-blue-700',
        archived: 'bg-slate-100 text-slate-700'
    };
    
    const title = document.createElement('h3');
    title.className = 'font-medium text-sm text-slate-900 mb-2 truncate';
    title.textContent = note.title;
    
    const content = document.createElement('p');
    content.className = 'text-xs text-slate-600 line-clamp-2 mb-2';
    content.textContent = note.content.substring(0, 100);
    
    const footer = document.createElement('div');
    footer.className = 'flex gap-2 items-center justify-between';
    
    const badges = document.createElement('div');
    badges.className = 'flex gap-1 flex-wrap';
    
    // Priority badge
    if (note.priority && note.priority !== 'medium') {
        const priorityBadge = document.createElement('span');
        priorityBadge.className = `inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${priorityColors[note.priority] || 'bg-slate-100 text-slate-700'}`;
        priorityBadge.textContent = note.priority;
        badges.appendChild(priorityBadge);
    }
    
    // Deadline badge
    if (note.deadline) {
        const deadlineDate = new Date(note.deadline);
        const today = new Date();
        today.setHours(0, 0, 0, 0);
        const isOverdue = deadlineDate < today;
        
        const deadlineBadge = document.createElement('span');
        deadlineBadge.className = `inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${isOverdue ? 'bg-red-100 text-red-700' : 'bg-slate-100 text-slate-700'}`;
        deadlineBadge.textContent = deadlineDate.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
        badges.appendChild(deadlineBadge);
    }
    
    footer.appendChild(badges);
    
    // Status indicator (visual feedback of current status)
    const statusIndicator = document.createElement('span');
    statusIndicator.className = `inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${statusBadgeColors[note.status] || 'bg-slate-100 text-slate-700'}`;
    statusIndicator.textContent = note.status;
    footer.appendChild(statusIndicator);
    
    card.appendChild(title);
    card.appendChild(content);
    card.appendChild(footer);
    
    return card;
}

async function loadProjects() {
    try {
        const response = await fetch('/api/notes?para_category=projects&limit=100');
        if (!response.ok) throw new Error('Failed to load projects');
        
        const data = await response.json();
        const projects = data.notes || data.results || [];
        
        // Group projects by status
        const projectsByStatus = {
            active: [],
            completed: [],
            archived: []
        };
        
        projects.forEach(project => {
            if (projectsByStatus[project.status]) {
                projectsByStatus[project.status].push(project);
            }
        });
        
        // Populate columns
        Object.keys(projectsByStatus).forEach(status => {
            const column = document.querySelector(`[data-status-column="${status}"]`);
            if (!column) return;
            
            const projectsInStatus = projectsByStatus[status];
            
            if (projectsInStatus.length === 0) {
                const empty = document.createElement('div');
                empty.className = 'flex min-h-24 items-center justify-center rounded-lg border border-dashed border-slate-200 bg-slate-50/50';
                empty.innerHTML = '<p class="text-sm text-slate-400">No projects</p>';
                column.appendChild(empty);
            } else {
                projectsInStatus.forEach(project => {
                    column.appendChild(createProjectCard(project));
                });
            }
        });
        
        initializeSortables();
    } catch (error) {
        console.error('Error loading projects:', error);
        const columns = document.querySelectorAll('[data-drop-zone]');
        columns.forEach(column => {
            const error = document.createElement('div');
            error.className = 'flex items-center justify-center text-red-500 text-sm p-4';
            error.textContent = 'Failed to load projects';
            column.appendChild(error);
        });
    }
}

function initializeSortables() {
    const columns = document.querySelectorAll('[data-drop-zone]');
    
    columns.forEach(column => {
        new Sortable(column, {
            group: 'projects',
            animation: 150,
            ghostClass: 'opacity-50',
            dragClass: 'drag-active',
            touchStartThreshold: 5,
            fallbackOnBody: true,
            
            onEnd: async function(evt) {
                const card = evt.item;
                const noteId = card.dataset.id;
                const newStatus = evt.to.dataset.statusColumn;
                const oldStatus = evt.from.dataset.statusColumn;
                
                if (oldStatus === newStatus) {
                    return;
                }
                
                card.dataset.status = newStatus;
                
                try {
                    const response = await fetch(`/api/notes/${noteId}`, {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ status: newStatus })
                    });
                    
                    if (!response.ok) {
                        throw new Error(`Failed to update note status: ${response.statusText}`);
                    }
                    
                    // Smooth animation feedback
                    card.style.transition = 'all 0.3s ease';
                    setTimeout(() => {
                        card.style.transition = '';
                    }, 300);
                    
                } catch (error) {
                    console.error('Error updating note status:', error);
                    // Revert the move
                    evt.from.insertBefore(card, evt.oldIndex < evt.newIndex ? evt.from.children[evt.oldIndex] : evt.from.children[evt.oldIndex - 1] || null);
                    card.dataset.status = oldStatus;
                    alert(`Error: ${error.message}`);
                }
            }
        });
    });
}

// Load projects when DOM is ready
document.addEventListener('DOMContentLoaded', loadProjects);
