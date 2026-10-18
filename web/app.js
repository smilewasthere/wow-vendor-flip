/* WoW Vendor-Flip Finder UI (M6). Vanilla JS, no deps, offline-only.
   All item-derived strings go through textContent - never innerHTML. */
(function () {
  "use strict";

  var WOWHEAD = "https://www.wowhead.com/item=";
  var LIST_LIMIT = 300;
  var FETCH_LIMIT = 5000;
  var VERDICT_CLASS = {
    "GOOD": "v-good",
    "CHECK VENDOR": "v-check",
    "RISKY": "v-risky",
    "CHECK IN GAME": "v-game",
    "NOT WORTH IT": "v-no",
    "NOT VENDOR-SOLD": "v-nosale"
  };
  // M14 T2: what each verdict color means, shown in the segment tooltip.
  var VERDICT_TIP = {
    "GOOD": "vendor-bought and sells well on the AH - safe steady profit",
    "CHECK VENDOR": "looks profitable - confirm the vendor really sells it",
    "RISKY": "thin margin or odd pricing - check the numbers first",
    "NOT WORTH IT": "cost eats the profit - skip this one",
    "CHECK IN GAME": "vendor price looks off - check the real price in game",
    "NOT VENDOR-SOLD": "no NPC vendor sells this - listed for reference"
  };
  var QUALITY_CLASS = {
    "poor": "q0", "common": "q1", "uncommon": "q2", "rare": "q3",
    "epic": "q4", "legendary": "q5", "artifact": "q6", "heirloom": "q7"
  };
  var SORT_KEY = {
    "score": "score",
    "profit": "net_profit_unit",
    "roi": "roi",
    "sold": "expected_sold_per_day"
  };
  // reverse map: item field -> dropdown value (for header/dropdown sync)
  var KEY_TO_DROPDOWN = {
    "score": "score",
    "net_profit_unit": "profit",
    "roi": "roi",
    "expected_sold_per_day": "sold"
  };

  var state = {
    items: [],
    total: 0,
    counts: {},
    settings: {},
    thresholds: {},
    overrides: [],
    iconUrls: {},
    iconPending: {},
    iconMissing: {},
    iconRetries: {},
    running: false,
    sort: { key: "score", dir: "desc" }
  };

  var $ = function (id) { return document.getElementById(id); };

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text !== undefined && text !== null) { node.textContent = text; }
    return node;
  }

  function toast(message, isError) {
    var node = $("toast");
    node.textContent = message;
    node.className = "in" + (isError ? " error" : "");
    node.setAttribute("role", isError ? "alert" : "status");
    node.hidden = false;
    clearTimeout(toast._timer);
    toast._timer = setTimeout(function () {
      node.hidden = true;
      node.className = "";
    }, 2600);
  }

  function gold(copper) {
    var value = Number(copper);
    if (!Number.isFinite(value)) { value = 0; }
    value = value / 10000;
    var text = value.toFixed(2);
    var parts = text.split(".");
    parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    return parts[0] + "." + parts[1] + "g";
  }

  function num(value, digits) {
    return (Number(value) || 0).toFixed(digits === undefined ? 2 : digits);
  }

  function percent(rate, digits) {
    return (Number(rate) || 0).toFixed(digits === undefined ? 1 : digits) + "%";
  }

  function roiTipText(item) {
    var ah = (Number(item.sell_price) || 0) / 10000;
    var cost = (Number(item.vendor_cost_unit) || 0) / 10000;
    var roi = Math.round((Number(item.roi) || 0) * 100);
    return "ROI = (AH × 0.95 − cost) ÷ cost = (" +
      ah.toFixed(2) + " × 0.95 − " + cost.toFixed(2) + ") ÷ " +
      cost.toFixed(2) + " = " + roi + "%";
  }

  function showRoiTip(btn) {
    var tip = $("roi-tip");
    tip.textContent = btn.dataset.tip || "";
    tip.classList.add("on");
    var rect = btn.getBoundingClientRect();
    var left = Math.max(8, Math.min(rect.left, window.innerWidth - 360));
    tip.style.left = left + "px";
    tip.style.top = (rect.bottom + 6) + "px";
  }

  function hideRoiTip() {
    $("roi-tip").classList.remove("on");
  }

  // M12: the tip is positioned for a fixed moment in layout; any scroll
  // moves the button out from under it, so it must go.
  function hideRoiTipOnScroll() {
    var tip = $("roi-tip");
    if (tip.classList.contains("on")) { tip.classList.remove("on"); }
  }

  // M14 T2: segmented-bar tooltip - verdict name + live count + meaning.
  function showVerdictTip(btn) {
    var tip = $("verdict-tip");
    var verdict = btn.getAttribute("data-verdict");
    var countSpan = btn.querySelector("span");
    var head = document.createElement("b");
    head.textContent = verdict + " (" +
      (countSpan ? countSpan.textContent : "0") + ")";
    tip.textContent = "";
    tip.appendChild(head);
    tip.appendChild(document.createTextNode(
      ": " + (VERDICT_TIP[verdict] || "")));
    tip.classList.add("on");
    var rect = btn.getBoundingClientRect();
    var left = Math.max(8, Math.min(rect.left, window.innerWidth - 320));
    tip.style.left = left + "px";
    tip.style.top = (rect.bottom + 6) + "px";
  }

  function hideVerdictTip() {
    var tip = $("verdict-tip");
    if (tip.classList.contains("on")) { tip.classList.remove("on"); }
  }

  function getJSON(path) {
    return fetch(path).then(function (resp) {
      return resp.json().then(function (body) {
        if (!resp.ok) {
          throw new Error(body && body.error ? body.error : ("HTTP " + resp.status));
        }
        return body;
      });
    });
  }

  function postJSON(path, payload) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {})
    }).then(function (resp) {
      return resp.json().then(function (body) {
        if (!resp.ok) {
          throw new Error(body && body.error ? body.error : ("HTTP " + resp.status));
        }
        return body;
      });
    });
  }

  /* ---------- lazy icons ---------- */

  // Observe the cell, never the img: the img is display:none until it has a
  // src (styles.css), and a display:none target has no layout box, so
  // IntersectionObserver would never report it intersecting -> deadlock.
  var iconObserver = ("IntersectionObserver" in window) ?
    new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) { return; }
        var cell = entry.target;
        iconObserver.unobserve(cell);
        var img = cell.querySelector("img[data-icon-id]");
        if (img) { loadIcon(img); }
      });
    }, { rootMargin: "200px" }) : null;

  function iconCell(itemId, quality, tag) {
    var cell = el(tag || "td", "icon-cell " + (QUALITY_CLASS[quality] || "q1"));
    var img = document.createElement("img");
    img.alt = "";
    img.setAttribute("aria-hidden", "true");
    img.dataset.iconId = String(itemId);
    if (state.iconUrls[itemId]) {
      img.src = state.iconUrls[itemId];
      cell.appendChild(img);
      return cell;
    }
    cell.appendChild(el("span", "icon-ph"));
    if (state.iconMissing[itemId]) {
      return cell;  // upstream has none; keep the placeholder only
    }
    cell.appendChild(img);
    if (iconObserver) {
      cell.dataset.iconId = String(itemId);
      iconObserver.observe(cell);  // the cell is always laid out -> fires
    } else {
      loadIcon(img);
    }
    return cell;
  }

  function loadIcon(img) {
    var id = img.getAttribute("data-icon-id");
    if (!id || state.iconPending[id] || state.iconMissing[id]) { return; }
    if (state.iconUrls[id]) {
      img.src = state.iconUrls[id];
      swapPlaceholder(img);
      return;
    }
    state.iconPending[id] = true;
    getJSON("/api/icon?id=" + encodeURIComponent(id)).then(function (body) {
      delete state.iconPending[id];
      delete state.iconRetries[id];
      if (body && body.ok && body.url) {
        state.iconUrls[id] = body.url;
        document.querySelectorAll('img[data-icon-id="' + id + '"]').forEach(
          function (node) {
            node.src = body.url;
            swapPlaceholder(node);
          });
      } else {
        state.iconMissing[id] = true;  // upstream has none; do not refetch
      }
    }).catch(function () {
      // Transient failure (429/503/DB busy). Re-observe the still-visible cell
      // so it retries when it re-enters view, capped so a hard-down upstream
      // cannot spin forever.
      delete state.iconPending[id];
      var cell = img.parentNode;
      var tried = state.iconRetries[id] || 0;
      if (iconObserver && cell && cell.isConnected && tried < 2) {
        state.iconRetries[id] = tried + 1;
        cell.dataset.iconId = String(id);
        iconObserver.observe(cell);
      }
    });
  }

  function swapPlaceholder(img) {
    var cell = img.parentNode;
    if (!cell) { return; }
    var ph = cell.querySelector(".icon-ph");
    if (ph) { cell.removeChild(ph); }
  }

  /* ---------- summary + top picks ---------- */

  function renderSummary() {
    $("count-good").textContent = state.counts["GOOD"] || 0;
    $("count-check-vendor").textContent = state.counts["CHECK VENDOR"] || 0;
    $("count-risky").textContent = state.counts["RISKY"] || 0;
    $("count-not-worth-it").textContent = state.counts["NOT WORTH IT"] || 0;
    $("count-check-in-game").textContent = state.counts["CHECK IN GAME"] || 0;
    $("count-not-vendor-sold").textContent =
      state.counts["NOT VENDOR-SOLD"] || 0;
  }

  // M14 T2: reflect f-verdict on the segments - selected keeps its color,
  // the others gray out (CSS #summary.has-sel rules).
  function syncVerdictSegments() {
    var value = $("f-verdict").value;
    var summary = $("summary");
    summary.classList.toggle("has-sel", !!value);
    summary.querySelectorAll("button[data-verdict]").forEach(
      function (btn) {
        var active = btn.getAttribute("data-verdict") === value;
        btn.setAttribute("aria-pressed", active ? "true" : "false");
      });
  }

  function pickCard(item) {
    var card = el("div", "card");
    card.appendChild(iconCell(item.id, item.quality, "div"));
    var main = el("div", "card-main");
    var link = el("a", "card-name " + (QUALITY_CLASS[item.quality] || ""));
    link.href = WOWHEAD + item.id;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = item.name || ("item " + item.id);
    main.appendChild(link);
    var ue = extLink(item.undermine, "UE", "ue");
    if (ue) { main.appendChild(ue); }
    main.appendChild(el("div", "card-stats",
      "net " + gold(item.net_profit_unit) + "/unit · roi " +
      percent(item.roi * 100, 0) + " · ~" + num(item.expected_sold_per_day) +
      "/day"));
    var profit = el("div", "card-stats");
    var strong = el("b", item.net_profit_unit > 0 ? "net-pos" : "net-neg",
      gold(item.sell_price) + " → " + gold(item.net_profit_unit));
    profit.appendChild(strong);
    main.appendChild(profit);
    card.appendChild(main);
    return card;
  }

  function renderTopPicks() {
    var host = $("top-picks");
    var note = $("top-picks-note");
    if (iconObserver) {
      host.querySelectorAll(".icon-cell[data-icon-id]").forEach(function (cell) {
        iconObserver.unobserve(cell);
      });
    }
    while (host.firstChild) { host.removeChild(host.firstChild); }
    var good = state.items.filter(function (i) { return i.verdict === "GOOD"; });
    var source = good;
    if (!good.length) {
      source = state.items.filter(function (i) {
        return i.verdict === "CHECK VENDOR";
      });
      note.textContent = source.length ?
        "Nothing vendor-verified yet — showing the top unverified " +
        "candidates (" + (state.counts["CHECK VENDOR"] || 0) + " total). " +
        "Confirm one at a vendor, then mark it seen." : "";
    } else {
      note.textContent = "";
    }
    source.slice(0, 5).forEach(function (item) {
      host.appendChild(pickCard(item));
    });
    revealCards(host);
  }

  // M11: cards fade up as they scroll into view.
  // M12: never re-hide an already-revealed card (a re-render must not flash
  // it back to invisible) + a timer fallback so a card that the observer
  // never reports still becomes visible.
  var cardObserver = ("IntersectionObserver" in window) ?
    new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("in-view");
          cardObserver.unobserve(entry.target);
        }
      });
    }, { threshold: 0.15 }) : null;
  var revealed = ("WeakSet" in window) ? new WeakSet() : null;

  function revealCards(host) {
    host.querySelectorAll(".card").forEach(function (card) {
      if (revealed && revealed.has(card)) { return; }
      if (!cardObserver) {
        card.classList.add("in-view");
        if (revealed) { revealed.add(card); }
        return;
      }
      card.classList.remove("in-view");
      cardObserver.observe(card);
      setTimeout(function () {
        // M13 T7: rescue only cards already inside the viewport; off-screen
        // cards stay observed so their scroll-in reveal still plays.
        if (card.getBoundingClientRect().top < window.innerHeight) {
          card.classList.add("in-view");
          cardObserver.unobserve(card);
          if (revealed) { revealed.add(card); }
        }
      }, 700);
    });
  }

  /* ---------- M15: Chrome-style name suggestions ---------- */

  function suggestScore(name, query) {
    var n = String(name || "").toLowerCase();
    if (!query) { return -1; }
    if (n === query) { return 50; }
    if (n.indexOf(query) === 0) { return 40; }
    var at = n.indexOf(query);
    if (at > 0 && n.charAt(at - 1) === " ") { return 35; }
    if (at !== -1) { return 25; }
    // letters-in-order subsequence: "mthrail" still finds "mithril ore"
    var i = 0;
    for (var j = 0; j < n.length && i < query.length; j++) {
      if (n.charAt(j) === query.charAt(i)) { i += 1; }
    }
    return i === query.length ? 10 : -1;
  }

  function suggestItems(query) {
    var hits = [];
    var seen = {};
    state.items.forEach(function (item) {
      if (!item.name) { return; }
      var score = suggestScore(item.name, query);
      if (score < 0) { return; }
      var key = String(item.name).toLowerCase();
      if (seen[key]) { return; }
      seen[key] = 1;
      hits.push({ name: item.name, verdict: item.verdict, score: score });
    });
    hits.sort(function (a, b) {
      if (b.score !== a.score) { return b.score - a.score; }
      return String(a.name).localeCompare(String(b.name));
    });
    return hits.slice(0, 8);
  }

  function hideSuggest() {
    var box = $("search-suggest");
    if (box) { box.hidden = true; box.textContent = ""; }
  }

  function applySuggestion(name) {
    $("f-search").value = name;
    hideSuggest();
    queueTableRender();
    $("f-search").focus();
  }

  function renderSuggest() {
    var box = $("search-suggest");
    if (!box) { return; }
    var q = $("f-search").value.trim().toLowerCase();
    if (!q) { hideSuggest(); return; }
    var exact = state.items.some(function (item) {
      return String(item.name || "").toLowerCase() === q;
    });
    if (exact) { hideSuggest(); return; }
    var hits = suggestItems(q);
    if (!hits.length) { hideSuggest(); return; }
    box.textContent = "";
    hits.forEach(function (hit) {
      var row = document.createElement("div");
      row.className = "suggest-row";
      row.setAttribute("role", "option");
      row.dataset.name = hit.name;
      var label = document.createElement("span");
      label.className = "suggest-name";
      label.textContent = hit.name;
      row.appendChild(label);
      row.appendChild(el("span", "badge " +
        (VERDICT_CLASS[hit.verdict] || ""), hit.verdict));
      row.addEventListener("mousedown", function (event) {
        event.preventDefault();  // keep focus in the search box
        applySuggestion(hit.name);
      });
      box.appendChild(row);
    });
    box.hidden = false;
  }

  /* ---------- table ---------- */

  function viewFilters() {
    return {
      search: $("f-search").value.trim().toLowerCase(),
      verdict: $("f-verdict").value,
      minSold: Number($("f-min-sold").value) || 0,
      minProfit: Number($("f-min-profit").value) || 0,
      minSaleRate: (Number($("f-min-sale-rate").value) || 0) / 100,
      minRoi: (Number($("f-min-roi").value) || 0) / 100,
      onlyFlagged: $("f-only-flagged").checked,
      hideExcluded: $("f-hide-excluded").checked
    };
  }

  function filteredItems() {
    var f = viewFilters();
    var rows = state.items.filter(function (item) {
      if (f.hideExcluded && item.excluded) { return false; }
      if (f.verdict && item.verdict !== f.verdict) { return false; }
      if (f.search) {
        var haystack = (item.name || "").toLowerCase() + " " + item.id;
        if (haystack.indexOf(f.search) === -1) { return false; }
      }
      if (item.net_profit_unit / 10000 < f.minProfit) { return false; }
      if (item.roi < f.minRoi) { return false; }
      if (item.sale_rate < f.minSaleRate) { return false; }
      if (item.expected_sold_per_day < f.minSold) { return false; }
      if (f.onlyFlagged &&
          !(item.suspicious || item.flagged_risky || item.notes)) {
        return false;
      }
      return true;
    });
    var s = state.sort;
    var desc = s.dir !== "asc";
    var key = s.key;
    rows.sort(function (a, b) {
      if (key === "name") {
        var cmp = String(a.name || "").localeCompare(String(b.name || ""));
        return desc ? -cmp : cmp;
      }
      var av = Number(a[key]) || 0;
      var bv = Number(b[key]) || 0;
      return desc ? (bv - av) : (av - bv);
    });
    return rows;
  }

  function chip(text, cls) {
    return el("span", "tag" + (cls ? " " + cls : ""), text);
  }

  function extLink(href, label, cls) {
    if (!href) { return null; }
    var a = document.createElement("a");
    a.className = "ext-link" + (cls ? " " + cls : "");
    a.href = href;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.textContent = label;
    a.title = label === "UE" ? "Open on Undermine Exchange" : label;
    a.setAttribute("aria-label", a.title);
    return a;
  }

  function itemCell(item) {
    var cell = el("td");
    var link = document.createElement("a");
    link.className = "item-link " + (QUALITY_CLASS[item.quality] || "");
    link.href = WOWHEAD + item.id;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = item.name || ("item " + item.id);
    cell.appendChild(link);
    cell.appendChild(el("span", "item-id", "#" + item.id));
    var ue = extLink(item.undermine, "UE", "ue");
    if (ue) { cell.appendChild(ue); }
    if (item.vendor_sold === 1 && item.vendor_npcs &&
        item.vendor_npcs.length) {
      var names = item.vendor_npcs.slice(0, 2).map(function (npc) {
        return npc && npc.name ? npc.name : "";
      }).filter(Boolean).join(", ");
      var extra = item.vendor_npcs.length > 2 ?
        " (+" + (item.vendor_npcs.length - 2) + " more)" : "";
      cell.appendChild(el("span", "vendor-line",
        "sold by " + (names || "vendors") + extra + " (Wowhead)"));
    }
    var tags = el("div", "chips-row");
    tags.appendChild(chip(item.commodity ? "commodity" : "realm",
      item.commodity ? "tag-commodity" : "tag-realm"));
    tags.appendChild(chip(item.tier, "tier-" + item.tier));
    (item.chips || []).forEach(function (hint) {
      tags.appendChild(chip(hint, hint === "cost mismatch" ?
        "tag-cost-mismatch" : "tag-hint"));
    });
    cell.appendChild(tags);
    return cell;
  }

  function verdictCell(item) {
    var cell = el("td");
    cell.appendChild(el("span", "badge " + (VERDICT_CLASS[item.verdict] || ""),
      item.verdict));
    cell.appendChild(el("span", "reason", item.reason));
    return cell;
  }

  function seenCell(item) {
    var cell = el("td");
    var btn = el("button", "seen-btn", "✓ seen");
    btn.type = "button";
    btn.setAttribute("aria-pressed", item.vendor_verified ? "true" : "false");
    btn.setAttribute("aria-label",
      "Mark " + (item.name || item.id) + " as seen at a vendor");
    btn.addEventListener("click", function () {
      var next = !item.vendor_verified;
      btn.disabled = true;
      postJSON("/api/item/verify", { id: item.id, vendor_verified: next })
        .then(function () {
          item.vendor_verified = next ? 1 : 0;
          btn.setAttribute("aria-pressed", next ? "true" : "false");
          toast(next ? "Marked seen: " + (item.name || item.id)
                     : "Cleared: " + (item.name || item.id));
          setTimeout(loadItems, 400);
        })
        .catch(function (err) {
          toast("Verify failed: " + err.message, true);
        })
        .then(function () { btn.disabled = false; });
    });
    cell.appendChild(btn);
    return cell;
  }

  function notesCell(item) {
    var cell = el("td");
    var input = document.createElement("input");
    input.className = "notes-input";
    input.type = "text";
    input.maxLength = 500;
    input.value = item.notes || "";
    input.setAttribute("aria-label",
      "Notes for " + (item.name || item.id));
    input.addEventListener("change", function () {
      postJSON("/api/item/notes", { id: item.id, notes: input.value })
        .then(function () {
          item.notes = input.value;
          toast("Notes saved");
        })
        .catch(function (err) {
          input.value = item.notes || "";
          toast("Notes failed: " + err.message, true);
        });
    });
    cell.appendChild(input);
    return cell;
  }

  /* ---- M16: Posting Advisor ---- */

  var qtyState = { id: 0, btn: null, mv: 0, low: 0, seq: 0 };

  function advisorCell(item) {
    var cell = el("td");
    var btn = el("button", "advisor", "Advisor");
    btn.type = "button";
    btn.dataset.id = String(item.id);
    btn.dataset.name = item.name || "";
    btn.dataset.sell = String(item.sell_price || 0);
    btn.setAttribute("aria-label",
      "Posting advisor for " + (item.name || item.id));
    cell.appendChild(btn);
    return cell;
  }

  function qtyPriceCopper() {
    var value = parseFloat($("qty-price").value);
    if (!Number.isFinite(value) || value <= 0) { return 0; }
    return Math.round(value * 10000);
  }

  function qtyMyUnits() {
    var value = parseInt($("qty-myqty").value, 10);
    if (!Number.isFinite(value) || value <= 0) { return 0; }
    return Math.min(value, 1000000000);
  }

  function group(value) {
    var text = String(Math.round(Number(value) || 0));
    return text.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }

  function clearQtyDetail() {
    $("qty-stack").hidden = true;
    $("qty-stack").textContent = "";
    $("qty-age").textContent = "";
  }

  function positionQty(btn) {
    var pop = $("qty-pop");
    if (!btn || pop.hidden) { return; }
    var rect = btn.getBoundingClientRect();
    var width = pop.offsetWidth || 360;
    var left = Math.max(8, Math.min(rect.left, window.innerWidth - width - 8));
    var top = rect.bottom + 6;
    var height = pop.offsetHeight || 320;
    if (top + height > window.innerHeight - 8) {
      top = Math.max(8, rect.top - height - 6);
    }
    pop.style.left = left + "px";
    pop.style.top = top + "px";
  }

  function closeQty() {
    var pop = $("qty-pop");
    if (pop.hidden) { return; }
    pop.hidden = true;
    qtyState.btn = null;
  }

  function openQty(btn) {
    qtyState.id = Number(btn.dataset.id) || 0;
    qtyState.btn = btn;
    qtyState.mv = 0;
    qtyState.low = 0;
    $("qty-item").textContent = btn.dataset.name || "";
    var sell = Number(btn.dataset.sell) || 0;
    $("qty-price").value = sell ? (sell / 10000).toFixed(2) : "";
    $("qty-myqty").value = "";
    $("qty-result").textContent = "Loading…";
    clearQtyDetail();
    $("qty-pop").hidden = false;
    positionQty(btn);
    loadQty();
  }

  function loadQty() {
    var price = qtyPriceCopper();
    var seq = ++qtyState.seq;
    if (!qtyState.id) { return; }
    if (!price) {
      $("qty-result").textContent = "Enter a post price (gold)";
      clearQtyDetail();
      return;
    }
    var my = qtyMyUnits();
    var url = "/api/item/quantity?id=" + qtyState.id +
      "&price=" + price + "&confidence=0.95" + (my ? "&qty=" + my : "");
    getJSON(url).then(function (data) {
      if (seq !== qtyState.seq) { return; }  // a newer request owns the pop
      renderQty(data);
    }).catch(function (err) {
      if (seq !== qtyState.seq) { return; }
      renderQtyError(err.message);
    });
  }

  function renderQty(data) {
    if (!data) { return; }
    if (!data.ok) { renderQtyError(data.error); return; }
    if (!data.commodity) {
      $("qty-result").textContent = data.message ||
        "Commodities only - no sold/day data for this item.";
      clearQtyDetail();
      return;
    }
    qtyState.mv = Number(data.market_value) || 0;
    qtyState.low = Number(data.lowest_price) || 0;
    var confPct = Math.round((Number(data.confidence) || 0.95) * 100);
    var maxQty = Number(data.max_qty) || 0;
    var lam = Number(data.lam_eff) || 0;
    if (maxQty > 0) {
      $("qty-result").textContent = "Post up to " + group(maxQty) +
        " units - " + confPct + "% sure: all sell within 24 hours";
    } else if (lam > 0) {
      $("qty-result").textContent =
        "Not " + confPct + "% sellable in 24h here - about 1 unit every ~" +
        num(1 / lam, 1) + " days";
    } else {
      $("qty-result").textContent = "No TSM demand at this price.";
    }
    renderQtyStack(data);
    var age = "TSM sold " + group(data.sold_per_day) +
      "/day region-wide · scan " + agoText(data.tsm_updated_at) +
      " · wall " + agoText(data.auctions_fetched_at);
    if (data.refreshing) { age += " · refreshing…"; }
    $("qty-age").textContent = age;
    positionQty(qtyState.btn);
  }

  function renderQtyStack(data) {
    var host = $("qty-stack");
    host.textContent = "";
    var stack = data.stack;
    if (!stack) {
      host.hidden = true;
      return;
    }
    host.hidden = false;
    var conf = Number(data.confidence) || 0.95;
    var confPct = Math.round(conf * 100);
    var achieved = Number(stack.achieved) || 0;
    var line = document.createElement("div");
    if (achieved >= conf - 1e-9) {
      line.className = "qty-stack-line good";
      line.textContent = "Your " + group(stack.qty) + ": " + confPct +
        "%+ sure sold in 24h";
    } else {
      line.className = "qty-stack-line warn";
      var pct = Math.round(achieved * 1000) / 10;
      var within = stack.days_95 === null || stack.days_95 === undefined
        ? "" : " - " + confPct + "% sure within ~" +
          num(stack.days_95, Number(stack.days_95) < 10 ? 1 : 0) + " days";
      line.textContent = "Your " + group(stack.qty) + ": only " + pct +
        "% sure in 24h" + within;
    }
    host.appendChild(line);
    var sub = document.createElement("div");
    sub.className = "qty-stack-sub";
    sub.textContent =
      stack.typical_days === null || stack.typical_days === undefined
        ? "no demand at this price"
        : "(clears in ~" +
          num(stack.typical_days, Number(stack.typical_days) < 10 ? 1 : 0) +
          " days)";
    host.appendChild(sub);
  }

  function renderQtyError(message) {
    if (message === "auction_data_warming_up") {
      $("qty-result").textContent =
        "Auction data is downloading - try again in a minute.";
    } else if (message === "no_sales_data") {
      $("qty-result").textContent = "No TSM sales data for this item yet.";
    } else {
      $("qty-result").textContent = message || "Advisor unavailable";
    }
    clearQtyDetail();
    positionQty(qtyState.btn);
  }

  function itemRow(item, animate) {
    var row = document.createElement("tr");
    if (animate) { row.className = "row-enter"; }
    row.appendChild(iconCell(item.id, item.quality));
    row.appendChild(itemCell(item));
    row.appendChild(verdictCell(item));
    var vendor = el("td", "num", gold(item.purchase_price));
    if (item.purchase_price === 1) {
      vendor.appendChild(el("div", "reason", "check in game"));
    }
    row.appendChild(vendor);
    row.appendChild(el("td", "num", String(item.purchase_quantity)));
    row.appendChild(el("td", "num", gold(item.vendor_cost_unit)));
    row.appendChild(el("td", "num", gold(item.sell_price)));
    row.appendChild(el("td", "num", num(item.sold_per_day, 1)));
    row.appendChild(el("td", "num", percent(item.sale_rate * 100)));
    var net = el("td", "num " + (item.net_profit_unit > 0 ? "net-pos"
                                                        : "net-neg"),
      gold(item.net_profit_unit));
    row.appendChild(net);
    var roiCell = el("td", "num", percent(item.roi * 100, 0));
    var info = el("button", "roi-info", "i");
    info.type = "button";
    info.dataset.tip = roiTipText(item);
    info.setAttribute("aria-label",
      "How ROI is calculated for " + (item.name || item.id));
    roiCell.appendChild(info);
    row.appendChild(roiCell);
    row.appendChild(seenCell(item));
    row.appendChild(notesCell(item));
    row.appendChild(advisorCell(item));
    return row;
  }

  function renderTable(animate) {
    closeQty();  // M16: row anchors are about to be rebuilt
    var body = $("items-body");
    var scroller = document.querySelector(".table-scroll");
    // M12: rebuilding the rows resets scroll to the top unless it is saved
    // and restored around the swap.
    var keepTop = scroller ? scroller.scrollTop : 0;
    var rows = filteredItems();
    if (iconObserver) {
      // release observations of rows about to die (prevents unbounded growth)
      body.querySelectorAll(".icon-cell[data-icon-id]").forEach(function (cell) {
        iconObserver.unobserve(cell);
      });
    }
    while (body.firstChild) { body.removeChild(body.firstChild); }
    var shown = rows.slice(0, LIST_LIMIT);
    // M12: entrance stagger only for a fresh top-of-list load; a re-render
    // while scrolled (or a filter change) swaps rows in place, no jitter.
    var animateRows = !!animate && keepTop === 0;
    shown.forEach(function (item, index) {
      // M13 T7: stagger only the first 40 rows (the rest are below the fold
      // on load); --vf-i travels inline, capped at 15.
      var animate = animateRows && index < 40;
      var row = itemRow(item, animate);
      if (animate) {
        row.style.setProperty("--vf-i", Math.min(index, 15));
      }
      body.appendChild(row);
    });
    if (scroller && scroller.scrollTop !== keepTop) {
      scroller.scrollTop = keepTop;
    }
    $("items-count").textContent =
      "showing " + shown.length + " of " + rows.length + " matching · " +
      state.total + " scored (top " + FETCH_LIMIT + " by score)";
    $("empty-state").hidden = rows.length > 0;
  }

  function render(animate) {
    renderSummary();
    renderTopPicks();
    renderTable(animate);
  }

  /* ---------- data loading ---------- */

  function loadItems() {
    return getJSON("/api/items?limit=" + FETCH_LIMIT + "&sort=score")
      .then(function (payload) {
        state.items = payload.items || [];
        state.total = payload.total || 0;
        state.counts = payload.counts || {};
        render(true);  // fresh data -> entrance animation plays
      })
      .catch(function (err) {
        toast("Items failed: " + err.message, true);
      });
  }

  // M15: relative age for the TSM chip ("2026-10-15T09:00:00" is parsed
  // as local time, the same clock extract's updated_at stamps use).
  function agoText(iso) {
    if (!iso) { return "never"; }
    var then = new Date(iso).getTime();
    if (!isFinite(then)) { return "unknown"; }
    var secs = (Date.now() - then) / 1000;
    if (secs < 90) { return "just now"; }
    if (secs < 3600) { return Math.round(secs / 60) + "m ago"; }
    if (secs < 86400) { return Math.round(secs / 3600) + "h ago"; }
    return Math.round(secs / 86400) + "d ago";
  }

  function renderTsmChip(block) {
    var node = $("tsm-chip");
    if (!node) { return; }
    node.classList.remove("tsm-newer", "tsm-err");
    if (!block) {
      node.textContent = "TSM: unknown";
      return;
    }
    if (block.running) {
      node.textContent = "TSM refreshing…";
      return;
    }
    var age = agoText(block.fetched_at);
    if (block.error) {
      node.textContent = "TSM " + age + " · refresh failed";
      node.classList.add("tsm-err");
      return;
    }
    if (block.newer_than_extract) {
      node.textContent = "TSM " + age + " · re-extract for new items";
      node.classList.add("tsm-newer");
      return;
    }
    node.textContent = "TSM " + age + " · in sync";
  }

  function loadStatus() {
    return getJSON("/api/extract/status").then(function (payload) {
      state.running = !!payload.running;
      var chipNode = $("region-chip");
      chipNode.textContent = state.settings.region || "eu";
      var when = payload.updated_at || payload.started_at || "";
      $("data-chip").textContent = "data: " + (payload.phase || "?") +
        (when ? " · " + when : "") +
        (payload.running ? " · running" : "");
      renderTsmChip(payload.tsm);
      $("extract-status").textContent =
        "phase " + (payload.phase || "?") +
        " · scanned " + (payload.scanned || 0) +
        " · candidates " + (payload.candidates || 0) +
        (payload.tsm_skipped ? " · tsm skipped " + payload.tsm_skipped : "") +
        (payload.running ? " · running" : "") +
        (payload.last_error ? " · error: " + payload.last_error : "");
    }).catch(function () {
      $("data-chip").textContent = "data: offline";
      renderTsmChip(null);
    });
  }

  /* ---------- settings ---------- */

  var SETTINGS_FIELDS = [
    ["region", "s-region"],
    ["icon_host", "s-icon-host"]
  ];

  function fillSettingsForm() {
    SETTINGS_FIELDS.forEach(function (pair) {
      var node = $(pair[1]);
      var value = state.settings[pair[0]];
      if (value === undefined || value === null) { return; }
      node.value = String(value);
    });
  }

  // View sliders are client-side only; seed them from the server's
  // effective thresholds once (load/reset) so later settings saves do not
  // yank a live filter.
  function seedViewSliders(th) {
    var s = th || {};
    if (s.min_sold_per_day !== undefined) {
      $("f-min-sold").value = String(Math.min(100, s.min_sold_per_day));
    }
    if (s.min_profit !== undefined) {
      $("f-min-profit").value = String(Math.min(1000, s.min_profit));
    }
    if (s.min_sale_rate !== undefined) {
      $("f-min-sale-rate").value =
        String(Math.round(Math.min(1, s.min_sale_rate) * 100));
    }
    if (s.min_roi !== undefined) {
      $("f-min-roi").value = String(Math.round(Math.min(1, s.min_roi) * 100));
    }
    syncSliderOutputs();
  }

  function loadSettings() {
    return getJSON("/api/settings").then(function (payload) {
      state.settings = payload.settings || {};
      state.thresholds = payload.thresholds || {};
      state.overrides = payload.overrides || [];
      fillSettingsForm();
      seedViewSliders(state.thresholds);
      $("region-chip").textContent = state.settings.region || "eu";
    }).catch(function (err) {
      toast("Settings failed: " + err.message, true);
    });
  }

  /* ---------- API keys (M11) ---------- */

  function akStatus(message, isError) {
    var node = $("ak-status");
    node.textContent = message;
    node.className = "ak-status" + (isError ? " error" : "");
    node.hidden = false;
  }

  function loadApiKeys() {
    return getJSON("/api/api-keys").then(function (payload) {
      var list = $("api-keys-list");
      while (list.firstChild) { list.removeChild(list.firstChild); }
      var keys = payload.keys || [];
      keys.forEach(function (key) {
        var li = el("li", "ak-row");
        li.appendChild(el("span", "ak-label", key.label || "(no label)"));
        li.appendChild(el("code", "ak-id", key.client_id));
        li.appendChild(el("span", "ak-mask", key.secret_masked));
        if (key.active) { li.appendChild(el("span", "ak-active", "active")); }
        if (key.enabled === false || key.enabled === 0) {
          li.appendChild(el("span", "ak-off", "disabled"));
        }
        if (key.cooldown_until) {
          li.appendChild(el("span", "ak-cool", "cooling"));
        }
        li.appendChild(el("span", "spacer"));
        var testBtn = el("button", "btn btn-quiet", "Test");
        testBtn.type = "button";
        testBtn.addEventListener("click", function () { testKey(key.id); });
        var delBtn = el("button", "btn btn-warn", "Delete");
        delBtn.type = "button";
        delBtn.addEventListener("click", function () {
          postJSON("/api/api-keys", { action: "delete", id: key.id })
            .then(function () {
              toast("Key removed");
              return loadApiKeys();
            }).catch(function (err) { akStatus(err.message, true); });
        });
        li.appendChild(testBtn);
        li.appendChild(delBtn);
        list.appendChild(li);
      });
      if (!keys.length) {
        list.appendChild(el("li", "ak-row ak-empty muted",
          "No keys added yet — type your Blizzard credentials below " +
          "and press Test, then Add."));
      }
    }).catch(function (err) {
      akStatus("Keys failed to load: " + err.message, true);
    });
  }

  // Pre-save probe: tests the credentials typed in the form without storing
  // them. The route accepts raw client_id/client_secret for exactly this.
  function testFormCredentials() {
    var id = $("ak-id").value.trim();
    var secret = $("ak-secret").value;
    if (!id || !secret) {
      akStatus("Client ID and Secret are required", true);
      return;
    }
    akStatus("Testing credentials…", false);
    postJSON("/api/api-keys", {
      action: "test", client_id: id, client_secret: secret
    }).then(function (body) {
      akStatus((body.ok ? "OK — " : "Failed — ") + (body.message || ""),
               !body.ok);
    }).catch(function (err) { akStatus(err.message, true); });
  }

  function addApiKey() {
    var label = $("ak-label").value.trim();
    var id = $("ak-id").value.trim();
    var secret = $("ak-secret").value;
    if (!id || !secret) {
      akStatus("Client ID and Secret are required", true);
      return;
    }
    postJSON("/api/api-keys", {
      action: "add", label: label, client_id: id, client_secret: secret
    }).then(function () {
      $("ak-label").value = "";
      $("ak-id").value = "";
      $("ak-secret").value = "";
      akStatus("Key added", false);
      return loadApiKeys();
    }).catch(function (err) { akStatus(err.message, true); });
  }

  function testKey(id) {
    akStatus("Testing key…", false);
    postJSON("/api/api-keys", { action: "test", id: id })
      .then(function (body) {
        akStatus((body.ok ? "OK — " : "Failed — ") + (body.message || ""),
                 !body.ok);
      }).catch(function (err) { akStatus(err.message, true); });
  }

  // Only keys the user actually changed are sent: empty fields stop wiping
  // overrides, and untouched keys never re-render the whole panel.
  function collectSettingsDiff() {
    var mapping = {};
    var problem = null;
    SETTINGS_FIELDS.forEach(function (pair) {
      var key = pair[0];
      var node = $(pair[1]);
      var raw = node.value.trim();
      var next;
      if (raw === "") {
        next = null;
      } else if (node.type === "number") {
        next = Number(raw);
        if (!Number.isFinite(next)) {
          problem = problem || (key + " is not a valid number");
        }
      } else {
        next = raw;
      }
      var current = state.settings[key];
      var same = (next === null &&
                  (current === null || current === undefined)) ||
                 (next !== null && String(next) === String(current));
      if (!same) {
        mapping[key] = next;
      }
    });
    return problem ? {error: problem} : {mapping: mapping};
  }

  function saveSettings() {
    var diff = collectSettingsDiff();
    if (diff.error) {
      toast(diff.error, true);
      return;
    }
    if (!Object.keys(diff.mapping).length) {
      toast("No changes to save");
      return;
    }
    postJSON("/api/settings", { settings: diff.mapping }).then(
      function (payload) {
        state.settings = payload.settings || {};
        state.overrides = payload.overrides || [];
        toast("Settings saved");
        fillSettingsForm();
        loadStatus();
        loadItems();
      }).catch(function (err) {
      toast("Save failed: " + err.message, true);
    });
  }

  function resetSettings() {
    var mapping = {};
    state.overrides.forEach(function (key) { mapping[key] = null; });
    if (!Object.keys(mapping).length) {
      toast("No overrides to reset");
      return;
    }
    postJSON("/api/settings", { settings: mapping }).then(function (payload) {
      state.settings = payload.settings || {};
      state.overrides = payload.overrides || [];
      toast("Overrides reset");
      fillSettingsForm();
      loadStatus();
      loadItems();
    }).catch(function (err) {
      toast("Reset failed: " + err.message, true);
    });
  }

  function reextract() {
    if (!window.confirm(
        "Re-extract vendor data? This calls the Blizzard API for every " +
        "item and can take an hour. Progress is resumable.")) {
      return;
    }
    postJSON("/api/extract/start", {restart: true}).then(function () {
      toast("Extraction started");
      pollStatus();
    }).catch(function (err) {
      toast("Start failed: " + err.message, true);
    });
  }

  var pollTimer = null;

  function pollStatus() {
    loadStatus().then(function () {
      clearTimeout(pollTimer);
      if (state.running) {
        pollTimer = setTimeout(pollStatus, 2000);
      } else {
        loadItems();
      }
    });
  }

  /* ---------- vendor check (M9) ---------- */

  var vcTimer = null;

  function renderVendorCheck(st) {
    var parts = st.phase || "idle";
    if (st.total) { parts += " · " + st.done + "/" + st.total; }
    parts += " · sold " + (st.sold || 0) +
      " · no vendor " + (st.not_sold || 0);
    if (st.blocked) { parts += " · blocked " + st.blocked; }
    $("vc-tally").textContent = parts;
    var pill = document.querySelector(".vc-pill");
    if (pill) {
      pill.className = "vc-pill" + (st.running ? " on" : "");
    }
    var pct = st.total ? Math.round(100 * st.done / st.total) : 0;
    $("vc-bar").style.width = pct + "%";
    $("vc-attention").hidden = !st.needs_attention;
    $("btn-vc-cancel").hidden = !st.running;
    $("btn-vc-start").disabled = !!st.running;
    $("btn-vc-restart").disabled = !!st.running;
  }

  function pollVendorCheck() {
    getJSON("/api/vendor-check/status").then(function (st) {
      renderVendorCheck(st);
      clearTimeout(vcTimer);
      if (st.running && !$("settings-panel").hidden) {
        vcTimer = setTimeout(pollVendorCheck, 2000);
      } else if (st.phase === "complete" || st.phase === "paused" ||
                 st.phase === "cancelled") {
        loadItems();  // verdicts changed with the new vendor data
      }
    }).catch(function () {
      $("vc-tally").textContent = "server offline";
      var offlinePill = document.querySelector(".vc-pill");
      if (offlinePill) { offlinePill.className = "vc-pill err"; }
      $("btn-vc-start").disabled = false;
      $("btn-vc-restart").disabled = false;
      $("btn-vc-cancel").hidden = true;
      clearTimeout(vcTimer);
      if (!$("settings-panel").hidden) {
        vcTimer = setTimeout(pollVendorCheck, 8000);
      }
    });
  }

  function vcStart(restart) {
    postJSON("/api/vendor-check/start", restart ? {restart: true} : {})
      .then(function () {
        toast(restart ? "Re-checking every item" :
              "Vendor check started — keep the Edge window open");
        pollVendorCheck();
      }).catch(function (err) {
      toast("Vendor check failed: " + err.message, true);
    });
  }

  /* ---------- settings drawer ---------- */

  function openDrawer() {
    $("settings-panel").hidden = false;
    document.body.classList.add("drawer-open");
    $("btn-settings").setAttribute("aria-expanded", "true");
    var status = $("ak-status");
    status.hidden = true;
    status.textContent = "";
    pollVendorCheck();
    loadApiKeys();
  }

  function closeDrawer() {
    document.body.classList.remove("drawer-open");
    $("settings-panel").hidden = true;
    $("btn-settings").setAttribute("aria-expanded", "false");
    clearTimeout(vcTimer);
  }

  /* ---------- wiring ---------- */

  function syncSliderOutputs() {
    $("out-min-sold").textContent = $("f-min-sold").value;
    $("out-min-profit").textContent = $("f-min-profit").value;
    $("out-min-sale-rate").textContent = $("f-min-sale-rate").value;
    $("out-min-roi").textContent = $("f-min-roi").value;
  }

  function updateSortHeaders() {
    var key = state.sort.key;
    var asc = state.sort.dir === "asc";
    document.querySelectorAll("table.items thead button[data-sort]").forEach(
      function (btn) {
        var active = btn.getAttribute("data-sort") === key;
        var th = btn.closest("th");
        if (th) {
          th.setAttribute("aria-sort", active ? (asc ? "ascending"
                                                     : "descending") : "none");
        }
        var arrow = btn.querySelector(".sort-arrow");
        if (arrow) { arrow.textContent = active ? (asc ? "▲" : "▼") : ""; }
      });
  }

  function syncSortDropdown() {
    // Unmapped column sorts (name, sale_rate, ...) have no dropdown option;
    // show the disabled "Custom sort…" placeholder instead of a stale label.
    $("f-sort").value = KEY_TO_DROPDOWN[state.sort.key] || "";
  }

  function applySort(key, dir) {
    if (state.sort.key === key && !dir) {
      state.sort.dir = state.sort.dir === "asc" ? "desc" : "asc";
    } else {
      state.sort.key = key;
      state.sort.dir = dir || "desc";
    }
    syncSortDropdown();
    updateSortHeaders();
    renderTable();
  }

  function resetViewFilters() {
    $("f-search").value = "";
    hideSuggest();
    $("f-verdict").value = "";
    $("f-sort").value = "score";
    $("f-only-flagged").checked = false;
    $("f-hide-excluded").checked = true;
    state.sort = { key: "score", dir: "desc" };
    updateSortHeaders();
    seedViewSliders();
    syncVerdictSegments();
    renderTable();
  }

  var renderQueued = false;
  function queueTableRender() {
    if (renderQueued) { return; }
    renderQueued = true;
    var raf = window.requestAnimationFrame || function (fn) {
      return setTimeout(fn, 16);
    };
    raf(function () {
      renderQueued = false;
      renderTable();
    });
  }

  function wire() {
    $("btn-refresh").addEventListener("click", function () {
      loadStatus();
      loadItems();
      toast("Refreshed");
    });
    $("btn-settings").addEventListener("click", function () {
      if ($("settings-panel").hidden) { openDrawer(); } else { closeDrawer(); }
    });
    $("btn-settings-close").addEventListener("click", closeDrawer);
    $("settings-backdrop").addEventListener("click", closeDrawer);
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !$("settings-panel").hidden) {
        closeDrawer();
      }
    });
    /* M13 T4: filter popover (additive — the drawer Escape above is untouched) */
    function closeFilterPop() {
      $("filter-pop").hidden = true;
      $("btn-filters").setAttribute("aria-expanded", "false");
    }
    $("btn-filters").addEventListener("click", function () {
      var pop = $("filter-pop");
      pop.hidden = !pop.hidden;
      $("btn-filters").setAttribute("aria-expanded", pop.hidden ? "false"
                                                                : "true");
    });
    document.addEventListener("click", function (event) {
      if ($("filter-pop").hidden) { return; }
      if (event.target.closest("#btn-filters, #filter-pop")) { return; }
      closeFilterPop();
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !$("filter-pop").hidden) {
        closeFilterPop();
      }
    });
    /* M13 T6: scroll-to-top — listener on the persistent list scroller
       (renderTable rebuilds rows inside it; the scroller element and its
       scroll position survive). Never the window. */
    var scroller = document.querySelector(".table-scroll");
    var btnTop = $("btn-top");
    scroller.addEventListener("scroll", function () {
      btnTop.hidden = !(scroller.scrollTop > 240);
    });
    btnTop.addEventListener("click", function () {
      scroller.scrollTo({
        top: 0,
        behavior: matchMedia("(prefers-reduced-motion: reduce)").matches
          ? "auto" : "smooth"
      });
    });
    $("btn-vc-start").addEventListener("click", function () { vcStart(false); });
    $("btn-vc-restart").addEventListener("click", function () {
      if (window.confirm(
          "Re-check every item including already-verified ones? " +
          "An Edge window stays open for the whole run (~2h).")) {
        vcStart(true);
      }
    });
    $("btn-vc-cancel").addEventListener("click", function () {
      postJSON("/api/vendor-check/cancel", {}).then(function (body) {
        toast(body.cancelled ? "Stopping after the current item"
                             : "Nothing was running");
        pollVendorCheck();
      }).catch(function (err) {
        toast("Cancel failed: " + err.message, true);
      });
    });
    var itemsBody = $("items-body");
    itemsBody.addEventListener("mouseover", function (event) {
      var btn = event.target.closest(".roi-info");
      if (btn) { showRoiTip(btn); }
    });
    itemsBody.addEventListener("mouseout", function (event) {
      if (event.target.closest(".roi-info")) { hideRoiTip(); }
    });
    itemsBody.addEventListener("focusin", function (event) {
      var btn = event.target.closest(".roi-info");
      if (btn) { showRoiTip(btn); }
    });
    itemsBody.addEventListener("focusout", function (event) {
      if (event.target.closest(".roi-info")) { hideRoiTip(); }
    });
    // M12: scroll moves the row out from under the fixed-position tip.
    // Capture phase: element scroll events do not bubble.
    window.addEventListener("scroll", hideRoiTipOnScroll, true);
    /* M16: Posting Advisor popover */
    itemsBody.addEventListener("click", function (event) {
      var btn = event.target.closest(".advisor");
      if (btn) { openQty(btn); }
    });
    $("qty-price").addEventListener("change", loadQty);
    $("qty-preset-mv").addEventListener("click", function () {
      if (!qtyState.mv) {
        toast("Market value unknown for this item", true);
        return;
      }
      $("qty-price").value = (qtyState.mv / 10000).toFixed(2);
      loadQty();
    });
    $("qty-preset-low").addEventListener("click", function () {
      if (!qtyState.low) {
        toast("Nothing listed at a price right now", true);
        return;
      }
      $("qty-price").value = (qtyState.low / 10000).toFixed(2);
      loadQty();
    });
    $("qty-myqty").addEventListener("change", loadQty);
    document.addEventListener("click", function (event) {
      if ($("qty-pop").hidden) { return; }
      if (event.target.closest("#qty-pop, .advisor")) { return; }
      closeQty();
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !$("qty-pop").hidden) { closeQty(); }
    });
    // Capture phase: the popover scrolls itself without losing position,
    // but any other scroll moves the anchor row out from under it.
    window.addEventListener("scroll", function (event) {
      if ($("qty-pop").hidden) { return; }
      if (event.target && $("qty-pop").contains(event.target)) { return; }
      closeQty();
    }, true);
    $("f-search").addEventListener("input", function () {
      queueTableRender();
      renderSuggest();
    });
    $("f-search").addEventListener("keydown", function (event) {
      var box = $("search-suggest");
      if (event.key === "Escape") {
        // input[type=search] natively clears on Escape; keep the query.
        event.preventDefault();
        hideSuggest();
        return;
      }
      if (!box || box.hidden || !box.firstChild) { return; }
      var rows = Array.prototype.slice.call(box.children);
      var current = box.querySelector(".suggest-row.active");
      var idx = current ? rows.indexOf(current) : -1;
      if (event.key === "ArrowDown") {
        event.preventDefault();
        if (current) { current.classList.remove("active"); }
        idx = (idx + 1) % rows.length;
        rows[idx].classList.add("active");
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        if (current) { current.classList.remove("active"); }
        if (idx > 0) { rows[idx - 1].classList.add("active"); }
      } else if (event.key === "Enter") {
        var pick = current || rows[0];
        if (pick) {
          event.preventDefault();
          applySuggestion(pick.dataset.name);
        }
      }
    });
    document.addEventListener("mousedown", function (event) {
      if (!event.target.closest(".search-wrap")) { hideSuggest(); }
    });
    ["f-verdict", "f-only-flagged", "f-hide-excluded"].forEach(
      function (id) {
        $(id).addEventListener("change", queueTableRender);
      });
    $("f-sort").addEventListener("change", function () {
      applySort(SORT_KEY[$("f-sort").value] || "score", "desc");
    });
    document.querySelector("table.items thead").addEventListener("click",
      function (event) {
        var btn = event.target.closest("button[data-sort]");
        if (!btn) { return; }
        applySort(btn.getAttribute("data-sort"));
      });
    ["f-min-sold", "f-min-profit", "f-min-sale-rate", "f-min-roi"].forEach(
      function (id) {
        $(id).addEventListener("input", function () {
          syncSliderOutputs();
          queueTableRender();
        });
      });
    $("f-reset").addEventListener("click", resetViewFilters);
    $("summary").addEventListener("click", function (event) {
      var button = event.target.closest("button[data-verdict]");
      if (!button) { return; }
      var verdict = button.getAttribute("data-verdict");
      $("f-verdict").value =
        $("f-verdict").value === verdict ? "" : verdict;
      syncVerdictSegments();
      renderTable();
    });
    // M14 T2: the select stays contract-visible but the bar is primary -
    // keep segment state in sync when the dropdown changes directly.
    $("f-verdict").addEventListener("change", function () {
      syncVerdictSegments();
    });
    // M14 T2: segment tooltip on hover and keyboard focus (touch: click
    // still selects, the select's options carry the same labels).
    $("summary").addEventListener("mouseover", function (event) {
      var btn = event.target.closest("button[data-verdict]");
      if (btn) { showVerdictTip(btn); }
    });
    $("summary").addEventListener("mouseout", function (event) {
      if (event.target.closest("button[data-verdict]")) {
        hideVerdictTip();
      }
    });
    $("summary").addEventListener("focusin", function (event) {
      var btn = event.target.closest("button[data-verdict]");
      if (btn) { showVerdictTip(btn); }
    });
    $("summary").addEventListener("focusout", function (event) {
      if (event.target.closest("button[data-verdict]")) {
        hideVerdictTip();
      }
    });
    window.addEventListener("scroll", hideVerdictTip, true);
    syncVerdictSegments();
    $("btn-settings-save").addEventListener("click", saveSettings);
    $("btn-settings-reset").addEventListener("click", resetSettings);
    $("btn-reextract").addEventListener("click", reextract);
    $("btn-ak-add").addEventListener("click", addApiKey);
    $("btn-ak-test").addEventListener("click", testFormCredentials);
  }

  // M11: click feedback - press-in gleam + a ripple at the exact click point
  document.addEventListener("click", function (event) {
    var target = event.target.closest(
      ".btn, .count, button.th-sort, .seen-btn, #items-body tr");
    if (!target) { return; }
    target.classList.remove("clicked");
    void target.offsetWidth;  // restart the CSS animation
    target.classList.add("clicked");
    if (target.tagName !== "TR") {
      var rect = target.getBoundingClientRect();
      var ripple = document.createElement("span");
      ripple.className = "ripple";
      ripple.style.left = (event.clientX - rect.left) + "px";
      ripple.style.top = (event.clientY - rect.top) + "px";
      target.appendChild(ripple);
      ripple.addEventListener("animationend", function () {
        if (ripple.parentNode) { ripple.parentNode.removeChild(ripple); }
      });
    }
  });
  document.addEventListener("animationend", function (event) {
    if (event.target.classList) {
      event.target.classList.remove("clicked");
    }
  });

  wire();
  updateSortHeaders();
  loadSettings().then(loadStatus).then(loadItems);
})();
