// ==UserScript==
// @name         Viva Engage token-sync
// @namespace    viva-engage-rescue
// @version      1.7
// @description  Fångar din aktiva Yammer-aud-token ur sidans fetch och skickar den till dump-panelen. Piggybackar på appens egna API-anrop, så token hålls färsk. Ingen credential lämnar din maskin utöver till din egen panel.
// @match        https://*.yammer.com/*
// @match        https://engage.cloud.microsoft/*
// @match        https://*.engage.cloud.microsoft/*
// @match        https://web.yammer.com/*
// @run-at       document-start
// @grant        GM_xmlhttpRequest
// @grant        unsafeWindow
// @connect      ubuntu-ai
// ==/UserScript==

// Token bärs via window.fetch i huvudtråden (init.headers är ett Headers-objekt).
// Eftersom GM_xmlhttpRequest gör att scriptet kör i Tampermonkeys sandlåda måste
// vi hooka SIDANS fetch via unsafeWindow - en hook på sandlådans window.fetch ser
// aldrig appens anrop. POST till panelen går via GM_xmlhttpRequest (kringgår både
// CORS och mixed content; sidan är HTTPS, panelen HTTP).

(function () {
  "use strict";

  // Panelens adress. Ändra om du kör den på annan host/port.
  const PANEL = "http://ubuntu-ai:8050";
  const PAGE = typeof unsafeWindow !== "undefined" ? unsafeWindow : window;

  let lastSent = "";
  const stat = { yammer: 0, other: 0, lastAud: "-", sent: "" };

  function log() { console.debug("[viva-token-sync]", ...arguments); }

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
        else paint("Panelen svarade " + r.status, true);
      },
      onerror: function () { paint("Når inte panelen (" + PANEL + ")", true); lastSent = ""; },
    });
  }

  function audOf(tok) {
    try {
      let b64 = tok.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
      while (b64.length % 4) b64 += "=";
      const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
      return JSON.parse(new TextDecoder("utf-8").decode(bytes)).aud || "";
    } catch (e) { return ""; }
  }

  function grab(value) {
    if (typeof value !== "string" || value.indexOf("Bearer ") !== 0) return;
    const tok = value.slice(7).trim();
    const aud = audOf(tok);
    stat.lastAud = String(aud).slice(0, 48);
    if (String(aud).indexOf("yammer.com") !== -1) { stat.yammer++; send(tok); }
    else stat.other++;
    paint();
  }

  // Läs Authorization ur valfri header-representation (Headers-objekt via .get,
  // array av par, eller vanligt objekt).
  function headerAuth(h) {
    if (!h) return null;
    try {
      if (typeof h.get === "function") return h.get("authorization") || h.get("Authorization");
      if (Array.isArray(h)) { const e = h.find((x) => String(x[0]).toLowerCase() === "authorization"); return e && e[1]; }
      for (const k in h) if (k.toLowerCase() === "authorization") return h[k];
    } catch (e) {}
    return null;
  }

  // Hooka SIDANS fetch.
  const origFetch = PAGE.fetch;
  PAGE.fetch = function (input, init) {
    try {
      if (init && init.headers) grab(headerAuth(init.headers));
      if (input && typeof input === "object" && input.headers) grab(headerAuth(input.headers));
    } catch (e) {}
    return origFetch.apply(this, arguments);
  };

  // Hooka SIDANS XHR (för säkerhets skull - en del anrop kan gå den vägen).
  const origSet = PAGE.XMLHttpRequest.prototype.setRequestHeader;
  PAGE.XMLHttpRequest.prototype.setRequestHeader = function (k, v) {
    try { if (String(k).toLowerCase() === "authorization") grab(v); } catch (e) {}
    return origSet.apply(this, arguments);
  };

  // Statusruta nere till höger.
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
    if (msg) { el.textContent = "Viva-token-sync: " + msg; el.style.background = isError ? "#dc2626" : "#2563eb"; return; }
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
