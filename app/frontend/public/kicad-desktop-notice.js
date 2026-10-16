/*
 * A notice laid over the KiCad desktop tab: opening a schematic or a board is
 * slow, and looks like a hang while it happens.
 *
 * KiCad loads every enabled symbol library before a schematic appears, and
 * every footprint library before a board does. With the stock libraries and
 * the vendor set registered, that is a minute or more of a window that does
 * not repaint, and the natural response, clicking again or closing it, is the
 * one that loses work. Nothing in KiCad says it is loading.
 *
 * nginx adds this script to the desktop's page (frontend/nginx.conf,
 * location /kicad/), so it is there however the file is opened: from the
 * app's "Open KiCad" button, from File > Open, or from the project manager.
 * It sits over the remote desktop's page, not inside KiCad, so it cannot know
 * when a load starts or ends; it says so once, until dismissed for this tab.
 *
 * Plain script rather than part of the app's bundle: it runs in the desktop's
 * page, which loads none of the app.
 */
(function () {
  "use strict";
  var KEY = "blpl.kicadLoadNotice.dismissed";
  try {
    if (window.sessionStorage.getItem(KEY) === "1") return;
  } catch (e) {
    /* storage blocked: show it, and dismissing it lasts until reload */
  }

  function show() {
    if (document.getElementById("blpl-kicad-load-notice")) return;
    var box = document.createElement("div");
    box.id = "blpl-kicad-load-notice";
    box.setAttribute("role", "status");
    // Bottom right, clear of KiCad's menu bar and toolbars at the top, and
    // small: it covers part of the status bar at most.
    box.style.cssText = [
      "position:fixed", "right:16px", "bottom:16px", "z-index:2147483647",
      "max-width:360px", "padding:10px 36px 10px 12px", "border-radius:6px",
      "background:rgba(30,30,30,0.92)", "color:#f2f2f2",
      "font:13px/1.4 system-ui,-apple-system,Segoe UI,sans-serif",
      "box-shadow:0 2px 10px rgba(0,0,0,0.4)",
    ].join(";");

    var title = document.createElement("strong");
    title.textContent = "Opening a schematic or board takes a while";
    title.style.display = "block";
    title.style.marginBottom = "4px";
    var body = document.createElement("span");
    body.textContent =
      "KiCad loads its symbol and footprint libraries first, and the window " +
      "can look frozen for a minute or more. It is not hung: wait for it " +
      "rather than clicking again or closing it.";

    var close = document.createElement("button");
    close.type = "button";
    close.setAttribute("aria-label", "Dismiss");
    close.textContent = "×";
    close.style.cssText = [
      "position:absolute", "top:4px", "right:6px", "border:0", "background:none",
      "color:inherit", "font-size:18px", "line-height:1", "cursor:pointer",
      "padding:4px",
    ].join(";");
    close.addEventListener("click", function () {
      box.remove();
      try {
        window.sessionStorage.setItem(KEY, "1");
      } catch (e) {
        /* nothing to remember it in; it is gone until reload either way */
      }
    });

    box.appendChild(title);
    box.appendChild(body);
    box.appendChild(close);
    document.body.appendChild(box);
  }

  if (document.body) show();
  else document.addEventListener("DOMContentLoaded", show);
})();
