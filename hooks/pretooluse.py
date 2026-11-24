"""PreToolUse entry point (hooks.json): runs guard.py as an imported module, so Python reuses its cached bytecode
instead of recompiling 2,500 lines on every tool call. This folder goes on sys.path itself: with PYTHONSAFEPATH set,
Python leaves it off, the import fails, and a failed hook lets every command through."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import guard  # noqa: E402

guard.main()
