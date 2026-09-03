"""THE ISOLATION BOUNDARY.

Only modules in this package may open VarAC's database, read VarAC's .ini
files, tail VarAC's GPS log, or drive VarAC's GUI.  Everything crossing the
boundary is one of the plain dataclasses in `contracts.py`.
"""
from .contracts import Observation, OwnFix, SourceHealth  # noqa: F401
