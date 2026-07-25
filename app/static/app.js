// Lightweight UI helpers for HTMX navigation and the PARA kanban board.

function allowDrop(event) {
    event.preventDefault();
    const dropZone = event.currentTarget.querySelector("[data-drop-zone]");
    dropZone?.classList.add("bg-emerald-50/50");
}

function leaveDropZone(event) {
    if (event.currentTarget.contains(event.relatedTarget)) return;
    const dropZone = event.currentTarget.querySelector("[data-drop-zone]");
    dropZone?.classList.remove("bg-emerald-50/50");
}

function dragNote(event) {
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("noteId", event.currentTarget.dataset.id);
    event.currentTarget.classList.add("opacity-50");
}

function endDrag(event) {
    event.currentTarget.classList.remove("opacity-50");
    document.querySelectorAll("[data-drop-zone]").forEach((zone) => {
        zone.classList.remove("bg-emerald-50/50");
    });
}

async function dropNote(event, category) {
    event.preventDefault();
    const noteId = event.dataTransfer.getData("noteId");
    const dropZone = event.currentTarget.querySelector("[data-drop-zone]");
    dropZone?.classList.remove("bg-emerald-50/50");
    if (!noteId) return;

    try {
        const response = await fetch(`/api/notes/${noteId}/move`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ para_category: category }),
        });
        if (!response.ok) throw new Error("Unable to move note");
        htmx.trigger(document.body, "refresh");
    } catch (error) {
        window.alert(error.message);
    }
}

function initializeNavigation() {
    const toggle = document.querySelector("[data-nav-toggle]");
    const menu = document.querySelector("[data-nav-menu]");
    if (!toggle || !menu) return;

    toggle.addEventListener("click", () => {
        const willOpen = menu.classList.contains("hidden");
        menu.classList.toggle("hidden");
        menu.classList.toggle("flex", willOpen);
        menu.classList.toggle("flex-col", willOpen);
        toggle.setAttribute("aria-expanded", String(willOpen));
    });
}

function createDeadlineRow(deadline) {
    const row = document.createElement("a");
    row.href = `/notes/${deadline.id}`;
    row.className = "grid gap-2 px-5 py-4 transition hover:bg-slate-50 sm:grid-cols-[1fr_auto_auto] sm:items-center sm:gap-6";

    const title = document.createElement("span");
    title.className = "text-sm font-medium text-slate-800";
    title.textContent = deadline.title;

    const date = document.createElement("span");
    date.className = "text-xs text-slate-500";
    date.textContent = deadline.deadline;

    const days = document.createElement("span");
    days.className = "w-fit rounded-md bg-amber-50 px-2 py-1 text-xs font-medium text-amber-700";
    days.textContent = deadline.days_left === 0 ? "Due today" : `${deadline.days_left} days left`;

    row.append(title, date, days);
    return row;
}

async function loadDeadlines() {
    const container = document.querySelector("[data-deadline-list]");
    if (!container) return;

    try {
        const response = await fetch("/api/deadlines?days=14");
        if (!response.ok) throw new Error("Unable to load deadlines");
        const data = await response.json();
        container.replaceChildren();

        if (!data.deadlines.length) {
            const empty = document.createElement("p");
            empty.className = "px-5 py-8 text-center text-sm text-slate-400";
            empty.textContent = "No deadlines in the next 14 days.";
            container.append(empty);
            return;
        }

        const list = document.createElement("div");
        list.className = "divide-y divide-slate-100";
        data.deadlines.forEach((deadline) => list.append(createDeadlineRow(deadline)));
        container.append(list);
    } catch (error) {
        const message = document.createElement("p");
        message.className = "px-5 py-8 text-center text-sm text-red-500";
        message.textContent = error.message;
        container.replaceChildren(message);
    }
}

document.addEventListener("DOMContentLoaded", () => {
    initializeNavigation();
    loadDeadlines();
});
