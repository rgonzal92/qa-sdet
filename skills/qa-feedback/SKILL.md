---
name: qa-feedback
description: Log a qa-sdet plugin bug, nuisance, or improvement idea, inspect the local plugin log, or export a work-safe anonymized Markdown digest to share back for plugin improvements. Use when the user wants to report a qa-sdet bug or nuisance, log plugin feedback, check plugin denials/errors, or export plugin logs to share.
argument-hint: [note [--kind bug|nuisance|idea] [--severity low|med|high] <text> | export [--all] [--since <ISO|Nd>] [--preview] [--stdout] | show [--limit N] [--type TYPE] | clear]
---

Manage the project's qa-sdet plugin self-improvement log for: $ARGUMENTS

Project: `${CLAUDE_PROJECT_DIR}`. All reads and writes go through `qa-mem plugin-log` (never edit `<project>/qa-out/plugin-log.jsonl` directly). Single-quote every user-supplied argument, writing each `'` inside it as `'\''`.

1. **Choose action** from `$ARGUMENTS` (or the user's plain-English request):
   - **Export** (`export`, "share logs", "export plugin logs", "make a digest"): run `qa-mem plugin-log export --project '${CLAUDE_PROJECT_DIR}'` plus any flags the user asked for (`--all` for full history, `--since <YYYY-MM-DD|7d|24h>` for a time window, `--preview` to print without writing a file or advancing the cursor, `--stdout` to print and advance the cursor). By default this writes a dated anonymized Markdown digest under `${CLAUDE_PROJECT_DIR}/qa-out/` and advances the export cursor so the next export includes only new events.
   - **Show** (`show`, "show recent plugin logs", "check denials/errors"): run `qa-mem plugin-log show --project '${CLAUDE_PROJECT_DIR}'` with optional `--limit N` and `--type denial|hook_error|nudge|tool_fail|stop_bounce|cli_error|run_summary|user_note`.
   - **Clear** (`clear`, "reset plugin log"): confirm with the user if they did not explicitly ask to clear, then run `qa-mem plugin-log clear --project '${CLAUDE_PROJECT_DIR}'`.
   - **Note** (`note ...`, or any plain-English bug/nuisance/idea description when no other action matches): pick `--kind` (`bug`, `nuisance`, or `idea`; default `nuisance`) and optional `--severity` (`low`, `med`, or `high`) from what the user said, then run:
     `qa-mem plugin-log note --project '${CLAUDE_PROJECT_DIR}' --kind <kind> [--severity <sev>] '<text>'`
     (`qa-mem` automatically attaches the active or latest run folder's metadata and scrubs secrets.)
   - **No arguments:** run `qa-mem plugin-log show --project '${CLAUDE_PROJECT_DIR}' --limit 15` and tell the user they can log a note (`/qa-sdet:qa-feedback <note>`) or export a shareable digest (`/qa-sdet:qa-feedback export`).
2. **Reply:** show `qa-mem`'s output verbatim (the logged confirmation, recent lines, or digest summary and output path). If `export` wrote a digest file, end with one line reminding the user the digest has hosts, Jira keys, paths, and typed values anonymized and is ready to share into the `qa-sdet` repo session.
