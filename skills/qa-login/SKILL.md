---
name: qa-login
description: Save a logged-in QA browser session for qa-sdet runs (you do SSO + MFA in a browser window).
disable-model-invocation: true
---

0. If `playwright-cli --version` fails (not installed or broken), tell the user: "qa-login needs a working playwright-cli (`npm i -g @playwright/cli`). Without it, qa-explore and qa-ticket fall back to a Playwright MCP and ask you to sign in in that MCP's browser window when needed (it must run headed); the login lasts until Claude Code restarts." and stop.
1. Read `qaBaseUrl`, `ssoHost` and `browser` from `~/.config/qa-sdet/config.json` (if the file is missing, or `browser` is missing or not one of chrome, msedge, firefox, webkit, follow step C1 in `${CLAUDE_SKILL_DIR}/../../references/common.md`).
2. Run `env -u PLAYWRIGHT_MCP_STORAGE_STATE playwright-cli -s=qa-login open --browser=<browser> --headed <qaBaseUrl>`. (If `--headed` is rejected, check `playwright-cli open --help` once. If `open` itself fails, show its first error line and the fix `python3 ${CLAUDE_SKILL_DIR}/../../bin/qa-mem doctor` prints (often `playwright-cli install-browser <browser>`), and stop.)
3. Tell the user: "Complete SSO + MFA in the browser window, wait until the app's home page loads, then reply `done`." Then wait for `done`: the user enters credentials and MFA codes in the browser themselves; they never pass through you.
4. On `done`: `touch ~/.config/qa-sdet/qa-state.json && chmod 600 ~/.config/qa-sdet/qa-state.json` first (the browser keeps an existing file's mode, so the cookies are never readable by others), then `playwright-cli -s=qa-login state-save ~/.config/qa-sdet/qa-state.json`, then `playwright-cli -s=qa-login close`.
5. Verify: `env -u PLAYWRIGHT_MCP_STORAGE_STATE playwright-cli -s=qa open --browser=<browser>`, `state-load` the file, `goto <qaBaseUrl>`; if the URL host is `ssoHost`, say the save failed; else say "Session saved." `playwright-cli -s=qa close`.
