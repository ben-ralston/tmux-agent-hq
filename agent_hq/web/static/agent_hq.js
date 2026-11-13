// Agent HQ — thin glue between SSE / htmx / the page DOM.
// Keep this file small. Most behavior lives in htmx attributes; this
// file owns SSE → DOM dispatch and a couple of UI niceties.

(function () {
  "use strict";

  // iOS detection (#137). iPadOS masquerades as desktop Safari, so the
  // UA test alone misses it — "MacIntel" + multitouch is the tell.
  // Exposed on window because the Jump-to-Terminal button's hx-vals
  // ``js:`` expression reads it to decide focus="0" vs "1".
  var IS_IOS =
    /iPad|iPhone|iPod/.test(navigator.userAgent) ||
    (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  window.agentHQIsIOS = IS_IOS;

  // On iOS, a Jump-to-Terminal tap ALSO opens the configured Shortcut
  // (shortcuts://run-shortcut?name=…) from body[data-ios-jump-url].
  // No preventDefault: htmx still fires the POST (tmux pane switch,
  // focus=0), and navigating to a custom scheme app-switches without
  // unloading the page, so the request completes.
  function bootIosJump() {
    if (!IS_IOS) return;
    document.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-jump-terminal]");
      if (!btn) return;
      var url = document.body.getAttribute("data-ios-jump-url");
      if (!url) return;
      window.location.href = url;
    });
  }

  // SSE → htmx swap. The server publishes ``agent.invalidated`` events
  // with ``{pane_id}`` as data; this listener translates the pane_id
  // into a htmx.ajax call against the per-card swap route. That route
  // (``/agents/{slug}/card``) lives in Task 12.
  function bootSSE() {
    if (typeof EventSource === "undefined") return;
    if (window.__agentHqSSEStarted) return;
    window.__agentHqSSEStarted = true;

    // Subscribe to dispatch + issue topics too so the Dispatch view
     // can react to background ``dispatch_via_claude`` progress and
     // GitHub-cache invalidation events.
    var es = new EventSource(
      "/events?topics=agent.*,toast.*,dispatch.*,issue.*"
    );

    // Close the stream the moment the page goes away. Without this,
    // rapid tab navigation piles up zombie SSE sockets: each one holds
    // a slot in the browser's 6-per-origin HTTP/1.1 connection pool
    // (uvicorn is HTTP/1.1), and once the pool is full every request —
    // including the next page navigation itself — queues 10-30s until
    // the browser reaps them (#226 follow-up).
    window.addEventListener("pagehide", function () {
      es.close();
    });
    // With the stream closed the page becomes bfcache-eligible; a
    // back/forward restore would resurface it with dead SSE and stale
    // htmx content, so reload instead.
    window.addEventListener("pageshow", function (e) {
      if (e.persisted) window.location.reload();
    });

    // Dispatch progress → per-card state on /issues (#95). The card
    // itself is the feedback surface — a green spinner on start and a
    // "started" tag on completion — replacing the page-global toast that
    // used to fire on dispatch.started / .completed. dispatch.failed
    // keeps the toast because a failure deserves attention even if the
    // user navigated away from /issues.
    //
    // Race-safety: dispatch.started/completed/failed SSE events can
    // arrive BEFORE the POST /issues/dispatch outerHTML swap installs
    // the inflight cards. Without state-replay the swap overwrites the
    // SSE-applied terminal state, leaving cards stuck at "Starting…".
    // pendingDispatchState stores the latest state per key; markIssueCards
    // both records and applies; htmx:afterSettle re-applies to any
    // freshly swapped issue cards. State expires after STATE_EXPIRY_MS
    // so stale "Started ✓" tags don't persist across unrelated swaps.
    var pendingDispatchState = new Map();
    var STATE_EXPIRY_MS = 30000;

    function applyStateToNodes(nodes, state) {
      nodes.forEach(function (card) {
        card.classList.remove("issue-card--inflight", "issue-card--done");
        if (state.addClass) card.classList.add(state.addClass);
        card.querySelectorAll(".dispatch-state-tag").forEach(function (el) {
          el.remove();
        });
        if (state.tagText) {
          var meta = card.querySelector(".issue-meta, .issue-line");
          if (meta) {
            var tag = document.createElement("span");
            tag.className = "dispatch-state-tag" + (state.tagClass ? " " + state.tagClass : "");
            tag.textContent = state.tagText;
            // Hub cards (#302 follow-up): the tag goes before the
            // right-aligned status group, matching the server render.
            meta.insertBefore(tag, meta.querySelector(".issue-line__right"));
          }
        }
      });
    }

    function selectorForKey(key) {
      var escaped = window.CSS && window.CSS.escape ? CSS.escape(key) : key;
      return '[data-issue-key="' + escaped + '"]';
    }

    // Dispatch events name the cards they are about in issue_keys.
    function dispatchKeys(payload) {
      return payload.issue_keys;
    }

    function markIssueCards(keys, addClass, tagText, tagClass) {
      if (!keys || !keys.length) return;
      var now = Date.now();
      var state = {
        addClass: addClass, tagText: tagText, tagClass: tagClass, ts: now,
      };
      keys.forEach(function (key) {
        pendingDispatchState.set(key, state);
        applyStateToNodes(document.querySelectorAll(selectorForKey(key)), state);
      });
    }

    function replayPendingDispatchState(root) {
      if (!pendingDispatchState.size) return;
      var cutoff = Date.now() - STATE_EXPIRY_MS;
      var scope = root && root.querySelectorAll ? root : document;
      pendingDispatchState.forEach(function (state, key) {
        if (state.ts < cutoff) {
          pendingDispatchState.delete(key);
          return;
        }
        applyStateToNodes(scope.querySelectorAll(selectorForKey(key)), state);
      });
    }

    es.addEventListener("dispatch.started", function (e) {
      try {
        var payload = JSON.parse(e.data) || {};
        markIssueCards(dispatchKeys(payload), "issue-card--inflight", "Starting…");
      } catch (_err) { /* swallow */ }
    });
    es.addEventListener("dispatch.completed", function (e) {
      try {
        var payload = JSON.parse(e.data) || {};
        markIssueCards(dispatchKeys(payload), "issue-card--done", "Started ✓");
        // The server publishes residual warnings in the payload; silently rendering "Started ✓" hid
        // them entirely (#183 / AHQ-016).
        if (payload.warning) {
          showTransientToast("Dispatch warning: " + payload.warning, "warning");
        }
      } catch (_err) { /* swallow */ }
    });
    es.addEventListener("dispatch.failed", function (e) {
      try {
        var payload = JSON.parse(e.data) || {};
        var detail = payload.branch || payload.repo || "";
        var msg = detail ? "Dispatch failed: " + detail : "Dispatch failed";
        if (payload.error) msg += " — " + payload.error;
        showTransientToast(msg, "error");
        // Also flag the cards inline so the failure is anchored to the
        // issue the user actually selected.
        markIssueCards(
          dispatchKeys(payload), null, "Failed", "dispatch-state-tag--failed"
        );
      } catch (_err) { /* swallow */ }
    });

    // Re-apply any pending dispatch state to freshly swapped issue cards.
    // Fixes the SSE-before-swap race where terminal state arrives before
    // the POST swap installs the inflight cards (the swap would otherwise
    // overwrite the SSE-applied state).
    document.body.addEventListener("htmx:afterSettle", function (evt) {
      var node = evt.target;
      if (!node || !pendingDispatchState.size) return;
      // Cheap pre-check: only scan if the swapped subtree could contain
      // issue cards. Avoids walking every other htmx swap.
      if (node.querySelector && node.querySelector("[data-issue-key]")) {
        replayPendingDispatchState(node);
      }
    });

    // Re-dispatch guard: when an issue is dispatched again while a prior
    // terminal state ("Started ✓" / "Failed") is still inside its 30s
    // window, drop that stale entry at request time. Otherwise the POST
    // response server-renders the card as inflight and the afterSettle
    // replay above would immediately re-stamp the old terminal tag over
    // it. Clearing here lets only the NEW dispatch's SSE repopulate it.
    var DISPATCH_POSTS = {
      "/issues/dispatch": "data-issue-key",
    };
    document.body.addEventListener("htmx:beforeRequest", function (evt) {
      var elt = evt.detail && evt.detail.elt;
      if (!elt) return;
      // The dispatch buttons carry ?mode=… (#247) — strip the query so
      // the purge (the #59 stale-terminal-tag fix) still fires.
      var post = (elt.getAttribute("hx-post") || "").split("?")[0];
      var attr = DISPATCH_POSTS[post];
      if (!attr) return;
      document
        .querySelectorAll(".issue-card--selected[" + attr + "]")
        .forEach(function (card) {
          pendingDispatchState.delete(card.getAttribute(attr));
        });
    });

    // Request sources for the /issues SSE refetches (#269). htmx keeps one
    // sync queue per request source. With no ``source`` every refetch shared
    // document.body's queue, whose default ``queue last`` strategy evicted
    // all but the final queued call — the Triage badge refetch vanished and
    // its count went stale after a Confirm. The swapped surface can't be its
    // own source either: an outerHTML swap wipes the old element's queue and
    // htmx drops a request whose source has left the DOM, so a refetch queued
    // behind an in-flight one (an event burst, the skeleton's ``load`` GET, a
    // chip tap) was lost. So each surface gets a hidden element that is never
    // swapped, with ``hx-sync="this:replace"``: a newer refetch aborts the
    // in-flight one and goes out at once against the CURRENT target (htmx.ajax
    // resolves the target when called, so a queued request would swap into a
    // stale element). beforeSwap / the live-target extension fire on the
    // target, not the source, so they are unaffected.
    var refetchSources = {};
    function refetchSource(name) {
      var el = refetchSources[name];
      if (!el || !document.body.contains(el)) {
        el = document.createElement("span");
        el.hidden = true;
        el.setAttribute("hx-sync", "this:replace");
        el.setAttribute("data-refetch-source", name);
        document.body.appendChild(el);
        refetchSources[name] = el;
      }
      return el;
    }

    // ``/issues/container`` re-renders from the current cache without
    // ``force=True`` (the write already updated it) and without
    // ``clear_selection()`` (only the full GET ``/issues`` does that);
    // ``data-mode`` keeps the refetch in the mode the container was rendered
    // under (#247). No-op on pages without the container.
    function refetchDispatchContainer() {
      if (!window.htmx) return;
      var target = document.getElementById("dispatch-container");
      if (!target) return;
      window.htmx.ajax(
        "GET",
        "/issues/container?mode=" + (target.dataset.mode || ""),
        { source: refetchSource("container"), target: target, swap: "outerHTML" }
      );
    }

    // Refetch the /issues surfaces that a record-set change can move: the
    // whole dispatch container (a list-only swap left the filter bar showing
    // pre-edit chip counts until the next nav) and the mode-switch Triage
    // (#248) and Hygiene (#249) badges. Every lookup is guarded, so this is
    // a no-op on pages that carry none of the elements.
    function refetchIssuesSurfaces() {
      if (!window.htmx) return;
      refetchDispatchContainer();
      var badge = document.getElementById("triage-badge");
      if (badge) {
        window.htmx.ajax("GET", "/issues/triage/badge", {
          source: refetchSource("triage-badge"),
          target: badge,
          swap: "outerHTML",
        });
      }
      var hygieneBadge = document.getElementById("hygiene-badge");
      if (hygieneBadge) {
        window.htmx.ajax("GET", "/issues/hygiene/badge", {
          source: refetchSource("hygiene-badge"),
          target: hygieneBadge,
          swap: "outerHTML",
        });
      }
    }

    // A triage/priority/status write invalidated an issue.
    es.addEventListener("issue.invalidated", refetchIssuesSurfaces);

    // Stale-while-revalidate completion (#226): a container that was
    // served from an expired cache kicked a background refresh; when it
    // lands (status ok OR error — the error refetch renders stale data
    // without the indicator, thanks to the server-side cooldown) every
    // open tab refetches the container once to swap in the result.
    es.addEventListener("dispatch.refreshed", refetchDispatchContainer);

    // AI triage worker finished (#248): Triage mode shows the new cards and
    // Queue's Untriaged section gains "AI proposed" pills, so the same two
    // surfaces move as for an invalidation.
    es.addEventListener("triage.proposed", refetchIssuesSurfaces);

    // Hygiene sweep finished (#249): new/resolved findings move the Hygiene
    // badge and the mode's cards.
    es.addEventListener("hygiene.swept", refetchIssuesSurfaces);

    es.addEventListener("agent.invalidated", function (e) {
      try {
        var payload = JSON.parse(e.data);
        var paneId = payload && payload.pane_id;
        if (!paneId) return;
        var slug = paneId.replace(/^%/, "");
        // Prefer the renumber-stable key (#132): after a tmux pane
        // renumber the card still carries its original data-card-key but
        // the event's pane_id (and so the swap URL) points at the NEW
        // pane. Finding by key reuses the existing card in place; the
        // swap rewrites its data-card-slug to the new pane. Fall back to
        // the slug for events with no key (bare invalidations).
        var target = findCard(payload && payload.key, slug);
        if (target && window.htmx) {
          // Card variant matters: a kanban card must stay a kanban card
          // (it targets #overlay-slot, not #detail-slot). Without this,
          // a state/phase change in kanban view would swap the card
          // back to the comfortable list variant pointed at the wrong
          // slot, breaking click-to-overlay until the next full refresh.
          var variant = target.classList.contains("card--kanban")
            ? "kanban"
            : "comfortable";
          var swapped = window.htmx.ajax("GET", "/agents/" + slug + "/card?variant=" + variant, {
            target: target,
            swap: "outerHTML",
          });
          // A mute that didn't come through the web route (TUI `m`, MCP
          // mute_agent/unmute_agent) only reaches us as
          // this invalidation — and the swapped card now carries a muted
          // state its section can't express (#278). When the card's
          // ``card--muted`` disagrees with where it sits, re-fetch the
          // whole view so it moves, instead of waiting for the 10s poll.
          if (swapped && typeof swapped.then === "function") {
            swapped.then(function () {
              var card = findCard(payload && payload.key, slug);
              if (!card) return;
              var muted = card.classList.contains("card--muted");
              var inMuted = !!card.closest("[data-muted-section]");
              var pinned = !!card.closest("[data-pinned-section]");
              if (!pinned && muted !== inMuted) {
                document.body.dispatchEvent(new CustomEvent("agenthq:pins-changed"));
              }
            });
          }
        }
        // If the open detail/overlay is for this agent, refresh it too
        // so the phase chip / summary / mute state stay in sync with
        // the card. Without this, an override-via-menu updates the card
        // but leaves the detail pane stale until the user re-clicks.
        refreshDetailForSlug(slug);
      } catch (_err) { /* swallow malformed event */ }
    });

    // Pin/unpin (#141). Pinning moves a card between sections, which a
    // per-card swap can't express — so re-fetch the whole #agents-view
    // (the same partial the 10s poll loads) so the card lands in / leaves
    // the Pinned section. Fire a body-level custom event that the
    // #agents-view element listens for via hx-trigger; this reaches the
    // acting tab AND every other open tab uniformly. Also refresh the open
    // detail so its Pin/Unpin label flips.
    function onSectionMove(e) {
      try {
        document.body.dispatchEvent(new CustomEvent("agenthq:pins-changed"));
        var payload = JSON.parse(e.data);
        var paneId = payload && payload.pane_id;
        if (paneId) refreshDetailForSlug(paneId.replace(/^%/, ""));
      } catch (_err) { /* swallow malformed event */ }
    }
    es.addEventListener("agent.pins_changed", onSectionMove);
    // Mute/unmute (#278) moves a card into / out of the bottom Muted
    // section — the same "re-fetch the whole view" mechanism as pins.
    es.addEventListener("agent.mute_changed", onSectionMove);

    es.addEventListener("agent.removed", function (e) {
      try {
        var payload = JSON.parse(e.data);
        var paneId = payload && payload.pane_id;
        var slug = paneId ? paneId.replace(/^%/, "") : null;
        // Match the card by its stable key first (the pane_id in a removal
        // can be stale / null after a renumber); fall back to slug (#132).
        var target = findCard(payload && payload.key, slug);
        if (target) {
          target.remove();
          // The card's section header and count are server-rendered around
          // it: removing the last card of a group left a bare header (#338).
          // Re-render the whole view, as pins and mutes do.
          document.body.dispatchEvent(new CustomEvent("agenthq:pins-changed"));
        }
        // Detail wrappers carry data-detail-slug — when the open one
        // matches the removed agent, empty its slot so the preview poll
        // stops 404'ing and the user isn't left looking at frozen state.
        if (slug != null) clearSlotsForDetailSlug(slug);
      } catch (_err) { /* swallow */ }
    });
  }

  // Locate an agent card in the DOM. Prefer the renumber-stable
  // ``data-card-key`` (#132) so a card found after a tmux pane renumber
  // is the SAME element even though its ``data-card-slug`` still holds
  // the old pane; fall back to the slug for events that carry no key.
  function findCard(key, slug) {
    if (key) {
      var escaped = window.CSS && window.CSS.escape ? CSS.escape(key) : key;
      var byKey = document.querySelector('[data-card-key="' + escaped + '"]');
      if (byKey) return byKey;
    }
    if (slug != null && slug !== "") {
      return document.querySelector('[data-card-slug="' + slug + '"]');
    }
    return null;
  }

  // Show a transient toast message in the floating toast slot. Used
  // by the dispatch SSE listeners — agent action toasts come back as
  // server-rendered partials via htmx swaps, which already work; this
  // helper is for purely-client-side toast firings.
  function showTransientToast(message, level) {
    var slot = document.getElementById("toast-slot");
    if (!slot) return;
    var node = document.createElement("div");
    node.className = "toast toast--" + (level || "info");
    node.textContent = message;
    slot.appendChild(node);
    // Match the CSS toast-out animation (~3 s total). Remove after
    // a tick longer so the fadeout completes.
    setTimeout(function () {
      if (node.parentNode) node.parentNode.removeChild(node);
    }, 3200);
  }

  // Refresh the detail view for ``slug`` if it's the one currently open
  // (in either the desktop side pane or the kanban overlay). No-op when
  // the slot is empty or shows a different agent — keeps SSE invalidation
  // for OTHER agents from yanking the user out of the current detail.
  function refreshDetailForSlug(slug) {
    if (!window.htmx) return;
    var detailSlot = document.getElementById("detail-slot");
    if (detailSlot
        && detailSlot.querySelector('[data-detail-slug="' + slug + '"]')) {
      window.htmx.ajax("GET", "/agents/" + slug, {
        target: detailSlot,
        swap: "innerHTML",
      });
    }
    var overlaySlot = document.getElementById("overlay-slot");
    if (overlaySlot
        && overlaySlot.querySelector('[data-detail-slug="' + slug + '"]')) {
      window.htmx.ajax("GET", "/agents/" + slug + "/overlay", {
        target: overlaySlot,
        swap: "innerHTML",
      });
    }
  }

  // Clear the detail-slot AND overlay-slot if either currently holds
  // ``slug``'s detail wrapper. Used by ``agent.removed`` so a vanished
  // agent doesn't leave a dead detail view + 404'ing preview poll.
  function clearSlotsForDetailSlug(slug) {
    ["detail-slot", "overlay-slot"].forEach(function (slotId) {
      var slot = document.getElementById(slotId);
      if (slot && slot.querySelector('[data-detail-slug="' + slug + '"]')) {
        emptySlot(slot);
      }
    });
  }

  // Bottom-sheet drag-to-dismiss on mobile detail. Falls back gracefully
  // on browsers without pointer events.
  function bindSheetDrag(sheet) {
    if (!sheet || !window.PointerEvent) return;
    var handle = sheet.querySelector(".detail-sheet__handle");
    if (!handle) return;
    var startY = null;
    var dy = 0;

    handle.addEventListener("pointerdown", function (e) {
      startY = e.clientY;
      dy = 0;
      handle.setPointerCapture(e.pointerId);
    });
    handle.addEventListener("pointermove", function (e) {
      if (startY == null) return;
      dy = Math.max(0, e.clientY - startY);
      sheet.style.transform = "translateY(" + dy + "px)";
    });
    function end() {
      if (startY == null) return;
      if (dy > 80) sheet.remove();
      else sheet.style.transform = "";
      startY = null;
      dy = 0;
    }
    handle.addEventListener("pointerup", end);
    handle.addEventListener("pointercancel", end);
  }

  // Phase override menu — close on re-click, outside click, or Escape.
  // The menu is rendered server-side into ``.phase-menu-slot`` divs via
  // the ``…`` trigger button. We don't manage open state in JS — we just
  // empty the slot when the user wants the menu gone.

  function emptySlot(slot) {
    if (!slot) return;
    while (slot.firstChild) slot.removeChild(slot.firstChild);
  }

  function clearAllPhaseMenus(except) {
    document.querySelectorAll(".phase-menu-slot").forEach(function (slot) {
      if (slot === except) return;
      emptySlot(slot);
    });
  }

  // Toggle: if the trigger's slot already has the menu, second click
  // closes it instead of re-fetching. We listen at the document level
  // so re-bound triggers (after htmx swaps) keep working.
  document.addEventListener("click", function (evt) {
    var trigger = evt.target.closest("[data-phase-menu-trigger]");
    if (trigger) {
      var slotSelector = trigger.getAttribute("data-phase-menu-trigger");
      var slot = document.querySelector(slotSelector);
      // Close any other open menus first.
      clearAllPhaseMenus(slot);
      if (slot && slot.children.length > 0) {
        // Already open → close and short-circuit the htmx GET.
        emptySlot(slot);
        evt.preventDefault();
        evt.stopPropagation();
      }
      return;
    }
    // Click outside any open menu → close them all. Clicks inside a
    // ``.phase-menu`` (e.g. selecting an override) flow through to htmx
    // before this handler runs, so the slot is naturally cleared by the
    // user's action.
    if (!evt.target.closest(".phase-menu")) {
      clearAllPhaseMenus(null);
    }
  });

  // Escape is tiered — close only the topmost dismissable layer so a
  // single tap doesn't blow away the underlying sheet/overlay along
  // with the phase menu the user actually wanted to dismiss.
  document.addEventListener("keydown", function (evt) {
    if (evt.key !== "Escape") return;
    if (document.querySelector(".phase-menu")) {
      clearAllPhaseMenus(null);
      return;
    }
    // Modal sits above the sheet/overlay layer by z-index. When it's
    // open, Escape dismisses just the modal so the underlying list
    // (and any existing detail sheet) stay where they are.
    var modal = document.getElementById("modal");
    if (modal && !modal.hasAttribute("hidden")) {
      closeModal();
      return;
    }
    clearOverlay();
    clearDetailSheet();
  });

  // --- Dispatch modal -------------------------------------------------
  // The modal lives in dispatch.html as an empty shell. Issue cards open
  // it via ``hx-get="/issues/issue/{key}" hx-target="#modal-content"``;
  // this listener flips ``hidden`` off after the swap, locks body scroll,
  // and the close button + backdrop tap + Escape return us to the list.
  function openModal() {
    var modal = document.getElementById("modal");
    if (!modal) return;
    modal.removeAttribute("hidden");
    document.body.style.overflow = "hidden";
  }
  function closeModal() {
    var modal = document.getElementById("modal");
    if (!modal) return;
    modal.setAttribute("hidden", "");
    document.body.style.overflow = "";
    var content = document.getElementById("modal-content");
    if (content) content.replaceChildren();
  }

  // #224: with the desktop split layout active, a collapsed description
  // leaves the modal's right column empty — default it open there. The
  // trigger is the *computed* layout (the grid set by components.css), so
  // JS never re-states the breakpoint or the :has() support gate: wherever
  // the CSS falls back to single-column (mobile sheet, no :has()), the
  // description keeps its collapsed-by-default state.
  function autoOpenModalDescription() {
    var content = document.getElementById("modal-content");
    if (!content) return;
    var root = content.querySelector(".issue-detail--has-body");
    if (!root || getComputedStyle(root).display !== "grid") return;
    var desc = root.querySelector("details");
    if (desc) desc.open = true;
  }

  // A picker POST re-renders the whole partial; carry the description's
  // open state across the swap so a deliberate collapse (or expand on
  // mobile) isn't reverted by priority/status clicks. A fresh modal open
  // starts from an emptied #modal-content (closeModal), so the state is
  // null there and the split-layout default above applies.
  var modalDescWasOpen = null;
  document.body.addEventListener("htmx:beforeSwap", function (evt) {
    var target = evt.detail && evt.detail.target;
    if (target && target.id === "modal-content") {
      var desc = target.querySelector(".issue-detail--has-body details");
      modalDescWasOpen = desc ? desc.open : null;
    }
  });

  document.body.addEventListener("htmx:afterSwap", function (evt) {
    var target = evt.detail && evt.detail.target;
    if (target && target.id === "modal-content" && target.children.length > 0) {
      openModal();
      var desc = target.querySelector(".issue-detail--has-body details");
      if (desc && modalDescWasOpen !== null) {
        desc.open = modalDescWasOpen;
      } else {
        autoOpenModalDescription();
      }
      modalDescWasOpen = null;
    }
  });

  // Crossing into the split layout with the modal already open (e.g. a
  // tablet rotating to landscape) gets the same default — without this the
  // wide right column appears empty until the next swap.
  window.addEventListener("resize", autoOpenModalDescription);

  document.addEventListener("click", function (evt) {
    if (evt.target.closest("[data-modal-close]")) {
      closeModal();
      evt.preventDefault();
    }
  });

  // Floating overlay (kanban click target). Same dismiss recipe as the
  // phase menu — empty the slot on outside-click, ×-button, or Escape.
  function clearOverlay() {
    var slot = document.getElementById("overlay-slot");
    if (slot) emptySlot(slot);
  }

  // Mobile detail sheet — sits inside #detail-slot. Dismiss recipe:
  // explicit × button, tap on the backdrop area, drag-down past 80 px
  // (handled in bindSheetDrag), or Escape. We empty the slot rather
  // than just hiding the sheet so the next card click re-fetches a
  // fresh body (preview included).
  function clearDetailSheet() {
    var sheet = document.querySelector(".detail-sheet");
    if (!sheet) return;
    var slot = document.getElementById("detail-slot");
    if (slot) emptySlot(slot);
  }

  document.addEventListener("click", function (evt) {
    // ``data-overlay-close`` button (×) inside the overlay.
    if (evt.target.closest("[data-overlay-close]")) {
      clearOverlay();
      evt.preventDefault();
      return;
    }
    // ``data-sheet-close`` button (×) inside the detail sheet.
    if (evt.target.closest("[data-sheet-close]")) {
      clearDetailSheet();
      evt.preventDefault();
      return;
    }
    // Click landed somewhere outside the overlay AND not on a kanban
    // card (which would trigger a fresh fetch and re-fill the slot).
    var slot = document.getElementById("overlay-slot");
    if (slot && slot.children.length > 0
        && !evt.target.closest(".detail-overlay")
        && !evt.target.closest(".card--kanban")) {
      clearOverlay();
    }
    // Mobile sheet: tap-outside dismiss. ``partials/detail.html``
    // renders BOTH the sheet and the desktop pane in the same slot
    // (CSS hides whichever doesn't apply for the viewport), so this
    // guard has to check whether the sheet is actually visible.
    // The sheet uses ``position: fixed`` on mobile, which breaks the
    // ``offsetParent`` check (always null for fixed elements), so we
    // use ``getComputedStyle`` instead. Without that check, clicking
    // anywhere inside the desktop ``.detail-pane`` would empty the
    // slot and yank the user out of their open detail.
    //
    // Even on mobile, we exempt the chrome that sits visually outside
    // the sheet (toast, header, overlay) and the phase-menu (which is
    // anchored above the sheet but not inside it) — clicking those
    // shouldn't dismiss the sheet underneath.
    var sheet = document.querySelector(".detail-sheet");
    if (sheet
        && getComputedStyle(sheet).display !== "none"
        && !evt.target.closest(".detail-sheet")
        && !evt.target.closest(".card")
        && !evt.target.closest(".phase-menu")
        && !evt.target.closest("#toast-slot")
        && !evt.target.closest(".app-header")
        && !evt.target.closest("#overlay-slot")) {
      clearDetailSheet();
    }
  });

  // After clicking inside a phase menu (override / confirm / clear),
  // dismiss the menu once htmx has fired the request — the toast slot
  // shows the result, and leaving the menu open is just visual noise.
  document.body.addEventListener("htmx:afterRequest", function (evt) {
    var elt = evt.detail && evt.detail.elt;
    if (elt && elt.closest && elt.closest(".phase-menu")) {
      clearAllPhaseMenus(null);
    }
    // New-session picker (#169): picking a target fires the POST; the
    // toast shows the result, so dismiss the picker once the request
    // completes.
    if (elt && elt.closest && elt.closest("[data-new-session-picker]")) {
      clearOverlay();
    }
  });

  // Preview pane: stick to the bottom (TUI-style "show latest first")
  // unless the user has scrolled up to read older content. We can't
  // observe user-scroll directly across the every-3s tick, so on each
  // beforeSwap we measure the host's distance from bottom; on afterSwap
  // we re-pin to bottom only if it WAS near bottom (or this is the
  // first render of this host). 24 px tolerance handles antialiasing
  // and the partial-line cases that show up with monospace text.
  var NEAR_BOTTOM_PX = 24;

  function previewHostFromTarget(target) {
    if (!target) return null;
    if (target.classList && target.classList.contains("pane-preview-host")) {
      return target;
    }
    return target.closest && target.closest(".pane-preview-host");
  }

  document.body.addEventListener("htmx:beforeSwap", function (evt) {
    var target = evt.detail && evt.detail.target;
    var host = previewHostFromTarget(target);
    if (!host) return;
    // History pagination (#171): the top sentinel is about to be
    // replaced by the previous page. Record the host's geometry so
    // afterSwap can keep the reader anchored on the turn they were
    // looking at (iOS Safari has no overflow-anchor).
    if (target.classList && target.classList.contains("history-sentinel")) {
      host.dataset.historyPrevHeight = String(host.scrollHeight);
      host.dataset.historyPrevTop = String(host.scrollTop);
      return;
    }
    var dist = host.scrollHeight - host.scrollTop - host.clientHeight;
    host.dataset.wasNearBottom = dist < NEAR_BOTTOM_PX ? "1" : "0";
    // Horizontal lives on the inner <pre class="pane-preview"> (it owns
    // overflow-x), which the poll swap REPLACES — so unlike the host's
    // vertical scrollTop it resets every tick. Park it on the persistent
    // host and re-apply after the swap.
    var pre = host.querySelector(".pane-preview");
    host.dataset.savedScrollLeft = pre ? String(pre.scrollLeft) : "0";
  });

  // Live | History toggle (#171): reflect the active mode on the
  // segmented control. The actual swap is htmx's job; this is display
  // state only.
  document.body.addEventListener("click", function (evt) {
    var btn =
      evt.target && evt.target.closest && evt.target.closest("[data-preview-toggle] button");
    if (!btn) return;
    btn.parentElement.querySelectorAll("button").forEach(function (b) {
      b.classList.toggle("is-active", b === btn);
    });
  });

  // After-swap handler combines two responsibilities — re-binding sheet
  // drag (existing) and preview scroll-to-bottom (new). Both want the
  // same event, no point listening twice.
  document.body.addEventListener("htmx:afterSwap", function (evt) {
    var node = evt.target;
    if (!node) return;

    // Re-bind sheet drag on freshly inserted nodes.
    if (node.classList && node.classList.contains("detail-sheet")) {
      bindSheetDrag(node);
    } else if (node.querySelectorAll) {
      node.querySelectorAll(".detail-sheet").forEach(bindSheetDrag);
    }

    // When the detail-slot itself is replaced (a new agent was selected),
    // any preview hosts inside should re-initialize to bottom on first
    // tick — clear their init flag so the heuristic below treats the
    // next swap as the first.
    if (node.id === "detail-slot" || node.id === "overlay-slot") {
      node.querySelectorAll(".pane-preview-host").forEach(function (h) {
        delete h.dataset.previewInitialized;
        delete h.dataset.wasNearBottom;
        delete h.dataset.savedScrollLeft;
        delete h.dataset.historyInitialized;
      });
    }

    // History pagination anchor (#171): a sentinel swap prepended older
    // turns above the reader. Restore their visual position by the
    // height delta, and skip the stick-to-bottom logic entirely.
    var anchored = document.querySelector(".pane-preview-host[data-history-prev-height]");
    if (anchored) {
      var prevHeight = parseInt(anchored.dataset.historyPrevHeight, 10) || 0;
      var prevTop = parseInt(anchored.dataset.historyPrevTop, 10) || 0;
      delete anchored.dataset.historyPrevHeight;
      delete anchored.dataset.historyPrevTop;
      anchored.scrollTop = prevTop + (anchored.scrollHeight - prevHeight);
      return;
    }

    var host = previewHostFromTarget(node);
    if (!host) return;

    // History mode (#171): the host holds transcript turns, not the
    // live capture. Snap to the newest turn once on entry; after that
    // the reader's scroll position is entirely their own (no 3s poll
    // runs in history mode, so nothing fights them).
    if (!host.querySelector(".preview-live")) {
      if (host.dataset.historyInitialized !== "1") {
        host.scrollTop = host.scrollHeight;
        requestAnimationFrame(function () {
          host.scrollTop = host.scrollHeight;
        });
        host.dataset.historyInitialized = "1";
      }
      return;
    }
    delete host.dataset.historyInitialized;

    // Preview host swap → snap to bottom if appropriate. We scroll
    // synchronously first (the new <pre> is already in the DOM at this
    // point) and then again on the next frame to catch any post-layout
    // height changes (web fonts settling, etc.).
    var firstRender = host.dataset.previewInitialized !== "1";
    var stickToBottom = firstRender || host.dataset.wasNearBottom !== "0";
    if (stickToBottom) {
      host.scrollTop = host.scrollHeight;
      requestAnimationFrame(function () {
        host.scrollTop = host.scrollHeight;
      });
      host.dataset.previewInitialized = "1";
    }
    // Restore the horizontal position saved in beforeSwap onto the
    // freshly swapped-in <pre>. Clamps naturally (scrollLeft caps at
    // max) if the new content is narrower.
    var savedLeft = parseInt(host.dataset.savedScrollLeft || "0", 10);
    if (savedLeft > 0) {
      var newPre = host.querySelector(".pane-preview");
      if (newPre) newPre.scrollLeft = savedLeft;
    }
  });

  // Chip-row horizontal scroll (dispatch filter bar). Chip
  // toggles swap the whole container (the chip's --active state lives in
  // server HTML), which replaces the scrolled chip row and silently
  // resets it to the left edge. Save the row's scrollLeft when a swap
  // is about to replace it; restore onto the new row after settle.
  var savedChipScroll = null;

  document.body.addEventListener("htmx:beforeSwap", function (evt) {
    var target = evt.detail && evt.detail.target;
    if (!target || !target.querySelector) return;
    var chips = target.querySelector(".filter-bar__chips");
    if (chips) savedChipScroll = chips.scrollLeft;
  });

  document.body.addEventListener("htmx:afterSettle", function (evt) {
    if (savedChipScroll == null) return;
    var node = evt.target;
    if (!node || !node.querySelector) return;
    var chips = node.querySelector(".filter-bar__chips");
    if (chips) {
      chips.scrollLeft = savedChipScroll;
      savedChipScroll = null;
    }
  });

  // Stale swap targets (#268). Two outerHTML swaps of the same container can
  // be in flight at once: the skeleton's hx-trigger="load" fetch racing an
  // SSE-driven htmx.ajax refetch (dispatch.refreshed / issue.invalidated),
  // two JS refetches (htmx queues the second behind the
  // first but keeps the target element it captured at issue time), or a chip
  // tap while ↻ is still pending. The later response then targets an element
  // the earlier swap already replaced. htmx 2.0.3's swap() runs its
  // hx-preserve handling — which moves the live #dispatch-search out of the
  // document — BEFORE swapOuterHTML notices the target has no parent and
  // silently returns; the restore step then finds only the parked copy and
  // removes it, so the Browse search field is gone until the next successful
  // container swap re-creates it. htmx:beforeSwap is dispatched on the
  // detached target, so no document-level listener ever sees it; an
  // extension named in hx-ext ON THE CONTAINER does (htmx resolves extensions
  // from the event element's own attributes). Retarget the swap to whatever
  // live element now carries the id, or drop it outright when nothing does.
  //
  // Retargeting is narrow (two review rounds): every request against an
  // opted-in container is stamped with a sequence number and its verb when
  // it is issued, and the element a swap produces carries both. A late
  // response is retargeted only when it is not older than the live element
  // AND it is not a read landing over a write; a read over an older read, or
  // a write over anything older, applies. Everything else drops, exactly as
  // before this extension existed.
  // Why: issue order is not state order. A slow ↻ (POST) landing after a
  // chip tap would repaint the pre-tap chips, and a container refetch (GET)
  // issued while a chip's POST is still in flight was rendered from the
  // pre-tap cookie — retargeting the younger GET over the POST's element
  // would repaint the same stale state. A live READ element with a seq at or
  // below the late read's implies no write landed since that read was issued
  // (a write landing would have become the live element, and a read landing
  // on it drops), so the read's render was current. The cases that win are
  // the ones that matter: the SWR refetch that lands right after a slow
  // skeleton load, and a ↻ tapped during that load (review round 3), now
  // apply instead of being lost.
  //
  // What this does NOT do (review round 4): the guard only decides what the
  // DOM shows. A dropped response's Set-Cookie still lands — /issues/refresh
  // re-writes dispatch_state from the cookie it was sent — so a slow ↻ that
  // lands after a chip tap still reverts the cookie to pre-tap, and the next
  // render shows it. That race predates this extension and can only be
  // closed server-side (stop re-writing an unchanged state cookie on refresh
  // / GET container, or version it).
  if (window.htmx) {
    window.htmx.defineExtension("live-target", {
      onEvent: function (name, evt) {
        if (name !== "htmx:beforeSwap") return true;
        var detail = evt.detail;
        var target = detail && detail.target;
        if (!target || document.body.contains(target)) return true;
        var live = target.id ? document.getElementById(target.id) : null;
        var mine = detail.xhr ? detail.xhr.ahqSwapSeq : null;
        var read = !!detail.xhr && detail.xhr.ahqSwapVerb === "get";
        var liveRead = !!live && live.getAttribute("data-swap-verb") === "get";
        var liveSeq = live ? Number(live.getAttribute("data-swap-seq")) : NaN;
        var ordered = mine != null && !(liveSeq > mine) && (!read || liveRead);
        if (live && live !== target && document.body.contains(live) && ordered) {
          detail.target = live;
        } else {
          detail.shouldSwap = false;
        }
        return true;
      },
    });
  }
  var liveTargetSeq = 0;
  document.addEventListener("htmx:beforeRequest", function (evt) {
    var target = evt.detail && evt.detail.target;
    if (!target || !target.matches || !target.matches('[hx-ext~="live-target"]')) return;
    if (!evt.detail.xhr) return;
    evt.detail.xhr.ahqSwapSeq = ++liveTargetSeq;
    var config = evt.detail.requestConfig;
    evt.detail.xhr.ahqSwapVerb = config && config.verb ? String(config.verb).toLowerCase() : "";
  });
  document.addEventListener("htmx:afterSwap", function (evt) {
    var xhr = evt.detail && evt.detail.xhr;
    var el = evt.target;
    if (!xhr || xhr.ahqSwapSeq == null || !el || !el.matches || !el.matches('[hx-ext~="live-target"]')) return;
    el.setAttribute("data-swap-seq", String(xhr.ahqSwapSeq));
    el.setAttribute("data-swap-verb", xhr.ahqSwapVerb || "");
  });

  // Vertical page scroll across full-container swaps (#253). Same problem
  // as the chip row above, one axis up and one scope out: every /dispatch
  // control (issue checkbox, Group/Ungroup, Dispatch, ↻, sort, chips) is
  // hx-target="#dispatch-container" hx-swap="outerHTML", so a single tap
  // destroys and rebuilds the entire scrollable page content. The window
  // is the scroll container on these pages — .dispatch-list has no
  // overflow of its own — so Chrome's scroll anchoring, its anchor node
  // gone, re-anchors against the freshly inserted list and throws the
  // viewport to the bottom of the page. .dispatch-layout carries
  // overflow-anchor: none to stop the re-anchor; this puts the user's
  // actual position back.
  var SCROLL_PRESERVING_CONTAINERS = "#dispatch-container";
  var savedPageScroll = null;

  // Both events hand us the container itself: beforeSwap the element about
  // to be replaced, afterSettle the one just inserted, because every
  // control targeting these containers swaps them outerHTML. An
  // ancestor-matching branch (node.querySelector(...)) lived here briefly;
  // it had no caller in this codebase and would have actively misfired if
  // hx-boost were ever added to the nav — beforeSwap matching the old
  // page's <body> and afterSettle the new one's, carrying /dispatch's
  // offset onto the next page instead of landing at the top.
  function isScrollPreservingContainer(node) {
    return !!node && !!node.matches && node.matches(SCROLL_PRESERVING_CONTAINERS);
  }

  document.body.addEventListener("htmx:beforeSwap", function (evt) {
    var target = evt.detail && evt.detail.target;
    if (isScrollPreservingContainer(target)) savedPageScroll = window.scrollY;
  });

  // A 4xx/5xx still fires beforeSwap — that event is the hook for overriding
  // shouldSwap — but nothing settles afterwards, so the saved offset would
  // otherwise linger until some later swap happened to consume it. Clearing
  // here makes that explicit rather than leaning on "the next beforeSwap
  // overwrites it first". Deliberately NOT htmx:afterRequest: with the
  // default 20ms settle delay that fires BEFORE afterSettle and would
  // defeat the restore on the success path.
  ["htmx:responseError", "htmx:sendError", "htmx:swapError"].forEach(function (name) {
    document.body.addEventListener(name, function () {
      savedPageScroll = null;
    });
  });

  document.body.addEventListener("htmx:afterSettle", function (evt) {
    if (savedPageScroll == null) return;
    // Gate the restore on the same test as the save: yanking the viewport
    // on an unrelated settle (detail modal, agent card) would be a worse
    // bug than the one being fixed.
    if (!isScrollPreservingContainer(evt.target)) return;
    var y = savedPageScroll;
    savedPageScroll = null;
    // Clamps naturally when the rebuilt list is shorter than the old one
    // (a filter narrowed it, a dispatch consumed selections).
    window.scrollTo(window.scrollX, y);
    // Second pass next frame, mirroring the preview-scroll code above: the
    // container can still grow after settle (bundled woff2 faces resolving
    // on a cold themed load, the --header-h ResizeObserver re-laying the
    // sticky filter bar), and the first scrollTo clamps against the
    // shorter height, landing the user short of where they were.
    requestAnimationFrame(function () {
      window.scrollTo(window.scrollX, y);
    });
  });

  // Publish the app header's real height as --header-h so sticky elements
  // (the filter bar) pin below it. The header is one row on desktop but
  // wraps to two on phones, so a hardcoded CSS offset can't work.
  function bootHeaderHeight() {
    var header = document.querySelector(".app-header");
    if (!header || typeof ResizeObserver === "undefined") return;
    var publish = function () {
      document.documentElement.style.setProperty(
        "--header-h", header.offsetHeight + "px");
    };
    new ResizeObserver(publish).observe(header);
    publish();
  }

  document.addEventListener("DOMContentLoaded", function () {
    bootSSE();
    bootIosJump();
    bootHeaderHeight();
    document
      .querySelectorAll(".detail-sheet")
      .forEach(bindSheetDrag);
  });
})();

// Hygiene Accept → Execute (#249 hand-test round): the preview modal turns
// into a live checklist. The POST itself stays synchronous (it swaps the
// container when the steps finish); meanwhile a poller swaps the step list
// from /progress every 700 ms, and when the POST returns the final log is
// loaded once (?final=1) and Cancel/Execute give way to Done.
document.body.addEventListener("htmx:beforeRequest", function (evt) {
  var btn = evt.detail && evt.detail.elt;
  if (!btn || !btn.hasAttribute || !btn.hasAttribute("data-hygiene-execute")) return;
  var root = btn.closest(".hygiene-preview");
  if (!root || !window.htmx) return;
  root.classList.add("is-executing");
  var status = root.querySelector("[data-hygiene-status]");
  if (status) status.hidden = false;
  // Mirror onto the card behind the modal (hand-test round 2): closing the
  // sheet mid-run left a card that looked idle. The container re-renders
  // from the store when the POST returns, so this is display-only.
  var fid = btn.getAttribute("data-finding-id");
  var card = null;
  document.querySelectorAll(".hygiene-card[data-finding-id]").forEach(function (el) {
    if (el.getAttribute("data-finding-id") === fid) card = el;
  });
  if (card) {
    card.classList.add("hygiene-card--busy");
    var busy = card.querySelector("[data-hygiene-card-busy]");
    if (busy) busy.hidden = false;
    card.querySelectorAll(".hygiene-card__actions button, .hygiene-card__remove").forEach(function (b) {
      b.disabled = true;
    });
  }
  var pending = root.querySelector("[data-hygiene-pending]");
  if (pending) pending.hidden = true;
  var poller = document.createElement("div");
  poller.className = "hygiene-progress__poller";
  poller.setAttribute("hx-get", btn.getAttribute("data-progress-url"));
  poller.setAttribute("hx-trigger", "every 700ms");
  poller.setAttribute("hx-target", "#hygiene-progress-steps");
  poller.setAttribute("hx-swap", "outerHTML");
  root.appendChild(poller);
  window.htmx.process(poller);
});

document.body.addEventListener("htmx:afterRequest", function (evt) {
  var btn = evt.detail && evt.detail.elt;
  if (!btn || !btn.hasAttribute || !btn.hasAttribute("data-hygiene-execute")) return;
  var root = btn.closest(".hygiene-preview");
  if (!root || !window.htmx) return;
  var poller = root.querySelector(".hygiene-progress__poller");
  if (poller) {
    // A poll still in flight would swap its (now empty) reply over the
    // final checklist loaded below — abort it before removing the element.
    window.htmx.trigger(poller, "htmx:abort");
    poller.remove();
  }
  root.classList.remove("is-executing");
  root.classList.add("is-finished");
  var status = root.querySelector("[data-hygiene-status]");
  var finished = root.querySelector("[data-hygiene-finished]");
  var xhr = evt.detail.xhr;
  var ran = xhr && xhr.getResponseHeader ? xhr.getResponseHeader("X-Hygiene-Ran") : null;
  if (status) {
    if (ran === "0") {
      status.textContent = "Nothing ran — this finding changed since the preview was opened.";
    } else if (ran === null) {
      // No response at all (network drop, proxy timeout): the server may
      // still be executing under its claim. Don't paint a "final" list.
      status.textContent = "Could not reach the server — reload to see this finding's state.";
    } else if (!evt.detail.successful && ran !== "1") {
      status.textContent = "Execute failed — see the card for the last attempt.";
    } else {
      status.hidden = true;
    }
  }
  var title = root.querySelector(".detail-pane__title");
  if (title && ran === "1") {
    title.textContent = evt.detail.successful ? "Executed" : "Execute failed";
  }
  if (ran !== "0" && ran !== null) {
    window.htmx.ajax("GET", btn.getAttribute("data-progress-url") + "?final=1", {
      target: "#hygiene-progress-steps",
      swap: "outerHTML",
    });
  }
  if (finished) finished.hidden = false;
});
