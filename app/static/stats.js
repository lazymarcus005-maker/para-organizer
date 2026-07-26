/**
 * Statistics and LLM usage dashboard.
 * Fetches usage data from /api/usage and renders charts via Chart.js
 */

let modelPieChart = null;
let taskBarChart = null;
let trendLineChart = null;

async function loadUsageData() {
    try {
        const response = await fetch('/api/usage?days=7');
        if (!response.ok) throw new Error('Failed to load usage data');
        
        const data = await response.json();
        updateKPICards(data);
        renderModelPieChart(data);
        renderTaskBarChart(data);
        renderTrendLineChart(data);
        
    } catch (error) {
        console.error('Error loading usage data:', error);
        showUsageError(error.message);
    }
}

function updateKPICards(data) {
    // Update token counts
    const promptTokens = data.total_prompt_tokens || 0;
    const completionTokens = data.total_completion_tokens || 0;
    const totalTokens = promptTokens + completionTokens;
    
    document.querySelector('[data-tokens="prompt"]').textContent = formatNumber(promptTokens);
    document.querySelector('[data-tokens="completion"]').textContent = formatNumber(completionTokens);
    document.querySelector('[data-tokens="total"]').textContent = formatNumber(totalTokens);
    
    // Estimate cost based on common model pricing
    // Rough average: ~$0.0001 per prompt token, ~$0.0003 per completion token
    const estimatedCost = (promptTokens * 0.0001 + completionTokens * 0.0003) / 1000;
    document.querySelector('[data-cost]').textContent = `$${estimatedCost.toFixed(2)}`;
}

function formatNumber(num) {
    if (num >= 1_000_000) {
        return (num / 1_000_000).toFixed(1) + 'M';
    } else if (num >= 1_000) {
        return (num / 1_000).toFixed(1) + 'K';
    }
    return num.toString();
}

function renderModelPieChart(data) {
    const ctx = document.getElementById('modelPieChart');
    if (!ctx) return;
    
    const modelData = data.by_model || {};
    const labels = Object.keys(modelData);
    const totalTokens = labels.map(m => modelData[m].prompt_tokens + modelData[m].completion_tokens);
    
    // Color palette
    const colors = [
        '#10b981', '#3b82f6', '#f59e0b', '#ef4444', '#8b5cf6',
        '#ec4899', '#14b8a6', '#f97316', '#06b6d4', '#84cc16'
    ];
    
    if (modelPieChart) {
        modelPieChart.destroy();
    }
    
    modelPieChart = new Chart(ctx, {
        type: 'doughnut',
        data: {
            labels: labels.length > 0 ? labels : ['No data'],
            datasets: [{
                data: totalTokens.length > 0 ? totalTokens : [1],
                backgroundColor: labels.length > 0 ? colors.slice(0, labels.length) : ['#e2e8f0'],
                borderColor: '#ffffff',
                borderWidth: 2,
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    position: 'bottom',
                    labels: {
                        font: { size: 11 },
                        padding: 12,
                        color: '#64748b'
                    }
                }
            }
        }
    });
}

function renderTaskBarChart(data) {
    const ctx = document.getElementById('taskBarChart');
    if (!ctx) return;
    
    const taskData = data.by_task || {};
    const labels = Object.keys(taskData);
    const promptTokens = labels.map(t => taskData[t].prompt_tokens);
    const completionTokens = labels.map(t => taskData[t].completion_tokens);
    
    if (taskBarChart) {
        taskBarChart.destroy();
    }
    
    taskBarChart = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: labels.length > 0 ? labels : ['No data'],
            datasets: [
                {
                    label: 'Prompt Tokens',
                    data: promptTokens,
                    backgroundColor: '#10b981',
                    borderRadius: 4,
                },
                {
                    label: 'Completion Tokens',
                    data: completionTokens,
                    backgroundColor: '#3b82f6',
                    borderRadius: 4,
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            indexAxis: 'y',
            plugins: {
                legend: {
                    labels: {
                        font: { size: 11 },
                        padding: 12,
                        color: '#64748b'
                    }
                }
            },
            scales: {
                x: {
                    stacked: false,
                    ticks: { color: '#94a3b8', font: { size: 10 } },
                    grid: { color: '#f1f5f9' }
                },
                y: {
                    ticks: { color: '#64748b', font: { size: 11 } }
                }
            }
        }
    });
}

function renderTrendLineChart(data) {
    const ctx = document.getElementById('trendLineChart');
    if (!ctx) return;
    
    const dayData = data.by_day || [];
    const labels = dayData.map(d => {
        const date = new Date(d.day);
        return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
    });
    const promptTokens = dayData.map(d => d.prompt_tokens);
    const completionTokens = dayData.map(d => d.completion_tokens);
    const totalTokens = dayData.map(d => d.prompt_tokens + d.completion_tokens);
    
    if (trendLineChart) {
        trendLineChart.destroy();
    }
    
    trendLineChart = new Chart(ctx, {
        type: 'line',
        data: {
            labels: labels.length > 0 ? labels : ['No data'],
            datasets: [
                {
                    label: 'Total Tokens',
                    data: totalTokens,
                    borderColor: '#1e293b',
                    backgroundColor: 'rgba(30, 41, 59, 0.05)',
                    tension: 0.4,
                    fill: true,
                    pointRadius: 4,
                    pointBackgroundColor: '#10b981',
                    pointBorderColor: '#ffffff',
                    pointBorderWidth: 2,
                },
                {
                    label: 'Prompt Tokens',
                    data: promptTokens,
                    borderColor: '#10b981',
                    backgroundColor: 'transparent',
                    tension: 0.4,
                    fill: false,
                    pointRadius: 3,
                    pointBackgroundColor: '#10b981',
                    borderDash: [5, 5],
                },
                {
                    label: 'Completion Tokens',
                    data: completionTokens,
                    borderColor: '#3b82f6',
                    backgroundColor: 'transparent',
                    tension: 0.4,
                    fill: false,
                    pointRadius: 3,
                    pointBackgroundColor: '#3b82f6',
                    borderDash: [5, 5],
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    labels: {
                        font: { size: 11 },
                        padding: 12,
                        color: '#64748b'
                    }
                }
            },
            scales: {
                x: {
                    ticks: { color: '#94a3b8', font: { size: 10 } },
                    grid: { color: '#f1f5f9' }
                },
                y: {
                    ticks: { color: '#94a3b8', font: { size: 10 } },
                    grid: { color: '#f1f5f9' }
                }
            }
        }
    });
}

function showUsageError(message) {
    const containers = [
        document.getElementById('modelPieChart'),
        document.getElementById('taskBarChart'),
        document.getElementById('trendLineChart')
    ];
    
    containers.forEach(container => {
        if (container) {
            const parent = container.parentElement;
            parent.innerHTML = `<div class="flex items-center justify-center h-full text-red-500 text-sm">${message}</div>`;
        }
    });
}

// Load deadlines functionality (shared with index.html)
function createDeadlineRow(deadline) {
    const row = document.createElement('a');
    row.href = `/notes/${deadline.id}`;
    row.className = 'grid gap-2 px-5 py-4 transition hover:bg-slate-50 sm:grid-cols-[1fr_auto_auto] sm:items-center sm:gap-6';
    
    const title = document.createElement('span');
    title.className = 'text-sm font-medium text-slate-800';
    title.textContent = deadline.title;
    
    const date = document.createElement('span');
    date.className = 'text-xs text-slate-500';
    date.textContent = deadline.deadline;
    
    const days = document.createElement('span');
    days.className = 'w-fit rounded-md bg-amber-50 px-2 py-1 text-xs font-medium text-amber-700';
    days.textContent = deadline.days_left === 0 ? 'Due today' : `${deadline.days_left} days left`;
    
    row.append(title, date, days);
    return row;
}

async function loadDeadlines() {
    const container = document.querySelector('[data-deadline-list]');
    if (!container) return;
    
    try {
        const response = await fetch('/api/deadlines?days=14');
        if (!response.ok) throw new Error('Unable to load deadlines');
        const data = await response.json();
        container.replaceChildren();
        
        if (!data.deadlines.length) {
            const empty = document.createElement('p');
            empty.className = 'px-5 py-8 text-center text-sm text-slate-400';
            empty.textContent = 'No deadlines in the next 14 days.';
            container.append(empty);
            return;
        }
        
        const list = document.createElement('div');
        list.className = 'divide-y divide-slate-100';
        data.deadlines.forEach((deadline) => list.append(createDeadlineRow(deadline)));
        container.append(list);
    } catch (error) {
        const message = document.createElement('p');
        message.className = 'px-5 py-8 text-center text-sm text-red-500';
        message.textContent = error.message;
        container.replaceChildren(message);
    }
}

// Initialize on DOM load
document.addEventListener('DOMContentLoaded', () => {
    loadUsageData();
    loadDeadlines();
});
