/* Ground imaging station compact-storage drill UI — talks to the real API. */

const $ = (id) => document.getElementById(id);

let artifacts = [];
let currentDrill = null;

const api = {
  async req(method, path, body) {
    const res = await fetch(path, {
      method,
      headers: { "Content-Type": "application/json" },
      ...(body ? { body: JSON.stringify(body) } : {}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw Object.assign(new Error(
        data?.reject?.reason || data?.detail || res.statusText
      ), { status: res.status, data });
    }
    return data;
  },
  health: () => fetch("/health").then((r) => r.json()),
  listDrills: () => api.req("GET", "/api/drills"),
  createDrill: (b) => api.req("POST", "/api/drills", b),
  drill: (id) => api.req("GET", `/api/drills/${id}`),
  reRegister: (id, b) => api.req("PUT", `/api/drills/${id}/artifacts`, b),
  compact: (id, b) => api.req("POST", `/api/drills/${id}/compact`, b),
  reopen: () => api.req("POST", "/api/admin/reopen", {}),
  recovery: () => fetch("/api/recovery").then((r) => r.json()),
  reassemble: (id, i) =>
    fetch(`/api/drills/${id}/artifacts/${i}/reassemble`).then((r) => r.json()),
};

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (m) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[m]
  ));
}

// ------------------------------------------------------------- drill builder

function addArtifact(fragments) {
  artifacts.push({
    name: `工件 ${String.fromCharCode(65 + artifacts.length)}`,
    fragments: fragments ? [...fragments] : [""],
  });
}

function collectEditor() {
  document.querySelectorAll('input[data-role="aname"]').forEach((el) => {
    artifacts[+el.dataset.a].name = el.value;
  });
  document.querySelectorAll('input[data-role="frag"]').forEach((el) => {
    artifacts[+el.dataset.a].fragments[+el.dataset.f] = el.value;
  });
}

function renderEditor() {
  const box = $("artifacts-editor");
  box.innerHTML = artifacts.map((a, ai) => `
    <div class="artifact-card">
      <div class="row" style="margin-bottom:6px">
        <label>工件名
          <input class="name" data-role="aname" data-a="${ai}" value="${escapeHtml(a.name)}" />
        </label>
        <button type="button" data-role="addfrag" data-a="${ai}">＋ 添加片段</button>
        <button type="button" class="ghost" data-role="removeart" data-a="${ai}"
                ${artifacts.length <= 2 ? "disabled" : ""}>移除工件</button>
      </div>
      ${a.fragments.map((f, fi) => `
        <div class="frag-row">
          <span class="idx">#${fi + 1}</span>
          <input data-role="frag" data-a="${ai}" data-f="${fi}" value="${escapeHtml(f)}" placeholder="短文本片段" />
          <button type="button" class="remove" data-role="removefrag" data-a="${ai}" data-f="${fi}"
                  ${a.fragments.length <= 1 ? "disabled" : ""}>✕</button>
        </div>`).join("")}
    </div>`).join("");

  document.querySelectorAll('input[data-role="aname"], input[data-role="frag"]')
    .forEach((el) => { el.oninput = () => collectEditor(); });
  document.querySelectorAll('button[data-role="addfrag"]').forEach((el) => {
    el.onclick = () => {
      collectEditor();
      artifacts[+el.dataset.a].fragments.push("");
      renderEditor();
    };
  });
  document.querySelectorAll('button[data-role="removefrag"]').forEach((el) => {
    el.onclick = () => {
      collectEditor();
      artifacts[+el.dataset.a].fragments.splice(+el.dataset.f, 1);
      if (artifacts[+el.dataset.a].fragments.length === 0) {
        artifacts[+el.dataset.a].fragments.push("");
      }
      renderEditor();
    };
  });
  document.querySelectorAll('button[data-role="removeart"]').forEach((el) => {
    el.onclick = () => {
      collectEditor();
      artifacts.splice(+el.dataset.a, 1);
      renderEditor();
    };
  });
}

$("add-artifact").onclick = () => {
  collectEditor();
  if (artifacts.length >= 8) return alert("每份演练至多 8 份工件");
  addArtifact();
  renderEditor();
};

$("seed-demo").onclick = () => {
  artifacts = [
    { name: "光谱校准帧", fragments: ["【帧头】站点S-07 2026-10-07", "光谱基线 L=550", "【帧尾】校验 0x9F"] },
    { name: "地形条带A", fragments: ["【帧头】站点S-07 2026-10-07", "地形条带 A1 起伏 +3.2m", "【帧尾】校验 0x9F"] },
    { name: "地形条带B", fragments: ["【帧头】站点S-07 2026-10-07", "地形条带 B4 起伏 -1.1m", "【帧尾】校验 0x9F"] },
    { name: "云量速报", fragments: ["【帧头】站点S-07 2026-10-07", "云量 18%", "【帧尾】校验 0x9F"] },
  ];
  renderEditor();
  $("drill-name").value = "晨间过境演练";
  $("cid").value = "CID-2026-ALPHA";
};

$("create-drill").onclick = async () => {
  $("create-error").textContent = "";
  try {
    const payload = editorPayload();
    if (payload.artifacts.length < 2 || payload.artifacts.length > 8) {
      throw new Error("工件数量必须在 2–8 之间，且每份至少 1 个片段");
    }
    const drill = await api.createDrill(payload);
    await loadDrills();
    $("drill-select").value = drill.id;
    renderDetail(drill);
  } catch (e) {
    const detail = e.data?.detail;
    const msg = Array.isArray(detail)
      ? detail.map((d) => d.msg).join("；")
      : e.message;
    $("create-error").textContent = "创建失败：" + msg;
  }
};

// ------------------------------------------------------------- drill detail

function editorPayload() {
  collectEditor();
  const arts = artifacts
    .map((a) => ({
      name: a.name.trim() || "未命名工件",
      fragments: a.fragments.filter((f) => f.length > 0),
    }))
    .filter((a) => a.fragments.length > 0);
  return {
    name: $("drill-name").value.trim() || "未命名演练",
    artifacts: arts,
  };
}

$("reregister").onclick = async () => {
  if (!currentDrill) return alert("请先选择要重新登记工件的演练");
  const payload = editorPayload();
  if (payload.artifacts.length < 2 || payload.artifacts.length > 8) {
    return alert("工件数量必须在 2–8 之间，且每份至少 1 个片段");
  }
  const d = await api.reRegister(currentDrill.id, payload);
  renderDetail(d);
};

async function loadDrills(selectId) {
  const data = await api.listDrills();
  const sel = $("drill-select");
  sel.innerHTML = `<option value="">— 选择演练 —</option>` +
    data.drills.map((d) =>
      `<option value="${d.id}">${escapeHtml(d.name)}（活动代次 ${d.active_generation ?? "—"}）</option>`
    ).join("");
  if (selectId) sel.value = selectId;
  return data;
}

function renderDetail(d) {
  currentDrill = d;

  const genRows = d.generations.map((g) => `
    <tr>
      <td>第 ${g.generation} 代</td>
      <td><span class="tag ${g.status}">${
        { active: "活动", staged: "暂存（待切换）", retired: "退役" }[g.status]
      }</span></td>
      <td class="mono">${g.segment}</td>
      <td>${g.fragment_count}</td>
      <td>${g.byte_length} B</td>
      <td>${g.created_at}</td>
    </tr>`).join("");

  const artBlocks = d.artifacts.map((a, i) => `
    <div class="artifact-card">
      <div class="row" style="margin-bottom:4px">
        <strong>${escapeHtml(a.name)}</strong>
        <span class="muted">${a.fragment_count} 个引用 / ${a.unique_fragments} 个去重片段${
          a.fragment_count > a.unique_fragments
            ? `（共享复用 ${a.fragment_count - a.unique_fragments} 次）`
            : ""
        }</span>
        ${a.rebuild_ok === true ? '<span class="tag active">可重组 ✓</span>' : ""}
        ${a.rebuild_ok === false
          ? '<span class="tag" style="color:var(--bad);border-color:var(--bad)">无法重组 ✕</span>'
          : ""}
        <button type="button" data-role="reassemble" data-i="${i}"
                ${a.rebuild_ok !== true ? "disabled" : ""}>从段中重新拼出</button>
      </div>
      <table>
        <tr><th>#</th><th>片段摘要</th><th>所在段</th><th>偏移</th><th>长度</th><th>引用</th></tr>
        ${a.fragments.map((f, fi) => `
          <tr>
            <td>${fi + 1}</td>
            <td class="mono">${f.digest.slice(0, 16)}…</td>
            <td class="mono">${f.segment ?? '<span class="muted">未入段</span>'}</td>
            <td>${f.offset ?? "—"}</td>
            <td>${f.length ?? "—"}</td>
            <td>${f.reused
              ? '<span class="tag reused">重复引用同一副本</span>'
              : '<span class="tag active">唯一副本</span>'}</td>
          </tr>`).join("")}
      </table>
      <div class="muted" style="margin-top:4px">
        重组摘要 <span class="mono">${a.reassembled_digest.slice(0, 20)}…</span>
      </div>
    </div>`).join("");

  const r = d.recovery;
  $("drill-detail").innerHTML = `
    <div class="row" style="margin-bottom:4px">
      <strong>${escapeHtml(d.name)}</strong>
      <span class="muted mono">${d.id}</span>
      <span class="muted">整理标识：${d.consolidation_id ?? "尚未整理"}</span>
      <span class="muted">活动代次：<strong>${d.active_generation ?? "无"}</strong></span>
    </div>
    <table>
      <tr><th>代次</th><th>状态</th><th>段</th><th>唯一片段数</th><th>段大小</th><th>建立时间</th></tr>
      ${genRows || '<tr><td colspan="6" class="muted">暂无代次</td></tr>'}
    </table>
    <h3>每份工件重组摘要 · 片段所在段与偏移</h3>
    ${artBlocks}
    <h3>恢复裁决</h3>
    <p class="${r.complete ? "verdict-ok" : "verdict-bad"}">
      ${r.complete ? "✓ " : "✕ "}${escapeHtml(r.detail)}
      ${r.retransmission ? '<span class="tag reused">本次重开存在片段重传</span>' : ""}
    </p>
    ${r.missing_fragments.length
      ? `<div class="error">缺失 ${r.missing_fragments.length} 个片段：${
          r.missing_fragments.slice(0, 4).map((d) => d.slice(0, 10)).join(", ")
        }</div>`
      : ""}
    <p class="muted">检查 ${r.checked_artifacts} 份工件、${r.checked_fragments} 个片段引用；磁盘上共有 ${d.segments.length} 个段文件。</p>
  `;

  document.querySelectorAll('button[data-role="reassemble"]').forEach((b) => {
    b.onclick = async () => {
      const text = await api.reassemble(d.id, +b.dataset.i);
      $("ra-title").textContent = `${text.artifact}（段 ${text.segment}）重新拼出`;
      $("ra-text").textContent = text.text;
      $("reassemble-dialog").showModal();
    };
  });
}

// ------------------------------------------------------------- compaction

async function runCompact(cid, crashAfter) {
  const box = $("compact-result");
  box.innerHTML = '<span class="muted">执行中…</span>';
  try {
    const res = await api.compact(currentDrill.id, {
      consolidation_id: cid,
      crash_after: crashAfter,
    });
    box.innerHTML = `<span class="ok">✓ 压缩完成</span>
      <span class="tag active pill">第 ${res.generation} 代活动</span>
      ${res.retransmission ? '<span class="tag reused pill">重传幂等 · 未新建段</span>' : ""}
      ${res.swept?.length
        ? `<span class="muted pill">已清扫旧段：${res.swept.join(", ")}</span>`
        : ""}
      ${res.previous_generation
        ? `<span class="muted pill">替代第 ${res.previous_generation} 代</span>`
        : ""}`;
    const d = await api.drill(currentDrill.id);
    renderDetail(d);
  } catch (e) {
    if (e.status === 409) {
      box.innerHTML = `<span class="bad">✗ 已拒绝（原活动目录保留）</span>
        <div class="muted" style="margin-top:6px">首个拒因：${escapeHtml(e.data.reject.reason)}</div>
        <div class="muted">拒因代码：${e.data.reject.code} · 当前活动代次：${e.data.reject.active_generation ?? "无"}</div>`;
      renderDetail(e.data.drill);
    } else if (e.status === 503) {
      const b = $("crash-banner");
      b.classList.remove("hidden");
      b.textContent = `模拟断电（${e.data.crash_point === "segments" ? "新段落盘后、目录切换前" : "目录切换后、旧段清扫前"}）。`
        + "磁盘此刻即崩溃现场 —— 点击「⚡ 模拟重开」验证启动收敛，收敛后即得到一份完整目录；随后用同一整理标识重传不得新建段。";
      box.innerHTML = `<span class="warn">⚡ ${escapeHtml(e.data.note)}</span>`;
      renderDetail(e.data.drill);
    } else {
      box.innerHTML = `<span class="bad">✗ ${escapeHtml(e.message)}</span>`;
    }
  }
}

$("compact").onclick = () => {
  if (!currentDrill) return alert("请先创建并选择演练");
  const cid = $("cid").value.trim();
  if (!cid) return alert("请填写稳定整理标识");
  return runCompact(cid, $("crash-after").value);
};

$("retransmit").onclick = () => {
  if (!currentDrill) return alert("请先创建并选择演练");
  const cid = $("cid").value.trim();
  if (!cid) return alert("请填写稳定整理标识");
  return runCompact(cid, "none");
};

$("reopen").onclick = async () => {
  const report = await api.reopen();
  $("crash-banner").classList.add("hidden");
  $("recovery-report").textContent = JSON.stringify(report, null, 2);
  if (currentDrill) {
    const d = await api.drill(currentDrill.id);
    renderDetail(d);
  }
};

$("refresh").onclick = async () => {
  await loadDrills(currentDrill?.id);
  if (currentDrill) renderDetail(await api.drill(currentDrill.id));
};

$("drill-select").onchange = async (e) => {
  if (!e.target.value) {
    currentDrill = null;
    $("drill-detail").textContent = "尚未选择演练。";
    return;
  }
  renderDetail(await api.drill(e.target.value));
};

// ------------------------------------------------------------- bootstrap

(async function init() {
  try {
    const h = await api.health();
    $("health").textContent = `● ${h.status} · 崩溃模式 ${h.crash_mode} · 数据目录 ${h.data_dir}`;
    $("health").classList.add("ok");
  } catch (e) {
    $("health").textContent = "● API 不可达：" + e.message;
    $("health").classList.add("bad");
  }
  addArtifact();
  addArtifact();
  renderEditor();
  try {
    const data = await loadDrills();
    $("recovery-report").textContent =
      JSON.stringify(await api.recovery(), null, 2);
    if (data.drills.length) renderDetail(data.drills[0]);
  } catch (e) {
    /* health badge already surfaces reachability problems */
  }
})();
