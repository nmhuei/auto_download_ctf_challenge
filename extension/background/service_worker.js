import { BridgeClient } from './bridge_client.js';
import { setupCookieTracker } from './cookie_tracker.js';

const bridge = new BridgeClient();
bridge.init();
setupCookieTracker(bridge);

// Listen to messages from Popup UI
chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  if (request.action === 'GET_STATUS') {
    sendResponse({
      connected: bridge.connected,
      port: bridge.port,
      token: bridge.token,
      requestCount: bridge.requestCount,
    });
  } else if (request.action === 'RECONNECT') {
    bridge.connect();
    sendResponse({ status: 'reconnecting' });
  } else if (request.action === 'SET_TOKEN') {
    bridge.setToken(request.token);
    bridge.connect();
    sendResponse({ status: 'ok' });
  }
  return true;
});
