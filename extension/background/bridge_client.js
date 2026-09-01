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

  bytesToBase64(bytes) {
    let binaryStr = '';
    const encodeStep = 8192;
    for (let i = 0; i < bytes.length; i += encodeStep) {
      binaryStr += String.fromCharCode.apply(
        null,
        bytes.subarray(i, i + encodeStep)
      );
    }
    return btoa(binaryStr);
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

      if (binary || response.body) {
        // Stream response bytes instead of materializing one large text or
        // ArrayBuffer payload. This protects both binary attachments and
        // large JSON/HTML responses from WebSocket per-message limits.
        // 192 KiB raw chunks become ~256 KiB base64 JSON frames, safely below
        // websockets' default 1 MiB per-message limit.
        this.send('RESPONSE_START', {
          id,
          status_code: response.status,
          status_text: response.statusText,
          headers: resHeaders,
          is_base64: true,
          error: null
        });

        const reader = response.body?.getReader();
        let seq = 0;
        let totalBytes = 0;
        const maxChunk = 192 * 1024;

        if (reader) {
          while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            if (!value || value.length === 0) continue;

            for (let offset = 0; offset < value.length; offset += maxChunk) {
              const chunk = value.subarray(
                offset,
                Math.min(offset + maxChunk, value.length)
              );
              totalBytes += chunk.length;
              this.send('RESPONSE_CHUNK', {
                id,
                seq,
                body: this.bytesToBase64(chunk)
              });
              seq++;
            }
          }
        } else {
          // Body-less / older browser fallback. Still split the payload
          // into bounded WebSocket frames when ReadableStream isn't available.
          const bytes = new Uint8Array(await response.arrayBuffer());
          for (let offset = 0; offset < bytes.length; offset += maxChunk) {
            const chunk = bytes.subarray(
              offset,
              Math.min(offset + maxChunk, bytes.length)
            );
            totalBytes += chunk.length;
            this.send('RESPONSE_CHUNK', {
              id,
              seq,
              body: this.bytesToBase64(chunk)
            });
            seq++;
          }
        }

        this.send('RESPONSE_END', {
          id,
          bytes: totalBytes,
          chunks: seq
        });
      } else {
        const resBody = await response.text();
        this.send('RESPONSE_FORWARD', {
          id,
          status_code: response.status,
          status_text: response.statusText,
          headers: resHeaders,
          body: resBody,
          is_base64: false,
          error: null
        });
      }

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
