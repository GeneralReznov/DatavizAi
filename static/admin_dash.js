/**
 * admin_dash.js — Admin dashboard JS for DataVizAI.
 * Fetches and renders user list, file list, and handles role/status updates.
 */

document.addEventListener("DOMContentLoaded", () => {
  fetchCurrentUser();
  fetchUsers();
  fetchFiles(1);
});

// ── Toast helper ─────────────────────────────────────────────────────────────

function showToast(msg, success = true) {
  const el   = document.getElementById("action-toast");
  const msgEl = document.getElementById("toast-msg");
  if (!el || !msgEl) return;
  msgEl.textContent = msg;
  el.className = `toast align-items-center border-0 text-white ${success ? "bg-success" : "bg-danger"}`;
  const t = new bootstrap.Toast(el, { delay: 3500 });
  t.show();
}

// ── Current user (populate header) ───────────────────────────────────────────

async function fetchCurrentUser() {
  try {
    const res  = await fetch("/me");
    if (!res.ok) return;
    const data = await res.json();
    const el   = document.getElementById("header-username");
    if (el) el.textContent = data.name || data.email;
  } catch (_) { /* ignore */ }
}

// ── Users ─────────────────────────────────────────────────────────────────────

async function fetchUsers() {
  const tbody = document.getElementById("users-tbody");
  tbody.innerHTML = `<tr><td colspan="8" class="text-center text-muted py-4">
    <span class="spinner-border spinner-border-sm me-2"></span>Loading…</td></tr>`;

  let users = [];
  try {
    const res = await fetch("/admin/users");
    if (!res.ok) throw new Error("Failed");
    users = await res.json();
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="8" class="text-center text-danger py-3">
      Could not load users.</td></tr>`;
    return;
  }

  // Update stat boxes
  const total      = users.length;
  const active     = users.filter(u => u.status === "ACTIVE").length;
  const unverified = users.filter(u => u.status === "UNVERIFIED").length;
  _setStat("stat-total",      total);
  _setStat("stat-active",     active);
  _setStat("stat-unverified", unverified);

  if (!users.length) {
    tbody.innerHTML = `<tr><td colspan="8" class="text-center text-muted py-4">No users found.</td></tr>`;
    return;
  }

  tbody.innerHTML = users.map(u => {
    const roleBadge   = `<span class="badge badge-role-${u.role}">${u.role}</span>`;
    const statusBadge = `<span class="badge badge-status-${u.status.toLowerCase()}">${u.status}</span>`;
    const joined      = new Date(u.created_at).toLocaleDateString("en-IN", { year:"numeric", month:"short", day:"numeric" });

    const roleToggle = u.role === "admin"
      ? `<button class="btn btn-sm btn-outline-secondary" onclick="changeRole(${u.id},'user')">→ User</button>`
      : `<button class="btn btn-sm btn-outline-primary"   onclick="changeRole(${u.id},'admin')">→ Admin</button>`;

    const statusToggle = u.status === "DISABLED"
      ? `<button class="btn btn-sm btn-outline-success" onclick="changeStatus(${u.id},'ACTIVE')">Enable</button>`
      : (u.status === "ACTIVE"
          ? `<button class="btn btn-sm btn-outline-danger" onclick="changeStatus(${u.id},'DISABLED')">Disable</button>`
          : "");

    return `
      <tr id="user-row-${u.id}">
        <td>${u.id}</td>
        <td><strong>${_esc(u.name)}</strong></td>
        <td>${_esc(u.email)}</td>
        <td>${_esc(u.phone)}</td>
        <td>${roleBadge}</td>
        <td>${statusBadge}</td>
        <td>${joined}</td>
        <td><div class="action-btns">${roleToggle}${statusToggle}</div></td>
      </tr>`;
  }).join("");
}

async function changeRole(userId, newRole) {
  try {
    const res  = await fetch(`/admin/users/${userId}/role`, {
      method:  "PATCH",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ role: newRole }),
    });
    const data = await res.json();
    if (res.ok) {
      showToast(`Role updated to '${data.role}'.`);
      fetchUsers();
    } else {
      showToast(data.error || "Could not update role.", false);
    }
  } catch (e) {
    showToast("Network error.", false);
  }
}

async function changeStatus(userId, newStatus) {
  try {
    const res  = await fetch(`/admin/users/${userId}/status`, {
      method:  "PATCH",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ status: newStatus }),
    });
    const data = await res.json();
    if (res.ok) {
      showToast(`Account ${data.status === "ACTIVE" ? "enabled" : "disabled"}.`);
      fetchUsers();
    } else {
      showToast(data.error || "Could not update status.", false);
    }
  } catch (e) {
    showToast("Network error.", false);
  }
}

// ── Files ─────────────────────────────────────────────────────────────────────

async function fetchFiles(page = 1) {
  const tbody      = document.getElementById("files-tbody");
  const pagination = document.getElementById("files-pagination");
  tbody.innerHTML  = `<tr><td colspan="5" class="text-center text-muted py-4">
    <span class="spinner-border spinner-border-sm me-2"></span>Loading…</td></tr>`;

  let data;
  try {
    const res = await fetch(`/admin/files?page=${page}&per_page=10`);
    if (!res.ok) throw new Error("Failed");
    data = await res.json();
  } catch (e) {
    tbody.innerHTML = `<tr><td colspan="5" class="text-center text-danger py-3">
      Could not load files.</td></tr>`;
    return;
  }

  _setStat("stat-files", data.total);

  if (!data.files.length) {
    tbody.innerHTML = `<tr><td colspan="5" class="text-center text-muted py-4">No files found.</td></tr>`;
    pagination.innerHTML = "";
    return;
  }

  tbody.innerHTML = data.files.map(f => {
    const kb   = (f.file_size / 1024).toFixed(1);
    const date = new Date(f.upload_time).toLocaleString("en-IN");
    return `
      <tr>
        <td>${f.id}</td>
        <td><code style="font-size:0.82rem">${_esc(f.filename)}</code></td>
        <td style="font-size:0.82rem">${date}</td>
        <td>${kb} KB</td>
        <td><span class="badge bg-secondary">${f.status}</span></td>
      </tr>`;
  }).join("");

  // Pagination
  pagination.innerHTML = "";
  if (data.pages > 1) {
    for (let i = 1; i <= data.pages; i++) {
      const li = document.createElement("li");
      li.className = `page-item${i === data.current_page ? " active" : ""}`;
      const a = document.createElement("a");
      a.className   = "page-link";
      a.href        = "#";
      a.textContent = i;
      a.addEventListener("click", e => { e.preventDefault(); fetchFiles(i); });
      li.appendChild(a);
      pagination.appendChild(li);
    }
  }
}

// ── Utilities ─────────────────────────────────────────────────────────────────

function _esc(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function _setStat(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}
