"""PreToolUse entry point (hooks.json): runs guard.py as an imported module, so Python reuses its cached bytecode
instead of recompiling 2,500 lines on every tool call. sys.path[0] is this folder, as when guard.py runs directly."""
import guard

guard.main()
