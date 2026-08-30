/**
 * Bridge Client: Manages WebSocket connection to local CLI server (ws://127.0.0.1:18888/ws)
 * and executes fetch() requests within the browser context.
 */

export class BridgeClient {
  constructor(port = 18888) {
    this.port = port;
    this.ws = null;
    this.connected = false;
    this.reconnectTimer = null;
    this.token = null;
    this.requestCount = 0;
  }

  async init() {
    const data = await chrome.storage.local.get(['bridge_token', 'bridge_port']);
    this.token = data.bridge_token || '';
    if (data.bridge_port) {
      this.port = data.bridge_port;
    }
    this.connect();
  }

  setToken(token) {
    this.token = token;
    chrome.storage.local.set({ bridge_token: token });
    if (this.ws) {
      this.ws.close();
    }
  }

  connect() {
    if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) {
      return;
    }

    const url = `ws://127.0.0.1:${this.port}/ws`;
    console.log(`[CTF Bridge] Connecting to ${url}...`);

    try {
      this.ws = new WebSocket(url);
    } catch (e) {
      console.warn('[CTF Bridge] Connection error:', e);
      this.scheduleReconnect();
      return;
    }

    this.ws.onopen = () => {
      console.log('[CTF Bridge] WebSocket connected! Sending handshake...');
      this.send('HANDSHAKE', {
        token: this.token,
        client: 'chrome-extension',
        version: '1.0.0'
      });
    };

    this.ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        this.handleMessage(msg);
      } catch (err) {
        console.error('[CTF Bridge] Error parsing message:', err);
      }
    };

    this.ws.onclose = () => {
      this.connected = false;
      this.updateBadge('OFF');
      this.scheduleReconnect();
    };

    this.ws.onerror = (err) => {
      console.warn('[CTF Bridge] WebSocket error:', err);
      this.connected = false;
      this.updateBadge('ERR');
    };
  }

  scheduleReconnect() {
    if (this.reconnectTimer) return;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, 3000);
  }

  send(type, payload) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.ws.send(JSON.stringify({ type, data: payload }));
  }

  updateBadge(status) {
    if (status === 'OK') {
      chrome.action.setBadgeText({ text: 'ON' });
      chrome.action.setBadgeBackgroundColor({ color: '#22c55e' }); // Green
    } else if (status === 'ERR') {
      chrome.action.setBadgeText({ text: '!' });
      chrome.action.setBadgeBackgroundColor({ color: '#ef4444' }); // Red
    } else {
      chrome.action.setBadgeText({ text: '' });
    }
  }

  async handleMessage(msg) {
    const { type, data } = msg;

    if (type === 'HANDSHAKE_ACK') {
      this.connected = true;
      this.updateBadge('OK');
      console.log('[CTF Bridge] Handshake ACK received:', data);
    } else if (type === 'REQUEST_FORWARD') {
      await this.executeForwardRequest(data);
    } else if (type === 'PING') {
      this.send('PONG', {});
    }
  }

  async executeForwardRequest(req) {
    const { id, method, url, headers, body, binary } = req;
    this.requestCount++;

    const fetchOptions = {
      method: method || 'GET',
      headers: { ...headers },
      credentials: 'include',
      mode: 'cors',
    };

    // Remove forbidden or browser-managed headers
    delete fetchOptions.headers['host'];
    delete fetchOptions.headers['origin'];
    delete fetchOptions.headers['referer'];
    delete fetchOptions.headers['content-length'];

    if (body && method !== 'GET' && method !== 'HEAD') {
      fetchOptions.body = body;
    }

    try {
      const response = await fetch(url, fetchOptions);
      const resHeaders = {};
      response.headers.forEach((val, key) => {
        resHeaders[key] = val;
      });

      let resBody = '';
      let isBase64 = false;

      if (binary) {
        const buffer = await response.arrayBuffer();
        const bytes = new Uint8Array(buffer);
        let binaryStr = '';
        const chunk = 8192;
        for (let i = 0; i < bytes.length; i += chunk) {
          binaryStr += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
        }
        resBody = btoa(binaryStr);
        isBase64 = true;
      } else {
        resBody = await response.text();
      }

      this.send('RESPONSE_FORWARD', {
        id,
        status_code: response.status,
        status_text: response.statusText,
        headers: resHeaders,
        body: resBody,
        is_base64: isBase64,
        error: null
      });

    } catch (err) {
      console.error('[CTF Bridge] Fetch failed for:', url, err);
      this.send('RESPONSE_FORWARD', {
        id,
        status_code: 0,
        status_text: 'Fetch Error',
        headers: {},
        body: '',
        is_base64: false,
        error: err.toString()
      });
    }
  }
}
