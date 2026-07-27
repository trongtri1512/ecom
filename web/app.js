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
    html += `<div class="kpi"><div class="label">${c}</div><div class="value">${n}</div></div>`;
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
    <td>${idx}</td>
    <td class="code">${escapeHtml(r.code)}${dupBadge}</td>
    <td><span class="badge ${isOther ? "other" : ""}">${escapeHtml(r.carrier)}</span></td>
    <td><input class="cell-edit" data-field="supplier" value="${escapeAttr(r.supplier || "")}" placeholder="—" /></td>
    <td><input class="cell-edit" data-field="note" value="${escapeAttr(r.note || "")}" placeholder="—" /></td>
    <td>${fmtTime(r.scanned_at)}</td>
    <td>${escapeHtml(r.source_agent || "")}</td>
    <td class="row-actions"><button class="icon-btn btn-del" title="Xoá">🗑️</button></td>
  </tr>`;
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
}

async function refresh() {
  await Promise.all([loadSummary(), loadRows()]);
}

/* ------------------------- Sự kiện bảng (sửa/xoá) ------------------------- */
rowsEl.addEventListener("change", async (e) => {
  const inp = e.target.closest(".cell-edit");
  if (!inp) return;
  const id = inp.closest("tr").dataset.id;
  const field = inp.dataset.field;
  try {
    await api(`/api/scans/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ [field]: inp.value }),
    });
    toast("Đã lưu", `${field === "supplier" ? "NCC" : "Ghi chú"} cập nhật`, "ok");
  } catch (err) {
    toast("Lỗi lưu", String(err), "danger");
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
$("#btnCsv").addEventListener("click", () => downloadExport("csv"));
function downloadExport(fmt) {
  const params = new URLSearchParams({ format: fmt });
  if (filterEl.value) params.set("carrier", filterEl.value);
  if (currentPeriod()) params.set("period", currentPeriod());
  window.location.href = API + "/api/export?" + params.toString();
}

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

/* ------------------------- Quản lý ĐVVC (modal) ------------------------- */
const modal = $("#carrierModal");

function carrierRowHtml(r) {
  return `<tr data-id="${r.id}">
    <td><input class="c-name" value="${escapeAttr(r.name)}" /></td>
    <td><input class="c-prefix" value="${escapeAttr(r.prefix)}" /></td>
    <td><input class="c-priority" type="number" value="${r.priority}" /></td>
    <td><button class="icon-btn c-del" title="Xoá luật">🗑️</button></td>
  </tr>`;
}

async function loadCarrierRules() {
  const data = await api("/api/carriers");
  $("#carrierRows").innerHTML = (data.items || []).map(carrierRowHtml).join("");
}

$("#btnCarriers").addEventListener("click", async () => {
  try {
    await loadCarrierRules();
    modal.hidden = false;
  } catch (err) {
    toast("Lỗi tải luật ĐVVC", String(err), "danger");
  }
});
$("#closeCarrier").addEventListener("click", () => { modal.hidden = true; });
modal.addEventListener("click", (e) => { if (e.target === modal) modal.hidden = true; });

// Thêm hãng mới
$("#btnAddCarrier").addEventListener("click", async () => {
  const name = $("#newName").value.trim();
  const prefix = $("#newPrefix").value.trim();
  const priority = parseInt($("#newPriority").value, 10) || 100;
  if (!name || !prefix) { toast("Thiếu thông tin", "Nhập cả tên ĐVVC và prefix", "warn"); return; }
  try {
    await api("/api/carriers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, prefix, priority }),
    });
    $("#newName").value = ""; $("#newPrefix").value = ""; $("#newPriority").value = "100";
    await loadCarrierRules();
    toast("Đã thêm ĐVVC", `${name} (${prefix})`, "ok");
  } catch (err) {
    toast("Lỗi thêm", String(err), "danger");
  }
});

// Xoá 1 luật
$("#carrierRows").addEventListener("click", async (e) => {
  const del = e.target.closest(".c-del");
  if (!del) return;
  const tr = del.closest("tr");
  const name = tr.querySelector(".c-name").value;
  if (!confirm(`Xoá luật ĐVVC "${name}"?`)) return;
  try {
    await api(`/api/carriers/${tr.dataset.id}`, { method: "DELETE" });
    tr.remove();
    toast("Đã xoá luật", name, "warn");
  } catch (err) {
    toast("Lỗi xoá", String(err), "danger");
  }
});

// Lưu tất cả luật (các dòng đã sửa) rồi phân loại lại
$("#btnSaveReclassify").addEventListener("click", async () => {
  const rows = [...$("#carrierRows").querySelectorAll("tr")];
  try {
    for (const tr of rows) {
      const id = tr.dataset.id;
      const name = tr.querySelector(".c-name").value.trim();
      const prefix = tr.querySelector(".c-prefix").value.trim();
      const priority = parseInt(tr.querySelector(".c-priority").value, 10) || 100;
      if (!name || !prefix) continue;
      await api(`/api/carriers/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, prefix, priority }),
      });
    }
    const r = await api("/api/reclassify", { method: "POST" });
    modal.hidden = true;
    toast("Đã lưu & phân loại lại", `${r.changed} mã được cập nhật`, "ok");
    refresh();
  } catch (err) {
    toast("Lỗi lưu", String(err), "danger");
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

  es.onerror = () => {
    conn.className = "dot dot-off";
    connText.textContent = "Mất kết nối, đang thử lại…";
    // EventSource tự reconnect.
  };
}

/* ------------------------- Khởi động ------------------------- */
refresh().catch((e) => toast("Không tải được dữ liệu", String(e), "danger"));
connectStream();
