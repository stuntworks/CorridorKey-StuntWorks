# Part 01. Identity and Rules

**Scope: what the project is, and the rules that define it. Load this when a decision might change the nature of the project. This is the shortest and most load-bearing part.**

---

## Identity

CorridorKey (StuntWorks build) is a free, open, offline AI green-screen keying plugin that runs one neural keyer (CK, Niko Pueringer / Corridor Digital's engine) plus an optional SAM2 support-mask stage inside three host editors (DaVinci Resolve, Adobe After Effects, Adobe Premiere Pro), built by Roberto and Elvis Lopez (StuntWorks Cinema) to key their own stunt and action footage when off-the-shelf keyers could not handle imperfect lighting, motion blur, and fast action.

Tag: USER DECISION. Last verified: 2026-07-22. Recheck when: the project's stated purpose or ownership changes.

---

## Rules (the constitution)

**Rule 1. CK IS the matte.** SAM2, the merge stage, zone tools, and every slider exist to support the CK neural keyer's output. None of them are allowed to become the primary source of the matte, and no change ships unless it is judged by whether it improves the CK matte specifically.
Why: two attempts to redesign around a SAM-primary or two-mask architecture (branch feat/multi-object-sam2, built 2026-05-03; re-litigated 2026-06-21/22 from a DaVinci git-forensics comparison) both dead-ended. Berto's own verdict both times: it "did not produce the desired effect" and "improved nothing in the actual matte." The rule exists because this argument keeps recurring and has already been settled twice.
Tag: USER DECISION. Last verified: 2026-06-22. Recheck when: someone proposes SAM2 (or any other stage) as the primary matte source again.

**Rule 2. One change, one test. Never ship a stack of untested changes.**
Why: every multi-change overnight session on record broke a previously-working key. The 2026-06-12 session ran 3-4 full architecture rewrites in 24 hours with synthetic receipts passing while real quality regressed; the 2026-06-30/07-01 session stacked six untested changes (interior-fill, per-frame correction dots, scrub post-pass, an off-by-one fix, render-review, full-res SAM) and had to fully revert to HEAD; the 2026-06-22 "whack-a-mole" night patched symptoms (SAM noise, green fringe, feather halo) with ~10 stacked edits and shipped nothing usable. Each time, the fix was a full revert followed by isolating one variable at a time.
Tag: USER DECISION. Last verified: 2026-07-01. Recheck when: a session is tempted to bundle more than one merge/matte change before testing.

**Rule 3. No single-clip (or single-frame) tuning ships without the corpus gate.**
Why: the corpus-gate law was born directly out of the 2026-06-12 revert (see Rule 2): a narrow-feather merge variant won a Sobel-gradient metric on one frame and was shipped as default, then judged worse than the prior day in motion by Berto's eye; that gauntlet was later found to have used auto-generated NN-centroid dots instead of Berto's real dot pattern, invalidating the run entirely. No merge or matte change ships without 6-8 diverse clips and a contact sheet for Berto's own eye, not a metric.
Tag: USER DECISION. Last verified: 2026-06-12. Recheck when: any automated metric (Sobel, MAD, or similar) is proposed as the sole gate for a merge/matte change.

**Rule 4. No Gaussian blur anywhere in the SAM merge pipeline (mask, weight, or soft transition).**
Why: it produces visible ghost bands and halos at body/green edges every single time it has been tried, and has been relearned the hard way three or more times: 2026-05-01 SMART BLEND stacked a Gaussian on top of SAM/chroma weight and produced a 50 percent ghost band; the standing rule was broken and re-broken across 2026-05-14/16/17; a 2026-06-27 attempt to smooth the shadow-kill boundary was likewise reverted. Hard-in/hard-out binary cv2.dilate is the only approved shape operation in this stage.
Tag: USER DECISION. Last verified: 2026-06-27. Recheck when: anyone proposes smoothing, feathering, or blurring inside the merge stage specifically (post-merge feather in the separate post-proc stage is a different, allowed thing).

**Rule 5. Preview must equal render. No two-path model (a sampled/low-res preview plus a separate full-res render path).**
Why: a two-path preview/render model was identified as "the root of every bug this session" on 2026-06-30, after weeks (2026-06-22 to 06-29) of patching symptoms (SAM noise, green fringe, feather halo) one at a time under the false belief that preview and render were "the same engine, differently tuned," when in fact preview used SAM2's image predictor and render used the video predictor: two different engines that were never going to match. Berto's directive: kill the sampled preview entirely. One render, full-range, full-res, lands on the timeline; review scrubs the actual rendered frames, not a proxy.
Tag: USER DECISION. Last verified: 2026-06-30. Recheck when: any new preview optimization is proposed that would reintroduce a second, cheaper code path distinct from the render path.

**Rule 6. CorridorKey is free and cannot be sold, in any packaging.**
Why: the plugin is a derivative of Niko Pueringer / Corridor Digital's CorridorKey engine, licensed CC-BY-NC-SA-4.0. The NonCommercial clause legally forecloses a commercial sale path for the whole project, not just the engine file. A 2026-05-21 release plan (LLC formation, EV code-signing certificate, Stripe pricing) was abandoned specifically because of this. Revenue path, if any, is reputation via the Corridor Crew / StuntWorks Cinema audience (a Ko-fi tip link), never a purchase gate.
Tag: USER DECISION. Last verified: 2026-05-21. Recheck when: any monetization idea is proposed for this codebase, or the upstream engine's license changes.

**Rule 7. Never cross-contaminate with NoWire (a separate, related StuntWorks project).**
Why: on 2026-06-22, five or six NoWire-derived theories and evidence were applied to CK, including a code comment in corridorkey_sam_merge.py citing a NoWire test clip as CK proof; the cited clip was never actually a CK clip, and a NoWire model choice (SAM3.1) was wrongly imported as a requirement for CK's client-machine sizing. Each project's claims must stand on that project's own evidence.
Tag: USER DECISION. Last verified: 2026-06-22. Recheck when: any fix, model choice, or evidence is proposed to move between CK and NoWire.

---

## Governing sentence

> Drop green-screen footage in your editor, click one button, and the CK neural matte, never anything downstream of it, is what decides whether the shot is clean.

---

## Protected list

See `00_INDEX.md` for the full table with file:line references. Summary of what it protects and why: the CK-is-the-matte law and the SUBTRACT/merge formula (Rule 1 and Rule 4, both repeatedly re-litigated and repeatedly reconfirmed the same way); the CUDA-sandbox bridge (ck_broker.py) and its dead predecessor ck_launch.py (the hard part, Part 04); the AE junction and the dead root-level ae_processor.py dummy (Part 03, Part 07); the Premiere frame-alignment functions in ALIGNMENT.md (four interacting offsets, broken four times); the SAM2 video-predictor frame-shape contract and the always-fresh-subprocess rule for SAM2 jobs; and the CC-BY-NC-SA-4.0 license inheritance (Rule 6).

---

## What the rules buy

A two-person crew (Roberto and Elvis Lopez) maintains a CUDA-heavy, three-host, multi-process AI pipeline without a dedicated engineering team, because each rule above closed an argument that had already cost real session-time (weeks in several cases) to settle once. Holding them without exception means the same architecture debate does not get re-opened, re-argued, and re-lost every time a new session (human or AI) picks up the project fresh. The cost of writing them down once is far smaller than the cost already paid discovering them.
