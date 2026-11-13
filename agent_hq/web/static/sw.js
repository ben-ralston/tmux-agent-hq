// Agent HQ service worker. Issue #42.
//
// Strategy:
//   * Precache /offline + /static/* core assets on install. REQUIRED list
//     (shell-critical: /offline + the 4 CSS files) must succeed; OPTIONAL
//     list (icons, vendored JS) is best-effort via Promise.allSettled, so a
//     single 404 or transient blip doesn't kill the whole offline shell.
//   * /static/*: exact-match cache → network (cache new ?v= on success) →
//     stale ignoreSearch fallback only on network failure. This preserves
//     the server's static_version (?v=...) cache-busting (a new ?v= always
//     wins via network) while still keeping the app usable offline through
//     the precached unversioned URL.
//   * Navigations: network, fall back to cached /offline on network error.
//   * SSE (/events) and /api/*: SW skips entirely — default network behavior.
//   * Other GETs (htmx partials, /healthz, etc.): SW skips.
//   * Non-GET requests: SW skips (mutations must hit the network).
//
// Bump VERSION when the precache list, strategy, OR the content of a
// precached shell asset changes, so the activate handler evicts the stale
// shell cache and install re-fetches the fresh copies. (v4: the theme
// stylesheets graduated from dev-only previews to a persisted user
// preference (#200 follow-up), so themes/*.css joined the OPTIONAL
// precache. Note this does NOT theme the offline page: navigations are
// network-first with /offline as the only fallback, and that document
// links no stylesheets. The entries just spare a themed user a network
// round-trip for the theme layer on a slow-but-online load. v5: history
// view + preview toggle for #171 — agent_hq.js, components.css, ansi.css,
// incl. history system-turn styling and the preview-toggle flex-stretch
// fix. v6: the five expressive themes (#220) — new themes/*.css, the
// bundled woff2 fonts they reference, and the structural-token changes
// to tokens/layout/components.css. v7: the #221 bake-off graft — three
// Codex themes + the weight/leading/page-texture/shadow token wiring.
// v8: bake-off outcome — paper-codex/terminal-codex removed, bubblegum
// reworked, paper pane went light (ansi.css light palette), terminal
// de-striped, neon rewritten, console type scale restored. v9: agent_hq.js
// dispatch-state handling now matches /personal cards by data-item-key and
// reads item_keys off the SSE payload (#59). v10: SSE pagehide close +
// bfcache-restore reload in agent_hq.js — the #226 rapid-tab-navigation
// connection-pool fix. v11: Errors tab (#148) + sentinel issue styling
// (#235) — components.css additions. v12: sentinel badge no longer
// stretches in the issue detail (display/width/justify-self fix). v13:
// #242 mobile overflow fix — layout.css overflow-x backstop +
// components.css .issue-meta wrap. v14: #253 scroll-jump — agent_hq.js
// saves/restores window.scrollY across #dispatch-container and
// #personal-container swaps, layout.css opts .dispatch-layout out of
// scroll anchoring. v15–v16: #247 issues hub — mode switch / rank chip /
// pin styles in components.css, then the unified pin glyph + gold active
// state; v16 also clears clients that cached the pre-merge v15. v17:
// #248 AI triage worker — agent_hq.js gained the triage.proposed
// listener. v18: #248 Triage inbox mode — components.css gained the
// mode-switch badge + .triage-card block. v19: triage-card sentinel
// stripe restored over the .triage-card border shorthand. v20: agent_hq.js
// issue.invalidated / triage.proposed share one refetchIssuesSurfaces
// helper. v24: #249 Hygiene mode — components.css hygiene block,
// agent_hq.js hygiene.swept listener. v29: issue-body pre wraps. v28: hidden busy line. v27: card mirrors Execute progress. v26: mode-switch pills kept inline. v25: #249 Execute checklist —
// agent_hq.js progress poller + components.css progress block. v30: #273
// off-board findings — components.css hygiene-repo header. v31: #275
// preview wraps (ansi.css/layout.css), #278 muted section (components.css
// .muted-chip, agent_hq.js agent.mute_changed listener). v32: #302
// Autopilot card rows wrap at phone width (components.css). v33: #302
// hand-test — three-row issue cards, violet --autopilot token (tokens +
// neon/paper/bubblegum), Discard + tappable rows (components.css, agent_hq.js).
// v34: #269 the /issues SSE refetches use hidden per-surface htmx sources
// (hx-sync this:replace), so the Triage badge refetch is no longer evicted
// from document.body's sync queue. v35: companion tab — components.css
// thread/tray/bundle block, layout.css viewport-locked chat shell,
// agent_hq.js companion.updated listener + thread scroll/compose glue. v36:
// companion card gate/note lines and the Activity "lost" status (components.css). v37:
// companion shell height subtracts the body safe-area padding (layout.css,
// components.css). v38: companion cards render once inline; the tray is
// replaced by a one-line waiting bar (components.css, layout.css, agent_hq.js). v39:
// flat check-ins: one line per item, no bundle/count dropdowns (components.css). v40:
// companion test-instance banner and dev-agent messages (components.css). v41:
// companion Enter-to-send and message editing (components.css, agent_hq.js). v42:
// companion check-back timer lines wrap with a clock glyph (components.css). v43:
// attached files/ documents as chips under a companion message (components.css). v44:
// companion reactions and reply-to (components.css, agent_hq.js). v45:
// companion open-threads page (components.css). v46: companion decorations
// (components.css). v47: companion shell pinned to the visual viewport
// while the iOS keyboard is up (layout.css, agent_hq.js). v48: the companion
// reply and edit bars end in a round x that a long quote can no longer push
// off-screen (companion.html, components.css, agent_hq.js). v49: companion
// attachments open in a viewer sheet (companion.html, components.css,
// agent_hq.js). v53 (v50-v52 went to branch and combined test builds):
// companion long-press action sheet and swipe-to-reply replace the inline
// message buttons; a reaction shows as a badge (companion.html,
// components.css, agent_hq.js). v54: the companion island bundle
// (companion-island/companion.{js,css}, #329 C) joins OPTIONAL. v55: the
// island bundle gains the Test tap on approval cards, #340. v56: the Agents
// tab re-renders its view after a card removal and styles the data-source
// error banner, #338 (agent_hq.js, components.css). v57: the
// Personal tab is gone (#345) — its JS listeners and CSS left
// agent_hq.js, components.css and layout.css. v58: the
// island gets the iOS app's native features and modal sheets, #350. v59:
// the htmx companion tab is gone (#350 step 4) — its thread, compose,
// viewer and gesture blocks left agent_hq.js, components.css and
// layout.css. v60: the island bundle renders GFM tables, strikethrough
// and task lists (remark-gfm), from the owner's #359 phone pass. v61: push
// notifications (#347) — agent_hq.js gains the app's register() bridge and
// the island registers the device and opens #card-<id> from a tap. v62:
// the island drops message editing (#346) and the file sheet treats a
// login redirect as an expired session (#363). v63: the companion chat
// redesign (#343, #349) — check-ins as one bubble with an item sheet,
// Dots-style thread, chat tokens in tokens.css and the paper/bubblegum
// themes, components.css loses the island's retired rules. v64: the
// companion bubble gets its own --chat-theirs-bg token, lifted off the page
// in the dark themes, from the owner's #367 phone pass. v65: same pass — the
// composer's single line centred on + and Send, and a trimmed approval card.
// v66: review round — the approval card's tool label keeps the server's
// exact name, components.css drops the retired disclosure rule. v67: the
// island's + menu opens the photo/video picker and the owner's uploads show as
// thumbnails (#344). v68: Files opens the document picker and takes PDFs,
// Word/RTF and text (#344, the owner's hand test). v69: Files also takes web
// pages, .xlsx and Pages/Numbers/Keynote (#344). v70: the item sheet's
// Open session link for a supervised coding session (#348). v71: the
// companion is gone (#399): no island in the precache, its CSS, chat
// tokens and the native-app bridge left agent_hq.js and the stylesheets.)

const VERSION = "v71";
const SHELL_CACHE = `agent-hq-shell-${VERSION}`;
const RUNTIME_CACHE = `agent-hq-runtime-${VERSION}`;

// Shell-critical: the offline shell renders without JS, just CSS. If any of
// these fail to cache, the install fails and the browser surfaces an error.
const REQUIRED = [
  "/offline",
  "/static/tokens.css",
  "/static/layout.css",
  "/static/components.css",
  "/static/ansi.css",
];

// Best-effort: icons + vendored JS. App-critical for normal use but they'll
// be fetched on first online visit anyway. Don't let a 404 here block install.
const OPTIONAL = [
  "/static/htmx.min.js",
  "/static/htmx-sse.min.js",
  "/static/alpine.min.js",
  "/static/agent_hq.js",
  "/static/manifest.json",
  "/static/icons/icon-192.svg",
  "/static/icons/icon-512.svg",
  "/static/themes/midnight.css",
  "/static/themes/warm.css",
  "/static/themes/paper.css",
  "/static/themes/terminal.css",
  "/static/themes/neon.css",
  "/static/themes/brutalist.css",
  "/static/themes/console.css",
  "/static/themes/bubblegum.css",
  "/static/fonts/lora-latin-400-normal.woff2",
  "/static/fonts/lora-latin-600-normal.woff2",
  "/static/fonts/ibm-plex-mono-latin-400-normal.woff2",
  "/static/fonts/ibm-plex-mono-latin-600-normal.woff2",
  "/static/fonts/space-grotesk-latin-500-normal.woff2",
  "/static/fonts/space-grotesk-latin-700-normal.woff2",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then(async (cache) => {
      await cache.addAll(REQUIRED);
      await Promise.allSettled(OPTIONAL.map((u) => cache.add(u)));
    })
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  const keep = new Set([SHELL_CACHE, RUNTIME_CACHE]);
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((k) => !keep.has(k)).map((k) => caches.delete(k)))
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // Never intercept SSE or API — must hit live server.
  if (url.pathname === "/events" || url.pathname.startsWith("/api/")) return;

  // Static assets:
  //   1. Exact match (so ?v=NEW doesn't satisfy a cached ?v=OLD).
  //   2. Network fetch, cache versioned URL on success.
  //   3. Network failure → stale ignoreSearch fallback for offline support
  //      (precached /static/foo.css satisfies /static/foo.css?v=ANY).
  if (url.pathname.startsWith("/static/")) {
    event.respondWith(
      caches.match(request).then((cached) => {
        if (cached) return cached;
        return fetch(request)
          .then((response) => {
            if (response && response.ok) {
              const clone = response.clone();
              caches
                .open(RUNTIME_CACHE)
                .then((cache) => cache.put(request, clone))
                .catch(() => {});
            }
            return response;
          })
          .catch(() => caches.match(request, { ignoreSearch: true }));
      })
    );
    return;
  }

  // Top-level navigations: network, fall back to cached offline shell.
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).catch(() => caches.match("/offline"))
    );
    return;
  }

  // Everything else (htmx partials, healthz, etc.) — default network.
});
