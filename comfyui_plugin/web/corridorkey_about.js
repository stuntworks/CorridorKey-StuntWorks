// CorridorKey StuntWorks — About panel for ComfyUI.
// Adds an "About CorridorKey" item to the ComfyUI menu that opens a modal with
// credits + clickable links. ComfyUI's equivalent of the DaVinci/AE live-view
// About dialog (which doesn't translate 1:1 — this is the ComfyUI-native spot).
//
// CorridorKey engine (c) Niko Pueringer / Corridor Digital — CC BY-NC-SA 4.0.
// Plugin by Roberto & Elvis Lopez / StuntWorks Cinema.

import { app } from "../../scripts/app.js";

const LINKS = {
  engineGithub: "https://github.com/nikopueringer/CorridorKey",
  corridor: "https://corridordigital.com",
  corridorYt: "https://www.youtube.com/@CorridorDigital", // Niko / Corridor's channel
  pluginGithub: "https://github.com/stuntworks/CorridorKey-Plugin",
  youtube: "https://www.youtube.com/@StuntWorksCinema",
  kofi: "https://ko-fi.com/stuntworks",
};

function openAbout() {
  const existing = document.getElementById("ck-sw-about-overlay");
  if (existing) {
    existing.remove();
    return;
  }

  const overlay = document.createElement("div");
  overlay.id = "ck-sw-about-overlay";
  Object.assign(overlay.style, {
    position: "fixed", inset: "0", background: "rgba(0,0,0,0.6)",
    zIndex: "10000", display: "flex", alignItems: "center", justifyContent: "center",
  });
  overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.remove(); });

  const a = (href, text, color) =>
    `<a href="${href}" target="_blank" rel="noopener" style="color:${color};text-decoration:none;font-weight:bold;">${text}</a>`;

  const box = document.createElement("div");
  Object.assign(box.style, {
    width: "460px", maxHeight: "85vh", overflowY: "auto",
    background: "#1b1b1b", color: "#ccc", border: "1px solid #333",
    borderRadius: "8px", padding: "22px 26px", fontFamily: "sans-serif", fontSize: "13px", lineHeight: "1.5",
  });
  box.innerHTML = `
    <div style="text-align:center;">
      <div style="font-size:22px;font-weight:bold;color:#4CAF50;">CorridorKey</div>
      <div style="color:#aaa;font-size:12px;">StuntWorks Cinema build — AI green screen keyer for ComfyUI</div>
    </div>
    <hr style="border:none;border-top:1px solid #333;margin:14px 0;">
    <div style="text-align:center;">
      <div style="color:#FF9800;font-weight:bold;">CorridorKey Engine</div>
      <div>Created by Niko Pueringer / Corridor Digital</div>
      <div>${a(LINKS.engineGithub, "GitHub", "#2196F3")} &nbsp;·&nbsp; ${a(LINKS.corridor, "corridordigital.com", "#2196F3")} &nbsp;·&nbsp; ${a(LINKS.corridorYt, "▶ Corridor on YouTube", "#cc3300")}</div>
      <div style="color:#777;font-size:11px;margin-top:4px;">License: CC BY-NC-SA 4.0 (NonCommercial) — free, cannot be sold</div>
    </div>
    <hr style="border:none;border-top:1px solid #333;margin:14px 0;">
    <div style="text-align:center;">
      <div style="color:#FF9800;font-weight:bold;">ComfyUI Plugin</div>
      <div>by Roberto &amp; Elvis Lopez</div>
      <div style="color:#E91E63;font-weight:bold;">StuntWorks Cinema</div>
      <div>${a(LINKS.pluginGithub, "GitHub", "#2196F3")}</div>
    </div>
    <hr style="border:none;border-top:1px solid #333;margin:14px 0;">
    <div style="text-align:center;color:#ccc;font-size:12px;">
      <div style="color:#FF9800;font-weight:bold;margin-bottom:4px;">What makes this build unique</div>
      The green-aware garbage matte: CorridorKey's neural chroma key combined with a
      SAM2 subject mask AND the green-screen geography. Cuts the set cleaner than a raw
      subject mask and keeps the key locked to the subject on every frame.
    </div>
    <hr style="border:none;border-top:1px solid #333;margin:14px 0;">
    <div style="text-align:center;color:#777;font-size:11px;">
      Subject mask powered by SAM2 (Segment Anything Model 2) &copy; Meta AI, Apache 2.0.
    </div>
    <hr style="border:none;border-top:1px solid #333;margin:14px 0;">
    <div style="text-align:center;font-style:italic;color:#ccc;font-size:12px;">
      StuntWorks is a professional stunt rigging company. In our spare time we build the
      tools we wish existed — free plugins and workflow helpers. If you find this useful,
      a coffee helps us keep building.
    </div>
    <div style="text-align:center;margin-top:10px;">
      ${a(LINKS.youtube, "▶ YouTube — Tutorials", "#cc3300")} &nbsp;&nbsp; ${a(LINKS.kofi, "☕ Ko-fi", "#FF5E5B")}
    </div>
    <div style="text-align:center;margin-top:18px;">
      <button id="ck-sw-about-close" style="background:#607D8B;color:#fff;border:none;border-radius:4px;padding:8px 22px;cursor:pointer;">Close</button>
    </div>
  `;
  overlay.appendChild(box);
  document.body.appendChild(overlay);
  box.querySelector("#ck-sw-about-close").addEventListener("click", () => overlay.remove());
}

app.registerExtension({
  name: "CorridorKey.StuntWorks.About",
  // New ComfyUI menu (topbar command + About panel entry)
  commands: [
    { id: "corridorkey.about", label: "About CorridorKey (StuntWorks)", function: openAbout },
  ],
  menuCommands: [
    { path: ["Help"], commands: ["corridorkey.about"] },
  ],
  aboutPageBadges: [
    { label: "CorridorKey — StuntWorks Cinema", url: LINKS.youtube, icon: "pi pi-youtube" },
  ],
  setup() {
    // Fallback for older ComfyUI menus: add a button to the settings/menu area.
    try {
      const menu = document.querySelector(".comfy-menu");
      if (menu && !document.getElementById("ck-sw-about-btn")) {
        const btn = document.createElement("button");
        btn.id = "ck-sw-about-btn";
        btn.textContent = "About CorridorKey";
        btn.style.marginTop = "4px";
        btn.addEventListener("click", openAbout);
        menu.appendChild(btn);
      }
    } catch (e) { /* new menu only — commands/menuCommands cover it */ }
  },
});
