// ═══════════════════════════════════════════════════════════════
// Experiment DOM refs
// ═══════════════════════════════════════════════════════════════

const experimentPanel   = document.getElementById('experimentPanel');
const experimentList    = document.getElementById('experimentList');
const expRefreshBtn     = document.getElementById('expRefreshBtn');
const expSearchInput    = document.getElementById('expSearchInput');
const expModal          = document.getElementById('expModal');
const expModalTitle     = document.getElementById('expModalTitle');
const expModalClose     = document.getElementById('expModalClose');
const expTitle          = document.getElementById('expTitle');
const expTags           = document.getElementById('expTags');
const expId             = document.getElementById('expId');
const expTaskId         = document.getElementById('expTaskId');
const expNodeId         = document.getElementById('expNodeId');
const expCommand        = document.getElementById('expCommand');
const expSaveBtn        = document.getElementById('expSaveBtn');
const expDeleteBtn      = document.getElementById('expDeleteBtn');
const expFolder         = document.getElementById('expFolder');
const folderTree        = document.getElementById('folderTree');

// ═══════════════════════════════════════════════════════════════
// Notebook mode: 'preview' | 'edit'
// ═══════════════════════════════════════════════════════════════

let _notebookMode = 'preview';

// ═══════════════════════════════════════════════════════════════
// Folder state
// ═══════════════════════════════════════════════════════════════

let _currentFolderId = '';      // 当前选中的文件夹（'' = 全部）
let _folderData = [];

// ═══════════════════════════════════════════════════════════════
// Experiment management
// ═══════════════════════════════════════════════════════════════

async function fetchExperiments() {
  try {
    const search = expSearchInput.value.trim();
    const params = new URLSearchParams();
    if (search) params.set('search', search);
    if (_currentFolderId) params.set('folder_id', _currentFolderId);
    const resp = await fetch(`/experiments/?${params}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    renderExperimentList(data.experiments || []);
  } catch (err) {
    experimentList.innerHTML = `<div class="queue-empty">加载失败: ${err.message}</div>`;
  }
}

// ═══════════════════════════════════════════════════════════════
// Folder tree
// ═══════════════════════════════════════════════════════════════

async function fetchFolders() {
  try {
    const resp = await fetch('/experiments/folders');
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    _folderData = data.folders || [];
    renderFolderTree(_folderData);
  } catch (err) {
    // silently fail
  }
}

function renderFolderTree(folders) {
  let html = `<div class="folder-item${_currentFolderId === '' ? ' active' : ''}" data-folder-id="">
    <span class="folder-name">📁 全部实验</span>
    <span class="folder-actions" style="display:flex">
      <button class="folder-act-btn" data-action="add-child" title="新建文件夹">＋</button>
    </span>
  </div>`;
  for (const f of folders) {
    html += _renderFolderNode(f, 0);
  }
  folderTree.innerHTML = html;
}

function _renderFolderNode(f, depth) {
  const hasChildren = f.children && f.children.length > 0;
  const isExpanded = _expandedFolders.has(f.id);
  const count = f.total_exp_count || 0;
  const isActive = _currentFolderId === f.id;

  let html = `<div class="folder-item${hasChildren ? '' : ' no-children'}${isActive ? ' active' : ''}" data-folder-id="${f.id}">
    <span class="folder-arrow${isExpanded ? ' expanded' : ''}">▶</span>
    <span class="folder-name">📁 ${escapeHtml(f.name)}</span>
    <span class="folder-count">${count}</span>
    <span class="folder-actions">
      <button class="folder-act-btn" data-action="add-child" title="新建子文件夹">＋</button>
      <button class="folder-act-btn" data-action="rename" title="重命名">✎</button>
      <button class="folder-act-btn" data-action="delete" title="删除">🗑</button>
    </span>
  </div>`;

  if (hasChildren && isExpanded) {
    html += '<div class="folder-children">';
    for (const c of f.children) {
      html += _renderFolderNode(c, depth + 1);
    }
    html += '</div>';
  }
  return html;
}

const _expandedFolders = new Set();

async function selectFolder(folderId) {
  _currentFolderId = folderId;
  renderFolderTree(_folderData);
  await fetchExperiments();
}

folderTree.addEventListener('click', async (e) => {
  const item = e.target.closest('.folder-item');
  if (!item) return;

  const folderId = item.dataset.folderId;

  // 操作按钮
  const actBtn = e.target.closest('.folder-act-btn');
  if (actBtn) {
    e.stopPropagation();
    const action = actBtn.dataset.action;
    if (action === 'add-child') {
      _startInlineCreate(item, folderId);
    } else if (action === 'rename') {
      _startInlineRename(item, folderId);
    } else if (action === 'delete') {
      if (!confirm('确定删除此文件夹？子文件夹会上移，实验会移到上级文件夹。')) return;
      await fetch(`/experiments/folders/${encodeURIComponent(folderId)}`, { method: 'DELETE' });
      await fetchFolders();
      await fetchExperiments();
    }
    return;
  }

  // 折叠/展开
  const arrow = item.querySelector('.folder-arrow');
  if (arrow && e.target.closest('.folder-arrow') !== null && !item.classList.contains('no-children')) {
    if (_expandedFolders.has(folderId)) {
      _expandedFolders.delete(folderId);
    } else {
      _expandedFolders.add(folderId);
    }
    renderFolderTree(_folderData);
    return;
  }

  // 选中
  await selectFolder(folderId);
});

function _startInlineCreate(parentItem, parentId) {
  const existing = parentItem.nextElementSibling;
  if (existing && existing.classList.contains('folder-new-input-wrapper')) return;

  const wrapper = document.createElement('div');
  wrapper.className = 'folder-new-input-wrapper';
  wrapper.style.paddingLeft = '16px';
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'folder-new-input';
  input.placeholder = '新文件夹名称…';
  wrapper.appendChild(input);

  if (parentItem.nextSibling) {
    parentItem.parentNode.insertBefore(wrapper, parentItem.nextSibling);
  } else {
    parentItem.parentNode.appendChild(wrapper);
  }
  input.focus();

  const done = async () => {
    const name = input.value.trim();
    wrapper.remove();
    if (!name) return;
    await fetch('/experiments/folders', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, parent_id: parentId || null }),
    });
    _expandedFolders.add(parentId);
    await fetchFolders();
  };

  input.addEventListener('blur', done);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); done(); }
    if (e.key === 'Escape') { input.value = ''; input.blur(); }
  });
}

function _startInlineRename(item, folderId) {
  const nameEl = item.querySelector('.folder-name');
  if (!nameEl) return;
  const oldName = nameEl.textContent.replace(/^📁\s*/, '');
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'folder-new-input';
  input.value = oldName;
  input.style.margin = '0';
  nameEl.replaceWith(input);
  input.focus();
  input.select();

  const done = async () => {
    const newName = input.value.trim();
    if (newName && newName !== oldName) {
      await fetch(`/experiments/folders/${encodeURIComponent(folderId)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: newName }),
      });
      await fetchFolders();
    } else {
      renderFolderTree(_folderData);
    }
  };

  input.addEventListener('blur', done);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); done(); }
    if (e.key === 'Escape') { renderFolderTree(_folderData); }
  });
}

// ── 加载任务日志（优先 master 缓存，回退 worker） ──

async function _loadTaskLog(taskId, nodeId) {
  // 1. 先试 master 缓存
  try {
    const r = await fetch(`/experiments/log/${taskId}`);
    if (r.ok) return await r.text();
  } catch {}
  // 2. 回退 worker
  if (nodeId) {
    try {
      const r = await fetch(
        `/command/result/${taskId}/log?node_id=${encodeURIComponent(nodeId)}&raw=1`);
      if (r.ok) return await r.text();
    } catch {}
  }
  return null;
}

// ═══════════════════════════════════════════════════════════════
// Block rendering — edit mode
// ═══════════════════════════════════════════════════════════════

function blockEditHtml(block, index) {
  if (block.type === 'text') {
    return `
      <div class="nb-block" data-block-idx="${index}">
        <button class="nb-block-del" data-idx="${index}" title="删除">×</button>
        <textarea class="nb-textarea terminal-box" data-idx="${index}"
                  placeholder="写笔记…">${escapeHtml(block.content || '')}</textarea>
      </div>`;
  }
  if (block.type === 'task') {
    const hasRef = block.task_id;
    const inlineLog = block.log || '';
    const refAttrs = hasRef ? `data-task-id="${escapeHtml(block.task_id)}" data-node-id="${escapeHtml(block.node_id || '')}"` : '';
    return `
      <div class="nb-block" data-block-idx="${index}" ${refAttrs}>
        <button class="nb-block-del" data-idx="${index}" title="删除">×</button>
        <div class="nb-task-fields">
          <div class="nb-task-header">
            <input type="text" class="text-input nb-task-cmd" data-idx="${index}" data-field="command"
                   value="${escapeHtml(block.command || '')}" placeholder="命令，如 python train.py --lr 0.01">
            <button class="nb-task-toggle" data-idx="${index}" title="收起/展开日志">▶</button>
          </div>
          ${hasRef ? `
          <div class="nb-task-ref">
            <span class="nb-task-ref-label">任务引用: ${escapeHtml(block.task_id)} @ ${escapeHtml(block.node_id || '?')}</span>
            <button class="submit-btn nb-task-load" data-idx="${index}" style="padding:2px 10px;font-size:11px">加载日志</button>
          </div>
          <textarea class="nb-textarea terminal-box nb-task-log collapsed" data-idx="${index}" data-field="log"
                    placeholder="点击加载日志…" readonly></textarea>
          ` : `
          <textarea class="nb-textarea terminal-box nb-task-log collapsed" data-idx="${index}" data-field="log"
                    placeholder="日志输出…">${escapeHtml(inlineLog)}</textarea>
          `}
        </div>
      </div>`;
  }
  return '';
}

// ═══════════════════════════════════════════════════════════════
// Block rendering — preview mode
// ═══════════════════════════════════════════════════════════════

function blockPreviewHtml(block, index) {
  if (block.type === 'text') {
    const content = block.content || '';
    const html = content.trim() ? marked.parse(content) : '<p style="color:var(--sub);font-style:italic">空笔记</p>';
    return `
      <div class="nb-block nb-block-preview" data-block-idx="${index}">
        ${html}
      </div>`;
  }
  if (block.type === 'task') {
    const cmd = escapeHtml(block.command || '');
    const hasRef = block.task_id;
    const inlineLog = block.log || '';
    const hasContent = hasRef || (inlineLog && inlineLog.trim());
    return `
      <div class="nb-block" data-block-idx="${index}">
        <div class="nb-task-preview-cmd nb-task-cmd-toggle ${hasContent ? 'has-log' : ''}">
          ${hasContent ? '<span class="nb-toggle-arrow">▶</span> ' : ''}${cmd || '<span style="color:var(--sub);font-style:italic">无命令</span>'}
        </div>
        <div class="nb-task-preview-log" style="display:none" data-block-idx="${index}"
             ${hasRef ? `data-task-id="${escapeHtml(block.task_id)}" data-node-id="${escapeHtml(block.node_id || '')}"` : ''}>
          ${hasRef ? '<div class="nb-log-placeholder">点击展开以加载日志…</div>' : escapeHtml(inlineLog)}
        </div>
      </div>`;
  }
  return '';
}

function insertBarHtml(index) {
  return `
    <div class="nb-insert-bar" data-pos="${index}">
      <button class="nb-ins-btn" data-action="text" data-pos="${index}">+ 笔记</button>
      <button class="nb-ins-btn" data-action="task" data-pos="${index}">+ 任务</button>
    </div>`;
}

// ── Notebook block re-render (module-level, called from pickers) ──

function renderNotebookBlocks(currentExp) {
  _currentExpData = currentExp;
  const blocks = currentExp.blocks || [];
  const isEdit = _notebookMode === 'edit';
  const blockFn = isEdit ? blockEditHtml : blockPreviewHtml;
  let blocksHtml = '';
  if (blocks.length === 0) {
    if (isEdit) blocksHtml += insertBarHtml(0);
  } else {
    blocks.forEach((block, i) => {
      if (isEdit) blocksHtml += insertBarHtml(i);
      blocksHtml += blockFn(block, i);
    });
    if (isEdit) blocksHtml += insertBarHtml(blocks.length);
  }
  const area = document.getElementById('nbBlocksArea');
  if (area) area.innerHTML = blocksHtml;
  if (isEdit) bindNotebookEvents();
}

function bindNotebookEvents() {
  // 插入按钮
  document.querySelectorAll('.nb-ins-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const pos = parseInt(btn.dataset.pos);
      if (btn.dataset.action === 'text') {
        const blocks = collectNotebookBlocks();
        blocks.splice(pos, 0, { type: 'text', content: '' });
        _currentExpData.blocks = blocks;
        renderNotebookBlocks(_currentExpData);
      } else {
        showTaskPicker(pos, _currentExpData);
      }
    });
  });

  // 删除块
  document.querySelectorAll('.nb-block-del').forEach(btn => {
    btn.addEventListener('click', () => {
      const idx = parseInt(btn.dataset.idx);
      const blocks = collectNotebookBlocks();
      blocks.splice(idx, 1);
      _currentExpData.blocks = blocks;
      renderNotebookBlocks(_currentExpData);
    });
  });

  // 加载日志按钮（引用模式，edit 视图）
  document.querySelectorAll('.nb-task-load').forEach(btn => {
    btn.addEventListener('click', async () => {
      const idx = parseInt(btn.dataset.idx);
      const block = document.querySelector(`.nb-block[data-block-idx="${idx}"]`);
      if (!block) return;
      const logEl = block.querySelector('.nb-task-log');
      if (!logEl) return;
      const taskId = block.dataset.taskId;
      const nodeId = block.dataset.nodeId || state.selectedNodeId;
      if (!taskId) return;
      btn.textContent = '加载中…';
      btn.disabled = true;
      try {
        const text = await _loadTaskLog(taskId, nodeId);
        if (text != null) {
          logEl.value = _handleCR(text);
          logEl.readOnly = false;
          logEl.placeholder = '日志输出…';
          // 展开
          logEl.classList.remove('collapsed');
          const toggle = block.querySelector('.nb-task-toggle');
          if (toggle) toggle.textContent = '▼';
          logEl.style.height = 'auto';
          logEl.style.height = Math.max(44, logEl.scrollHeight) + 'px';
          btn.textContent = '已加载';
        } else {
          btn.textContent = '加载失败';
          btn.disabled = false;
        }
      } catch {
        btn.textContent = '加载失败';
        btn.disabled = false;
      }
    });
  });

  // 日志收起/展开
  document.querySelectorAll('.nb-task-toggle').forEach(btn => {
    btn.addEventListener('click', () => {
      const idx = parseInt(btn.dataset.idx);
      const block = document.querySelector(`.nb-block[data-block-idx="${idx}"]`);
      const logEl = block ? block.querySelector('.nb-task-log') : null;
      if (!logEl) return;
      const collapsed = logEl.classList.toggle('collapsed');
      btn.textContent = collapsed ? '▶' : '▼';
      if (!collapsed && !logEl.readOnly) {
        logEl.style.height = 'auto';
        logEl.style.height = Math.max(44, logEl.scrollHeight) + 'px';
      }
    });
  });

  // Textarea auto-height（跳过只读引用和已折叠的）
  document.querySelectorAll('.nb-textarea').forEach(ta => {
    if (ta.readOnly || ta.classList.contains('collapsed')) return;
    ta.style.height = 'auto';
    ta.style.height = Math.max(44, ta.scrollHeight) + 'px';
    ta.addEventListener('input', () => {
      ta.style.height = 'auto';
      ta.style.height = Math.max(44, ta.scrollHeight) + 'px';
    });
    // ── 粘贴图片上传 ──
    ta.addEventListener('paste', async (e) => {
      const items = e.clipboardData?.items;
      if (!items) return;
      for (const item of items) {
        if (item.type.startsWith('image/')) {
          e.preventDefault();
          const file = item.getAsFile();
          if (!file) continue;
          // 显示上传中提示
          const placeholder = '![上传中…]()';
          const start = ta.selectionStart;
          const end = ta.selectionEnd;
          const before = ta.value.substring(0, start);
          const after = ta.value.substring(end);
          ta.value = before + placeholder + after;
          ta.selectionStart = start + placeholder.length;
          ta.selectionEnd = start + placeholder.length;
          ta.dispatchEvent(new Event('input'));
          try {
            const form = new FormData();
            form.append('file', file);
            const r = await fetch('/experiments/upload-image', {
              method: 'POST',
              body: form,
            });
            const d = await r.json();
            if (r.ok && d.url) {
              const md = `![image](${d.url})`;
              const cur = ta.value;
              ta.value = cur.replace(placeholder, md);
              const pos = cur.indexOf(placeholder) + md.length;
              ta.selectionStart = pos;
              ta.selectionEnd = pos;
              showToast('图片已上传', 'success');
            } else {
              ta.value = ta.value.replace(placeholder, '');
              showToast(d.error || '上传失败', 'error');
            }
          } catch (err) {
            ta.value = ta.value.replace(placeholder, '');
            showToast('上传失败: ' + err.message, 'error');
          }
          ta.dispatchEvent(new Event('input'));
          return;
        }
      }
    });
  });
}

function collectNotebookBlocks() {
  // 预览模式下直接返回内存中的数据
  if (_notebookMode === 'preview') {
    return (_currentExpData && _currentExpData.blocks) ? [..._currentExpData.blocks] : [];
  }
  const blocks = [];
  const area = document.getElementById('nbBlocksArea');
  if (!area) return blocks;
  const blockEls = area.querySelectorAll('.nb-block');
  blockEls.forEach(el => {
    const ta = el.querySelector('.nb-textarea');
    const cmdInput = el.querySelector('.nb-task-cmd');
    if (cmdInput) {
      const logEl = el.querySelector('[data-field="log"]');
      const taskId = el.dataset.taskId;
      const nodeId = el.dataset.nodeId;
      const block = { type: 'task', command: cmdInput.value };
      if (taskId) {
        // 引用模式：保留 task_id + node_id，日志可选
        block.task_id = taskId;
        block.node_id = nodeId || '';
        if (logEl && logEl.value) block.log = logEl.value;
      } else {
        // 内联模式（兼容旧数据）
        block.log = logEl ? logEl.value : '';
      }
      blocks.push(block);
    } else if (ta) {
      blocks.push({ type: 'text', content: ta.value });
    }
  });
  return blocks;
}

// ── 任务选择弹窗（关联已有 / 手动输入） ──

function showTaskPicker(pos, expData) {
  // Remove any existing picker
  document.querySelectorAll('.nb-picker-overlay').forEach(el => el.remove());

  const overlay = document.createElement('div');
  overlay.className = 'nb-picker-overlay';
  overlay.innerHTML = `
    <div class="nb-picker-card">
      <div class="nb-picker-tabs">
        <button class="nb-picker-tab active" data-tab="link">🔗 关联已有任务</button>
        <button class="nb-picker-tab" data-tab="custom">✏️ 手动输入</button>
      </div>
      <div class="nb-picker-panel" id="pickerPanelLink">
        <input type="text" class="text-input" id="pickerTaskId" placeholder="Worker 上的 task_id" style="margin-bottom:6px">
        <input type="text" class="text-input" id="pickerNodeId" placeholder="节点（默认当前选中）" style="margin-bottom:6px">
        <button class="submit-btn" id="pickerFetchBtn" style="width:100%">获取日志</button>
        <div id="pickerFetchResult" style="margin-top:6px;font-size:12px"></div>
      </div>
      <div class="nb-picker-panel" id="pickerPanelCustom" style="display:none">
        <input type="text" class="text-input" id="pickerCmd" placeholder="命令，如 python train.py" style="margin-bottom:6px">
        <textarea class="text-input nb-picker-log" id="pickerLog" placeholder="日志输出…" rows="5" style="font-size:12px;font-family:monospace;resize:vertical"></textarea>
        <button class="submit-btn" id="pickerDoneCustom" style="width:100%;margin-top:6px">插入</button>
      </div>
    </div>`;

  document.body.appendChild(overlay);

  const close = () => overlay.remove();
  overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });

  // Tab switching
  overlay.querySelectorAll('.nb-picker-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      overlay.querySelectorAll('.nb-picker-tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      document.getElementById('pickerPanelLink').style.display = tab.dataset.tab === 'link' ? '' : 'none';
      document.getElementById('pickerPanelCustom').style.display = tab.dataset.tab === 'custom' ? '' : 'none';
    });
  });

  // Fetch from Worker
  overlay.querySelector('#pickerFetchBtn').addEventListener('click', async () => {
    const taskId = overlay.querySelector('#pickerTaskId').value.trim();
    const nodeId = overlay.querySelector('#pickerNodeId').value.trim() || state.selectedNodeId;
    const resultEl = overlay.querySelector('#pickerFetchResult');
    if (!taskId) { resultEl.innerHTML = '<span style="color:#ff5f57">请输入 task_id</span>'; return; }
    if (!nodeId) { resultEl.innerHTML = '<span style="color:#ff5f57">请选择节点</span>'; return; }
    resultEl.innerHTML = '获取中…';
    try {
      const params = new URLSearchParams({
        node_id: nodeId,
        user_id: state.cmdUserId,
      });
      const r = await fetch(`/command/result/${taskId}?${params}`);
      const task = await r.json();
      if (!r.ok) {
        resultEl.innerHTML = `<span style="color:#ff5f57">HTTP ${r.status}</span>`;
        return;
      }
      if (task.permission_denied) {
        resultEl.innerHTML = '<span style="color:#ff5f57">密码错误，无权限查看该任务</span>';
        return;
      }
      if (task.error) {
        resultEl.innerHTML = `<span style="color:#ff5f57">${escapeHtml(task.error)}</span>`;
        return;
      }
      const blocks = expData.blocks || [];
      blocks.splice(pos, 0, {
        type: 'task',
        command: task.command || '',
        task_id: taskId,
        node_id: nodeId,
      });
      expData.blocks = blocks;
      renderNotebookBlocks(expData);
      close();
    } catch (err) {
      console.error('fetch task log error:', err);
      resultEl.innerHTML = `<span style="color:#ff5f57">${escapeHtml(err.message || '网络错误')}</span>`;
    }
  });

  // Manual insert
  overlay.querySelector('#pickerDoneCustom').addEventListener('click', () => {
    const cmd = overlay.querySelector('#pickerCmd').value.trim();
    const log = overlay.querySelector('#pickerLog').value;
    const blocks = expData.blocks || [];
    blocks.splice(pos, 0, { type: 'task', command: cmd, log: log });
    expData.blocks = blocks;
    renderNotebookBlocks(expData);
    close();
  });
}

// ═══════════════════════════════════════════════════════════════
// Experiment list
// ═══════════════════════════════════════════════════════════════

function renderExperimentList(experiments) {
  if (experiments.length === 0) {
    experimentList.innerHTML = '<div class="queue-empty">暂无实验记录</div>';
    return;
  }
  experimentList.innerHTML = experiments.map(exp => {
    const tagsHtml = (exp.tags || []).map(t =>
      `<span class="exp-tag">${escapeHtml(t)}</span>`).join('');
    const timeStr = formatTime(exp.updated_at || exp.created_at);
    const blocks = exp.blocks || [];
    const taskCount = blocks.filter(b => b.type === 'task').length;
    const textCount = blocks.filter(b => b.type === 'text').length;
    const summary = [];
    if (textCount > 0) summary.push(`${textCount} 笔记`);
    if (taskCount > 0) summary.push(`${taskCount} 任务`);
    return `
      <div class="exp-card" data-exp-id="${escapeHtml(exp.id)}">
        <div class="exp-card-header">
          <span class="exp-card-title">${escapeHtml(exp.title)}</span>
          <span class="exp-card-time">${timeStr}</span>
        </div>
        <div class="exp-card-footer">
          <span class="exp-card-author">${escapeHtml(exp.created_by || '—')}</span>
          <span class="exp-card-sub">${summary.join(' · ')}</span>
          <span class="exp-card-tags">${tagsHtml}</span>
        </div>
      </div>`;
  }).join('');

  experimentList.querySelectorAll('.exp-card').forEach(card => {
    card.addEventListener('click', () => {
      experimentList.querySelectorAll('.exp-card.active').forEach(el => el.classList.remove('active'));
      card.classList.add('active');
      const expId = card.dataset.expId;
      if (expId) viewExperiment(expId);
    });
  });
}

// ═══════════════════════════════════════════════════════════════
// Export experiment to Markdown file
// ═══════════════════════════════════════════════════════════════

function exportExperimentToMd(title, tags, author, updatedAt, blocks) {
  const lines = [];
  lines.push(`# ${title}`);
  lines.push('');
  const meta = [];
  if (author) meta.push(`创建者: ${author}`);
  if (updatedAt) meta.push(`更新: ${formatTime(updatedAt)}`);
  if (tags && tags.length > 0) meta.push(`标签: ${tags.join(', ')}`);
  if (meta.length > 0) lines.push(`> ${meta.join(' | ')}`);
  lines.push('');

  (blocks || []).forEach((block, i) => {
    lines.push('---');
    lines.push('');
    if (block.type === 'text') {
      lines.push(block.content || '');
      lines.push('');
    } else if (block.type === 'task') {
      lines.push(`### 任务 ${i + 1}: \`${block.command || '未命名'}\``);
      lines.push('');
      if (block.log && block.log.trim()) {
        lines.push('```bash');
        lines.push(block.log);
        lines.push('```');
        lines.push('');
      }
    }
  });

  const md = lines.join('\n');
  const blob = new Blob([md], { type: 'text/markdown;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  const safeName = (title || 'experiment').replace(/[\\/:*?"<>|]/g, '-').substring(0, 60);
  a.download = `${safeName}.md`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
  showToast('已导出', 'success');
}

// ═══════════════════════════════════════════════════════════════
// Notebook editor
// ═══════════════════════════════════════════════════════════════

async function viewExperiment(expId) {
  logPlaceholder.style.display = 'none';
  logContent.style.display = '';
  logContent.innerHTML = '<div style="color:#636366">加载中…</div>';
  logActions.style.display = 'none';

  let exp;
  try {
    const resp = await fetch(`/experiments/${expId}`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    exp = await resp.json();
  } catch (err) {
    logContent.innerHTML = `<div style="color:#ff5f57">加载失败: ${err.message}</div>`;
    return;
  }

  _notebookMode = 'preview';
  const blocks = exp.blocks || [];
  const tagsStr = (exp.tags || []).join(', ');

  let html = '<div class="exp-notebook" id="notebookRoot">';

  // 标题
  html += `<input type="text" class="exp-nb-title" id="nbTitle" value="${escapeHtml(exp.title)}" placeholder="实验标题">`;

  // 元信息 + 标签
  html += '<div class="exp-detail-meta">';
  html += `<span>创建者: ${escapeHtml(exp.created_by || '—')}</span>`;
  html += `<span>更新: ${formatTime(exp.updated_at)}</span>`;
  html += '</div>';
  html += '<div class="exp-nb-field" style="margin-top:8px">';
  html += `<input type="text" class="text-input" id="nbTags" value="${escapeHtml(tagsStr)}" placeholder="标签，逗号分隔">`;
  html += '</div>';

  // 文件夹选择
  html += '<div class="exp-nb-field" style="margin-top:6px">';
  html += '<label>文件夹</label>';
  html += '<select class="text-input" id="nbFolder" style="width:100%">';
  html += '<option value="">— 无 —</option>';
  for (const f of _folderData) {
    html += _folderSelectOptions(f, '', exp.folder_id || '', 0);
  }
  html += '</select>';
  html += '</div>';

  // ── 模式切换 ──
  html += '<div class="nb-mode-bar">';
  html += '<button class="nb-mode-btn active" data-mode="preview">预览</button>';
  html += '<button class="nb-mode-btn" data-mode="edit">编辑</button>';
  html += '</div>';

  // ── 块区域 ──
  html += '<div class="nb-blocks-area" id="nbBlocksArea">';
  const isEdit = _notebookMode === 'edit';
  const blockFn = isEdit ? blockEditHtml : blockPreviewHtml;
  if (blocks.length === 0) {
    if (isEdit) html += insertBarHtml(0);
    else html += '<div style="color:var(--sub);font-size:13px;padding:8px 0;text-align:center">暂无内容，切换到编辑模式添加</div>';
  } else {
    blocks.forEach((block, i) => {
      if (isEdit) html += insertBarHtml(i);
      html += blockFn(block, i);
    });
    if (isEdit) html += insertBarHtml(blocks.length);
  }
  html += '</div>';

  // ── 保存 / 导出 / 删除 ──
  html += '<div class="exp-nb-actions">';
  html += `<button class="save-exp-btn" id="nbSaveBtn" style="flex:1">💾 保存</button>`;
  html += `<button class="save-exp-btn" id="nbExportBtn" style="flex:0">📥 导出</button>`;
  html += `<button class="submit-btn danger" id="nbDeleteBtn">🗑 删除</button>`;
  html += '</div>';

  html += '</div>'; // .exp-notebook

  logContent.innerHTML = html;

  // 设置当前实验引用 + 绑定块事件
  _currentExpData = exp;
  if (isEdit) bindNotebookEvents();

  // ── 模式切换 ──
  document.querySelectorAll('.nb-mode-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const newMode = btn.dataset.mode;
      if (newMode === _notebookMode) return;
      // 从编辑切换到预览时，先收集编辑内容
      if (_notebookMode === 'edit') {
        _currentExpData.blocks = collectNotebookBlocks();
      }
      _notebookMode = newMode;
      // 更新按钮 active 状态
      document.querySelectorAll('.nb-mode-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      // 完全重建笔记本视图
      renderNotebookBlocks(_currentExpData);
    });
  });

  // ── 保存 ──
  document.getElementById('nbSaveBtn').addEventListener('click', async () => {
    const title = document.getElementById('nbTitle').value.trim();
    if (!title) { showToast('标题不能为空', 'error'); return; }
    const tagsRaw = document.getElementById('nbTags').value.trim();
    const tags = tagsRaw ? tagsRaw.split(/[,，]/).map(t => t.trim()).filter(Boolean) : [];
    const blocks = collectNotebookBlocks();
    // 收集需要缓存的日志：已有内容直接发，无内容则从 master 缓存或 worker 拉
    const logs = {};
    for (const b of blocks) {
      if (b.type === 'task' && b.task_id && b.log) {
        logs[b.task_id] = b.log;
      } else if (b.type === 'task' && b.task_id) {
        try {
          const text = await _loadTaskLog(b.task_id, b.node_id);
          if (text != null) logs[b.task_id] = text;
        } catch {}
      }
    }
    try {
      const r = await fetch(`/experiments/${expId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          title, blocks, tags, logs,
          folder_id: nbFolder?.value || null,
        }),
      });
      const d = await r.json();
      if (r.ok) {
        showToast('已保存', 'success');
        // 更新本地缓存供重新渲染
        exp.title = title;
        exp.blocks = blocks;
        exp.tags = tags;
        if (state.mode === 'experiment') fetchExperiments();
      } else {
        showToast(d.error || '保存失败', 'error');
      }
    } catch (err) {
      showToast('网络错误: ' + err.message, 'error');
    }
  });

  // ── 导出 ──
  document.getElementById('nbExportBtn').addEventListener('click', () => {
    const title = document.getElementById('nbTitle').value.trim() || exp.title;
    const tagsRaw = document.getElementById('nbTags').value.trim();
    const tags = tagsRaw ? tagsRaw.split(/[,，]/).map(t => t.trim()).filter(Boolean) : [];
    const blocks = _notebookMode === 'edit' ? collectNotebookBlocks() : (_currentExpData.blocks || []);
    exportExperimentToMd(title, tags, exp.created_by, exp.updated_at, blocks);
  });

  // ── 删除 ──
  document.getElementById('nbDeleteBtn').addEventListener('click', async () => {
    if (!confirm('确定删除？')) return;
    try {
      const r = await fetch(`/experiments/${expId}`, { method: 'DELETE' });
      if (r.ok) {
        showToast('已删除', 'success');
        logPlaceholder.style.display = '';
        logContent.style.display = 'none';
        logContent.innerHTML = '';
        if (state.mode === 'experiment') fetchExperiments();
      } else {
        const d = await r.json();
        showToast(d.error || '删除失败', 'error');
      }
    } catch (err) {
      showToast('网络错误: ' + err.message, 'error');
    }
  });
}

// ═══════════════════════════════════════════════════════════════
// Quick-save from command log (modal)
// ═══════════════════════════════════════════════════════════════

function openExpModal(taskData) {
  expTitle.value = '';
  expTags.value = '';
  expId.value = '';
  expTaskId.value = '';
  expNodeId.value = '';
  expCommand.value = '';
  expDeleteBtn.style.display = 'none';
  expModalTitle.textContent = '保存实验记录';
  expFolder.value = '';

  // 填充文件夹选项
  _populateFolderSelect();

  if (taskData) {
    expTitle.value = taskData.command ? taskData.command.substring(0, 80) : '';
  }

  expModal.style.display = '';
}

function _populateFolderSelect(selectedId) {
  let opts = '<option value="">— 无 —</option>';
  for (const f of _folderData) {
    opts += _folderSelectOptions(f, '', selectedId || '', 0);
  }
  expFolder.innerHTML = opts;
}

function _folderSelectOptions(f, prefix, selectedId, depth) {
  const indent = '  '.repeat(depth);
  const sel = f.id === selectedId ? ' selected' : '';
  let html = `<option value="${f.id}"${sel}>${indent}📁 ${escapeHtml(f.name)}</option>`;
  if (f.children) {
    for (const c of f.children) {
      html += _folderSelectOptions(c, prefix, selectedId, depth + 1);
    }
  }
  return html;
}

function closeExpModal() {
  expModal.style.display = 'none';
}

async function saveExperiment() {
  const title = expTitle.value.trim();
  if (!title) { showToast('请输入实验标题', 'error'); return; }
  const tagsRaw = expTags.value.trim();
  const tags = tagsRaw ? tagsRaw.split(/[,，]/).map(t => t.trim()).filter(Boolean) : [];

  // 构建 blocks：存引用（task_id + node_id）
  const blocks = [];
  const logs = {};
  if (_currentTaskData && _currentTaskData.task_result) {
    const tid = _currentTaskData.task_id;
    const nid = _currentTaskData.node_id;
    blocks.push({
      type: 'task',
      command: _currentTaskData.command || '',
      task_id: tid,
      node_id: nid,
    });
    // 从 worker 拉日志副本，存到 master 缓存
    try {
      const logResp = await fetch(
        `/command/result/${tid}/log?node_id=${encodeURIComponent(nid)}&raw=1`);
      if (logResp.ok) logs[tid] = await logResp.text();
    } catch {}
  }

  try {
    const r = await fetch('/experiments/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        title, blocks, tags, logs,
        created_by: state.cmdUserId || '',
        folder_id: expFolder.value || null,
      }),
    });
    const d = await r.json();
    if (r.ok) {
      showToast('实验记录已创建', 'success');
      closeExpModal();
      if (state.mode === 'experiment') fetchExperiments();
    } else {
      showToast(d.error || '保存失败', 'error');
    }
  } catch (err) {
    showToast('网络错误: ' + err.message, 'error');
  }
}

async function deleteExperiment() {
  showToast('请在实验详情中使用删除按钮', 'error');
}

// ═══════════════════════════════════════════════════════════════
// Experiment event bindings
// ═══════════════════════════════════════════════════════════════

saveExpBtn.addEventListener('click', () => {
  if (_currentTaskData) {
    openExpModal(_currentTaskData);
  }
});

const expNewBtn = document.getElementById('expNewBtn');
expNewBtn.addEventListener('click', () => {
  _currentTaskData = null;
  openExpModal(null);
});

expSaveBtn.addEventListener('click', saveExperiment);
expDeleteBtn.addEventListener('click', deleteExperiment);
expModalClose.addEventListener('click', closeExpModal);
expModal.addEventListener('click', (e) => {
  if (e.target === expModal) closeExpModal();
});

expRefreshBtn.addEventListener('click', () => {
  expRefreshBtn.classList.add('spinning');
  fetchExperiments().finally(() => expRefreshBtn.classList.remove('spinning'));
});

expSearchInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    e.preventDefault();
    fetchExperiments();
  }
});

expSearchInput.addEventListener('input', () => {
  if (expSearchInput.value.trim() === '') {
    fetchExperiments();
  }
});

// ═══════════════════════════════════════════════════════════════
// Init
// ═══════════════════════════════════════════════════════════════

// ── 日志折叠（全局委托） + 引用模式懒加载 ──
document.getElementById('logContent').addEventListener('click', async (e) => {
  const toggle = e.target.closest('.nb-task-cmd-toggle.has-log');
  if (!toggle) return;
  const block = toggle.closest('.nb-block');
  const logEl = block ? block.querySelector('.nb-task-preview-log') : null;
  const arrow = toggle.querySelector('.nb-toggle-arrow');
  if (!logEl || !arrow) return;

  const isHidden = logEl.style.display === 'none';
  if (isHidden) {
    // 引用模式：首次展开时从 worker 懒加载日志
    const taskId = logEl.dataset.taskId;
    if (taskId && logEl.querySelector('.nb-log-placeholder')) {
      const nodeId = logEl.dataset.nodeId || state.selectedNodeId;
      logEl.innerHTML = '<span style="color:var(--sub)">加载中…</span>';
      try {
        const text = await _loadTaskLog(taskId, nodeId);
        if (text != null) {
          logEl.innerHTML = _handleCR(text).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        } else {
          logEl.textContent = '(加载失败)';
        }
      } catch {
        logEl.textContent = '(加载失败)';
      }
    }
    logEl.style.display = '';
    arrow.classList.add('expanded');
  } else {
    logEl.style.display = 'none';
    arrow.classList.remove('expanded');
  }
});

// ── Ctrl+S 保存实验 ──
document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === 's') {
    const saveBtn = document.getElementById('nbSaveBtn');
    if (saveBtn && saveBtn.offsetParent !== null) {
      e.preventDefault();
      saveBtn.click();
    }
  }
});

// Initial queue load if node is selected
if (state.selectedNodeId) {
  fetchQueue();
}
