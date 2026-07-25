// HTMX + native drag-and-drop helpers for the PARA kanban board.

function allowDrop(ev) {
    ev.preventDefault();
}

function dragNote(ev) {
    ev.dataTransfer.setData("noteId", ev.currentTarget.dataset.id);
}

async function dropNote(ev, category) {
    ev.preventDefault();
    const noteId = ev.dataTransfer.getData("noteId");
    if (!noteId) return;

    await fetch(`/api/notes/${noteId}/move`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ para_category: category }),
    });

    htmx.trigger(document.body, "refresh");
}
