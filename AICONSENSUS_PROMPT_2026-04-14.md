# AiConsensus Prompt — CorridorKey StuntWorks: What To Add Next

Paste everything below this line into AiConsensus. Recommend "Solve" or "Code"
mode. 7 AIs, hard vote.

---

## Context

I built a plugin called **CorridorKey StuntWorks** — an AI green-screen
keying plugin that wraps Niko Pueringer's open-source CorridorKey neural
network. The plugin runs inside DaVinci Resolve, Adobe After Effects, and
Adobe Premiere Pro. It does *not* belong in the App Store — I can't sell
it because I don't own the underlying model. This is for my personal use.

**About me:** I'm a stunt rigger with 30 years of experience, not a
full-time editor. I build real wire gags, cable rigs, and impact
protection for film, TV, and commercials. I edit my own stunt reels and
sometimes deliver plate fixes for clients. Green-screen on set is almost
always shot with tight turnaround and clients watching the monitor.

**How I actually use the plugin:**
1. Film a stunt element on green.
2. Bring the clip into Resolve (grading / color-first workflow) or
   Premiere (editorial-first), rarely AE unless I need comp work.
3. Select the clip, click "KEY CURRENT FRAME" for a quick check, or
   "PROCESS IN/OUT RANGE" for the full shot.
4. The keyed PNG sequence drops on V2 above the original. I scrub to
   check edges, spill, motion blur around the action.
5. If the key is good, I continue with the composite / grade / delivery.
   If it's bad, I re-shoot on set or fall back to rotoscoping.

**Current state (2026-04-14):**
- Resolve, AE, and Premiere flows all working.
- Batch keying works, sequence import works, frame-for-frame alignment
  works (5 Premiere API quirks all documented + fixed in ALIGNMENT.md).
- Single-frame preview shows keyed frame + matte side by side.
- Real log file at %TEMP%\corridorkey.log for troubleshooting.
- Four sliders: despill, edge refiner, despeckle toggle, despeckle-size.
- SAM2 click-to-mask is supported by the underlying engine but not
  surfaced in the plugin UI yet.
- ONNX export path: model has a 2048 static export and a dynamic export
  available, neither wired into the plugin.
- Install is manual (clone repo, run setup.bat, run install.py).
- Public repo: github.com/stuntworks/CorridorKey-StuntWorks.

**Constraints:**
- I cannot retrain or modify the CorridorKey model.
- I can add anything around the model (UI, workflow, batch controls,
  preview features, export options, integrations with other tools I own).
- Not trying to sell this. Goal is to save myself time on real stunt shots
  and build credibility for other projects.
- Must work on Windows 11 with NVIDIA GPU. Mac support is nice-to-have.

---

## My Question To The Council

**What's the highest-leverage next addition that would save me the most
time on real stunt-shot keying?**

Rank 3 to 5 concrete ideas by expected time savings per shot. For each:

1. One-sentence description.
2. Why a stunt-shot editor specifically would benefit (vs. a generic VFX
   editor).
3. Rough effort estimate (hours / days / weeks of plugin-layer work — no
   retraining).
4. What it replaces in my current workflow (the specific pain it removes).

Focus on the **plugin layer only** — UX, preview, batch controls,
integrations, export quality-of-life. Do NOT propose changes to the
CorridorKey model itself.

Prioritize ideas that:
- Work without me clicking through many menus.
- Integrate with tools I already own (Premiere / Resolve / AE).
- Help me evaluate key quality *faster* so I can decide "good enough" or
  "reshoot" in seconds, not minutes.
- Are realistic on a weekend-build budget.

If two or more of you converge on the same idea, call that out — I'll
weight consensus heavily.
