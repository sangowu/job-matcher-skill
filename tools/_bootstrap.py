"""Put the skill's `scripts/` directory on the import path.

The tools here are not on a round's critical path -- benchmarks, release gates,
first-contact smoke -- which is why they live outside `scripts/`. They do import
the production modules, and that is the point: a benchmark that measured a copy
of the parser would measure the copy. Importing this module first is what makes
`from ats_provider import ...` below mean the same module the pipeline runs.

Kept as a module rather than repeated in nine files so there is one place to
read, and so a tool that forgets it fails loudly on the import rather than
quietly finding something else.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
