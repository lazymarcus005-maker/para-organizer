/**
 * Graph visualization for note network using vis-network.
 * Supports:
 * - Interactive nodes (zoom, pan, drag)
 * - Click node → open note detail
 * - Color nodes by PARA category
 * - Edge labels showing link type
 */

let network = null;
const categoryColors = {
    projects: '#3b82f6',    // blue
    areas: '#10b981',       // green
    resources: '#f59e0b',   // amber
    inbox: '#a855f7',       // purple
    archives: '#6b7280',    // gray
};

/**
 * Fetch graph data from API and initialize vis-network.
 */
async function initializeGraph() {
    try {
        const response = await fetch('/api/graph');
        if (!response.ok) {
            throw new Error(`Failed to load graph data: ${response.statusText}`);
        }
        
        const data = await response.json();
        renderGraph(data.nodes, data.edges);
    } catch (error) {
        console.error('Error initializing graph:', error);
        document.getElementById('node-info').innerHTML = `
            <div class="text-red-600 text-sm">Error loading graph: ${error.message}</div>
        `;
    }
}

/**
 * Render the vis-network graph.
 */
function renderGraph(nodes, edges) {
    const container = document.getElementById('graph-container');
    
    // Transform nodes with category-based colors
    const visNodes = new vis.DataSet(
        nodes.map(node => ({
            id: node.id,
            label: truncateLabel(node.label, 20),
            title: node.label,  // Tooltip on hover
            size: Math.max(node.size, 15),
            color: {
                background: categoryColors[node.category] || '#6b7280',
                border: '#1e293b',
                highlight: {
                    background: '#fbbf24',
                    border: '#1e293b',
                },
                hover: {
                    background: '#fbbf24',
                    border: '#1e293b',
                },
            },
            font: {
                size: 14,
                face: 'Inter, sans-serif',
                color: '#ffffff',
            },
            borderWidth: 2,
            borderWidthSelected: 3,
        }))
    );
    
    // Transform edges with link type labels
    const visEdges = new vis.DataSet(
        edges.map(edge => ({
            from: edge.from,
            to: edge.to,
            label: edge.link_type,
            title: `${edge.link_type}`,
            arrows: 'to',
            smooth: {
                type: 'continuous',
            },
            color: {
                color: '#cbd5e1',
                highlight: '#64748b',
                hover: '#64748b',
            },
            font: {
                size: 12,
                face: 'Inter, sans-serif',
                color: '#475569',
                background: {
                    enabled: true,
                    color: '#f1f5f9',
                },
            },
        }))
    );
    
    const options = {
        physics: {
            enabled: true,
            barnesHut: {
                gravitationalConstant: -26000,
                centralGravity: 0.005,
                springLength: 200,
                springConstant: 0.08,
            },
            maxVelocity: 50,
            solver: 'barnesHut',
            timestep: 0.35,
            stabilization: {
                iterations: 150,
            },
        },
        interaction: {
            navigationButtons: false,
            keyboard: true,
            zoomView: true,
            dragView: true,
        },
        nodes: {
            shape: 'dot',
            margin: 10,
            widthConstraint: {
                maximum: 150,
            },
        },
    };
    
    network = new vis.Network(container, { nodes: visNodes, edges: visEdges }, options);
    
    // Handle node click
    network.on('click', function(params) {
        if (params.nodes.length > 0) {
            const nodeId = params.nodes[0];
            const nodeData = visNodes.get(nodeId);
            showNodeInfo(nodeId, nodeData);
            // Navigate to note detail on click
            window.location.href = `/notes/${nodeId}`;
        }
    });
    
    // Stabilization events
    network.once('stabilizationIterationsDone', function() {
        network.setOptions({ physics: false });
        console.log('Graph stabilized');
    });
}

/**
 * Truncate label to prevent long titles from breaking the layout.
 */
function truncateLabel(label, maxLength) {
    if (label.length > maxLength) {
        return label.substring(0, maxLength) + '...';
    }
    return label;
}

/**
 * Show node info panel.
 */
function showNodeInfo(nodeId, nodeData) {
    const panel = document.getElementById('node-info');
    panel.innerHTML = `
        <div class="flex items-start justify-between">
            <div>
                <h3 class="text-lg font-semibold text-slate-900">${nodeData.label}</h3>
                <p class="mt-1 text-sm text-slate-600">Note ID: ${nodeId}</p>
            </div>
            <a href="/notes/${nodeId}" class="inline-flex items-center justify-center rounded-lg bg-emerald-500 px-4 py-2 text-sm font-medium text-slate-950 transition hover:bg-emerald-400">
                Open Note
            </a>
        </div>
    `;
    panel.classList.remove('hidden');
}

/**
 * Zoom controls.
 */
function setupControls() {
    document.getElementById('btn-zoom-in').addEventListener('click', () => {
        const scale = network.getScale();
        network.setOptions({ interaction: { zoomView: true } });
        network.zoom(scale * 1.2);
    });
    
    document.getElementById('btn-zoom-out').addEventListener('click', () => {
        const scale = network.getScale();
        network.setOptions({ interaction: { zoomView: true } });
        network.zoom(scale / 1.2);
    });
    
    document.getElementById('btn-fit').addEventListener('click', () => {
        network.fit();
    });
}

/**
 * Initialize on document ready.
 */
document.addEventListener('DOMContentLoaded', () => {
    initializeGraph();
    setupControls();
});
