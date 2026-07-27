// ═══════════════════════════════════════════════════════════════
// State
// ═══════════════════════════════════════════════════════════════
const state = {
  mode:       null,            // set by switchMode on init
  selectedNodeId: null,
  cpu:        20,
  memory:     20,
  memUnit:    'GB',
  device_num: 1,
  device_ids: [],   // 用户选中的卡号
  cmdUserId:  localStorage.getItem('neu_box_cmd_user') || '',
};

const limits = {
  cpu:        { min: 0, max: Infinity   },
  memory:     { min: 0, max: Infinity  },
  device_num: { min: 0, max: 16   },
};

// ═══════════════════════════════════════════════════════════════
// DOM refs
// ═══════════════════════════════════════════════════════════════
const form              = document.getElementById('mainForm');
const submitBtn         = document.getElementById('submitBtn');
const resultDiv         = document.getElementById('result');
const toast             = document.getElementById('toast');
const memUnitEl         = document.getElementById('memUnit');
const nodeList          = document.getElementById('nodeList');
const nodeCount         = document.getElementById('nodeCount');
const refreshBtn        = document.getElementById('refreshBtn');

// Mode toggle
const modeToggle        = document.getElementById('modeToggle');
const terminalFields    = document.getElementById('terminalFields');
const commandFields     = document.getElementById('commandFields');

// Terminal fields
const usernameInput     = document.getElementById('usernameInput');
const passwordInput     = document.getElementById('passwordInput');

// Command fields
const cmdUserIdEl       = document.getElementById('cmdUserId');
const cmdInputEl        = document.getElementById('cmdInput');

// Queue
const queueList         = document.getElementById('queueList');
const queueRefreshBtn   = document.getElementById('queueRefreshBtn');
const queueBatchBar     = document.getElementById('queueBatchBar');
const queueBatchDeleteBtn = document.getElementById('queueBatchDeleteBtn');
const queueUserFilter    = document.getElementById('queueUserFilter');
const devicePicker       = document.getElementById('devicePicker');
const devicePickerField  = document.getElementById('devicePickerField');

// Experiment (shared refs)
const logActions        = document.getElementById('logActions');
const saveExpBtn        = document.getElementById('saveExpBtn');
// shared with experiment.js
let _currentTaskData = null;
let _currentExpData = null;

// Right panel
const rightPanel        = document.getElementById('rightPanel');
const terminalHeader    = document.getElementById('terminalHeader');
const terminalPlaceholder = document.getElementById('terminalPlaceholder');
const terminalIframe    = document.getElementById('terminalIframe');
const terminalUrlEl     = document.getElementById('terminalUrl');
const terminalClose     = document.getElementById('terminalClose');
const logViewer         = document.getElementById('logViewer');
const logPlaceholder    = document.getElementById('logPlaceholder');
const logContent        = document.getElementById('logContent');

// Init command user ID
if (state.cmdUserId) cmdUserIdEl.value = state.cmdUserId;

// ═══════════════════════════════════════════════════════════════
// Notebook layout helpers
// ═══════════════════════════════════════════════════════════════

function clearNotebookBars() {
  const top = document.getElementById('nbTopBar');
  const bot = document.getElementById('nbBottomActions');
  if (top) top.remove();
  if (bot) bot.remove();
}

// ═══════════════════════════════════════════════════════════════
// Formatting helpers
// ═══════════════════════════════════════════════════════════════

function formatBytes(bytes) {
  if (!bytes || bytes === 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0, val = bytes;
  while (val >= 1024 && i < units.length - 1) { val /= 1024; i++; }
  return val >= 100 ? `${Math.round(val)} ${units[i]}` : `${val.toFixed(1)} ${units[i]}`;
}

function isIdlePercent(cpuIdle) {
  // Worker 始终上报百分比（0-100），用 ≤100 判断，避免 100.0 / 0.0 等整数百分比误判为核心数
  return typeof cpuIdle === 'number' && cpuIdle <= 100;
}

function formatCpu(cpuIdle, cpuTotal) {
  if (!cpuTotal) return '? / ? 核';
  if (isIdlePercent(cpuIdle)) {
    const usedCores = ((100 - cpuIdle) / 100) * cpuTotal;
    return `${usedCores.toFixed(1)} / ${cpuTotal} 核`;
  }
  return `${cpuTotal - cpuIdle} / ${cpuTotal} 核`;
}

function cpuUsedPercent(cpuIdle, cpuTotal) {
  if (!cpuTotal) return 0;
  if (isIdlePercent(cpuIdle)) return 100 - cpuIdle;
  return ((cpuTotal - cpuIdle) / cpuTotal) * 100;
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

function formatTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const pad = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())} ` +
         `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

// ═══════════════════════════════════════════════════════════════
// Toast
// ═══════════════════════════════════════════════════════════════

function showToast(msg, type) {
  toast.textContent = msg;
  toast.className = `toast ${type}`;
  toast.style.display = 'block';
  setTimeout(() => { toast.style.display = 'none'; }, 3000);
}

// ═══════════════════════════════════════════════════════════════
// Mode switching
// ═══════════════════════════════════════════════════════════════

function switchMode(mode) {
  if (mode === state.mode) return;
  state.mode = mode;

  modeToggle.querySelectorAll('button').forEach(b => {
    b.classList.toggle('active', b.dataset.mode === mode);
  });

  if (mode === 'terminal') {
    terminalFields.style.display = '';
    commandFields.style.display = 'none';
    queuePanel.style.display = '';
    experimentPanel.style.display = 'none';
    submitBtn.style.display = '';
    submitBtn.textContent = '申请终端';
    rightPanel.classList.add('mode-terminal');
    rightPanel.classList.remove('mode-command');
    // 终端模式中栏显示活跃终端 & 沙盒
    document.querySelector('#queuePanel .section-label').textContent = '活跃终端 / 沙盒';
    document.getElementById('queueSelectAll').style.display = 'none';
    queueBatchBar.style.display = 'none';
    if (queueUserFilter) queueUserFilter.parentElement.style.display = 'none';
    fetchSandboxes();
  } else if (mode === 'command') {
    document.querySelector('#queuePanel .section-label').textContent = '任务队列';
    document.getElementById('queueSelectAll').style.display = '';
    if (queueUserFilter) queueUserFilter.parentElement.style.display = '';
    terminalFields.style.display = 'none';
    commandFields.style.display = '';
    // fetchQueue 在 command.js 中，此时可能尚未加载，延迟调用
    if (typeof fetchQueue === 'function') fetchQueue();
    queuePanel.style.display = '';
    experimentPanel.style.display = 'none';
    submitBtn.style.display = '';
    submitBtn.textContent = '提交命令';
    rightPanel.classList.add('mode-command');
    rightPanel.classList.remove('mode-terminal');
    // Reset log viewer to placeholder
    clearNotebookBars();
    logPlaceholder.style.display = '';
    logContent.style.display = 'none';
    logContent.innerHTML = '';
    logActions.style.display = 'none';
  } else if (mode === 'experiment') {
    terminalFields.style.display = 'none';
    commandFields.style.display = 'none';
    queuePanel.style.display = 'none';
    experimentPanel.style.display = '';
    submitBtn.style.display = 'none';
    rightPanel.classList.add('mode-command');
    rightPanel.classList.remove('mode-terminal');
    // Reset log viewer
    clearNotebookBars();
    logPlaceholder.style.display = '';
    logContent.style.display = 'none';
    logContent.innerHTML = '';
    logActions.style.display = 'none';
    // Load experiments and folders
    fetchFolders();
    fetchExperiments();
  }

  updateSubmitBtn();
  resultDiv.style.display = 'none';
}

modeToggle.addEventListener('click', (e) => {
  const btn = e.target.closest('button');
  if (!btn) return;
  switchMode(btn.dataset.mode);
});

// Init mode classes
rightPanel.classList.add('mode-command');

// ═══════════════════════════════════════════════════════════════
// Node rendering
// ═══════════════════════════════════════════════════════════════

function renderDeviceChips(idle) {
  if (idle === 0) return '<span class="device-text">无</span>';
  let html = '<span class="device-chips">';
  for (let i = 0; i < idle; i++) {
    html += `<span class="device-chip idle"></span>`;
  }
  html += '</span>';
  html += `<span class="device-text">${idle} 可用</span>`;
  return html;
}

function renderDevicePicker(devStatus) {
  // devStatus = {1: 0, 2: 1, 3: 0, ...}  key=minor, value=1忙碌/0空闲
  if (!devStatus || Object.keys(devStatus).length === 0) {
    devicePickerField.style.display = 'none';
    state.device_ids = [];
    return;
  }
  devicePickerField.style.display = '';
  const current = new Set(state.device_ids);
  const ids = Object.keys(devStatus).map(Number).sort((a, b) => a - b);
  devicePicker.innerHTML = ids.map(id => {
    const busy = devStatus[id];
    const checked = current.has(id) ? 'checked' : '';
    const disabled = busy ? 'disabled' : '';
    const label = busy ? `卡${id} (占用)` : `卡${id}`;
    return `<label class="device-check ${busy ? 'busy' : 'free'}">
      <input type="checkbox" value="${id}" ${checked} ${disabled}> ${label}
    </label>`;
  }).join('');
  // 监听变化
  devicePicker.querySelectorAll('input:not([disabled])').forEach(cb => {
    cb.addEventListener('change', () => {
      state.device_ids = Array.from(
        devicePicker.querySelectorAll('input:checked:not([disabled])')
      ).map(cb => parseInt(cb.value));
      // 选了卡就禁用数量 stepper
      updateSubmitBtn();
    });
  });
}

function progressClass(percent) {
  return percent > 85 ? 'high' : '';
}

function renderNodeCard(node) {
  const isSelected = node.node_id === state.selectedNodeId;
  const isOnline  = node.status === 'online';
  const cpuPct     = cpuUsedPercent(node.idle_cpu, node.total_cpu);
  const memUsed    = node.total_mem - node.idle_mem;
  const memPct     = node.total_mem > 0 ? (memUsed / node.total_mem) * 100 : 0;
  const memClass   = node.total_mem > 0 ? progressClass(memPct) : '';
  const cpuClass   = node.total_cpu > 0 ? progressClass(cpuPct) : '';

  return `
    <div class="node-card ${isSelected ? 'selected' : ''}"
         data-node-id="${node.node_id}"
         role="button" tabindex="0">
      <div class="node-card-header">
        <span class="node-card-addr">${node.name}</span>
        <span class="node-status-dot ${isOnline ? 'online' : 'offline'}"
              title="${isOnline ? '在线' : '离线'}"></span>
      </div>
      ${isOnline ? `
      <div class="node-resources">
        <div class="resource-row">
          <span class="resource-label">CPU</span>
          <div class="progress-bar">
            <div class="progress-fill ${cpuClass}" style="width:${cpuPct}%"></div>
          </div>
          <span class="resource-text">${formatCpu(node.idle_cpu, node.total_cpu)}</span>
        </div>
        <div class="resource-row">
          <span class="resource-label">MEM</span>
          <div class="progress-bar">
            <div class="progress-fill mem ${memClass}" style="width:${memPct}%"></div>
          </div>
          <span class="resource-text">${formatBytes(memUsed)} / ${formatBytes(node.total_mem)}</span>
        </div>
        <div class="device-row">
          <span class="device-label">设备</span>
          ${renderDeviceChips(node.idle_devices)}
          <span class="device-text" style="margin-left:4px">/ ${node.total_devices} 总</span>
        </div>
        ${node.active_sandboxes > 0 ? `
        <div class="sandbox-count">${node.active_sandboxes} 个沙盒运行中</div>
        ` : ''}
      </div>
      ` : `
      <div class="node-resources">
        <span style="font-size:10px;color:var(--sub)">节点离线</span>
      </div>
      `}
    </div>`;
}

function renderNodeCards(nodes) {
  nodeList.innerHTML = nodes.map(renderNodeCard).join('');

  nodeList.querySelectorAll('.node-card').forEach(card => {
    card.addEventListener('click', () => {
      const nodeId = card.dataset.nodeId;
      if (!nodeId) return;
      selectNode(nodeId, nodes);
    });
  });
}

function selectNode(nodeId, nodes) {
  state.selectedNodeId = nodeId;
  state.device_ids = [];  // 切节点清空已选卡
  renderNodeCards(nodes);
  updateSubmitBtn();
  // 渲染设备选择框
  const node = nodes.find(n => n.node_id === nodeId);
  renderDevicePicker(node ? node.dev_status : {});
  // 根据当前模式刷新中栏
  if (state.mode === 'terminal') fetchSandboxes();
  else fetchQueue();
}

// ═══════════════════════════════════════════════════════════════
// Node fetching
// ═══════════════════════════════════════════════════════════════

async function fetchNodes() {
  try {
    const resp = await fetch('/nodes/get_all_nodes', { method: 'POST' });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();

    const nodes = data.nodes || [];

    if (nodes.length === 0) {
      nodeList.innerHTML = `
        <div class="node-card node-card-loading">
          <span class="node-status-dot offline"></span>
          <span style="color:var(--sub)">无可用节点</span>
        </div>`;
      nodeCount.textContent = '无可用';
      submitBtn.disabled = true;
      return;
    }

    nodeCount.textContent = `${nodes.length} 个可用`;

    const currentSelected = nodes.find(n => n.node_id === state.selectedNodeId);
    if (!currentSelected) {
      const firstOnline = nodes.find(n => n.status === 'online');
      if (firstOnline) {
        state.selectedNodeId = firstOnline.node_id;
      }
    }

    renderNodeCards(nodes);
    updateSubmitBtn();
    // 根据当前模式刷新中栏
    if (state.selectedNodeId) {
      if (state.mode === 'terminal') fetchSandboxes();
      else fetchQueue();
    }

  } catch (err) {
    nodeList.innerHTML = `
      <div class="node-card node-card-loading">
        <span class="node-status-dot offline"></span>
        <span style="color:var(--sub)">节点加载失败</span>
      </div>`;
    nodeCount.textContent = '加载失败';
    showToast('节点列表加载失败: ' + err.message, 'error');
    submitBtn.disabled = true;
  }
}

fetchNodes();
setInterval(fetchNodes, 60000);

// ═══════════════════════════════════════════════════════════════
// Submit button logic
// ═══════════════════════════════════════════════════════════════

function isSelectedNodeOnline() {
  const cards = nodeList.querySelectorAll('.node-card');
  for (const card of cards) {
    if (card.dataset.nodeId === state.selectedNodeId) {
      const dot = card.querySelector('.node-status-dot');
      return dot && dot.classList.contains('online');
    }
  }
  return false;
}

function updateSubmitBtn() {
  const hasNode = !!state.selectedNodeId;
  const online = hasNode && isSelectedNodeOnline();

  if (!hasNode || !online) {
    submitBtn.disabled = true;
    return;
  }

  if (state.mode === 'terminal') {
    submitBtn.disabled = !(usernameInput.value.trim() && passwordInput.value);
  } else {
    submitBtn.disabled = !(cmdUserIdEl.value.trim() && cmdInputEl.value.trim());
  }
}

// Listen to input changes
usernameInput.addEventListener('input', updateSubmitBtn);
passwordInput.addEventListener('input', updateSubmitBtn);
cmdUserIdEl.addEventListener('input', () => {
  state.cmdUserId = cmdUserIdEl.value.trim();
  localStorage.setItem('neu_box_cmd_user', state.cmdUserId);
  updateSubmitBtn();
});
cmdInputEl.addEventListener('input', updateSubmitBtn);

// ═══════════════════════════════════════════════════════════════
// Stepper events
// ═══════════════════════════════════════════════════════════════

function setValDisplay(el, field, val) {
  if (val === 0) {
    if (el.tagName === 'INPUT') el.value = '不限制';
    else el.textContent = '不限制';
    el.classList.add('no-limit');
  } else {
    if (el.tagName === 'INPUT') el.value = val;
    else el.textContent = val;
    el.classList.remove('no-limit');
  }
}

function parseValInput(el, field) {
  const raw = (el.tagName === 'INPUT' ? el.value : el.textContent).trim();
  if (raw === '不限制' || raw === '') return 0;
  const n = parseInt(raw, 10);
  if (isNaN(n) || n < 0) return state[field];
  return n;
}

document.querySelectorAll('.stepper').forEach(stepper => {
  const field = stepper.dataset.field;
  const valEl = stepper.querySelector('.value');

  stepper.addEventListener('click', (e) => {
    const btn = e.target.closest('button');
    if (!btn) return;

    const action = btn.dataset.action;
    const lim    = limits[field];
    const cur    = state[field];
    const next   = action === 'up' ? cur + 1 : cur - 1;

    if (next < lim.min || next > lim.max) return;

    state[field] = next;
    setValDisplay(valEl, field, next);
    updateButtons(stepper, field);
  });

  const commitInput = () => {
    let val = parseValInput(valEl, field);
    const lim = limits[field];
    if (val < lim.min) val = lim.min;
    if (val > lim.max) val = lim.max;
    state[field] = val;
    setValDisplay(valEl, field, val);
    updateButtons(stepper, field);
  };

  valEl.addEventListener('blur', commitInput);
  valEl.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      valEl.blur();
    }
  });

  setValDisplay(valEl, field, state[field]);
  updateButtons(stepper, field);
});

function updateButtons(stepper, field) {
  const lim = limits[field];
  const cur = state[field];
  stepper.querySelector('[data-action=down]').disabled = cur <= lim.min;
  stepper.querySelector('[data-action=up]').disabled   = cur >= lim.max;
}

// ═══════════════════════════════════════════════════════════════
// Memory unit toggle
// ═══════════════════════════════════════════════════════════════

memUnitEl.addEventListener('click', (e) => {
  const btn = e.target.closest('button');
  if (!btn) return;

  memUnitEl.querySelectorAll('button').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  state.memUnit = btn.dataset.unit;

  if (state.memUnit === 'GB') {
    limits.memory = { min: 0, max: 256 };
    if (state.memory > 256) state.memory = 256;
  } else {
    limits.memory = { min: 0, max: 65536 };
    if (state.memory > 65536) state.memory = 65536;
  }

  const memStepper = document.querySelector('[data-field=memory]');
  const memValEl = memStepper.querySelector('.value');
  setValDisplay(memValEl, 'memory', state.memory);
  updateButtons(memStepper, 'memory');
});

// ═══════════════════════════════════════════════════════════════
// Manual refresh
// ═══════════════════════════════════════════════════════════════

refreshBtn.addEventListener('click', () => {
  refreshBtn.classList.add('spinning');
  fetchNodes().finally(() => refreshBtn.classList.remove('spinning'));
});

// ═══════════════════════════════════════════════════════════════
// Form submit
// ═══════════════════════════════════════════════════════════════

form.addEventListener('submit', async (e) => {
  e.preventDefault();

  if (!state.selectedNodeId) {
    showToast('请先选择一个节点', 'error');
    return;
  }

  if (state.mode === 'terminal') {
    await submitTerminal();
  } else {
    await submitCommand();
  }
});
// ═══════════════════════════════════════════════════════════════
// Node management modal
// ═══════════════════════════════════════════════════════════════

const manageModal    = document.getElementById('manageModal');
const manageNodesBtn = document.getElementById('manageNodesBtn');
const modalClose     = document.getElementById('modalClose');
const configNodeList = document.getElementById('configNodeList');
const configNodeCount = document.getElementById('configNodeCount');
const newNodeName    = document.getElementById('newNodeName');
const newNodeHost    = document.getElementById('newNodeHost');
const newNodePort    = document.getElementById('newNodePort');
const addNodeBtn     = document.getElementById('addNodeBtn');

function openManageModal() {
  manageModal.style.display = '';
  fetchConfigNodes();
}

function closeManageModal() {
  manageModal.style.display = 'none';
}

manageNodesBtn.addEventListener('click', openManageModal);
modalClose.addEventListener('click', closeManageModal);
manageModal.addEventListener('click', (e) => {
  if (e.target === manageModal) closeManageModal();
});

async function fetchConfigNodes() {
  try {
    const resp = await fetch('/nodes/config');
    const data = await resp.json();
    const nodes = data.nodes || [];
    configNodeCount.textContent = `${nodes.length} 个`;
    renderConfigNodes(nodes);
  } catch (err) {
    configNodeList.innerHTML = `<div style="color:var(--danger);font-size:13px;text-align:center;padding:12px">加载失败: ${err.message}</div>`;
  }
}

function renderConfigNodes(nodes) {
  if (nodes.length === 0) {
    configNodeList.innerHTML = '<div style="color:var(--sub);font-size:13px;text-align:center;padding:12px">无已配置节点</div>';
    return;
  }
  configNodeList.innerHTML = nodes.map(n => `
    <div class="modal-node-item">
      <span class="node-name">${escapeHtml(n.name)}</span>
      <span class="node-addr">${escapeHtml(n.host)}:${n.port}</span>
      <button class="modal-delete-btn" data-name="${escapeHtml(n.name)}" title="删除节点">×</button>
    </div>
  `).join('');

  configNodeList.querySelectorAll('.modal-delete-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      const name = btn.dataset.name;
      if (!confirm(`确定要删除节点 "${name}" 吗？`)) return;
      await removeConfigNode(name);
    });
  });
}

async function addConfigNode() {
  const name = newNodeName.value.trim();
  const host = newNodeHost.value.trim();
  const port = parseInt(newNodePort.value.trim(), 10);

  if (!name) { showToast('请输入节点名称', 'error'); return; }
  if (!host) { showToast('请输入 host', 'error'); return; }
  if (isNaN(port) || port < 1 || port > 65535) { showToast('端口必须在 1-65535 之间', 'error'); return; }

  addNodeBtn.disabled = true;
  addNodeBtn.textContent = '…';

  try {
    const resp = await fetch('/nodes/config/add', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, host, port }),
    });
    const data = await resp.json();
    if (resp.ok) {
      showToast(data.message, 'success');
      newNodeName.value = '';
      newNodeHost.value = '';
      newNodePort.value = '';
      fetchConfigNodes();
      fetchNodes(); // 刷新主节点列表
    } else {
      showToast(data.error || '添加失败', 'error');
    }
  } catch (err) {
    showToast('网络错误: ' + err.message, 'error');
  } finally {
    addNodeBtn.disabled = false;
    addNodeBtn.textContent = '添加';
  }
}

async function removeConfigNode(name) {
  try {
    const resp = await fetch('/nodes/config/remove', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    const data = await resp.json();
    if (resp.ok) {
      showToast(data.message, 'success');
      fetchConfigNodes();
      fetchNodes(); // 刷新主节点列表
    } else {
      showToast(data.error || '删除失败', 'error');
    }
  } catch (err) {
    showToast('网络错误: ' + err.message, 'error');
  }
}

addNodeBtn.addEventListener('click', addConfigNode);
// Enter key in port field triggers add
newNodePort.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') addConfigNode();
});

// ═══════════════════════════════════════════════════════════════
// Theme toggle
// ═══════════════════════════════════════════════════════════════

const themeToggle = document.getElementById('themeToggle');

function applyTheme(dark) {
  if (dark) {
    document.documentElement.setAttribute('data-theme', 'dark');
    themeToggle.textContent = '☀️';
  } else {
    document.documentElement.removeAttribute('data-theme');
    themeToggle.textContent = '🌙';
  }
}

// 从 localStorage 读取，默认浅色
const savedTheme = localStorage.getItem('neu_box_theme');
applyTheme(savedTheme === 'dark');

themeToggle.addEventListener('click', () => {
  const isDark = document.documentElement.hasAttribute('data-theme');
  applyTheme(!isDark);
  localStorage.setItem('neu_box_theme', !isDark ? 'dark' : 'light');
});

// Init UI to default mode
switchMode('command');

// ── 通知弹窗（每次进入页面弹出） ──
(async () => {
  try {
    const resp = await fetch('/static/notice.txt');
    if (!resp.ok) return;
    const text = await resp.text();
    if (!text.trim()) return;
    document.getElementById('noticeModalBody').textContent = text;
    const modal = document.getElementById('noticeModal');
    const close = document.getElementById('noticeModalClose');
    modal.style.display = '';
    const hide = () => { modal.style.display = 'none'; };
    close.onclick = hide;
    modal.onclick = (e) => { if (e.target === modal) hide(); };
  } catch {}
})();
