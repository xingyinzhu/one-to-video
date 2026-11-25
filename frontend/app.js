/**
 * AI 紀錄片導演工作台前端互動控制器
 * - 5 階段狀態機視覺同步 (Stepper + 燈號)
 * - SSE 即時進度與繁體中文工作紀錄推播
 * - 分鏡卡片行內編輯、拖曳排序、一鍵複製 Prompt 前往 Flow
 * - Downloads 資料夾監聽開關、拖曳檔案替換、單鏡預覽與成片交付
 */

const state = {
  projects: [],
  currentProject: null,
  viewingStage: 1,
  eventSource: null,
  systemStatus: null,
  dragShotId: null,
  lastRenderedVideoKey: "",
};

// ==================== 工具函式 ====================
function $(id) {
  return document.getElementById(id);
}

function showToast(msg, type = "success") {
  const container = $("toastContainer");
  const el = document.createElement("div");
  el.className = `toast toast-${type}`;
  el.textContent = msg;
  container.appendChild(el);
  setTimeout(() => {
    el.remove();
  }, 3400);
}

async function apiFetch(url, options = {}) {
  const opts = { ...options };
  if (opts.body && !(opts.body instanceof FormData) && typeof opts.body === "object") {
    opts.headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
    opts.body = JSON.stringify(opts.body);
  }
  const resp = await fetch(url, opts);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data.detail || `請求失敗 (${resp.status})`);
  }
  return data;
}

async function copyText(text, okMessage = "已複製到剪貼簿！") {
  try {
    await navigator.clipboard.writeText(text);
    showToast(okMessage, "success");
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
    showToast(okMessage, "success");
  }
}

function formatBytes(bytes) {
  if (!bytes || bytes <= 0) return "0 MB";
  const mb = bytes / (1024 * 1024);
  return mb >= 1 ? `${mb.toFixed(1)} MB` : `${(bytes / 1024).toFixed(0)} KB`;
}

function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

const STAGE_LABELS = {
  pending: "等待中",
  running: "執行中",
  action_needed: "等你動手",
  done: "已完成",
  failed: "失敗",
};

const DOT_CLASS = {
  pending: "dot-pending",
  running: "dot-running",
  action_needed: "dot-action",
  done: "dot-done",
  failed: "dot-failed",
};

// ==================== SSE 即時監控連線 ====================
function connectProjectSSE(projectId) {
  disconnectSSE();
  if (!projectId) return;

  const es = new EventSource(`/api/projects/${encodeURIComponent(projectId)}/events`);
  state.eventSource = es;
  $("sseConnBadge").textContent = "🟢 即時監控連線中";

  es.onmessage = (evt) => {
    try {
      const payload = JSON.parse(evt.data);
      if (payload.project) {
        const prevStage = state.currentProject ? state.currentProject.current_stage : null;
        state.currentProject = payload.project;
        // 若後端自動推進了階段（例如收齊素材自動進階段 4，或渲染完自動進階段 5），同步切換畫面
        if (prevStage !== payload.project.current_stage) {
          state.viewingStage = payload.project.current_stage;
        }
        renderAll();
        refreshProjectListSilent();
      } else if (payload.log) {
        appendSingleLog(payload.log);
      }
    } catch (err) {
      console.error("SSE parse error:", err);
    }
  };

  es.onerror = () => {
    $("sseConnBadge").textContent = "🟠 重新連線中...";
  };
}

function disconnectSSE() {
  if (state.eventSource) {
    state.eventSource.close();
    state.eventSource = null;
  }
}

// ==================== 資料載入 ====================
async function loadSystemStatus() {
  try {
    const sys = await apiFetch("/api/system/status");
    state.systemStatus = sys;
    const badge = $("ffmpegHealthBadge");
    if (sys.ffmpeg_ready) {
      badge.innerHTML = `<span class="dot dot-done"></span><span>FFmpeg 引擎就緒</span>`;
      $("ffmpegMissingAlert").classList.add("hidden");
    } else {
      badge.innerHTML = `<span class="dot dot-failed"></span><span>缺少 FFmpeg</span>`;
      $("ffmpegMissingAlert").classList.remove("hidden");
    }

    const colabAuth = sys.colab_auth || {};
    const cBadge = $("colabAuthBadge");
    if (cBadge) {
      if (colabAuth.authorized) {
        cBadge.innerHTML = `<span class="dot dot-done"></span><span>Colab 全自動已授權</span>`;
      } else {
        cBadge.innerHTML = `<span class="dot dot-action"></span><span>未授權 Colab（點此綁定）</span>`;
      }
    }
    const authBtn = $("btnOpenColabAuth");
    if (authBtn) {
      authBtn.textContent = colabAuth.authorized
        ? "✅ Google Colab 已授權（重新檢查）"
        : "🔑 授權 Google Colab（首次一次）";
    }
  } catch (err) {
    console.error(err);
  }
}

async function refreshProjectListSilent() {
  try {
    const data = await apiFetch("/api/projects");
    state.projects = data.projects || [];
    renderSidebarProjects();
  } catch (err) {
    console.error(err);
  }
}

async function selectProject(projectId) {
  try {
    const data = await apiFetch(`/api/projects/${encodeURIComponent(projectId)}`);
    state.currentProject = data.project;
    state.viewingStage = data.project.current_stage || 2;
    state.lastRenderedVideoKey = "";
    renderAll();
    connectProjectSSE(projectId);
    refreshProjectListSilent();
  } catch (err) {
    showToast(err.message, "error");
  }
}

function switchToNewProjectMode() {
  disconnectSSE();
  state.currentProject = null;
  state.viewingStage = 1;
  state.lastRenderedVideoKey = "";
  $("sseConnBadge").textContent = "待命中";
  renderAll();
  renderSidebarProjects();
}

// ==================== 畫面渲染總控 ====================
function renderAll() {
  renderHeaderAndStepper();
  renderStageVisibility();

  const p = state.currentProject;
  if (!p || state.viewingStage === 1) {
    renderStage1(p);
  } else if (state.viewingStage === 2) {
    renderStage2(p);
  } else if (state.viewingStage === 3) {
    renderStage3(p);
  } else if (state.viewingStage === 4) {
    renderStage4(p);
  } else if (state.viewingStage === 5) {
    renderStage5(p);
  }

  renderLogs(p ? p.logs || [] : []);
}

function renderSidebarProjects() {
  const listEl = $("projectList");
  $("projectCountBadge").textContent = String(state.projects.length);

  if (!state.projects.length) {
    listEl.innerHTML = `<div class="empty-hint">尚無歷史專案，寫一句話立即開拍！</div>`;
    return;
  }

  const curId = state.currentProject ? state.currentProject.id : "";
  listEl.innerHTML = state.projects
    .map((p) => {
      const stMap = p.stage_statuses || {};
      const curSt = stMap[String(p.current_stage)] || "pending";
      const dotCls = DOT_CLASS[curSt] || "dot-pending";
      const activeCls = p.id === curId ? "active" : "";
      return `
        <div class="proj-list-item ${activeCls}" data-pid="${escapeHtml(p.id)}">
          <div class="proj-item-main">
            <div class="proj-item-name">${escapeHtml(p.name)}</div>
            <div class="proj-item-meta">
              <span class="dot ${dotCls}"></span>
              <span>階段 ${p.current_stage}</span>
              <span>‧</span>
              <span>${p.done_shots || 0}/${p.total_shots || 0} 鏡</span>
              <span>‧</span>
              <span>${escapeHtml(p.aspect_ratio)}</span>
            </div>
          </div>
          <button class="btn-del-proj" data-del-pid="${escapeHtml(p.id)}" title="刪除專案">✕</button>
        </div>
      `;
    })
    .join("");

  listEl.querySelectorAll(".proj-list-item").forEach((el) => {
    el.addEventListener("click", (e) => {
      if (e.target.closest(".btn-del-proj")) return;
      selectProject(el.dataset.pid);
    });
  });

  listEl.querySelectorAll(".btn-del-proj").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const pid = btn.dataset.delPid;
      if (!confirm("確定要刪除此專案與已收集的影片檔案嗎？")) return;
      try {
        await apiFetch(`/api/projects/${encodeURIComponent(pid)}`, { method: "DELETE" });
        showToast("已刪除專案");
        await refreshProjectListSilent();
        if (state.currentProject && state.currentProject.id === pid) {
          if (state.projects.length > 0) {
            selectProject(state.projects[0].id);
          } else {
            switchToNewProjectMode();
          }
        }
      } catch (err) {
        showToast(err.message, "error");
      }
    });
  });
}

function renderHeaderAndStepper() {
  const p = state.currentProject;
  if (!p) {
    $("currentProjBadge").textContent = "新專案建立模式";
    $("currentProjTitle").textContent = "用一句話啟動你的 AI 紀錄片製作流水線";
  } else {
    $("currentProjBadge").textContent = `${p.aspect_ratio} ｜ 目標 ${p.target_duration}秒`;
    $("currentProjTitle").textContent = p.name;
  }

  const statuses = p
    ? p.stage_statuses || {}
    : { 1: "action_needed", 2: "pending", 3: "pending", 4: "pending", 5: "pending" };

  document.querySelectorAll("#pipelineStepper .step-item").forEach((btn) => {
    const stg = Number(btn.dataset.stage);
    const st = statuses[String(stg)] || "pending";
    btn.className = `step-item st-${st} ${state.viewingStage === stg ? "active-view" : ""}`;
    const dot = btn.querySelector(".step-dot");
    dot.className = `step-dot dot ${DOT_CLASS[st] || "dot-pending"}`;
    const stateTxt = btn.querySelector(".step-state-text");

    if (!p && stg === 1) {
      stateTxt.textContent = "等你輸入需求";
    } else if (p && stg === 3 && st === "action_needed") {
      stateTxt.textContent = `等你生片 (${p.done_shots}/${p.total_shots})`;
    } else if (p && stg === 4 && st === "running") {
      const pct = (p.render_progress && p.render_progress.percent) || 0;
      stateTxt.textContent = `渲染中 ${pct}%`;
    } else {
      stateTxt.textContent = STAGE_LABELS[st] || "等待中";
    }
  });
}

function renderStageVisibility() {
  for (let i = 1; i <= 5; i++) {
    const sec = $(`viewStage${i}`);
    if (sec) {
      sec.classList.toggle("hidden", state.viewingStage !== i);
    }
  }
}

// ==================== 階段 ① 渲染 ====================
function renderStage1(proj) {
  if (proj) {
    $("briefInput").value = proj.brief || "";
    $("selDuration").value = String(proj.target_duration || 60);
    $("selAspectRatio").value = proj.aspect_ratio || "16:9";
    $("selVisualStyle").value = proj.visual_style || "35mm_doc";
    $("selAudioMode").value = proj.audio_mode || "mute";
    $("selPlannerMode").value = proj.planner_mode || "paste";
  }
}

// ==================== 階段 ② 渲染 ====================
function renderStage2(proj) {
  if (!proj) return;
  const st2 = (proj.stage_statuses || {})["2"] || "action_needed";

  $("stage2RunningBanner").classList.toggle("hidden", st2 !== "running");
  $("stage2FailedBanner").classList.toggle("hidden", st2 !== "failed");
  $("stage2ActionBanner").classList.toggle("hidden", st2 === "running" || st2 === "failed");

  if (st2 === "failed") {
    $("stage2ErrorText").textContent = proj.error_message || "連線逾時或設定有誤";
  }

  if (document.activeElement !== $("inputStyleLock")) {
    $("inputStyleLock").value = proj.style_lock || "";
  }
  if (document.activeElement !== $("inputNegPrompt")) {
    $("inputNegPrompt").value = proj.negative_prompt || "";
  }

  const shots = proj.shots || [];
  $("stage2ShotsSummary").textContent = `共 ${shots.length} 個分鏡（預計總長約 ${shots
    .reduce((acc, s) => acc + Number(s.duration_sec || 8), 0)
    .toFixed(0)} 秒）`;

  // 若使用者正在編輯某張分鏡輸入框，避免 SSE 刷新搶走游標焦點
  const activeEl = document.activeElement;
  if (activeEl && activeEl.closest("#storyboardList")) {
    return;
  }

  const listEl = $("storyboardList");
  listEl.innerHTML = shots
    .map(
      (s) => `
      <div class="shot-edit-card" draggable="true" data-shot-id="${s.id}">
        <div class="shot-card-head">
          <span class="drag-handle" title="按住拖曳調整順序">⠿</span>
          <span class="shot-badge">Shot ${String(s.shot_order).padStart(2, "0")}</span>
          <input type="text" class="shot-title-input" data-field="title" value="${escapeHtml(s.title)}" placeholder="鏡頭標題" />
          <div class="shot-dur-wrap">
            <span>秒數</span>
            <input type="number" class="shot-dur-input" data-field="duration_sec" min="2" max="20" step="0.5" value="${s.duration_sec}" />
          </div>
          <button type="button" class="btn-tiny" data-copy-prompt="${s.id}">複製 Prompt</button>
          <button type="button" class="btn-del-proj" data-del-shot="${s.id}" title="刪除此鏡">🗑️</button>
        </div>

        <div class="shot-edit-grid">
          <div class="field-group">
            <label>中文畫面與運鏡描述</label>
            <textarea rows="2" data-field="scene_desc_zh">${escapeHtml(s.scene_desc_zh)}</textarea>
          </div>
          <div class="field-group">
            <label>後期配音旁白靈感（供之後錄音對照）</label>
            <textarea rows="2" data-field="voiceover_zh">${escapeHtml(s.voiceover_zh)}</textarea>
          </div>
          <div class="field-group shot-prompt-box">
            <label>完整英文生片 Prompt（已附比例與寫實紀錄片前綴，可直接貼入 Google Flow）</label>
            <textarea rows="2" class="code-textarea" data-field="prompt_en">${escapeHtml(s.prompt_en)}</textarea>
          </div>
        </div>
      </div>
    `
    )
    .join("");

  bindStage2CardEvents(proj);
}

function bindStage2CardEvents(proj) {
  const listEl = $("storyboardList");

  // 欄位自動儲存
  listEl.querySelectorAll(".shot-edit-card").forEach((card) => {
    const shotId = Number(card.dataset.shotId);

    card.querySelectorAll("[data-field]").forEach((inp) => {
      inp.addEventListener("change", async () => {
        const field = inp.dataset.field;
        const val = field === "duration_sec" ? Number(inp.value) : inp.value;
        try {
          const res = await apiFetch(
            `/api/projects/${encodeURIComponent(proj.id)}/shots/${shotId}`,
            { method: "PATCH", body: { [field]: val } }
          );
          state.currentProject = res.project;
          showToast(`已自動儲存 Shot 更新`);
        } catch (err) {
          showToast(err.message, "error");
        }
      });
    });

    // 拖曳排序
    card.addEventListener("dragstart", (e) => {
      state.dragShotId = shotId;
      card.classList.add("dragging");
      e.dataTransfer.effectAllowed = "move";
    });
    card.addEventListener("dragend", () => {
      card.classList.remove("dragging");
      state.dragShotId = null;
    });
    card.addEventListener("dragover", (e) => {
      e.preventDefault();
    });
    card.addEventListener("drop", async (e) => {
      e.preventDefault();
      const fromId = state.dragShotId;
      const toId = shotId;
      if (!fromId || fromId === toId) return;

      const ids = (state.currentProject.shots || []).map((x) => x.id);
      const fromIdx = ids.indexOf(fromId);
      const toIdx = ids.indexOf(toId);
      if (fromIdx < 0 || toIdx < 0) return;

      ids.splice(fromIdx, 1);
      ids.splice(toIdx, 0, fromId);

      try {
        const res = await apiFetch(
          `/api/projects/${encodeURIComponent(proj.id)}/shots/reorder`,
          { method: "POST", body: { shot_ids: ids } }
        );
        state.currentProject = res.project;
        renderStage2(state.currentProject);
        showToast("已調整分鏡順序");
      } catch (err) {
        showToast(err.message, "error");
      }
    });
  });

  listEl.querySelectorAll("[data-copy-prompt]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const sid = Number(btn.dataset.copyPrompt);
      const shot = (proj.shots || []).find((x) => x.id === sid);
      if (shot) copyText(shot.prompt_en, `已複製 Shot ${String(shot.shot_order).padStart(2, "0")} 英文 Prompt`);
    });
  });

  listEl.querySelectorAll("[data-del-shot]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const sid = Number(btn.dataset.delShot);
      try {
        const res = await apiFetch(
          `/api/projects/${encodeURIComponent(proj.id)}/shots/${sid}`,
          { method: "DELETE" }
        );
        state.currentProject = res.project;
        renderStage2(state.currentProject);
      } catch (err) {
        showToast(err.message, "error");
      }
    });
  });
}

// ==================== 階段 ③ 渲染（素材收集與自動監聽） ====================
function renderStage3(proj) {
  if (!proj) return;
  const shots = proj.shots || [];
  const doneCnt = proj.done_shots || 0;
  const totalCnt = proj.total_shots || shots.length;
  const pct = totalCnt > 0 ? Math.round((doneCnt / totalCnt) * 100) : 0;

  // 同步 Colab ComfyUI 控制台狀態
  const sys = state.systemStatus || {};
  if (document.activeElement !== $("inputComfyUrl") && !$("inputComfyUrl").value && sys.comfyui_url) {
    $("inputComfyUrl").value = sys.comfyui_url;
  }
  const comfyRunning = Boolean(proj.comfyui_generating);
  $("comfyuiHeroCard").classList.toggle("is-running", comfyRunning);
  $("btnStartColabZeroTouch").classList.toggle("hidden", comfyRunning);
  $("btnStartComfyAuto").disabled = comfyRunning;
  $("btnStopComfyAuto").classList.toggle("hidden", !comfyRunning);

  const cStrip = $("comfyStatusStrip");
  if (comfyRunning) {
    const gp = proj.comfyui_progress || {};
    cStrip.className = "comfy-status-strip st-running";
    cStrip.innerHTML = `<span class="spinner"></span><span><strong>🔵 Colab G4 雲端全自動生片執行中：</strong>${escapeHtml(gp.message || "排程處理中...")}</span>`;
    cStrip.classList.remove("hidden");
  } else if ((proj.stage_statuses || {})["3"] === "failed" && proj.error_message) {
    cStrip.className = "comfy-status-strip st-err";
    cStrip.textContent = `🔴 雲端生片中斷：${proj.error_message}`;
    cStrip.classList.remove("hidden");
  }

  // 監聽器橫幅
  const watching = Boolean(proj.watcher_active);
  $("watcherBanner").classList.toggle("paused", !watching);
  $("watcherPulse").classList.toggle("active", watching);
  $("chkWatcherToggle").checked = watching;
  $("watcherToggleLabel").textContent = watching ? "監聽開啟中" : "監聽已暫停";
  $("watcherStatusTitle").textContent = watching
    ? "📡 自動監聽 Mac 下載資料夾（開啟中）"
    : "⏸️ 自動監聽下載資料夾（目前暫停）";
  $("watcherPathText").textContent = `監聽目錄：${proj.watcher_dir || "~/Downloads"}（自動檢查檔案寫入穩定 >50KB 並通過 ffprobe 驗證後歸檔）`;

  const waiting = proj.waiting_shot;
  const callout = $("waitingShotCallout");
  if (doneCnt >= totalCnt && totalCnt > 0) {
    callout.innerHTML = `🟢 <strong>全部 ${totalCnt} 個鏡頭皆已收集完成！</strong> 可點擊右側「立即用現有鏡頭合成」進入剪輯。`;
  } else if (waiting) {
    const orderStr = String(waiting.shot_order).padStart(2, "0");
    callout.innerHTML = watching
      ? `🟠 正在等待：<strong>Shot ${orderStr}｜${escapeHtml(waiting.title)}</strong> — 點卡片複製 Prompt 去 Google Flow 點下載，Web App 將自動抓取歸檔！`
      : `⚪ 下一個待收鏡頭：<strong>Shot ${orderStr}｜${escapeHtml(waiting.title)}</strong>（開啟右上角監聽開關即可自動抓取下載檔案，或直接把影片拖進卡片）`;
  } else {
    callout.textContent = "等待新增鏡頭";
  }

  // 進度列
  $("assetProgressText").textContent = `素材收集進度：已完成 ${doneCnt} / ${totalCnt} 鏡`;
  const sumDur = shots
    .filter((s) => s.status === "done")
    .reduce((acc, s) => acc + Number(s.actual_duration || 0), 0);
  const trans = (proj.render_options && proj.render_options.transition_sec) ?? 0.5;
  const estFinal = doneCnt > 0 ? Math.max(0, sumDur - Math.max(0, doneCnt - 1) * trans) : 0;
  $("assetEstDuration").textContent =
    doneCnt > 0 ? `（目前已收片段合成預估約 ${estFinal.toFixed(1)} 秒）` : "";

  $("assetProgressFill").style.width = `${pct}%`;
  $("chkAutoRender").checked = Boolean(proj.auto_render);
  $("btnTriggerRenderNow").disabled = doneCnt === 0;

  // 看板卡片
  const grid = $("assetKanbanGrid");
  const waitingId = waiting ? waiting.id : null;

  grid.innerHTML = shots
    .map((s) => {
      const orderStr = String(s.shot_order).padStart(2, "0");
      const isDone = s.status === "done";
      const isNext = !isDone && s.id === waitingId;

      let cardCls = "asset-card";
      let pillHtml = `<span class="status-pill pill-pending">⚪ 等待中</span>`;
      if (isDone) {
        cardCls += " is-done";
        pillHtml = `<span class="status-pill pill-done">🟢 已完成</span>`;
      } else if (isNext) {
        cardCls += " is-waiting-next";
        pillHtml = `<span class="status-pill pill-waiting">🟠 等你動手</span>`;
      }

      const thumbUrl = `/api/projects/${encodeURIComponent(proj.id)}/shots/${s.id}/thumb?t=${encodeURIComponent(s.updated_at || "")}`;

      const mediaBoxHtml = isDone
        ? `
          <div class="asset-thumb-box" data-preview-shot="${s.id}">
            <img class="asset-thumb-img" src="${thumbUrl}" alt="Shot ${orderStr}" />
            <div class="play-overlay">▶️</div>
            <span class="clip-meta-overlay">${Number(s.actual_duration).toFixed(1)}s ｜ ${s.width}×${s.height}</span>
          </div>
        `
        : `
          <div class="asset-thumb-box" data-drop-shot="${s.id}">
            <div style="font-size:1.45rem; margin-bottom:4px;">${isNext ? "📥" : "🎞️"}</div>
            <div style="font-size:0.8rem; color:#cbd5e1;">
              ${isNext ? "去 Flow 點下載將自動歸入此鏡" : "等待依序歸檔"}
            </div>
            <div style="font-size:0.73rem; color:#64748b; margin-top:2px;">也可直接將 MP4/MOV 拖曳至此處替換</div>
          </div>
        `;

      const buttonsHtml = isDone
        ? `
          <button type="button" class="btn btn-secondary btn-sm" data-preview-btn="${s.id}">▶️ 預覽</button>
          <button type="button" class="btn btn-primary btn-sm" data-comfy-single="${s.id}" title="透過 Colab G4 MiniMax H3 單獨重新生成這幕">🤖 雲端重做</button>
          <button type="button" class="btn btn-ghost btn-sm" data-retake-shot="${s.id}">🔄 重設</button>
          <label class="btn btn-ghost btn-sm" style="cursor:pointer;">
            📂 替換
            <input type="file" accept=".mp4,.mov,.webm,.mkv" class="hidden" data-file-shot="${s.id}" />
          </label>
        `
        : `
          <button type="button" class="btn ${isNext ? "btn-cta" : "btn-primary"} btn-sm" data-comfy-single="${s.id}" title="使用上方 Colab G4 網址自動生成這幕">
            🤖 雲端生成這幕
          </button>
          <button type="button" class="btn btn-secondary btn-sm" data-copy-go-flow="${s.id}">
            📋 去 Flow
          </button>
          <label class="btn btn-ghost btn-sm" style="flex:0 0 auto; cursor:pointer;" title="手動選擇本機影片檔">
            📂
            <input type="file" accept=".mp4,.mov,.webm,.mkv" class="hidden" data-file-shot="${s.id}" />
          </label>
        `;

      return `
        <div class="${cardCls}" data-card-shot="${s.id}">
          <div class="asset-card-top">
            <div class="asset-shot-title" title="${escapeHtml(s.title)}">
              Shot ${orderStr}｜${escapeHtml(s.title)}
            </div>
            ${pillHtml}
          </div>
          ${mediaBoxHtml}
          <p class="asset-desc" title="${escapeHtml(s.scene_desc_zh)}">${escapeHtml(s.scene_desc_zh || "（無中文畫面說明）")}</p>
          <div class="asset-prompt-preview" title="${escapeHtml(s.prompt_en)}">${escapeHtml(s.prompt_en)}</div>
          <div class="asset-btn-group">
            ${buttonsHtml}
          </div>
        </div>
      `;
    })
    .join("");

  bindStage3CardEvents(proj);
}

function bindStage3CardEvents(proj) {
  const grid = $("assetKanbanGrid");

  grid.querySelectorAll("[data-comfy-single]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const sid = Number(btn.dataset.comfySingle);
      const url = $("inputComfyUrl").value.trim();
      if (!url) {
        showToast("請先在上方填入 Colab Cell 4 印出的 trycloudflare 連線網址！", "error");
        $("inputComfyUrl").focus();
        return;
      }
      try {
        const res = await apiFetch(
          `/api/projects/${encodeURIComponent(proj.id)}/comfyui/generate`,
          {
            method: "POST",
            body: {
              comfyui_url: url,
              relay_mode: $("chkComfyRelay").checked,
              use_turbo: $("chkComfyTurbo").checked,
              enable_realesrgan: $("chkComfyEsrgan").checked,
              only_shot_id: sid,
            },
          }
        );
        state.currentProject = res.project;
        renderAll();
        showToast("🤖 已送出單幕雲端生成任務給 Colab G4！");
      } catch (err) {
        showToast(err.message, "error");
      }
    });
  });

  grid.querySelectorAll("[data-copy-go-flow]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const sid = Number(btn.dataset.copyGoFlow);
      const shot = (proj.shots || []).find((x) => x.id === sid);
      if (!shot) return;
      await copyText(
        shot.prompt_en,
        `已複製 Shot ${String(shot.shot_order).padStart(2, "0")} Prompt！在 Flow 下載後將自動歸檔`
      );
      // 若監聽尚未開啟，貼心自動為這鏡啟動監聽
      if (!proj.watcher_active) {
        try {
          const res = await apiFetch(
            `/api/projects/${encodeURIComponent(proj.id)}/watcher/toggle`,
            { method: "POST", body: { enabled: true, target_shot_id: sid } }
          );
          state.currentProject = res.project;
          renderAll();
        } catch (err) {
          console.warn(err);
        }
      }
      window.open("https://labs.google/fx/tools/flow", "_blank", "noopener");
    });
  });

  grid.querySelectorAll("[data-copy-only]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const sid = Number(btn.dataset.copyOnly);
      const shot = (proj.shots || []).find((x) => x.id === sid);
      if (shot) {
        copyText(shot.prompt_en, `已複製 Shot ${String(shot.shot_order).padStart(2, "0")} 英文 Prompt！`);
      }
    });
  });

  // 單鏡預覽
  const openPreview = (sid) => {
    const shot = (proj.shots || []).find((x) => x.id === sid);
    if (!shot) return;
    $("previewModalTitle").textContent = `Shot ${String(shot.shot_order).padStart(2, "0")}｜${shot.title}`;
    const v = $("clipPreviewVideo");
    v.src = `/api/projects/${encodeURIComponent(proj.id)}/shots/${sid}/clip?t=${Date.now()}`;
    $("clipPreviewModal").classList.remove("hidden");
    v.play().catch(() => {});
  };

  grid.querySelectorAll("[data-preview-shot]").forEach((el) => {
    el.addEventListener("click", () => openPreview(Number(el.dataset.previewShot)));
  });
  grid.querySelectorAll("[data-preview-btn]").forEach((el) => {
    el.addEventListener("click", () => openPreview(Number(el.dataset.previewBtn)));
  });

  // 重新錄製這鏡
  grid.querySelectorAll("[data-retake-shot]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const sid = Number(btn.dataset.retakeShot);
      try {
        const res = await apiFetch(
          `/api/projects/${encodeURIComponent(proj.id)}/shots/${sid}/reset`,
          { method: "POST" }
        );
        state.currentProject = res.project;
        renderAll();
        showToast("已重設此鏡，下次下載將優先寫入此鏡！");
      } catch (err) {
        showToast(err.message, "error");
      }
    });
  });

  // 選取檔案上傳替換
  grid.querySelectorAll("[data-file-shot]").forEach((inp) => {
    inp.addEventListener("change", async () => {
      if (!inp.files || !inp.files[0]) return;
      await uploadClipToShot(proj.id, Number(inp.dataset.fileShot), inp.files[0]);
      inp.value = "";
    });
  });

  // 拖曳影片檔到卡片替換
  grid.querySelectorAll(".asset-card").forEach((card) => {
    const sid = Number(card.dataset.cardShot);
    card.addEventListener("dragover", (e) => {
      if (e.dataTransfer && Array.from(e.dataTransfer.types).includes("Files")) {
        e.preventDefault();
        card.classList.add("drag-over");
      }
    });
    card.addEventListener("dragleave", () => {
      card.classList.remove("drag-over");
    });
    card.addEventListener("drop", async (e) => {
      card.classList.remove("drag-over");
      if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length > 0) {
        e.preventDefault();
        await uploadClipToShot(proj.id, sid, e.dataTransfer.files[0]);
      }
    });
  });
}

async function uploadClipToShot(projectId, shotId, file) {
  const fd = new FormData();
  fd.append("file", file);
  showToast(`正在匯入並驗證「${file.name}」...`);
  try {
    const res = await apiFetch(
      `/api/projects/${encodeURIComponent(projectId)}/shots/${shotId}/upload`,
      { method: "POST", body: fd }
    );
    state.currentProject = res.project;
    renderAll();
    showToast(`已成功匯入「${file.name}」並產生縮圖！`);
  } catch (err) {
    showToast(err.message, "error");
  }
}

// ==================== 階段 ④ 渲染（FFmpeg 全自動剪輯流水線） ====================
function renderStage4(proj) {
  if (!proj) return;
  const st4 = (proj.stage_statuses || {})["4"] || "pending";
  const prog = proj.render_progress || { percent: 0, substep: "等待啟動", detail: "" };
  const pct = Number(prog.percent || 0);

  $("renderFailedBox").classList.toggle("hidden", st4 !== "failed");
  $("renderRunningHeader").classList.toggle("hidden", st4 === "failed");

  if (st4 === "failed") {
    $("renderErrorText").textContent = proj.error_message || "剪輯過程發生未知錯誤";
  }

  $("renderSubstepTitle").textContent = prog.substep || "正在執行自動剪輯管線...";
  $("renderDetailText").textContent =
    prog.detail || "正在統一解析度、幀率與色彩空間，並套用電影疊化轉場...";
  $("renderPercentText").textContent = `${pct}%`;
  $("renderProgressFill").style.width = `${pct}%`;

  // 高亮 4 個子步驟卡片
  let activeSub = 1;
  if (pct >= 90) activeSub = 4;
  else if (pct >= 68) activeSub = 3;
  else if (pct >= 15) activeSub = 2;

  document.querySelectorAll("#renderSubstepsGrid .substep-card").forEach((el) => {
    const idx = Number(el.dataset.sub);
    el.classList.remove("active", "done");
    if (pct >= 100 || idx < activeSub) {
      el.classList.add("done");
    } else if (idx === activeSub && st4 === "running") {
      el.classList.add("active");
    }
  });
}

// ==================== 階段 ⑤ 渲染（成品交付與重新合成） ====================
function renderStage5(proj) {
  if (!proj) return;
  const meta = proj.output_meta || {};
  const pid = encodeURIComponent(proj.id);
  const videoKey = `${proj.id}_${meta.rendered_at || ""}`;

  const videoUrl = `/api/projects/${pid}/output/video?t=${encodeURIComponent(meta.rendered_at || Date.now())}`;
  const player = $("finalVideoPlayer");
  if (meta.output_path && state.lastRenderedVideoKey !== videoKey) {
    player.src = videoUrl;
    state.lastRenderedVideoKey = videoKey;
  }

  $("btnDownloadVideo").href = `/api/projects/${pid}/output/video?download=true`;
  $("btnExportMd").href = `/api/projects/${pid}/voiceover-script?fmt=md`;
  $("btnExportTxt").href = `/api/projects/${pid}/voiceover-script?fmt=txt`;

  const displayPath = meta.desktop_path || meta.output_path || "尚未產出";
  const canReveal =
    state.systemStatus && state.systemStatus.can_reveal_in_finder !== undefined
      ? Boolean(state.systemStatus.can_reveal_in_finder)
      : true;
  $("deliveryPathLabel").textContent = canReveal
    ? `儲存位置：${displayPath}`
    : `儲存位置（Docker 容器模式）：${displayPath}（請點「下載 MP4 成片」或直接至掛載資料夾取檔）`;

  const revealBtn = $("btnRevealFinder");
  const dlBtn = $("btnDownloadVideo");
  if (canReveal) {
    revealBtn.textContent = "📂 在 Finder 中打開成品";
    revealBtn.className = "btn btn-cta";
    dlBtn.className = "btn btn-secondary";
  } else {
    revealBtn.textContent = "📋 複製成片路徑";
    revealBtn.className = "btn btn-secondary";
    dlBtn.className = "btn btn-cta";
  }

  $("specDuration").textContent = meta.duration ? `${Number(meta.duration).toFixed(1)} 秒` : "--";
  $("specResolution").textContent = meta.width ? `${meta.width} × ${meta.height}` : "--";
  $("specFps").textContent = meta.fps ? `${meta.fps} fps` : "--";
  $("specSize").textContent = formatBytes(meta.file_size);
  $("specAudio").textContent = meta.has_audio ? "保留原聲" : "純畫面無聲";

  // 同步右側快速重算表單預設值
  const opts = proj.render_options || {};
  $("reAspectRatio").value = proj.aspect_ratio || "16:9";
  $("reTransition").value = opts.transition_sec ?? 0.5;
  $("reFps").value = String(opts.fps || 24);
  $("reAudioMode").value = proj.audio_mode || "mute";
  $("reAlignDuration").checked = Boolean(opts.align_duration);
}

// ==================== 右側即時中文工作紀錄 ====================
function renderLogs(logs) {
  const box = $("timelineLogList");
  if (!logs || !logs.length) {
    box.innerHTML = `<div class="log-empty">系統待命中。建立或選擇專案後，這裡會用白話繁體中文即時播報每一步進度！</div>`;
    return;
  }

  box.innerHTML = logs
    .map(
      (lg) => `
      <div class="log-entry lvl-${escapeHtml(lg.level || "info")}">
        <span class="log-time">${escapeHtml(lg.time_str || "")}</span>
        <span>${escapeHtml(lg.message)}</span>
      </div>
    `
    )
    .join("");
  box.scrollTop = box.scrollHeight;
}

function appendSingleLog(lg) {
  const box = $("timelineLogList");
  const empty = box.querySelector(".log-empty");
  if (empty) empty.remove();
  const div = document.createElement("div");
  div.className = `log-entry lvl-${escapeHtml(lg.level || "info")}`;
  div.innerHTML = `<span class="log-time">${escapeHtml(lg.time_str || "")}</span><span>${escapeHtml(lg.message)}</span>`;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

// ==================== 全域事件綁定 ====================
function initEvents() {
  // 新建專案按鈕
  $("btnNewProject").addEventListener("click", switchToNewProjectMode);

  // 頂部 5 階段點擊切換檢視
  document.querySelectorAll("#pipelineStepper .step-item").forEach((btn) => {
    btn.addEventListener("click", () => {
      const targetStage = Number(btn.dataset.stage);
      if (!state.currentProject && targetStage > 1) {
        showToast("請先在階段 ① 輸入一句話啟動製作流程", "error");
        return;
      }
      state.viewingStage = targetStage;
      renderAll();
    });
  });

  // 階段 ①：範例按鈕一鍵填入
  document.querySelectorAll(".pill-btn").forEach((pill) => {
    pill.addEventListener("click", () => {
      $("briefInput").value = pill.dataset.brief || "";
      if (pill.dataset.dur) $("selDuration").value = pill.dataset.dur;
      if (pill.dataset.ratio) $("selAspectRatio").value = pill.dataset.ratio;
      if (pill.dataset.style) $("selVisualStyle").value = pill.dataset.style;
    });
  });

  // 階段 ①：啟動製作流程（標準先看分鏡 vs 一句話終極全自動一條龍）
  const createProjectAction = async (fullAutoColab) => {
    const brief = $("briefInput").value.trim();
    if (brief.length < 2) {
      showToast("請先輸入一句話描述你想做的影片！", "error");
      return;
    }
    if (fullAutoColab) {
      const authSt = (state.systemStatus && state.systemStatus.colab_auth) || {};
      if (!authSt.authorized) {
        openColabAuthModal();
        showToast("首次使用終極全自動請先完成 1 次 Google Colab 授權！", "error");
        return;
      }
    }
    const btn = fullAutoColab ? $("btnStartFullAutoOneShot") : $("btnStartPipeline");
    const origText = btn.textContent;
    btn.disabled = true;
    btn.textContent = fullAutoColab
      ? "⏳ 正在建立專案並啟動 Colab 全自動開機..."
      : "⏳ 正在建立專案並拆解分鏡...";
    try {
      const data = await apiFetch("/api/projects", {
        method: "POST",
        body: {
          brief,
          target_duration: Number($("selDuration").value),
          aspect_ratio: $("selAspectRatio").value,
          visual_style: $("selVisualStyle").value,
          audio_mode: $("selAudioMode").value,
          planner_mode: $("selPlannerMode").value,
          auto_render: true,
          full_auto_colab: fullAutoColab,
          gpu_priority: ["G4", "A100", "L4"],
        },
      });
      state.currentProject = data.project;
      state.viewingStage = data.project.current_stage || (fullAutoColab ? 3 : 2);
      renderAll();
      connectProjectSSE(data.project.id);
      refreshProjectListSilent();
      showToast(
        fullAutoColab
          ? "⚡ 已啟動終極零人工全自動拍片！自動開機 ➔ 生片 ➔ 合成 ➔ 關機！"
          : "專案已建立！進入階段 ② 審核分鏡"
      );
    } catch (err) {
      showToast(err.message, "error");
    } finally {
      btn.disabled = false;
      btn.textContent = origText;
    }
  };

  $("btnStartPipeline").addEventListener("click", () => createProjectAction(false));
  $("btnStartFullAutoOneShot").addEventListener("click", () => createProjectAction(true));

  // Google Colab 一次性網頁授權 Modal 邏輯
  const openColabAuthModal = async () => {
    $("colabAuthModal").classList.remove("hidden");
    $("inputColabAuthCode").value = "";
    try {
      const st = await apiFetch("/api/colab/auth/status");
      const box = $("colabAuthModalStatus");
      box.className = `comfy-status-strip ${st.authorized ? "st-ok" : "st-running"}`;
      box.textContent = (st.authorized ? "🟢 " : "🟠 ") + (st.message || "");
    } catch (err) {
      $("colabAuthModalStatus").textContent = err.message;
    }
  };
  const closeColabAuthModal = () => $("colabAuthModal").classList.add("hidden");

  $("colabAuthBadge").addEventListener("click", openColabAuthModal);
  $("btnOpenColabAuth").addEventListener("click", openColabAuthModal);
  $("btnCloseColabAuthModal").addEventListener("click", closeColabAuthModal);
  $("btnCancelColabAuth").addEventListener("click", closeColabAuthModal);

  $("btnLaunchColabOAuth").addEventListener("click", async () => {
    try {
      const data = await apiFetch("/api/colab/auth/start", { method: "POST" });
      const urlInp = $("inputColabAuthUrl");
      urlInp.value = data.auth_url;
      urlInp.classList.remove("hidden");
      window.open(data.auth_url, "_blank", "noopener");
      showToast("已在新分頁開啟 Google 授權頁面，點允許後請將授權碼貼回第 2 步！");
    } catch (err) {
      showToast(err.message, "error");
    }
  });

  $("btnCompleteColabAuth").addEventListener("click", async () => {
    const code = $("inputColabAuthCode").value.trim();
    if (!code) {
      showToast("請先貼上 Google 頁面顯示的授權碼！", "error");
      return;
    }
    const btn = $("btnCompleteColabAuth");
    btn.disabled = true;
    btn.textContent = "⏳ 正在驗證並寫入本機鑰匙圈...";
    try {
      const st = await apiFetch("/api/colab/auth/complete", {
        method: "POST",
        body: { code },
      });
      await loadSystemStatus();
      closeColabAuthModal();
      showToast(
        st.authorized
          ? "🎉 Google Colab 永久授權完成！現在可以一鍵零人工自動開機拍片並自動關機！"
          : "已送出驗證，請重新檢查授權狀態"
      );
    } catch (err) {
      showToast(err.message, "error");
    } finally {
      btn.disabled = false;
      btn.textContent = "✅ 完成永久授權";
    }
  });

  // 階段 ③：終極零人工全自動按鈕（Colab CLI 自動搶卡 -> 部署 -> 生片 -> 自動關機）
  $("btnStartColabZeroTouch").addEventListener("click", async () => {
    if (!state.currentProject) return;
    const authSt = (state.systemStatus && state.systemStatus.colab_auth) || {};
    if (!authSt.authorized) {
      openColabAuthModal();
      showToast("請先完成 1 次 Google Colab 授權，之後永久免開 Colab 網頁！", "error");
      return;
    }
    const gpuPriority = $("selColabGpuPriority")
      .value.split(",")
      .map((x) => x.trim())
      .filter(Boolean);
    try {
      const res = await apiFetch(
        `/api/projects/${encodeURIComponent(state.currentProject.id)}/colab/auto-run`,
        {
          method: "POST",
          body: {
            gpu_priority: gpuPriority,
            relay_mode: $("chkComfyRelay").checked,
            use_turbo: $("chkComfyTurbo").checked,
            enable_realesrgan: $("chkComfyEsrgan").checked,
            auto_stop: $("chkColabAutoStop").checked,
            auto_adapt_l4: true,
          },
        }
      );
      state.currentProject = res.project;
      renderAll();
      showToast("⚡ 已啟動終極零人工全自動拍片（自動搶卡開機 ➔ 生片 ➔ 合成 ➔ 自動關機）！");
    } catch (err) {
      showToast(err.message, "error");
    }
  });

  // 階段 ②：展開/收合免 Key 貼上解析器
  $("btnTogglePastePanel").addEventListener("click", () => {
    $("pasteParserPanel").classList.toggle("hidden");
  });

  $("btnCopyMasterPrompt").addEventListener("click", () => {
    if (!state.currentProject) return;
    copyText(
      state.currentProject.master_prompt || "",
      "已複製 AI 導演總指令！請貼給 Gemini 網頁版，再把結果貼回下方解析"
    );
  });

  $("btnParsePasted").addEventListener("click", async () => {
    if (!state.currentProject) return;
    const raw = $("pasteStoryboardInput").value.trim();
    if (!raw) {
      showToast("請先貼上分鏡 JSON 或文字內容", "error");
      return;
    }
    try {
      const res = await apiFetch(
        `/api/projects/${encodeURIComponent(state.currentProject.id)}/storyboard/parse`,
        { method: "POST", body: { raw_text: raw } }
      );
      state.currentProject = res.project;
      $("pasteParserPanel").classList.add("hidden");
      renderAll();
      showToast(`解析成功！共載入 ${res.project.shots.length} 個鏡頭`);
    } catch (err) {
      showToast(err.message, "error");
    }
  });

  $("btnRegenSmartDraft").addEventListener("click", async () => {
    if (!state.currentProject) return;
    try {
      const res = await apiFetch(
        `/api/projects/${encodeURIComponent(state.currentProject.id)}/storyboard/generate`,
        { method: "POST", body: { mode: "paste" } }
      );
      state.currentProject = res.project;
      renderAll();
      showToast("已重新生成智慧導演分鏡初稿！");
    } catch (err) {
      showToast(err.message, "error");
    }
  });

  $("btnRetryStage2").addEventListener("click", async () => {
    if (!state.currentProject) return;
    try {
      const res = await apiFetch(
        `/api/projects/${encodeURIComponent(state.currentProject.id)}/storyboard/generate`,
        { method: "POST", body: { mode: state.currentProject.planner_mode } }
      );
      state.currentProject = res.project;
      renderAll();
    } catch (err) {
      showToast(err.message, "error");
    }
  });

  $("btnFallbackSmartDraft").addEventListener("click", async () => {
    if (!state.currentProject) return;
    try {
      const res = await apiFetch(
        `/api/projects/${encodeURIComponent(state.currentProject.id)}/storyboard/generate`,
        { method: "POST", body: { mode: "paste" } }
      );
      state.currentProject = res.project;
      renderAll();
      showToast("已切換為免 Key 智慧初稿！");
    } catch (err) {
      showToast(err.message, "error");
    }
  });

  // Style Lock 與 Negative Prompt 修改儲存
  $("inputStyleLock").addEventListener("change", async () => {
    if (!state.currentProject) return;
    await apiFetch(`/api/projects/${encodeURIComponent(state.currentProject.id)}`, {
      method: "PATCH",
      body: { style_lock: $("inputStyleLock").value },
    });
    showToast("已更新統一視覺基因");
  });

  $("inputNegPrompt").addEventListener("change", async () => {
    if (!state.currentProject) return;
    await apiFetch(`/api/projects/${encodeURIComponent(state.currentProject.id)}`, {
      method: "PATCH",
      body: { negative_prompt: $("inputNegPrompt").value },
    });
    showToast("已更新負面提示詞");
  });

  $("btnCopyNegPrompt").addEventListener("click", () => {
    copyText($("inputNegPrompt").value, "已複製統一負面提示詞！");
  });

  $("btnAddShot").addEventListener("click", async () => {
    if (!state.currentProject) return;
    const nextNum = (state.currentProject.shots || []).length + 1;
    try {
      const res = await apiFetch(
        `/api/projects/${encodeURIComponent(state.currentProject.id)}/shots`,
        {
          method: "POST",
          body: {
            title: `補充鏡頭 ${String(nextNum).padStart(2, "0")}`,
            duration_sec: 8.0,
            scene_desc_zh: "請輸入畫面內容描述",
            voiceover_zh: "",
            prompt_en: `Generate a ${state.currentProject.aspect_ratio === "16:9" ? "16:9 landscape" : "9:16 vertical portrait"} realistic documentary video: smooth cinematic tracking shot, natural golden lighting, 35mm cinema camera, ultra-realistic 4K documentary, no text, no watermark.`,
          },
        }
      );
      state.currentProject = res.project;
      renderStage2(state.currentProject);
      showToast("已新增一鏡");
    } catch (err) {
      showToast(err.message, "error");
    }
  });

  // 確認分鏡進入階段 ③
  const confirmStoryboardFn = async () => {
    if (!state.currentProject) return;
    try {
      const res = await apiFetch(
        `/api/projects/${encodeURIComponent(state.currentProject.id)}/storyboard/confirm`,
        { method: "POST", body: { start_watcher: true } }
      );
      state.currentProject = res.project;
      state.viewingStage = 3;
      renderAll();
      showToast("分鏡已確認！自動監聽 ~/Downloads 已開啟");
    } catch (err) {
      showToast(err.message, "error");
    }
  };
  $("btnConfirmStoryboard").addEventListener("click", confirmStoryboardFn);
  $("btnConfirmStoryboardBottom").addEventListener("click", confirmStoryboardFn);

  // 階段 ③：Colab G4 ComfyUI 測試連線、一鍵全自動生片、停止生片
  $("btnTestComfyUrl").addEventListener("click", async () => {
    const url = $("inputComfyUrl").value.trim();
    if (!url) {
      showToast("請先貼上 Colab Cell 4 印出的網址（https://xxxx.trycloudflare.com）", "error");
      return;
    }
    const strip = $("comfyStatusStrip");
    strip.className = "comfy-status-strip st-running";
    strip.innerHTML = `<span class="spinner"></span><span>正在連線檢查 Colab GPU 與 MiniMax H3 模型狀態...</span>`;
    strip.classList.remove("hidden");
    try {
      const data = await apiFetch("/api/comfyui/test", {
        method: "POST",
        body: { url },
      });
      const c = data.comfyui || {};
      const h3Badge = c.has_h3_node ? "✅ MiniMax H3 就緒" : "⚠️ 未偵測到 MiniMax H3 節點";
      const upBadge = c.has_realesrgan ? "✅ RealESRGAN 就緒" : "⚠️ 將由本機 FFmpeg 縮放 1080p";
      strip.className = "comfy-status-strip st-ok";
      strip.textContent = `🟢 連線成功！GPU：${c.gpu_name}（${c.vram_total_gb} GB VRAM）｜${h3Badge}｜${upBadge}`;
      await loadSystemStatus();
      showToast(`已連線至 ${c.gpu_name}！`);
    } catch (err) {
      strip.className = "comfy-status-strip st-err";
      strip.textContent = `🔴 ${err.message}`;
      showToast(err.message, "error");
    }
  });

  $("btnStartComfyAuto").addEventListener("click", async () => {
    if (!state.currentProject) return;
    const url = $("inputComfyUrl").value.trim();
    if (!url) {
      showToast("請先貼上 Colab Cell 4 印出的 ComfyUI 連線網址！", "error");
      $("inputComfyUrl").focus();
      return;
    }
    try {
      const res = await apiFetch(
        `/api/projects/${encodeURIComponent(state.currentProject.id)}/comfyui/generate`,
        {
          method: "POST",
          body: {
            comfyui_url: url,
            relay_mode: $("chkComfyRelay").checked,
            use_turbo: $("chkComfyTurbo").checked,
            enable_realesrgan: $("chkComfyEsrgan").checked,
          },
        }
      );
      state.currentProject = res.project;
      renderAll();
      showToast("🚀 已啟動 Colab G4 全自動首尾幀接力生片！");
    } catch (err) {
      showToast(err.message, "error");
    }
  });

  $("btnStopComfyAuto").addEventListener("click", async () => {
    if (!state.currentProject) return;
    try {
      const res = await apiFetch(
        `/api/projects/${encodeURIComponent(state.currentProject.id)}/comfyui/stop`,
        { method: "POST" }
      );
      state.currentProject = res.project;
      renderAll();
      showToast("已發送中斷指令，已生成的鏡頭均完整保留");
    } catch (err) {
      showToast(err.message, "error");
    }
  });

  // 階段 ③：切換下載監聽開關
  $("chkWatcherToggle").addEventListener("change", async () => {
    if (!state.currentProject) return;
    const enabled = $("chkWatcherToggle").checked;
    try {
      const res = await apiFetch(
        `/api/projects/${encodeURIComponent(state.currentProject.id)}/watcher/toggle`,
        { method: "POST", body: { enabled } }
      );
      state.currentProject = res.project;
      renderAll();
      showToast(enabled ? "📡 已開啟下載資料夾自動監聽" : "已暫停自動監聽");
    } catch (err) {
      showToast(err.message, "error");
    }
  });

  $("btnImportLatestDownloads").addEventListener("click", async () => {
    if (!state.currentProject) return;
    try {
      const res = await apiFetch(
        `/api/projects/${encodeURIComponent(state.currentProject.id)}/import-downloads`,
        { method: "POST", body: {} }
      );
      state.currentProject = res.project;
      state.viewingStage = res.project.current_stage;
      renderAll();
      showToast("已從下載資料夾批次匯入最新影片！");
    } catch (err) {
      showToast(err.message, "error");
    }
  });

  $("chkAutoRender").addEventListener("change", async () => {
    if (!state.currentProject) return;
    const res = await apiFetch(
      `/api/projects/${encodeURIComponent(state.currentProject.id)}`,
      { method: "PATCH", body: { auto_render: $("chkAutoRender").checked } }
    );
    state.currentProject = res.project;
  });

  $("btnBackToStage2").addEventListener("click", () => {
    state.viewingStage = 2;
    renderAll();
  });

  const startRenderAction = async (overrides = {}) => {
    if (!state.currentProject) return;
    try {
      const res = await apiFetch(
        `/api/projects/${encodeURIComponent(state.currentProject.id)}/render`,
        { method: "POST", body: overrides }
      );
      state.currentProject = res.project;
      state.viewingStage = 4;
      renderAll();
      showToast("🎬 已啟動全自動後期剪輯合成！");
    } catch (err) {
      showToast(err.message, "error");
    }
  };

  $("btnTriggerRenderNow").addEventListener("click", () => startRenderAction({}));
  $("btnRetryRender").addEventListener("click", () => startRenderAction({}));
  $("btnBackToStage3FromFail").addEventListener("click", () => {
    state.viewingStage = 3;
    renderAll();
  });

  // 階段 ⑤：Finder 打開（Docker 模式自動改為複製成片路徑）、重新合成、返回素材
  $("btnRevealFinder").addEventListener("click", async () => {
    if (!state.currentProject) return;
    try {
      const res = await apiFetch(
        `/api/projects/${encodeURIComponent(state.currentProject.id)}/reveal`,
        { method: "POST" }
      );
      if (res.opened === false) {
        const copyTarget = res.host_path || res.path || "";
        await copyText(
          copyTarget,
          res.message || `已複製成片路徑：${copyTarget}`
        );
      } else {
        showToast(res.message || `已在 Finder 反白顯示：${res.path}`);
      }
    } catch (err) {
      showToast(err.message, "error");
    }
  });

  $("btnReRender").addEventListener("click", () => {
    startRenderAction({
      aspect_ratio: $("reAspectRatio").value,
      transition_sec: Number($("reTransition").value),
      fps: Number($("reFps").value),
      audio_mode: $("reAudioMode").value,
      align_duration: $("reAlignDuration").checked,
    });
  });

  $("btnBackToAssets").addEventListener("click", () => {
    state.viewingStage = 3;
    renderAll();
  });

  // 單鏡預覽 Modal 關閉
  const closePreview = () => {
    const v = $("clipPreviewVideo");
    v.pause();
    v.removeAttribute("src");
    $("clipPreviewModal").classList.add("hidden");
  };
  $("btnClosePreviewModal").addEventListener("click", closePreview);
  $("clipPreviewModal").addEventListener("click", (e) => {
    if (e.target === $("clipPreviewModal")) closePreview();
  });

  // 系統設定 Modal
  $("btnOpenSettings").addEventListener("click", async () => {
    try {
      const data = await apiFetch("/api/settings");
      const s = data.settings || {};
      const sys = data.system || {};
      $("cfgGeminiKey").value = s.gemini_api_key || "";
      $("cfgGeminiModel").value = s.gemini_model || "gemini-2.5-flash";
      $("cfgOllamaUrl").value = s.ollama_base_url || "http://127.0.0.1:11434";
      $("cfgOllamaModel").value = s.ollama_model || "qwen2.5:7b";
      $("cfgDownloadsDir").value = s.downloads_dir || "";
      $("cfgDesktopOutDir").value = s.desktop_output_dir || "";
      $("cfgVideoProvider").value = s.video_api_provider || "comfyui_colab";
      $("cfgComfyUrl").value = s.comfyui_url || "";
      $("cfgVideoApiKey").value = s.video_api_key || "";

      $("settingsFfmpegInfo").innerHTML = sys.ffmpeg_ready
        ? `✅ FFmpeg 狀態正常<br>ffmpeg: ${escapeHtml(sys.ffmpeg_path)}<br>ffprobe: ${escapeHtml(sys.ffprobe_path)}<br>目前生效下載目錄: ${escapeHtml(sys.downloads_dir)}`
        : `❌ 尚未偵測到 FFmpeg，請於終端機執行：brew install ffmpeg`;

      $("settingsModal").classList.remove("hidden");
    } catch (err) {
      showToast(err.message, "error");
    }
  });

  const closeSettings = () => $("settingsModal").classList.add("hidden");
  $("btnCloseSettingsModal").addEventListener("click", closeSettings);
  $("btnCancelSettings").addEventListener("click", closeSettings);

  $("btnSaveSettings").addEventListener("click", async () => {
    try {
      await apiFetch("/api/settings", {
        method: "PUT",
        body: {
          gemini_api_key: $("cfgGeminiKey").value.trim(),
          gemini_model: $("cfgGeminiModel").value.trim(),
          ollama_base_url: $("cfgOllamaUrl").value.trim(),
          ollama_model: $("cfgOllamaModel").value.trim(),
          downloads_dir: $("cfgDownloadsDir").value.trim(),
          desktop_output_dir: $("cfgDesktopOutDir").value.trim(),
          video_api_provider: $("cfgVideoProvider").value,
          comfyui_url: $("cfgComfyUrl").value.trim(),
          video_api_key: $("cfgVideoApiKey").value.trim(),
        },
      });
      closeSettings();
      await loadSystemStatus();
      showToast("系統設定已儲存！");
    } catch (err) {
      showToast(err.message, "error");
    }
  });
}

// ==================== 啟動初始化 ====================
async function bootstrap() {
  initEvents();
  await loadSystemStatus();
  await refreshProjectListSilent();
  if (state.projects.length > 0) {
    // 自動恢復上次進行中的專案（斷點續做）
    await selectProject(state.projects[0].id);
  } else {
    renderAll();
  }
}

document.addEventListener("DOMContentLoaded", bootstrap);
