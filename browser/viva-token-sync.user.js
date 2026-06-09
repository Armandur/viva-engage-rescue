// ==UserScript==
// @name         Viva Engage token-sync
// @namespace    viva-engage-rescue
// @version      1.1
// @description  Skickar din aktiva Viva Engage/Yammer-bearer-token till dump-panelen så fort webbläsaren förnyar den. Ingen credential lämnar din maskin utöver till din egen panel.
// @match        https://*.yammer.com/*
// @match        https://engage.cloud.microsoft/*
// @match        https://*.engage.cloud.microsoft/*
// @run-at       document-start
// @grant        GM_xmlhttpRequest
// @grant        GM_setValue
// @grant        GM_getValue
// @connect      ubuntu-ai
// ==/UserScript==

(function () {
  "use strict";

  // Panelens adress. Ändra om du kör den på annan host/port.
  const PANEL = "http://ubuntu-ai:8050";

  let lastSent = "";

  function send(token) {
    if (!token || token === lastSent) return;
    lastSent = token;
    GM_xmlhttpRequest({
      method: "POST",
      url: PANEL + "/api/token",
      headers: { "Content-Type": "application/json" },
      data: JSON.stringify({ token: token }),
      onload: function (r) {
        if (r.status === 200) banner("Token skickad till panelen " + new Date().toLocaleTimeString());
        else banner("Panelen svarade " + r.status, true);
      },
      onerror: function () {
        banner("Når inte panelen (" + PANEL + ")", true);
        lastSent = "";  // tillåt nytt försök
      },
    });
  }

  // Sant bara för token vars audience är Yammer-API:t (din webbläsare skickar
  // även Graph-/SharePoint-tokens som vi inte vill posta).
  function audIsYammer(tok) {
    try {
      let b64 = tok.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
      while (b64.length % 4) b64 += "=";
      const aud = JSON.parse(atob(b64)).aud || "";
      return typeof aud === "string" && aud.indexOf("yammer.com") !== -1;
    } catch (e) {
      return false;
    }
  }

  // Plocka "Bearer <token>" ur valfri header-representation.
  function grab(value) {
    if (typeof value === "string" && value.indexOf("Bearer ") === 0) {
      const tok = value.slice(7).trim();
      if (audIsYammer(tok)) send(tok);
    }
  }
  function scanHeaders(h) {
    try {
      if (!h) return;
      if (typeof h.get === "function") { grab(h.get("authorization") || h.get("Authorization")); return; }
      if (Array.isArray(h)) { h.forEach(([k, v]) => { if (String(k).toLowerCase() === "authorization") grab(v); }); return; }
      Object.keys(h).forEach((k) => { if (k.toLowerCase() === "authorization") grab(h[k]); });
    } catch (e) {}
  }

  // Hooka fetch.
  const origFetch = window.fetch;
  window.fetch = function (input, init) {
    try {
      if (init && init.headers) scanHeaders(init.headers);
      if (input && input.headers) scanHeaders(input.headers);
    } catch (e) {}
    return origFetch.apply(this, arguments);
  };

  // Hooka XHR.
  const origSet = XMLHttpRequest.prototype.setRequestHeader;
  XMLHttpRequest.prototype.setRequestHeader = function (k, v) {
    try { if (String(k).toLowerCase() === "authorization") grab(v); } catch (e) {}
    return origSet.apply(this, arguments);
  };

  // Liten statusindikator längst ner till höger.
  let el;
  function banner(text, isError) {
    if (!el) {
      el = document.createElement("div");
      el.style.cssText =
        "position:fixed;bottom:12px;right:12px;z-index:99999;padding:6px 10px;" +
        "font:12px system-ui,sans-serif;border-radius:6px;color:#fff;opacity:.9;" +
        "box-shadow:0 1px 4px #0006;pointer-events:none";
      (document.body || document.documentElement).appendChild(el);
    }
    el.textContent = "Viva-token-sync: " + text;
    el.style.background = isError ? "#dc2626" : "#16a34a";
  }
})();
