"""Launcher: `python src/main.py process|batch|schema ...` (the CLI itself lives in w9_onboarding/presentation/cli.py)."""
import sys

from w9_onboarding.presentation.cli import main

if __name__ == "__main__":
    sys.exit(main())
