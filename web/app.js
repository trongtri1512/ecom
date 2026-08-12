/* Scan Ecom — web app: gọi API, render bảng KPI, realtime SSE. */

// API base: rỗng nghĩa là cùng origin (khi backend phục vụ luôn UI).
// Khi chạy web tách riêng, đặt window.API_BASE trong 1 thẻ <script> trước file này.
const API = (window.API_BASE || "").replace(/\/$/, "");

const $ = (sel) => document.querySelector(sel);
const rowsEl = $("#rows");
const kpisEl = $("#kpis");
const emptyEl = $("#empty");
const searchEl = $("#search");
const filterEl = $("#filterCarrier");
const periodEl = $("#filterPeriod");

let carrierOrder = [];
let debounceTimer = null;

/* ------------------------- Helpers ------------------------- */
function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString("vi-VN", { hour12: false });
}

/* Tabs */
document.querySelectorAll(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".tab-content").forEach(c => c.classList.remove("active"));
    btn.classList.add("active");
    const target = document.getElementById(btn.dataset.tab);
    if (target) target.classList.add("active");
    if (btn.dataset.tab === "tab-sessions") {
      loadSessions();
    }
  });
});

/* Load OPS Sessions */
async function loadSessions() {
  const tbody = $("#sessionRows");
  const empty = $("#emptySessions");
  try {
    const res = await api("/api/ops/logs?limit=50");
    const items = res.items || [];
    // Chỉ lấy các log tạo phiên (auto_import hoặc manual_import)
    const sessions = items.filter(i => i.action.includes("import") && i.session_id);
    
    if (!sessions.length) {
      empty.hidden = false;
      tbody.innerHTML = "";
      return;
    }
    empty.hidden = true;
    tbody.innerHTML = sessions.map((s, idx) => `
      <tr>
        <td>${idx + 1}</td>
        <td>${s.carrier}</td>
        <td style="color:var(--primary);font-weight:600;">${s.session_id}</td>
        <td>Phiên giao</td>
        <td>${s.count}</td>
        <td>${s.count}</td>
        <td>
          <span style="background:${s.level === 'success' ? '#22c55e' : '#ef4444'};color:#fff;padding:2px 8px;border-radius:12px;font-size:11px;">
            ${s.level === 'success' ? 'Thành công' : 'Lỗi'}
          </span>
        </td>
        <td>Hệ thống</td>
        <td>${fmtTime(s.created_at)}</td>
        <td>
          <button class="icon-btn btn-del-session" data-id="${s.session_id}" title="Xóa phiên này">🗑️</button>
        </td>
      </tr>
    `).join("");
  } catch (err) {
    empty.hidden = false;
    empty.textContent = String(err);
  }
}

// Xử lý nút xóa phiên
$("#sessionRows").addEventListener("click", async (e) => {
  const btn = e.target.closest(".btn-del-session");
  if (!btn) return;
  const sid = btn.dataset.id;
  if (!confirm(`Xóa phiên ${sid} khỏi hệ thống? Các đơn hàng sẽ trở về trạng thái chờ đẩy lên OPS.`)) return;
  try {
    await api(`/api/sessions/${sid}`, { method: "DELETE" });
    toast("Đã xóa", `Phiên ${sid} đã được gỡ`, "ok");
    loadSessions();
  } catch (err) {
    toast("Lỗi", String(err), "danger");
  }
});

if ($("#btnRefreshSessions")) {
  $("#btnRefreshSessions").addEventListener("click", loadSessions);
}

function toast(title, msg, kind = "") {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.innerHTML = `<div class="t-title"></div><div class="t-msg"></div>`;
  el.querySelector(".t-title").textContent = title;
  el.querySelector(".t-msg").textContent = msg || "";
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), 4500);
}

async function api(path, opts = {}) {
  const res = await fetch(API + path, opts);
  if (!res.ok) throw new Error((await res.text()) || res.status);
  const ct = res.headers.get("content-type") || "";
  return ct.includes("application/json") ? res.json() : res;
}

/* period hiện đang chọn ('all' -> không gửi tham số) */
function currentPeriod() {
  return periodEl && periodEl.value && periodEl.value !== "all" ? periodEl.value : "";
}

/* ------------------------- KPI ------------------------- */
async function loadSummary() {
  const p = currentPeriod();
  const s = await api("/api/summary" + (p ? "?period=" + p : ""));
  carrierOrder = s.carrier_order || Object.keys(s.by_carrier);

  // Cập nhật dropdown filter (giữ lựa chọn hiện tại).
  const cur = filterEl.value;
  filterEl.innerHTML = '<option value="">— Tất cả ĐVVC —</option>' +
    carrierOrder.map((c) => `<option value="${c}">${c}</option>`).join("");
  filterEl.value = cur;

  // Vẽ thẻ KPI. Nhãn Total kèm kỳ đang xem.
  const periodLabel = periodEl && periodEl.selectedOptions[0] ? periodEl.selectedOptions[0].textContent : "";
  const totalLabel = currentPeriod() ? `Tổng — ${periodLabel}` : "Tổng mã đã quét";
  let html = `<div class="kpi total"><div class="label">${escapeHtml(totalLabel)}</div><div class="value">${s.total}</div></div>`;
  for (const c of carrierOrder) {
    const n = s.by_carrier[c] || 0;
    // Ẩn thẻ =0 (CSS .kpi.zero { display: none }) để KPI gọn 1 hàng.
    const cls = n === 0 ? "kpi zero" : "kpi";
    html += `<div class="${cls}"><div class="label">${c}</div><div class="value">${n}</div></div>`;
  }
  kpisEl.innerHTML = html;
}

/* ------------------------- Bảng ------------------------- */
function rowHtml(r, idx) {
  const isOther = r.carrier === "Other";
  const dup = r.dup_count || 0;
  // Dòng bị quét trùng: tô nền cảnh báo + badge số lần trùng cạnh mã.
  const dupBadge = dup > 0
    ? ` <span class="dup-badge" title="Đã bị quét trùng ${dup} lần (gần nhất: ${fmtTime(r.last_dup_at)})">⚠ trùng ×${dup}</span>`
    : "";
  return `<tr data-id="${r.id}" class="${dup > 0 ? "row-dup" : ""}">
    <td class="col-check"><input type="checkbox" class="row-check" /></td>
    <td>${idx}</td>
    <td class="code">${escapeHtml(r.code)}${dupBadge}</td>
    <td><span class="badge ${isOther ? "other" : ""}">${escapeHtml(r.carrier)}</span></td>
    <td>${locationCell(r)}</td>
    <td>${fmtTime(r.scanned_at)}</td>
    <td>${escapeHtml(r.source_agent || "")}</td>
    <td class="row-actions"><button class="icon-btn btn-edit" title="Sửa mã">✏️</button><button class="icon-btn btn-del" title="Xoá">🗑️</button></td>
  </tr>`;
}

/* Ô Vị trí: hiện "Sọt N" nếu đã vào sọt, "—" nếu chưa. */
function locationCell(r) {
  if (r.basket_seq) {
    return `<span class="loc-badge loc-in" title="Đã vào Sọt ${r.basket_seq}">📦 Sọt ${r.basket_seq}</span>`;
  }
  return `<span class="loc-badge loc-out" title="Chưa vào sọt nào">— Chưa</span>`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function escapeAttr(s) { return escapeHtml(s); }

async function loadRows() {
  const params = new URLSearchParams();
  if (searchEl.value.trim()) params.set("q", searchEl.value.trim());
  if (filterEl.value) params.set("carrier", filterEl.value);
  if (currentPeriod()) params.set("period", currentPeriod());
  params.set("limit", "1000");
  const data = await api("/api/scans?" + params.toString());
  const items = data.items || [];
  emptyEl.hidden = items.length > 0;
  rowsEl.innerHTML = items.map((r, i) => rowHtml(r, i + 1)).join("");
  // reset trạng thái chọn sau mỗi lần tải lại danh sách
  const ca = $("#checkAll"); if (ca) ca.checked = false;
  updateBulkBar();
}

/* ------------------------- Chọn nhiều dòng / xóa hàng loạt ------------------------- */
function selectedIds() {
  return [...rowsEl.querySelectorAll(".row-check:checked")]
    .map((c) => c.closest("tr").dataset.id);
}

function updateBulkBar() {
  const n = selectedIds().length;
  const bar = $("#bulkbar");
  bar.hidden = n === 0;
  if (n > 0) $("#bulkCount").textContent = `Đã chọn ${n}`;
  // đồng bộ trạng thái checkbox "chọn tất cả"
  const all = [...rowsEl.querySelectorAll(".row-check")];
  const ca = $("#checkAll");
  if (ca) ca.checked = all.length > 0 && all.every((c) => c.checked);
}

// tích/bỏ tích 1 dòng
rowsEl.addEventListener("change", (e) => {
  if (e.target.classList.contains("row-check")) updateBulkBar();
});

// chọn tất cả
$("#checkAll").addEventListener("change", (e) => {
  rowsEl.querySelectorAll(".row-check").forEach((c) => { c.checked = e.target.checked; });
  updateBulkBar();
});

// bỏ chọn
$("#btnBulkClear").addEventListener("click", () => {
  rowsEl.querySelectorAll(".row-check").forEach((c) => { c.checked = false; });
  $("#checkAll").checked = false;
  updateBulkBar();
});

// xóa hàng loạt
$("#btnBulkDelete").addEventListener("click", async () => {
  const ids = selectedIds();
  if (!ids.length) return;
  if (!confirm(`Xóa ${ids.length} mã đã chọn? Không hoàn tác được.`)) return;
  const pass = prompt("Nhập mật khẩu để xóa hàng loạt:");
  if (pass === null) return;
  try {
    const r = await api("/api/scans/bulk-delete", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Delete-Password": pass },
      body: JSON.stringify({ ids: ids.map(Number) }),
    });
    toast("Đã xóa", `${r.deleted} mã`, "warn");
    refresh();
  } catch (err) {
    if (String(err).includes("mật khẩu") || String(err).includes("403")) {
      toast("Sai mật khẩu", "Không xóa được", "danger");
    } else {
      toast("Lỗi xóa hàng loạt", String(err), "danger");
    }
  }
});

async function refresh() {
  await Promise.all([loadSummary(), loadRows()]);
}

/* ------------------------- Sự kiện bảng (sửa mã / xoá) ------------------------- */
// (Đã bỏ nút Trạng thái Đã/Chưa lấy — thay bằng cột Vị trí Sọt N, không cần click.)

// Bấm nút ✏️ Sửa mã vận đơn -> hỏi mã mới + mật khẩu.
rowsEl.addEventListener("click", async (e) => {
  const edit = e.target.closest(".btn-edit");
  if (!edit) return;
  const tr = edit.closest("tr");
  const id = tr.dataset.id;
  const codeCell = tr.querySelector(".code");
  const oldCode = (codeCell.firstChild ? codeCell.firstChild.textContent : codeCell.textContent).trim();
  const newCode = prompt("Nhập mã vận đơn mới:", oldCode);
  if (newCode === null || newCode.trim() === "" || newCode.trim() === oldCode) return;
  const pass = prompt("Nhập mật khẩu để sửa mã:");
  if (pass === null) return;
  try {
    await api(`/api/scans/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json", "X-Delete-Password": pass },
      body: JSON.stringify({ code: newCode.trim() }),
    });
    toast("Đã sửa mã", `${oldCode} → ${newCode.trim()}`, "ok");
    refresh();
  } catch (err) {
    if (String(err).includes("mật khẩu") || String(err).includes("403")) {
      toast("Sai mật khẩu", "Không sửa được", "danger");
    } else if (String(err).includes("409") || String(err).includes("tồn tại")) {
      toast("Mã đã tồn tại", `Mã ${newCode.trim()} đã có trong hệ thống`, "danger");
    } else {
      toast("Lỗi sửa mã", String(err), "danger");
    }
  }
});

rowsEl.addEventListener("click", async (e) => {
  const del = e.target.closest(".btn-del");
  if (!del) return;
  const tr = del.closest("tr");
  const id = tr.dataset.id;
  // Lấy mã sạch (bỏ badge trùng): text node đầu của ô .code.
  const codeCell = tr.querySelector(".code");
  const code = (codeCell.firstChild ? codeCell.firstChild.textContent : codeCell.textContent).trim();
  if (!confirm(`Xoá mã ${code}?`)) return;
  // Hỏi mật khẩu xoá; server sẽ kiểm (không lưu pass ở client).
  const pass = prompt("Nhập mật khẩu để xoá:");
  if (pass === null) return;  // bấm Cancel
  try {
    await api(`/api/scans/${id}`, {
      method: "DELETE",
      headers: { "X-Delete-Password": pass },
    });
    tr.remove();
    loadSummary();
    toast("Đã xoá", code, "warn");
  } catch (err) {
    // 403 = sai pass.
    if (String(err).includes("mật khẩu") || String(err).includes("403")) {
      toast("Sai mật khẩu", "Không xoá được", "danger");
    } else {
      toast("Lỗi xoá", String(err), "danger");
    }
  }
});

/* ------------------------- Toolbar ------------------------- */
searchEl.addEventListener("input", () => {
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(loadRows, 250);
});
// Đổi ĐVVC hoặc kỳ thời gian -> tải lại cả KPI (Total theo kỳ) lẫn bảng.
filterEl.addEventListener("change", refresh);
periodEl.addEventListener("change", refresh);

$("#btnXlsx").addEventListener("click", () => downloadExport("xlsx"));
function downloadExport(fmt) {
  const params = new URLSearchParams({ format: fmt });
  if (filterEl.value) params.set("carrier", filterEl.value);
  if (currentPeriod()) params.set("period", currentPeriod());
  window.location.href = API + "/api/export?" + params.toString();
}

// (Đã bỏ nút Tra trạng thái SPX — chuyển sang mô hình Sọt để quản lý vị trí mã.)

// ---- Modal xuất file import (với preview danh sách) ----
const importModal = $("#importModal");
const impCarrier = $("#impCarrier");
const impPeriod = $("#impPeriod");
const impLimit = $("#impLimit");
const impRows = $("#impRows");
const impEmpty = $("#impEmpty");
const impSummary = $("#impSummary");
const btnImportDownload = $("#btnImportDownload");

// Mở modal: đồng bộ ĐVVC từ dropdown chính, mặc định chọn ĐVVC đang lọc.
$("#btnImport").addEventListener("click", () => {
  // Cập nhật dropdown ĐVVC trong modal từ KPI data.
  const cur = filterEl.value;
  impCarrier.innerHTML = carrierOrder.map((c) =>
    `<option value="${c}" ${c === cur ? "selected" : ""}>${c}</option>`
  ).join("");
  if (!impCarrier.value && carrierOrder.length > 0) {
    impCarrier.value = carrierOrder[0];
  }
  // Đồng bộ period với dropdown chính.
  impPeriod.value = periodEl.value || "day";
  // Reset preview.
  impRows.innerHTML = "";
  impEmpty.hidden = true;
  impSummary.hidden = true;
  btnImportDownload.disabled = true;
  importModal.hidden = false;
  // Tự động preview nếu đã chọn ĐVVC.
  if (impCarrier.value) doImportPreview();
});

$("#closeImport").addEventListener("click", () => { importModal.hidden = true; });
importModal.addEventListener("click", (e) => { if (e.target === importModal) importModal.hidden = true; });

async function doImportPreview() {
  const carrier = impCarrier.value;
  if (!carrier) {
    toast("Chọn ĐVVC", "Chọn 1 ĐVVC để xem trước", "warn");
    return;
  }
  const params = new URLSearchParams({
    carrier,
    period: impPeriod.value || "day",
    limit: String(parseInt(impLimit.value, 10) || 500),
    only_picked: "false",  // đã bỏ điều kiện "đã lấy hàng" — xuất tất cả trong kỳ
  });
  try {
    const data = await api("/api/export/import-file/preview?" + params.toString());
    const items = data.items || [];
    const total = data.total || 0;
    impEmpty.hidden = items.length > 0;
    impRows.innerHTML = items.map((r, i) => {
      const locTxt = r.basket_seq
        ? `<span class="loc-badge loc-in">📦 Sọt ${r.basket_seq}</span>`
        : `<span class="loc-badge loc-out">— Chưa</span>`;
      return `<tr>
        <td>${i + 1}</td>
        <td class="code">${escapeHtml(r.code)}</td>
        <td><span class="badge">${escapeHtml(r.carrier)}</span></td>
        <td>${locTxt}</td>
        <td>${fmtTime(r.scanned_at)}</td>
      </tr>`;
    }).join("");
    // Hiện tóm tắt.
    const limitNum = parseInt(impLimit.value, 10) || 500;
    const shown = Math.min(items.length, limitNum);
    const periodLabel = impPeriod.selectedOptions[0] ? impPeriod.selectedOptions[0].textContent : "";
    impSummary.innerHTML = `<strong>${carrier}</strong> — ${periodLabel}: tổng <strong>${total}</strong> đơn. File xuất sẽ gồm <strong>${shown}</strong> đơn.`;
    impSummary.hidden = false;
    btnImportDownload.disabled = items.length === 0;
  } catch (err) {
    toast("Lỗi preview", String(err), "danger");
    impEmpty.hidden = false;
    impEmpty.textContent = "Lỗi tải dữ liệu: " + String(err);
    btnImportDownload.disabled = true;
  }
}

// Bấm preview, hoặc đổi filter -> auto preview.
$("#btnImportPreview").addEventListener("click", doImportPreview);
impCarrier.addEventListener("change", doImportPreview);
impPeriod.addEventListener("change", doImportPreview);

// Xuất file Excel.
btnImportDownload.addEventListener("click", () => {
  const carrier = impCarrier.value;
  if (!carrier) return;
  const params = new URLSearchParams({
    carrier,
    period: impPeriod.value || "day",
    limit: String(parseInt(impLimit.value, 10) || 100),
    only_picked: "false",
  });
  window.location.href = API + "/api/export/import-file?" + params.toString();
  toast("Đang tải", `File import ${carrier} đang được xuất…`, "ok");
})

$("#btnReclassify").addEventListener("click", async () => {
  if (!confirm("Chạy lại nhận diện ĐVVC cho TOÀN BỘ mã?")) return;
  try {
    const r = await api("/api/reclassify", { method: "POST" });
    toast("Phân loại lại xong", `${r.changed} mã được cập nhật`, "ok");
    refresh();
  } catch (err) {
    toast("Lỗi", String(err), "danger");
  }
});



/* ------------------------- Realtime SSE ------------------------- */
function connectStream() {
  const es = new EventSource(API + "/api/stream");
  const conn = $("#conn"), connText = $("#connText");

  es.addEventListener("ping", () => {
    conn.className = "dot dot-on";
    connText.textContent = "Realtime: đang kết nối";
  });
  es.addEventListener("scan", (e) => {
    const r = JSON.parse(e.data);
    toast("Mã mới", `${r.code} → ${r.carrier}`, "ok");
    refresh();
  });
  es.addEventListener("duplicate", (e) => {
    const r = JSON.parse(e.data);
    const lan = r.dup_count ? ` (lần ${r.dup_count})` : "";
    toast("⚠️ Mã TRÙNG", `${r.code} (${r.carrier})${lan} — đã đánh dấu + báo email`, "warn");
    refresh();  // cập nhật badge trùng trên dòng gốc
  });
  es.addEventListener("update", refresh);
  es.addEventListener("delete", refresh);
  es.addEventListener("reclassify", refresh);
  es.addEventListener("carriers", () => { if (!modal.hidden) loadCarrierRules(); refresh(); });
  es.addEventListener("auto_import", (e) => {
    const r = JSON.parse(e.data);
    toast("📤 Đã import lên OPS", `${r.carrier}: ${r.count} đơn — mã phiên: ${r.session_id}`, "ok");
    refresh();
  });
  es.addEventListener("auto_import_error", (e) => {
    const r = JSON.parse(e.data);
    toast("⚠️ Lỗi auto import OPS", `${r.carrier}: ${r.error}`, "danger");
  });

  es.onerror = () => {
    conn.className = "dot dot-off";
    connText.textContent = "Mất kết nối, đang thử lại…";
    // EventSource tự reconnect.
  };
}

/* ------------------------- Khởi động ------------------------- */
refresh().catch((e) => toast("Không tải được dữ liệu", String(e), "danger"));
connectStream();
