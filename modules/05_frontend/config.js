/* modules/05_frontend/config.js - where the backend lives.
 *
 * Both pages were written to be served BY the backend, so every call was
 * same-origin and none of this was needed. Hosting the pages somewhere else -
 * Vercel, Netlify, GitHub Pages - splits them apart, and then they have to be
 * told where the solver is.
 *
 * Resolution order, first match wins:
 *
 *   1. ?api=https://host          - a query parameter, for trying a backend
 *                                   without redeploying anything
 *   2. localStorage sih_api_base  - sticky, survives a reload, set it once
 *                                   from the console while testing
 *   3. SIH_API_BASE below         - the deployed default; edit this line
 *   4. "" (same origin)           - which is exactly what running locally
 *                                   against uvicorn already does
 *
 * Leave it as "" for the local demo. Nothing about `python -m uvicorn
 * modules.04_backend.api:app` changes.
 *
 * IMPORTANT when you do set it: the backend must allow the page's origin in
 * CORS, and it must be reachable over https if the page is https - a browser
 * refuses to call http:// or ws:// from an https:// page. See SIH_CORS_ORIGINS
 * in modules/04_backend/api.py.
 */

window.SIH_API_BASE = "";

(function () {
  "use strict";

  var base = window.SIH_API_BASE || "";

  try {
    var q = new URLSearchParams(location.search).get("api");
    if (q !== null) {
      base = q;
      /* Persist it. The console and the workflow page are separate documents
         and the nav links between them are plain hrefs with no query string,
         so a base that lived only in the URL would be lost on the first click
         and the page would quietly fall back to its own origin - where there
         is no solver. "?api=" with an empty value clears it again. */
      if (q) localStorage.setItem("sih_api_base", q);
      else localStorage.removeItem("sih_api_base");
    } else {
      var stored = localStorage.getItem("sih_api_base");
      if (stored) base = stored;
    }
  } catch (e) {
    /* Private mode can throw on localStorage. The default is still fine. */
  }

  // Trailing slashes turn "/api/runs" into "//api/runs" once concatenated.
  window.SIH_API_BASE = base.replace(/\/+$/, "");

  /** ws:// or wss:// for a path, whichever origin the API is actually on. */
  window.SIH_WS = function (path) {
    var b = window.SIH_API_BASE;
    if (!b) {
      return (location.protocol === "https:" ? "wss" : "ws") +
             "://" + location.host + path;
    }
    return b.replace(/^http/, "ws") + path;
  };
})();
