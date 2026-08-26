// Command queue & log viewer

// Queue
// ═══════════════════════════════════════════════════════════════

// 缓存命令全文（避免 data-* 属性对长命令的截断）
const _taskMeta = {};

// ── 任务标注（localStorage 持久化） ──
const MARK_KEY = 'neu_box_marked_tasks';
let _markedTasks = new Set();
try {
  const saved = JSON.parse(localStorage.getItem(MARK_KEY) || '[]');
  _markedTasks = new Set(saved);
} catch {}

function _isMarked(taskId) { return _markedTasks.has(taskId); }

function _toggleMark(taskId) {
  if (_markedTasks.has(taskId)) _markedTasks.delete(taskId);
  else _markedTasks.add(taskId);
  try { localStorage.setItem(MARK_KEY, JSON.stringify([..._markedTasks])); } catch {}
  // 重新渲染当前列表
  if (_lastQueueData) renderQueue(_lastQueueData);
}

function renderQueue(data) {
  const queue = data.queue || [];
  const filterUser = (queueUserFilter.value || '').trim().toLowerCase();

  if (queue.length === 0) {
    queueList.innerHTML = '<div class="queue-empty">队列为空</div>';
    queueBatchBar.style.display = 'none';
    return;
  }

  const filtered = filterUser
    ? queue.filter(t => (t.user_id || '').toLowerCase().includes(filterUser))
    : queue;

  if (filtered.length === 0) {
    queueList.innerHTML = `<div class="queue-empty">没有匹配 "${escapeHtml(filterUser)}" 的任务</div>`;
    queueBatchBar.style.display = 'none';
    return;
  }

  queueList.innerHTML = filtered.map(task => {
    const isRunning = task.status === 'running';
    const posText = isRunning ? '▶' : (task.position || '?');
    const isDone = task.status === 'completed' || task.status === 'failed';
    const canRerun = isDone && task.target?.type === 'host';
    const clickable = isDone || isRunning;

    // 缓存命令全文（避免 DOM 属性截断）
    if (isDone) {
      _taskMeta[task.task_id] = {
        command: task.command,
        user_id: task.user_id,
        cpu: task.cpu || 0,
        mem: task.mem || '0',
        device_num: task.device_num || 0,
        est_time: task.est_time || 0,
        priority: task.priority || 0,
      };
    }

    const marked = _isMarked(task.task_id);
    let etaStr = '';
    if (task.status === 'queued' && task.eta != null) {
      const mins = task.eta;
      etaStr = mins >= 60
        ? `⏳ ~${Math.floor(mins / 60)}h${mins % 60 > 0 ? (mins % 60) + 'm' : ''}`
        : (mins > 0 ? `⏳ ~${mins}min` : '⏳ 即将执行');
    }

    return `
      <div class="queue-item ${isRunning ? 'running' : ''} ${clickable ? 'clickable' : ''} ${marked ? 'marked' : ''}"
           data-task-id="${task.task_id}"
           title="${isDone ? '点击查看日志' : (isRunning ? '点击查看实时日志' : '')}">
        <input type="checkbox" class="queue-check" data-task-id="${task.task_id}" title="选择">
        <span class="queue-pos">${posText}</span>
        <span class="queue-user" title="${escapeHtml(task.user_id)}">${escapeHtml(task.user_id)}</span>
        <span class="queue-cmd" title="${escapeHtml(task.command)}">${escapeHtml(task.command)}</span>
        ${etaStr ? `<span class="queue-eta">${etaStr}</span>` : ''}
        <span class="queue-status ${task.status}">${statusLabel(task.status)}</span>
        <button class="queue-mark-btn ${marked ? 'active' : ''}" title="${marked ? '取消标注' : '标注此任务'}"
                data-task-id="${task.task_id}">${marked ? '★' : '☆'}</button>
        ${canRerun ? `<button class="queue-rerun-btn" title="重新执行此命令" data-task-id="${task.task_id}">↻</button>` : ''}
      </div>`;
  }).join('');

  // Bind click: only completed/failed → view log (ignore clicks on checkbox & rerun btn)
  queueList.querySelectorAll('.queue-item.clickable').forEach(item => {
    item.addEventListener('click', (e) => {
      if (e.target.classList.contains('queue-check')) return;
      if (e.target.classList.contains('queue-rerun-btn')) return;
      if (e.target.classList.contains('queue-mark-btn')) return;
      queueList.querySelectorAll('.queue-item.active').forEach(el => el.classList.remove('active'));
      item.classList.add('active');
      const taskId = item.dataset.taskId;
      if (taskId) viewTaskLog(taskId);
    });
  });

  // Bind re-run buttons
  queueList.querySelectorAll('.queue-rerun-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const taskId = btn.dataset.taskId;
      rerunTask(taskId);
    });
  });

  // Bind mark buttons
  queueList.querySelectorAll('.queue-mark-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      _toggleMark(btn.dataset.taskId);
    });
  });

  // Checkbox change → update batch bar
  queueList.querySelectorAll('.queue-check').forEach(cb => {
    cb.addEventListener('change', updateBatchBar);
    // Stop propagation so clicking checkbox doesn't trigger viewTaskLog
    cb.addEventListener('click', e => e.stopPropagation());
  });

  // Select-all
  const selectAllCb = document.getElementById('queueSelectAll');
  if (selectAllCb) selectAllCb.checked = false;
  queueBatchBar.style.display = 'none';
}

// ── 重新执行命令弹窗 ──────────────────────────────────────
const rerunModal      = document.getElementById('rerunModal');
const rerunCmdInput   = document.getElementById('rerunCmdInput');
const rerunCpu        = document.getElementById('rerunCpu');
const rerunMem        = document.getElementById('rerunMem');
const rerunDevNum     = document.getElementById('rerunDevNum');
const rerunConfirmBtn = document.getElementById('rerunConfirmBtn');
const rerunCancelBtn  = document.getElementById('rerunCancelBtn');
const rerunModalClose = document.getElementById('rerunModalClose');

function openRerunModal(cmd, cpu, mem, devNum) {
  return new Promise((resolve) => {
    rerunCmdInput.value = cmd;
    rerunCpu.value = cpu;
    rerunMem.value = mem;
    rerunDevNum.value = devNum;
    rerunModal.style.display = '';

    const cleanup = () => {
      rerunModal.style.display = 'none';
      rerunConfirmBtn.onclick = null;
      rerunCancelBtn.onclick = null;
      rerunModalClose.onclick = null;
      rerunModal.onclick = null;
    };

    rerunConfirmBtn.onclick = () => {
      const val = rerunCmdInput.value.trim();
      if (!val) { cleanup(); resolve(null); return; }
      cleanup();
      resolve({
        command: val,
        cpu: parseInt(rerunCpu.value, 10) || 0,
        memory: parseInt(rerunMem.value, 10) || 0,
        device_num: parseInt(rerunDevNum.value, 10) || 0,
      });
    };
    rerunCancelBtn.onclick = () => { cleanup(); resolve(null); };
    rerunModalClose.onclick = () => { cleanup(); resolve(null); };
    rerunModal.onclick = (e) => {
      if (e.target === rerunModal) { cleanup(); resolve(null); }
    };
    rerunCmdInput.focus();
  });
}

async function rerunTask(taskId) {
  const meta = _taskMeta[taskId];
  if (!meta) return;
  const cmd = meta.command;
  if (!cmd) return;

  // 解析 mem: "4G" → 4, "512M" → 512
  const memRaw = meta.mem || '0';
  let memNum = 0;
  const m = memRaw.match(/^(\d+)([MG]?)$/i);
  if (m) {
    memNum = parseInt(m[1], 10);
    if (m[2].toUpperCase() === 'M') memNum = Math.round(memNum / 1024);  // 转为 GB
  }

  const result = await openRerunModal(
    cmd, parseInt(meta.cpu, 10) || 0, memNum,
    parseInt(meta.device_num, 10) || 0);
  if (!result) return;

  const body = {
    node_id:    state.selectedNodeId,
    user_id:    meta.user_id,
    command:    result.command,
    cpu:        result.cpu,
    memory:     result.memory,
    mem_unit:   'GB',
    device_num: result.device_num,
    priority:   meta.priority || 0,
  };

  try {
    const resp = await fetch('/command/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (resp.ok) {
      showToast(`已重新提交，队列位置 #${data.position}`, 'success');
      fetchQueue();
    } else {
      showToast(data.error || '重新提交失败', 'error');
    }
  } catch (err) {
    showToast('网络错误: ' + err.message, 'error');
  }
}

function getCheckedTaskIds() {
  const checked = queueList.querySelectorAll('.queue-check:checked');
  return Array.from(checked).map(cb => cb.dataset.taskId);
}

function updateBatchBar() {
  const count = getCheckedTaskIds().length;
  const selectAllCb = document.getElementById('queueSelectAll');
  const countEl = document.getElementById('queueBatchCount');
  if (count > 0) {
    queueBatchBar.style.display = '';
    if (countEl) countEl.textContent = `已选 ${count} 项`;
    // Update select-all state
    const allCbs = queueList.querySelectorAll('.queue-check');
    if (selectAllCb) selectAllCb.checked = (count === allCbs.length);
  } else {
    queueBatchBar.style.display = 'none';
    if (selectAllCb) selectAllCb.checked = false;
  }
}

document.getElementById('queueSelectAll').addEventListener('change', function() {
  const checked = this.checked;
  queueList.querySelectorAll('.queue-check').forEach(cb => {
    cb.checked = checked;
  });
  updateBatchBar();
});

document.getElementById('queueBatchDeleteBtn').addEventListener('click', async () => {
  const ids = getCheckedTaskIds();
  if (ids.length === 0) return;
  if (!confirm(`确定删除 ${ids.length} 个任务吗？（运行中的任务将被强制终止）`)) return;

  try {
    const resp = await fetch('/command/tasks/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ node_id: state.selectedNodeId, task_ids: ids }),
    });
    const data = await resp.json();
    if (resp.ok) {
      showToast(data.message || `已删除`, 'success');
      fetchQueue();
    } else {
      showToast(data.error || '删除失败', 'error');
    }
  } catch (err) {
    showToast('网络错误: ' + err.message, 'error');
  }
});

function statusLabel(s) {
  const map = { queued: '排队中', running: '执行中', completed: '已完成', failed: '失败' };
  return map[s] || s;
}

// 缓存最近一次队列数据，用于本地筛选
let _lastQueueData = null;

async function fetchQueue() {
  if (!state.selectedNodeId) {
    queueList.innerHTML = '<div class="queue-empty">选择节点后刷新</div>';
    queueBatchBar.style.display = 'none';
    return;
  }

  try {
    const resp = await fetch(`/command/queue?node_id=${encodeURIComponent(state.selectedNodeId)}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    _lastQueueData = data;
    renderQueue(data);
  } catch (err) {
    queueList.innerHTML = `<div class="queue-empty">加载失败: ${err.message}</div>`;
    queueBatchBar.style.display = 'none';
  }
}

// Manual refresh
queueRefreshBtn.addEventListener('click', () => {
  queueRefreshBtn.classList.add('spinning');
  fetchQueue().finally(() => queueRefreshBtn.classList.remove('spinning'));
});

// Also refresh when switching nodes (handled in selectNode)

// User filter — 本地筛选
queueUserFilter.addEventListener('input', () => {
  if (_lastQueueData) renderQueue(_lastQueueData);
});

function buildExecutionTarget() {
  if (cmdTargetTypeEl.value !== 'docker_existing') return { type: 'host' };
  const container = cmdContainerEl.value.trim();
  if (!container) throw new Error('请输入已有 Docker 容器名称或 ID');
  const env = {};
  for (const rawLine of cmdEnvEl.value.split('\n')) {
    const line = rawLine.trim();
    if (!line) continue;
    const split = line.indexOf('=');
    if (split <= 0) throw new Error(`环境变量格式错误: ${line}`);
    env[line.slice(0, split).trim()] = line.slice(split + 1);
  }
  return {
    type: 'docker_existing',
    container,
    workdir: cmdWorkdirEl.value.trim(),
    user: cmdContainerUserEl.value.trim(),
    env,
  };
}

// ═══════════════════════════════════════════════════════════════
// Log viewer — 全量加载 + 进度条
// ═══════════════════════════════════════════════════════════════

// 处理 \r 字符：模拟终端行为，每行只保留最后一个 \r 之后的内容
function _handleCR(text) {
  if (!text || text.indexOf('\r') < 0) return text;
  return text.split('\n').map(line => {
    const idx = line.lastIndexOf('\r');
    return idx >= 0 ? line.substring(idx + 1) : line;
  }).join('\n');
}

function _renderMeta(task) {
  let m = `<div class="log-meta">`;
  m += `<strong>任务ID:</strong> ${escapeHtml(task.task_id)}<br>`;
  m += `<strong>用户:</strong> ${escapeHtml(task.user_id)}<br>`;
  m += `<strong>命令:</strong> ${escapeHtml(task.command)}<br>`;
  m += `<strong>资源:</strong> CPU=${task.cpu || 0}, 内存=${task.mem || '0'}, 设备=${task.device_num || 0}`;
  if (task.devices && task.devices.length > 0) {
    m += ` (${escapeHtml(task.devices.join(', '))})`;
  }
  m += `<br>`;
  m += `<strong>创建时间:</strong> ${formatTime(task.created_at)}<br>`;
  if (task.est_time > 0) {
    m += `<strong>预估耗时:</strong> ${task.est_time} 分钟<br>`;
  }
  if (task.priority > 0) {
    m += `<strong>优先级:</strong> ${task.priority}（赶论文）<br>`;
  }
  m += `<strong>状态:</strong> ${statusLabel(task.status)}`;
  if (task.result) {
    m += ` | <strong>返回码:</strong> ${task.result.returncode}`;
    if (task.result.timed_out) m += ` <span style="color:#ff5f57">(超时)</span>`;
  }
  m += `</div>`;
  return m;
}

function _renderProgress(total, loaded) {
  const pct = total > 0 ? Math.round(loaded / total * 100) : 0;
  const kb = total > 0 ? `${(loaded / 1024).toFixed(0)} / ${(total / 1024).toFixed(0)} KB` : '';
  return `<div class="log-progress">
    <div class="log-progress-bar" style="width:${pct}%"></div>
    <span class="log-progress-text">${pct}% ${kb}</span>
  </div>`;
}

async function viewTaskLog(taskId) {
  if (!state.selectedNodeId) return;
  if (state.mode !== 'command') switchMode('command');

  logPlaceholder.style.display = 'none';
  logContent.style.display = '';
  logActions.style.display = 'none';

  // 加载中 → 先显示进度条骨架
  logContent.innerHTML = _renderProgress(0, 0);

  try {
    // 1. 取元数据
    const metaResp = await fetch(
      `/command/result/${taskId}?node_id=${encodeURIComponent(state.selectedNodeId)}`);
    if (!metaResp.ok) throw new Error(`HTTP ${metaResp.status}`);
    const task = await metaResp.json();

    _currentTaskData = {
      task_id: task.task_id,
      node_id: state.selectedNodeId,
      command: task.command,
      user_id: task.user_id,
      cpu: task.cpu,
      mem: task.mem,
      device_num: task.device_num,
      devices: task.devices,
      status: task.status,
      task_result: task.result ? {
        status: task.status,
        command: task.command,
        cpu: task.cpu,
        mem: task.mem,
        device_num: task.device_num,
        created_at: task.created_at,
        result: task.result,
      } : null,
    };

    if (task.error) {
      logContent.innerHTML = `<div style="color:#ff5f57">错误: ${escapeHtml(task.error)}</div>`;
      return;
    }

    // 2. XHR 全量拉取日志，利用 onprogress 更新进度条
    const logText = await new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open('GET',
        `/command/result/${taskId}/log?node_id=${encodeURIComponent(state.selectedNodeId)}&raw=1`);
      xhr.responseType = 'text';

      let totalEst = 0;
      xhr.onprogress = () => {
        // 首次响应时从 Content-Length 头获取总大小
        if (!totalEst) {
          const cl = xhr.getResponseHeader('Content-Length');
          if (cl) totalEst = parseInt(cl, 10);
        }
        logContent.innerHTML = _renderProgress(totalEst || 1, xhr.responseText.length);
      };

      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(xhr.responseText);
        } else {
          reject(new Error(`HTTP ${xhr.status}`));
        }
      };
      xhr.onerror = () => reject(new Error('网络错误'));
      xhr.send();
    });

    // 3. 渲染：meta 用 innerHTML，日志正文用 textContent（避免大文本 escape 开销）
    logContent.innerHTML = _renderMeta(task);
    const processed = _handleCR(logText);
    if (processed) {
      const div = document.createElement('div');
      div.className = 'log-stdout';
      div.textContent = processed;
      logContent.appendChild(div);
    } else {
      const div = document.createElement('div');
      div.className = 'log-no-output';
      div.textContent = '(无输出)';
      logContent.appendChild(div);
    }

    // 滚到底部
    logContent.scrollTop = logContent.scrollHeight;

    if (task.status === 'completed' || task.status === 'failed') {
      logActions.style.display = '';
      document.getElementById('saveExpBtn').style.display = '';
    } else {
      // 运行中任务也显示导出按钮
      logActions.style.display = '';
      document.getElementById('saveExpBtn').style.display = 'none';
    }

  } catch (err) {
    logContent.innerHTML = `<div style="color:#ff5f57">加载失败: ${err.message}</div>`;
    logActions.style.display = 'none';
  }
}


// ── 导出日志 ──
document.getElementById('exportLogBtn').addEventListener('click', () => {
  if (!_currentTaskData) return;
  const el = logContent.querySelector('.log-stdout');
  const text = el ? el.textContent : (logContent.textContent || '');
  const ts = new Date().toISOString().slice(0, 19).replace(/:/g, '-');
  const filename = `${_currentTaskData.task_id}_${ts}.log`;
  const blob = new Blob([text], { type: 'text/plain' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
  showToast('日志已导出', 'success');
});

async function submitCommand() {
  const userId = cmdUserIdEl.value.trim();
  // 按行拆分，支持 \ 续行符，逻辑命令间 && 拼接
  const lines = cmdInputEl.value.trim()
    .split('\n').map(s => s.trim()).filter(s => s !== '');
  const commands = [];
  let buf = '';
  for (const line of lines) {
    if (line.endsWith('\\')) {
      buf += (buf ? ' ' : '') + line.slice(0, -1).trim();
    } else if (buf) {
      buf += ' ' + line;
      commands.push(buf);
      buf = '';
    } else {
      commands.push(line);
    }
  }
  if (buf) commands.push(buf);  // 最后一行以 \ 结尾的残余
  const command = commands.join(' && ');
  if (!userId)   { showToast('请输入用户标识', 'error'); return; }
  if (!command)  { showToast('请输入命令', 'error'); return; }

  const estTime = parseInt(cmdEstTimeEl.value, 10) || 0;

  let target;
  try {
    target = buildExecutionTarget();
  } catch (err) {
    showToast(err.message, 'error');
    return;
  }

  submitBtn.disabled = true;
  submitBtn.textContent = '提交中…';
  resultDiv.style.display = 'none';

  const body = {
    node_id:    state.selectedNodeId,
    user_id:    userId,
    command:    command,
    cpu:        state.cpu,
    memory:     state.memory,
    mem_unit:   state.memUnit,
    device_num: state.device_ids.length > 0 ? 0 : state.device_num,
    device_ids: state.device_ids.length > 0 ? state.device_ids.map(String) : undefined,
    est_time:   estTime,
    priority:   parseInt(cmdPriorityEl.value, 10) || 0,
    target,
  };

  try {
    const resp = await fetch('/command/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await resp.json();

    if (resp.ok) {
      showToast(`任务已提交，队列位置 #${data.position}`, 'success');
      cmdInputEl.value = '';
      resultDiv.className = 'result';
      resultDiv.innerHTML = `<strong>✓ 已提交</strong><br>任务ID: <code>${escapeHtml(data.task_id)}</code><br>队列位置: #${data.position}`;
      resultDiv.style.display = 'block';

      // Refresh queue immediately
      fetchQueue();
    } else {
      showToast(data.error || '提交失败', 'error');
      resultDiv.className = 'result error';
      resultDiv.innerHTML = `<strong>✗ 提交失败</strong><br>${escapeHtml(data.error || '未知错误')}`;
      resultDiv.style.display = 'block';
    }
  } catch (err) {
    showToast('网络错误: ' + err.message, 'error');
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = '提交命令';
  }
}


// ── 批量执行：每行一个任务，资源统一 ──
const batchBtn = document.getElementById('batchBtn');

batchBtn.addEventListener('click', async () => {
  const userId = cmdUserIdEl.value.trim();
  if (!userId) { showToast('请输入用户标识', 'error'); return; }

  // 按行拆分（不走 \ 续行拼接，每行独立任务）
  const lines = cmdInputEl.value.trim()
    .split('\n').map(s => s.trim()).filter(Boolean);
  if (lines.length < 2) {
    showToast('批量执行至少需要 2 行命令', 'error');
    return;
  }

  let target;
  try {
    target = buildExecutionTarget();
  } catch (err) {
    showToast(err.message, 'error');
    return;
  }

  if (!confirm(`将提交 ${lines.length} 个任务（资源统一为 CPU=${state.cpu}, 内存=${state.memory}${state.memUnit}, 设备=${state.device_num}）\n\n是否继续？`)) return;

  batchBtn.disabled = true;
  batchBtn.textContent = `提交中 0/${lines.length}…`;
  resultDiv.style.display = 'none';

  let ok = 0, fail = 0;
  for (let i = 0; i < lines.length; i++) {
    const body = {
      node_id:    state.selectedNodeId,
      user_id:    userId,
      command:    lines[i],
      cpu:        state.cpu,
      memory:     state.memory,
      mem_unit:   state.memUnit,
      device_num: state.device_ids.length > 0 ? 0 : state.device_num,
      device_ids: state.device_ids.length > 0 ? state.device_ids.map(String) : undefined,
      est_time:   parseInt(cmdEstTimeEl.value, 10) || 0,
      priority:   parseInt(cmdPriorityEl.value, 10) || 0,
      target,
    };
    try {
      const resp = await fetch('/command/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await resp.json();
      if (resp.ok) ok++; else fail++;
    } catch {
      fail++;
    }
    batchBtn.textContent = `提交中 ${i + 1}/${lines.length}…`;
  }

  batchBtn.disabled = false;
  batchBtn.textContent = '批量执行（每行一个任务）';
  showToast(`批量提交完成: 成功 ${ok} 个, 失败 ${fail} 个`, fail > 0 ? 'error' : 'success');
  cmdInputEl.value = '';
  fetchQueue();
  updateSubmitBtn();
});
