/**
 * Quick-capture UI for fast note entry.
 * - Cmd/Ctrl+Enter submits the form
 * - POST to /api/notes with auto-classification
 * - Shows toast on success, auto-hides after 2s
 * - Shows error toast on failure
 * - Auto-clears textarea after submit
 */

const form = document.getElementById("quick-capture-form");
const textarea = document.getElementById("note-input");
const toast = document.getElementById("toast");
const errorToast = document.getElementById("error-toast");
const errorMessage = document.getElementById("error-message");

/**
 * Show a toast notification
 */
function showToast(element, duration = 2000) {
    element.classList.remove("hidden");
    element.classList.add("flex");
    
    if (duration > 0) {
        setTimeout(() => {
            element.classList.add("hidden");
            element.classList.remove("flex");
        }, duration);
    }
}

/**
 * Hide a toast notification
 */
function hideToast(element) {
    element.classList.add("hidden");
    element.classList.remove("flex");
}

/**
 * Get API key from localStorage or prompt user
 */
function getApiKey() {
    let apiKey = form.dataset.apiKey || localStorage.getItem("para_api_key");
    if (!apiKey) {
        apiKey = prompt("Enter your PARA API key:");
        if (apiKey) {
            localStorage.setItem("para_api_key", apiKey);
        }
    }
    return apiKey;
}

/**
 * Submit note via /api/notes endpoint
 */
async function submitNote(content) {
    if (!content.trim()) {
        errorMessage.textContent = "Note cannot be empty";
        showToast(errorToast);
        return;
    }

    const apiKey = getApiKey();
    if (!apiKey) {
        errorMessage.textContent = "API key required";
        showToast(errorToast);
        return;
    }

    try {
        const response = await fetch("/api/notes", {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "Authorization": `Bearer ${apiKey}`,
            },
            body: JSON.stringify({
                title: content.substring(0, 100), // Use first 100 chars as title
                content: content,
                source: "quick-capture",
                auto_classify: true,
            }),
        });

        if (!response.ok) {
            const error = await response.json().catch(() => ({}));
            throw new Error(error.detail || `HTTP ${response.status}`);
        }

        // Success!
        textarea.value = "";
        textarea.focus();
        showToast(toast, 2000);
        hideToast(errorToast);
    } catch (error) {
        errorMessage.textContent = `Error: ${error.message}`;
        showToast(errorToast, 2000);
    }
}

/**
 * Handle form submission via button click
 */
form.addEventListener("submit", (event) => {
    event.preventDefault();
    const content = textarea.value;
    submitNote(content);
});

/**
 * Handle Cmd/Ctrl+Enter keyboard shortcut
 */
textarea.addEventListener("keydown", (event) => {
    const isMac = /Mac|iPhone|iPad|iPod/.test(navigator.platform);
    const isSubmitKey = isMac ? event.metaKey : event.ctrlKey;
    
    if (isSubmitKey && event.key === "Enter") {
        event.preventDefault();
        const content = textarea.value;
        submitNote(content);
    }
});

/**
 * Focus textarea on page load
 */
document.addEventListener("DOMContentLoaded", () => {
    textarea.focus();
});
