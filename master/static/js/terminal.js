// Terminal functions

// ═══════════════════════════════════════════════════════════════
// 活跃终端 & 沙盒列表（terminal 模式中栏显示）
// ═══════════════════════════════════════════════════════════════

async function fetchSandboxes() {
  if (!state.selectedNodeId) {
    queueList.innerHTML = '<div class="queue-empty">选择节点后刷新</div>';
    return;
  }
  try {
    const resp = await fetch(`/nodes/${encodeURIComponent(state.selectedNodeId)}/sandboxes`);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    renderSandboxes(data.sandboxes || []);
  } catch (err) {
    queueList.innerHTML = `<div class="queue-empty">加载失败: ${err.message}</div>`;
  }
}

function parseSandboxName(name) {
  // 统一格式: sbx_{owner}_{id}.slice
  if (name.startsWith('sbx_')) {
    const clean = name.endsWith('.slice') ? name.slice(0, -6) : name;
    const parts = clean.split('_');
    // parts = ['sbx', owner, id, ...]
    const owner = parts.length > 1 ? parts[1] : '?';
    const id = parts.length > 2 ? parts.slice(2).join('_') : '';
    return { owner, id };
  }
  return { owner: '?', id: name };
}

function renderSandboxes(sandboxes) {
  if (!sandboxes || sandboxes.length === 0) {
    queueList.innerHTML = '<div class="queue-empty">无活跃沙盒</div>';
    return;
  }

  queueList.innerHTML = sandboxes.map(sb => {
    const name = sb.name || '';
    const info = parseSandboxName(name);
    const devices = (sb.devices && sb.devices.length > 0)
      ? sb.devices.map(d => {
          const parts = (d+'').split(':');
          return parts[1] || parts[0];
        }).join(', ')
      : '';
    const res = [];
    if (sb.cpu) res.push(`CPU ${sb.cpu}`);
    if (sb.mem && sb.mem !== '0') res.push(sb.mem);
    const resStr = res.length > 0 ? res.join(' ') : '';
    const hasPort = !!sb.port;
    const icon = hasPort ? '⌨' : '📦';

    return `
      <div class="sandbox-item">
        <span class="sandbox-icon">${icon}</span>
        <div class="sandbox-info">
          <div class="sandbox-line1">
            <span class="sandbox-user">${escapeHtml(info.owner)}</span>
            <span class="sandbox-id" title="${escapeHtml(info.id)}">${escapeHtml(info.id.slice(0, 12))}</span>
          </div>
          <div class="sandbox-line2">
            ${devices ? `<span class="sandbox-dev">卡 ${devices}</span>` : ''}
            ${resStr ? `<span class="sandbox-res">${resStr}</span>` : ''}
          </div>
        </div>
      </div>`;
  }).join('');
}

// Terminal
// ═══════════════════════════════════════════════════════════════

function openTerminal(url) {
  terminalIframe.src = url;
  terminalUrlEl.textContent = url;
  rightPanel.classList.add('active');
}

function closeTerminal() {
  terminalIframe.src = '';
  terminalUrlEl.textContent = '';
  rightPanel.classList.remove('active');
}

terminalClose.addEventListener('click', closeTerminal);


async function submitTerminal() {
  const username = usernameInput.value.trim();
  const password = passwordInput.value;
  if (!username) { showToast('请输入用户名', 'error'); return; }
  if (!password) { showToast('请输入密码', 'error'); return; }

  submitBtn.disabled = true;
  submitBtn.textContent = '正在申请…';
  resultDiv.style.display = 'none';

  const body = {
    node_id:  state.selectedNodeId,
    cpu:      state.cpu,
    memory:   state.memory,
    mem_unit: state.memUnit,
    device_num: state.device_ids.length > 0 ? 0 : state.device_num,
    device_ids: state.device_ids.length > 0 ? state.device_ids.map(String) : undefined,
    username: username,
    password: password,
  };

  try {
    const resp = await fetch('/terminal/create', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await resp.json();

    if (resp.ok) {
      showToast('终端申请成功', 'success');
      resultDiv.className = 'result';
      resultDiv.innerHTML =
        `<strong>✓ 终端已创建</strong><br>` +
        (data.sandbox_id ? `ID: <code>${data.sandbox_id}</code><br>` : '') +
        (data.message ? `${data.message}<br>` : '');

      if (data.terminal_url) {
        resultDiv.innerHTML += `地址: <code>${data.terminal_url}</code>`;
        openTerminal(data.terminal_url);
      }
      resultDiv.style.display = 'block';
      fetchNodes();
    } else {
      const errMsg = data.error || '申请失败';
      const errDetail = data.details ? `<br><span style="font-size:12px;color:#8e8e93">${escapeHtml(data.details)}</span>` : '';
      showToast(errMsg, 'error');
      resultDiv.className = 'result error';
      resultDiv.innerHTML = `<strong>✗ ${escapeHtml(errMsg)}</strong>${errDetail}`;
      resultDiv.style.display = 'block';
    }
  } catch (err) {
    showToast('网络错误，请稍后重试', 'error');
    resultDiv.className = 'result error';
    resultDiv.innerHTML = `<strong>✗ 网络错误</strong><br>${escapeHtml(err.message)}`;
    resultDiv.style.display = 'block';
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = '申请终端';
  }
}
