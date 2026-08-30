/**
 * Cookie Tracker: Broadcasts fresh CTF platform cookies (cf_clearance, session) to CLI.
 */

export function setupCookieTracker(bridgeClient) {
  if (!chrome.cookies || !chrome.cookies.onChanged) return;

  chrome.cookies.onChanged.addListener((changeInfo) => {
    const { cookie, removed } = changeInfo;
    if (removed || !cookie) return;

    const name = cookie.name;
    const trackedNames = ['cf_clearance', 'session', 'GZCTF_Token', 'XSRF-TOKEN'];
    if (trackedNames.includes(name) || name.includes('session')) {
      if (bridgeClient && bridgeClient.connected) {
        bridgeClient.send('COOKIE_UPDATE', {
          domain: cookie.domain,
          name: cookie.name,
          value: cookie.value,
          path: cookie.path,
          secure: cookie.secure,
          httpOnly: cookie.httpOnly,
        });
      }
    }
  });
}
