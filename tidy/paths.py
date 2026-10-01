"""Locations derived from this package's own position on disk."""
import os
from pathlib import Path

# The only place that resolves paths from __file__; everything else imports these.
# abspath, not resolve(): a junction/subst route to the archive must stay as given.
DEDUP_DIR = Path(os.path.abspath(__file__)).parents[1]  # the .dedup repo root
ARCHIVE_ROOT = DEDUP_DIR.parent
