# Last modified: 2026-06-12 | Change: Phase 1 init — package entry point, torch-free
#
# WHAT IT DOES: Package init for Matte Fusion v2 (trimap architecture).
#   Intentionally empty of submodule imports — this file must not trigger
#   torch or CorridorKeyModule on import.  Callers import specific modules
#   directly (e.g. from fusion_v2.trimap_builder import build_trimap).
#
# DEPENDS ON: nothing
# AFFECTS: nothing
# ISOLATED: yes — safe to import in torch-free subprocesses
