document.addEventListener('DOMContentLoaded', () => {
  const statusEl = document.getElementById('status-indicator');
  const portEl = document.getElementById('port-display');
  const countEl = document.getElementById('req-count');
  const tokenInput = document.getElementById('token-input');
  const saveBtn = document.getElementById('btn-save');
  const refreshBtn = document.getElementById('btn-refresh');

  function updateUI() {
    chrome.runtime.sendMessage({ action: 'GET_STATUS' }, (response) => {
      if (!response) return;
      if (response.connected) {
        statusEl.textContent = 'CONNECTED';
        statusEl.className = 'status connected';
      } else {
        statusEl.textContent = 'DISCONNECTED';
        statusEl.className = 'status disconnected';
      }
      portEl.textContent = response.port || '18888';
      countEl.textContent = response.requestCount || '0';
      if (response.token && !tokenInput.value) {
        tokenInput.value = response.token;
      }
    });
  }

  saveBtn.addEventListener('click', () => {
    const token = tokenInput.value.trim();
    chrome.runtime.sendMessage({ action: 'SET_TOKEN', token }, () => {
      setTimeout(updateUI, 500);
    });
  });

  refreshBtn.addEventListener('click', () => {
    chrome.runtime.sendMessage({ action: 'RECONNECT' }, () => {
      setTimeout(updateUI, 500);
    });
  });

  updateUI();
  setInterval(updateUI, 1000);
});
