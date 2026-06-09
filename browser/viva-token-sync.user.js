// ==UserScript==
// @name         Viva Engage token-sync
// @namespace    viva-engage-rescue
// @version      1.5
// @description  Skickar din aktiva Viva Engage/Yammer-bearer-token till dump-panelen så fort webbläsaren förnyar den. Ingen credential lämnar din maskin utöver till din egen panel.
// @match        https://*.yammer.com/*
// @match        https://engage.cloud.microsoft/*
// @match        https://*.engage.cloud.microsoft/*
// @match        https://web.yammer.com/*
// @run-at       document-start
// @grant        GM_xmlhttpRequest
// @connect      ubuntu-ai
// ==/UserScript==

(function () {
  "use strict";

  // Panelens adress. Ändra om du kör den på annan host/port.
  const PANEL = "http://ubuntu-ai:8050";

  // Slå av om Viva börjar bråka - då används bara nät/lagring/IDB-fångst.
  const ENABLE_WORKER_HOOK = true;

  let lastSent = "";
  const stat = { yammer: 0, other: 0, lastAud: "-", sent: "", store: 0, worker: 0 };

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

  // Web Worker-hook: yammer-token används i worker-fetch (t.ex. /api/v2/events)
  // som huvudtrådens hooks inte ser. Wrappa workern så dess fetch rapporterar
  // Authorization via en BroadcastChannel (stör inte Vivas eget postMessage).
  if (ENABLE_WORKER_HOOK && typeof Worker !== "undefined" && typeof BroadcastChannel !== "undefined") {
    try {
      const bc = new BroadcastChannel("viva-token-sync");
      bc.onmessage = (e) => { if (e.data) { stat.worker++; grab("Bearer " + e.data); } };
      const Orig = window.Worker;
      window.Worker = function (url, opts) {
        if (opts && opts.type === "module") return new Orig(url, opts);  // importScripts stöds ej
        try {
          const abs = new URL(url, location.href).href;
          const shim =
            "(function(){var bc=new BroadcastChannel('viva-token-sync');var of=self.fetch;" +
            "self.fetch=function(i,n){try{var h=(n&&n.headers)||(i&&i.headers),a=null;" +
            "if(h){if(typeof h.get==='function')a=h.get('authorization');" +
            "else if(Array.isArray(h)){for(var p of h)if(String(p[0]).toLowerCase()==='authorization')a=p[1];}" +
            "else{for(var k in h)if(k.toLowerCase()==='authorization')a=h[k];}}" +
            "if(a&&a.indexOf('Bearer ')===0)bc.postMessage(a.slice(7));}catch(e){}" +
            "return of.apply(this,arguments);};" +
            "importScripts(" + JSON.stringify(abs) + ");})();";
          const burl = URL.createObjectURL(new Blob([shim], { type: "application/javascript" }));
          return new Orig(burl, opts);
        } catch (e) {
          return new Orig(url, opts);  // fallback: oförändrad worker
        }
      };
      window.Worker.prototype = Orig.prototype;
    } catch (e) { log("worker-hook misslyckades", e); }
  }

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
    el.style.background = (stat.yammer || stat.store) ? "#16a34a" : "#2563eb";
    el.textContent =
      "Viva-token-sync aktiv\n" +
      "Yammer-token: " + (stat.yammer + stat.store) +
        " (nät " + stat.yammer + " / lagring " + stat.store + " / worker " + stat.worker + ")" +
        (stat.sent ? "\nskickad " + stat.sent : "") + "\n" +
      "andra tokens: " + stat.other + "\n" +
      "senaste aud: " + stat.lastAud;
  }

  paint();
  log("aktivt på", location.href);
})();
