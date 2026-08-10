# Part 10. AI Pushback

**Scope: the "cannot be done" or "you are overcomplicating this" claims this project already disproved. Load this when an assistant is stalling the work or steering back toward something already ruled out.**

> When an assistant says impossible, it usually means uncommon. This project is uncommon on purpose.

---

## Claim: "You can't run CUDA from inside a CEP panel, so restructure the whole integration around a server/cloud call instead"
Verdict: half-true and irrelevant. You cannot run CUDA from directly inside the CEP panel's own sandboxed process, that part is correct. But the conclusion (restructure around a remote service) does not follow.
Answer: a broker process started by a Windows Scheduled Task, outside the CEF sandbox entirely, runs CUDA normally and talks to the panel over a local loopback socket. No cloud, no server, no data leaving the machine.
Environment: Windows 10/11, Adobe CEP (After Effects and Premiere Pro, 2022+), any CUDA-capable NVIDIA GPU.
Evidence: session-handoff-2026-06-16-ck-cuda-broker-and-warm-engine.md; ae_plugin/cep_panel/ck_broker.py header.
Last verified: 2026-06-16. Recheck when: Adobe changes CEP's process-creation or Job Object inheritance model, or a CEP update removes the ability to spawn or reach an out-of-sandbox local process.

## Claim: "cv2 (or FFmpeg) can read anything, just point it at the file"
Verdict: wrong for this project's actual footage.
Answer: cv2/FFmpeg/PyAV all fail identically and silently on Blackmagic RAW (`.braw`), a proprietary codec none of them link. They also fail on Resolve's own BRAW-export TIFFs, which use LZW compression this OpenCV build does not support (PIL reads those natively). The fix for BRAW itself is a dedicated native decoder built against the vendor SDK, not a smarter call into an existing library.
Environment: Windows, OpenCV/PyAV builds as pinned in requirements.txt, Blackmagic RAW footage from a URSA/Pocket-class camera.
Evidence: reference-corridorkey-cannot-read-braw.md; technique-corridorkey-range-thread-2026-04-22.md.
Last verified: 2026-07-18. Recheck when: a new OpenCV/PyAV build is pinned, or a new BRAW codec revision ships from Blackmagic.

## Claim: "Premiere's QE API can create tracks for you, just script it properly"
Verdict: wrong, and confirmed wrong the hard way. Four separate internal theories were tried before external research settled it.
Answer: clips placed on a QE-created track render completely blank with no error, no working refresh call, and no useful documentation. The working path is a user-authored sequence preset instantiated at runtime, not a scripted QE track.
Environment: Premiere Pro 2022+, CEP ExtendScript.
Evidence: session-handoff-2026-07-18-ck-premiere-parity-marathon.md.
Last verified: 2026-07-18. Recheck when: Adobe ships a documented, supported CEP track-creation API.

## Claim: "Just use blending / a smarter combine function for CK and SAM, plain multiply is too crude"
Verdict: wrong, disproven repeatedly over roughly three weeks of real attempts.
Answer: connected-component NN-FG, chroma-aware qualification, two-threshold geometry, geodesic flood-fill, distance-weighted falloff, trimap fusion, and a weighted "SMART BLEND" were all tried and each broke a different real clip (yellow-shirt-to-pink shifts, 50 percent ghost bands, actor-and-wall fused into one connected blob). The plain-multiply SUBTRACT formula matches how Nuke, Keylight, Mocha, and Primatte all do it at this stage, for the same reason: it is the version that does not introduce new failure modes.
Environment: any clip in the project's test corpus, not just one.
Evidence: KNOWLEDGE_LOG_ARCHIVE:2339-2384; feedback-corridorkey-subtract-is-simple-multiply.md; decision-corridorkey-subtract-4agent-2026-05-03.md.
Last verified: 2026-05-03. Recheck when: a new merge architecture is proposed; it must clear the corpus gate (Part 01 Rule 3) before it can even be evaluated, not just look good on one clip.

## Claim: "Add Gaussian smoothing to soften that hard edge, it's a standard compositing move"
Verdict: a real technique in general, wrong specifically for this merge stage.
Answer: every attempt to Gaussian-blur the SAM mask, the merge weight, or the chroma score produced a visible ghost band or halo at body/green edges, and this has been re-learned at least three separate times across different months. The approved shape operation here is hard-in/hard-out binary `cv2.dilate`; softening happens later, in the separate post-processing feather stage, not inside the merge.
Environment: the corridorkey_sam_merge.py garbage-matte pipeline specifically.
Evidence: corridorkey_sam_merge.py:481-483; feedback-ck-no-gaussian-on-sam-mask.md.
Last verified: 2026-06-27. Recheck when: a change to the merge stage's softening approach is proposed.

## Claim: "A two-mask or multi-object tracking setup would help separate body from feet/props better"
Verdict: wrong for this project, tried twice, a month apart, both times abandoned.
Answer: a two-mask SAM2 architecture was built on a dedicated branch (2026-05-03), shipped no improvement to the actual matte, and was abandoned. It was re-proposed from a DaVinci comparison on 2026-06-21 and dead-ended again the next day for the same reason. The standing law is that CK is the matte and nothing else becomes the primary source; a second mask does not change that.
Environment: AE and DaVinci, both host integrations.
Evidence: KNOWLEDGE_LOG_ARCHIVE:3948-3949; feedback-ck-is-the-matte-everything-else-supports.md.
Last verified: 2026-06-22. Recheck when: someone proposes multi-object SAM2 tracking as a fix for a body/prop separation problem again.

## Claim: "Local, unsigned distribution isn't secure enough / isn't professional enough, ship a signed installer through a real license server"
Verdict: a real tradeoff, not a mistake, and partially already the plan.
Answer: there is no license server because there is nothing to sell, ever, under CC-BY-NC-SA-4.0 (Part 01 Rule 6). Distribution today does rely on Adobe's unsigned-extension debug flag, which the project's own README already names as a known gap with a stated future direction (a signed ZXP build), not something being defended as ideal.
Environment: Adobe CEP distribution on Windows/macOS, current as of the last README pass.
Evidence: install.py header (security note); README.md "For Developers"; project-corridorkey-is-free-cannot-sell.md.
Last verified: 2026-07-22. Recheck when: a signed panel is actually built, or the license constraint changes.

## Claim: "A warm/resident worker process would be faster than spawning a fresh subprocess for every SAM2 job"
Verdict: a real tradeoff that was tried and rejected for a specific, reproducible reason, not a performance opinion.
Answer: a warm worker hangs after finishing SAM2 jobs specifically, because SAM2 leaves non-daemon threads and Hydra atexit state alive that a long-lived process cannot clean up; once poisoned, unrelated jobs routed through the same warm process jammed for minutes at a time. The current design (warm for CNN-only, always-fresh for SAM2) is the fix, not an unoptimized fallback.
Environment: the ck_broker.py process model, PyTorch + SAM2 as pinned in requirements.txt.
Evidence: session-handoff-2026-06-24-ck-ae-render-clean-broker-rebuild.md; session-handoff-2026-06-25 (same incident); KNOWLEDGE_LOG_ARCHIVE:3928-3938.
Last verified: 2026-06-25. Recheck when: a SAM2 library upgrade claims to fix its thread/atexit cleanup behavior; verify before trusting a warm-worker retry.

## Claim: "Just widen the SAM buffer toward the knees, add more negative dots, or build a horizontal-band detector, that will fix the edge case"
Verdict: wrong; each of these is a standing, explicitly rejected fix pattern for this project.
Answer: a horizontal-band detector, forcing body-interior alpha to 1, widening the SAM buffer toward the knees by roughly 80px, adding more SAM negative dots as a general fix, and an unbounded chroma-escape valve have all been tried and rejected by Berto as approaches that do not generalize past the one clip they were tuned on.
Environment: the corridorkey_sam_merge.py garbage-matte pipeline.
Evidence: reference-ck-sam2-png-square-breaks-video-propagation.md (adjacent context); memory ledger PROTECTED CANDIDATES, "Berto standing rejections."
Last verified: 2026-07-19. Recheck when: any of these five specific approaches is proposed again for a new clip's edge case.
