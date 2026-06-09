// ==UserScript==
// @name         Viva Engage token-sync
// @namespace    viva-engage-rescue
// @version      1.2
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

  let lastSent = "";
  const stat = { yammer: 0, other: 0, lastAud: "-", sent: "" };

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
    el.style.background = stat.yammer ? "#16a34a" : "#2563eb";
    el.textContent =
      "Viva-token-sync aktiv\n" +
      "Yammer-token: " + stat.yammer + (stat.sent ? " (skickad " + stat.sent + ")" : "") + "\n" +
      "andra tokens: " + stat.other + "\n" +
      "senaste aud: " + stat.lastAud;
  }

  paint();
  log("aktivt på", location.href);
})();
