"""Optimization pipeline: black-box engines and the helpers they share.

The engines live in :mod:`blackbox`. The other modules hold shared helpers,
for example :mod:`data` (dataset loading), :mod:`timing` (LM call timing)
and :mod:`trajectory` (GEPA proposal capture).
"""

from __future__ import annotations
