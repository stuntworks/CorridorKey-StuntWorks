# Last modified: 2026-06-12 | Change: Phase 2 — common solver interface
#
# WHAT IT DOES:
#   Defines the single entry-point contract for all matting solvers in the
#   Matte Fusion v2 pipeline.  Callers always go through solve_matte() and
#   never depend on a specific solver module.  Adding or replacing a solver
#   requires only (1) writing a new solver module, (2) calling register_solver
#   in that module — this file is never touched.
#
# DEPENDS ON: numpy (type annotations only; no numpy in runtime critical path)
# AFFECTS: any code that calls solve_matte() (Phase 2+)
# ISOLATED: yes — swapping solvers requires zero changes here

import numpy as np

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: dict = {}


def register_solver(name: str, fn) -> None:
    """Register a solver function under a name.

    Solver modules call this at import time so they self-register.
    fn signature: (frame_rgb, trimap, nn_alpha, **kwargs) -> float32 alpha [H, W]
    """
    _REGISTRY[name] = fn


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def solve_matte(
    frame_rgb: np.ndarray,
    trimap: np.ndarray,
    nn_alpha: np.ndarray,
    solver: str = "guided",
    **kwargs,
) -> np.ndarray:
    """Resolve the unknown band in a trimap to a soft alpha.

    Contract (all solvers must honour this):
      - Definite-FG pixels (trimap == 255) pass through as 1.0
      - Definite-BG pixels (trimap == 0)   pass through as 0.0
      - Unknown pixels  (trimap == 128) are solved from frame content
      - Return dtype float32, shape (H, W), values clamped [0, 1]

    Parameters
    ----------
    frame_rgb : uint8 or float32 (H, W, 3) — original frame, RGB channel order
    trimap    : uint8 (H, W) — 0=BG, 128=unknown, 255=FG (from trimap_builder)
    nn_alpha  : float32 (H, W) — soft NN alpha, values in [0, 1]
    solver    : registered solver name (default 'guided')
    **kwargs  : solver-specific parameters (see individual solver modules)

    Returns
    -------
    float32 (H, W) alpha, clamped to [0, 1]
    """
    if solver not in _REGISTRY:
        available = list(_REGISTRY.keys())
        raise ValueError(
            f"Unknown solver '{solver}'. "
            f"Import the solver module to register it. Available: {available}"
        )
    return _REGISTRY[solver](frame_rgb, trimap, nn_alpha, **kwargs)


def available_solvers() -> list:
    """Return list of currently registered solver names."""
    return list(_REGISTRY.keys())
