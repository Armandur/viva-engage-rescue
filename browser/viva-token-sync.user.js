// ==UserScript==
// @name         Viva Engage token-sync
// @namespace    viva-engage-rescue
// @version      1.6
// @description  Hämtar din aktiva Yammer-aud-token via MSAL (acquireTokenSilent) och skickar den till dump-panelen. MSAL sköter förnyelse, så token hålls färsk automatiskt. Ingen credential lämnar din maskin utöver till din egen panel.
// @match        https://*.yammer.com/*
// @match        https://engage.cloud.microsoft/*
// @match        https://*.engage.cloud.microsoft/*
// @match        https://web.yammer.com/*
// @run-at       document-start
// @grant        GM_xmlhttpRequest
// @grant        unsafeWindow
// @connect      ubuntu-ai
// ==/UserScript==

(function () {
  "use strict";

  // Panelens adress. Ändra om du kör den på annan host/port.
  const PANEL = "http://ubuntu-ai:8050";

  // Yammer-API-token identifieras av dessa MSAL-scopes.
  const YAMMER_SCOPES = ["https://www.yammer.com/access_as_user"];
  const PAGE = typeof unsafeWindow !== "undefined" ? unsafeWindow : window;

  let lastSent = "";
  const stat = { yammer: 0, other: 0, lastAud: "-", sent: "", store: 0, msal: 0 };

  function log() {
    console.debug("[viva-token-sync]", ...arguments);
  }

  function send(token) {
    if (!token || token === lastSent) return;
    lastSent = token;
    GM_xmlhttpRequest({
      method: "POST",
      url: PANEL + "/api/token",
      headers: { "Content-Type": "application/json" },
      data: JSON.stringify({ token: token }),
      onload: function (r) {
        if (r.status === 200) { stat.sent = new Date().toLocaleTimeString(); paint(); }
        else { paint("Panelen svarade " + r.status, true); }
        log("POST /api/token ->", r.status, r.responseText);
      },
      onerror: function () {
        paint("Når inte panelen (" + PANEL + ")", true);
        lastSent = "";
      },
    });
  }

  // UTF-8-säker avkodning av JWT-payload.
  function decodeJwt(tok) {
    let b64 = tok.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    while (b64.length % 4) b64 += "=";
    const bin = atob(b64);
    const bytes = Uint8Array.from(bin, (c) => c.charCodeAt(0));
    return JSON.parse(new TextDecoder("utf-8").decode(bytes));
  }

  // Hantera en sedd "Bearer <token>".
  function grab(value) {
    if (typeof value !== "string" || value.indexOf("Bearer ") !== 0) return;
    const tok = value.slice(7).trim();
    let aud = "(odekodbar)";
    try { aud = decodeJwt(tok).aud || "(ingen aud)"; } catch (e) {}
    stat.lastAud = String(aud).slice(0, 48);
    if (String(aud).indexOf("yammer.com") !== -1) {
      stat.yammer++;
      log("Yammer-token, aud =", aud);
      send(tok);
    } else {
      stat.other++;
      log("annan token, aud =", aud);
    }
    paint();
  }

  function scanHeaders(h) {
    try {
      if (!h) return;
      if (typeof h.get === "function") { grab(h.get("authorization") || h.get("Authorization")); return; }
      if (Array.isArray(h)) { h.forEach(([k, v]) => { if (String(k).toLowerCase() === "authorization") grab(v); }); return; }
      Object.keys(h).forEach((k) => { if (k.toLowerCase() === "authorization") grab(h[k]); });
    } catch (e) {}
  }

  const origFetch = window.fetch;
  window.fetch = function (input, init) {
    try {
      if (init && init.headers) scanHeaders(init.headers);
      if (input && input.headers) scanHeaders(input.headers);
    } catch (e) {}
    return origFetch.apply(this, arguments);
  };

  const origSet = XMLHttpRequest.prototype.setRequestHeader;
  XMLHttpRequest.prototype.setRequestHeader = function (k, v) {
    try { if (String(k).toLowerCase() === "authorization") grab(v); } catch (e) {}
    return origSet.apply(this, arguments);
  };

  // MSAL-vägen (primär): be Yammer Web:s MSAL-instans om en färsk yammer-token.
  // Token cachas krypterat i localStorage med icke-extraherbar nyckel, så enda
  // pålitliga vägen är MSAL:s eget API - som dessutom förnyar tyst åt oss.
  function findPca(root) {
    const seen = new Set(); const stack = [root]; let n = 0;
    while (stack.length && n++ < 8000) {
      const o = stack.pop();
      if (!o || typeof o !== "object" || seen.has(o)) continue;
      seen.add(o);
      if (typeof o.acquireTokenSilent === "function" && typeof o.getAllAccounts === "function") return o;
      for (const k in o) { try { stack.push(o[k]); } catch (e) {} }
    }
    return null;
  }

  async function pollMsal() {
    const pca = findPca(PAGE.msal) || findPca(PAGE);
    if (!pca) return false;
    try {
      const account = (pca.getActiveAccount && pca.getActiveAccount()) ||
                      (pca.getAllAccounts() || [])[0];
      const res = await pca.acquireTokenSilent({ scopes: YAMMER_SCOPES, account });
      if (res && res.accessToken) { stat.msal++; grab("Bearer " + res.accessToken); }
      return true;
    } catch (e) {
      log("acquireTokenSilent fel", e && e.errorCode || e);
      return true;  // instans hittad även om just detta anrop fallerade
    }
  }

  // MSAL laddas efter sidstart - leta tills instansen finns, glesa sedan ut.
  let msalTries = 0;
  const msalFind = setInterval(async () => {
    const found = await pollMsal();
    if (found) { clearInterval(msalFind); setInterval(pollMsal, 5 * 60 * 1000); }
    else if (++msalTries > 60) clearInterval(msalFind);  // ge upp efter ~3 min
  }, 3000);

  // Skanna webblagring efter en giltig, cachad Yammer-token (MSAL m.fl.).
  function looksLikeJwt(s) {
    return typeof s === "string" && s.split(".").length === 3 && s.length > 100;
  }
  function tryStored(s) {
    if (!looksLikeJwt(s)) return;
    let c;
    try { c = decodeJwt(s); } catch (e) { return; }
    if (String(c.aud || "").indexOf("yammer.com") === -1) return;
    if (c.exp && c.exp * 1000 < Date.now() + 60000) return;  // utgången/snart
    stat.store++;
    stat.lastAud = String(c.aud).slice(0, 48);
    log("Yammer-token i lagring, aud =", c.aud);
    send(s);
    paint();
  }
  function scanStorage() {
    [localStorage, sessionStorage].forEach((store) => {
      try {
        for (let i = 0; i < store.length; i++) {
          const v = store.getItem(store.key(i));
          tryStored(v);
          try {
            const o = JSON.parse(v);
            if (o && typeof o.secret === "string") tryStored(o.secret);  // MSAL-credential
          } catch (e) {}
        }
      } catch (e) {}
    });
  }
  scanStorage();
  setInterval(scanStorage, 30000);

  // Tidsboxad skanning av IndexedDB (MSAL kan cacha token där). Körs ett fåtal
  // gånger, inte i evig loop - async och potentiellt tungt.
  async function scanIndexedDB() {
    if (!indexedDB.databases) return;
    let dbs;
    try { dbs = await indexedDB.databases(); } catch (e) { return; }
    for (const info of dbs) {
      if (!info.name) continue;
      await new Promise((res) => {
        let req;
        try { req = indexedDB.open(info.name); } catch (e) { return res(); }
        req.onerror = () => res();
        req.onsuccess = () => {
          const db = req.result;
          const stores = Array.from(db.objectStoreNames);
          if (!stores.length) { db.close(); return res(); }
          let pending = stores.length;
          const done = () => { if (--pending === 0) { db.close(); res(); } };
          let tx;
          try { tx = db.transaction(stores, "readonly"); } catch (e) { db.close(); return res(); }
          stores.forEach((name) => {
            const g = tx.objectStore(name).getAll();
            g.onerror = done;
            g.onsuccess = () => {
              (g.result || []).forEach((v) => {
                if (typeof v === "string") tryStored(v);
                else if (v && typeof v === "object") {
                  if (typeof v.secret === "string") tryStored(v.secret);
                  Object.values(v).forEach((x) => { if (typeof x === "string") tryStored(x); });
                }
              });
              done();
            };
          });
        };
      });
    }
    paint();
  }
  scanIndexedDB();
  setTimeout(scanIndexedDB, 8000);
  setTimeout(scanIndexedDB, 30000);

  // Statusruta nere till höger - visas direkt så du ser att scriptet kör.
  let el;
  function paint(msg, isError) {
    if (!el) {
      if (!document.body) { setTimeout(() => paint(msg, isError), 200); return; }
      el = document.createElement("div");
      el.style.cssText =
        "position:fixed;bottom:12px;right:12px;z-index:99999;padding:6px 10px;" +
        "font:12px/1.4 system-ui,sans-serif;border-radius:6px;color:#fff;opacity:.92;" +
        "box-shadow:0 1px 4px #0006;max-width:320px;white-space:pre-line";
      document.body.appendChild(el);
    }
    if (msg) {
      el.textContent = "Viva-token-sync: " + msg;
      el.style.background = isError ? "#dc2626" : "#2563eb";
      return;
    }
    el.style.background = (stat.yammer || stat.store || stat.msal) ? "#16a34a" : "#2563eb";
    el.textContent =
      "Viva-token-sync aktiv\n" +
      "Yammer-token: " + (stat.yammer + stat.store + stat.msal) +
        " (MSAL " + stat.msal + " / nät " + stat.yammer + " / lagring " + stat.store + ")" +
        (stat.sent ? "\nskickad " + stat.sent : "") + "\n" +
      "andra tokens: " + stat.other + "\n" +
      "senaste aud: " + stat.lastAud;
  }

  paint();
  log("aktivt på", location.href);
})();
