"""A deliberately simple transit light-curve model shared by the simulated scene and the fitter.

The model is a symmetric **trapezoid**: flux falls linearly from 1 to ``1 - depth`` over
``ingress`` seconds, stays flat, then rises again over the same time.  ``duration`` is the
first-to-fourth contact time (T14), so the flat bottom lasts ``duration - 2 * ingress``.

Honest scope: real transits are limb-darkened (a rounded bottom) and are best fitted with a
Mandel-Agol model.  A trapezoid recovers depth, mid-time and duration well at the signal-to-noise
a 50 mm telescope delivers, but the fitted depth is the *mean in-transit* depth, a few
percent different from the physical Rp/Rs squared.  The same function is used to inject the
simulated transit and to fit it, so recovery tests in simulation are optimistic about
the shape and only validate the photometry and detrending.

All times are seconds in one consistent frame (the plugin uses seconds since the first frame).
"""

from __future__ import annotations

import numpy as np

MIN_INGRESS_S = 1.0  # avoid a zero-width ramp (division by zero, non-differentiable fit)


def transit_shape(t, t_mid: float, duration: float, ingress: float) -> np.ndarray:
    """Fraction (0..1) of the full transit depth at time(s) ``t``."""
    t = np.asarray(t, dtype=float)
    duration = max(float(duration), 2.0 * MIN_INGRESS_S)
    ingress = min(max(float(ingress), MIN_INGRESS_S), duration / 2.0)
    half = duration / 2.0
    # 0 outside the transit, ramps up over `ingress`, 1 on the floor, symmetric about t_mid.
    d = half - np.abs(t - t_mid)
    return np.clip(d / ingress, 0.0, 1.0)


def transit_flux(t, t_mid: float, depth: float, duration: float, ingress: float) -> np.ndarray:
    """Relative flux (1 out of transit, ``1 - depth`` at the bottom)."""
    return 1.0 - float(depth) * transit_shape(t, t_mid, duration, ingress)
