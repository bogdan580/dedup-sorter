"""Entry point: python dedup.py [--review | --series | --events | --inbox | --compress | --apply | --restore ...].

All code lives in tidy/cli/; run from the archive root or anywhere else.
"""
import sys

from tidy.cli.main import main

if __name__ == "__main__":
    sys.exit(main())
