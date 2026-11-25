"""`qa-mem crawl` and `qa-mem discover`: the read-only map passes (qa-init --crawl, qa-explore --discover), run by the
coordinator in one Bash call. Python drives playwright-cli as page-start does: no agent and no turn limit, and no model
retypes a locator. Page text never steers it: a page only answers which locators exist and which links it holds, and
every link is checked against allowedHosts before it is followed. The guard can't see these browser commands, so each
one is charged to the run marker here, with the guard's own locked counter."""

import json
import re
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from . import codemap, core, ops

HOOKS = Path(__file__).resolve().parents[2] / "hooks"
GOTO_SECONDS, EVAL_SECONDS = 60, 30
MAX_FRAMES = 5  # iframes checked per route, by index
CHUNK = 200  # locators per eval: keeps one argument far below Linux's 128 KB limit
FAILS_IN_A_ROW = 3  # pages that fail to load or answer in a row end the run: the app or the browser is down
NOTES_CAP = 1500
FRAME = "iframe >> nth={} >> internal:control=enter-frame >> :root"  # eval inside the frame, cross-origin too
SKIP_LINK_RE = re.compile(r"logout|sign-?out|delete|remove", re.I)  # as the harvester's own filter

# ---------- locators: the four documented forms (references/code-map.md) become DOM checks ----------
_STR = r"""'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*\""""
_CALL_RE = re.compile(
    rf"(?:[A-Za-z_$][\w$]*\.)*(getByTestId|getByRole|getByLabel)\(\s*({_STR})\s*(?:,\s*\{{(.*)\}}\s*)?\)", re.S
)
_OPT_RE = re.compile(rf"\s*(name|exact)\s*:\s*({_STR}|true|false)\s*,?\s*", re.S)


def _js_str(token):
    """The value of a quoted JS string token."""
    return re.sub(
        r"\\(u[0-9a-fA-F]{4}|.)",
        lambda m: (
            chr(int(m.group(1)[1:], 16)) if len(m.group(1)) == 5 else {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1))
        ),
        token[1:-1],
        flags=re.S,
    )


def _options(body):
    """{name|exact: token} of a locator's options object, or None when it holds anything else (a regex, `checked`, …)."""
    opts, pos, body = {}, 0, (body or "").strip()
    while pos < len(body):
        m = _OPT_RE.match(body, pos)
        if not m or m.group(1) in opts:
            return None
        opts[m.group(1)] = m.group(2)
        pos = m.end()
    return opts


def parse_locator(text):
    """The DOM-check spec of a code-map locator, or None when it can't be checked here (reported as unchecked, never
    as missing): ["testid", id] | ["role", role, name or None, exact] | ["label", text, exact] | ["css", selector]."""
    t = str(text or "").strip()
    m = _CALL_RE.fullmatch(t)
    if m:
        kind, arg, opts = m.group(1), _js_str(m.group(2)), _options(m.group(3))
        if (
            opts is None
            or opts.get("exact", "false") not in ("true", "false")
            or ("name" in opts and opts["name"] in ("true", "false"))
        ):
            return None
        exact = opts.get("exact") == "true"
        if kind == "getByTestId":
            return ["testid", arg] if not opts else None
        if kind == "getByLabel":
            return ["label", arg, exact] if "name" not in opts else None
        name = _js_str(opts["name"]) if "name" in opts else None
        return ["role", arg, name, exact] if re.fullmatch(r"[a-z]+", arg) else None  # the role goes into a selector
    if not t or "getBy" in t or "`" in t or re.match(r"[\w$.]+\s*\(", t):  # another method, a chain, a template
        return None
    return ["css", t]


# Text matches as Playwright's do: whitespace collapsed, case-insensitive substring; exact = whole and case-sensitive.
# The accessible name is approximate (aria-label, aria-labelledby, label, button value, alt, text, title, placeholder):
# a miss only leaves a locator unconfirmed. Polls up to ~4 s until every locator is found or the count stops moving.
CHECK_BODY = r"""const spec = __SPEC__;
const norm = s => String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
const match = (got, want, exact) => exact ? norm(got) === norm(want) : norm(got).toLowerCase().includes(norm(want).toLowerCase());
const IMPLICIT = {button: 'button, input[type=button], input[type=submit], input[type=reset], input[type=image], summary',
  link: 'a[href], area[href]', checkbox: 'input[type=checkbox]', radio: 'input[type=radio]', heading: 'h1, h2, h3, h4, h5, h6',
  textbox: 'input:not([type]), input[type=text], input[type=email], input[type=tel], input[type=url], input[type=search], textarea',
  combobox: 'select:not([multiple]), input[list]', listbox: 'select[multiple]', option: 'option', spinbutton: 'input[type=number]',
  slider: 'input[type=range]', img: 'img[alt]', dialog: 'dialog', table: 'table', row: 'tr', navigation: 'nav'};
const text = el => el ? el.textContent : '';
const accName = el => norm(el.getAttribute('aria-label'))
  || norm((el.getAttribute('aria-labelledby') || '').split(/\s+/).map(id => text(d.getElementById(id))).join(' '))
  || norm([...(el.labels || [])].map(text).join(' '))
  || (el.tagName === 'INPUT' && /^(submit|button|reset)$/.test(el.type) ? norm(el.value) : '') || norm(el.getAttribute('alt'))
  || (/^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName) ? '' : norm(el.textContent)) || norm(el.getAttribute('title'))
  || norm(el.getAttribute('placeholder'));
const check = () => {
  const ids = new Set([...d.querySelectorAll('[data-testid], [data-test], [data-cy]')]
    .flatMap(el => ['data-testid', 'data-test', 'data-cy'].map(a => el.getAttribute(a))));
  const has = s => s[0] === 'testid' ? ids.has(s[1])
    : s[0] === 'label' ? [...d.querySelectorAll('label')].some(l => match(l.textContent, s[1], s[2]))
      || [...d.querySelectorAll('[aria-label]')].some(el => match(el.getAttribute('aria-label'), s[1], s[2]))
    : s[0] === 'role' ? [...d.querySelectorAll('[role~="' + s[1] + '"]' + (IMPLICIT[s[1]] ? ', ' + IMPLICIT[s[1]] : ''))]
      .some(el => { const own = el.getAttribute('role'); return (!own || own.split(/\s+/)[0] === s[1]) && (s[2] === null || match(accName(el), s[2], s[3])); })
    : Boolean(d.querySelector(s[1]));
  const found = [], invalid = [];
  spec.forEach((s, i) => { try { if (has(s)) found.push(i); } catch (e) { invalid.push(i); } });
  return {found, invalid};
};
let r = check();
for (let t = 0, same = 0; t < 13 && same < 4 && r.found.length + r.invalid.length < spec.length; t++) {
  await new Promise(ok => setTimeout(ok, 300));
  const next = check();
  same = next.found.length === r.found.length ? same + 1 : 0;
  r = next;
}
"""


def check_js(spec, frame=False):
    """One eval that reports which of `spec` exist: the specs enter only as JSON data. The top document's also returns
    its iframe count and page state (sign-in, host, HTTP status); a frame's runs on FRAME's element."""
    body = CHECK_BODY.replace("__SPEC__", json.dumps(spec))
    if frame:
        return f"async el => {{ const d = el.ownerDocument; {body} return JSON.stringify(r); }}"
    return (
        f"(async () => {{ const d = document; {body} return JSON.stringify({{...r, frames: "
        f"d.querySelectorAll('iframe').length, ...{ops.PAGE_STATE_JS}}}); }})()"
    )


# The harvester of references/playwright-cli-tips.md ("Map the page's states and links"), verbatim: tests/test_docs.py
# pins the two together. shortcut: it keys links by location.pathname, so a hash-routed app (/#/x) collapses to one
# page; key by the hash too when a hash-routed app needs discover.
HARVESTER_JS = """(() => { const out = {links: [], states: []}, seen = new Set();
  for (const a of document.querySelectorAll('a[href]')) { try { const u = new URL(a.href, location.href), t = (a.textContent || a.getAttribute('aria-label') || '').trim();
    if (u.host === location.host && !/logout|sign-?out|delete|remove/i.test(u.pathname + ' ' + t) && !seen.has(u.pathname)) { seen.add(u.pathname); out.links.push([u.pathname, t.slice(0, 40)]); } } catch (e) {} }
  for (const el of document.querySelectorAll('[role=tab], [aria-haspopup], [aria-expanded], [role=menuitem], details > summary'))
    out.states.push([el.getAttribute('role') || el.tagName.toLowerCase(), (el.textContent || el.getAttribute('aria-label') || '').trim().slice(0, 40)]);
  return JSON.stringify({links: out.links.slice(0, 40), states: out.states.slice(0, 40)}); })()"""
HARVEST_JS = f"JSON.stringify({{...JSON.parse({HARVESTER_JS}), ...{ops.PAGE_STATE_JS}}})"


# ---------- the browser, the budget and the run file ----------
class _Stop(Exception):
    """The run's budget or time is used up (the stop_reason): finish with what is saved."""


class _Marker:
    """Charges each browser command to the run marker as the guard charges a Bash call; no marker: unlimited."""

    def __init__(self, project):
        if str(HOOKS) not in sys.path:
            sys.path.insert(0, str(HOOKS))
        import guard  # its locked counter: --budget and --minutes mean what they mean for every other browser command

        self.guard = guard
        self.path, m = guard.read_marker(str(project))
        if self.path and Path(self.path).resolve() != (Path(project) / "qa-out" / ".active").resolve():
            self.path, m = None, None  # read_marker's $CLAUDE_PROJECT_DIR fallback: another project's run
        self.deadline = float((m or {}).get("deadline") or 0)

    def charge(self):
        if self.path and self.guard.spend_budget(self.path, 1, explorer=True):
            raise _Stop("time_limit" if self.deadline and time.time() > self.deadline else "budget_limit")


def _pw(args, timeout):
    """(ok, output) of one `playwright-cli -s=qa` command: its stdout, or the error text."""
    try:
        r = subprocess.run(
            ["playwright-cli", "-s=qa", *args],
            env=ops._pw_env(),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    if r.returncode or (r.stdout or "").lstrip().startswith("### Error"):
        return False, (r.stderr or r.stdout or f"exited {r.returncode}").strip()
    return True, r.stdout or ""


def _parsed(out):
    v = ops._unwrap_raw(out)
    v = ops._unwrap_raw(v) if isinstance(v, str) else v  # --raw prints the JSON string, quoted or not
    return v if isinstance(v, dict) else None


class _Run:
    """The run file, rewritten after every page so a run cut short keeps what it found."""

    def __init__(self, run, slug, target, cfg):
        self.file, self.notes = run / ".data" / f"run-{slug}.json", []
        self.doc = {
            "run_id": f"map-{slug}-{datetime.now(timezone.utc):%Y%m%d-%H%M}",
            "mode": "map",
            "target": target,
            "build": core.UNKNOWN,
            "status": "INCOMPLETE",
            "summary": "",
            "env": {"url": target, "browser": cfg.get("browser")},
            "notes": "",
            "pages": [],
        }

    def save(self, **fields):
        self.doc.update(fields, notes=core.short("; ".join(self.notes), NOTES_CAP))
        core._write(self.file, json.dumps(self.doc, indent=1) + "\n")


def _run_dir(run, what):
    run = Path(run).resolve()
    if run.parent.name != "qa-out" or not (run / ".data").is_dir():
        raise core.BadInput(f"{what}: not a run folder (qa-out/<run>/): {run}")
    return run


def _session(run, slug, url, cfg, hosts, walk, page_blocks):
    """Open the browser at url (page-start), run walk(rec, marker) -> (status, fields), and finish on every path: save
    the browser signals, close it, write the final run file. A start page with an HTTP error blocks only when
    page_blocks (discover starts there; a crawl goes on to its routes)."""
    rec, marker, opened = _Run(run, slug, url, cfg), _Marker(run.parent.parent), False
    status, fields = "INCOMPLETE", {}
    try:
        marker.charge()  # page-start's one command, as the guard counts it
        started = ops._start_page(cfg, hosts, url)
        opened = started["status"] == "ok" or started["block_scope"] == "page"
        rec.doc["build"] = started.get("build") or core.UNKNOWN
        if started["status"] != "ok" and (started["block_scope"] != "page" or page_blocks):
            status, fields = (
                "BLOCKED",
                {"block_scope": started["block_scope"], "stop_reason": "blocked", "summary": started["reason"]},
            )
        else:
            if started["status"] != "ok":
                rec.notes.append(f"start page {url}: {started['reason']}")
            status, fields = walk(rec, marker)
    except _Stop as e:
        fields = {
            "stop_reason": str(e),
            "summary": f"stopped early: the run's {'time' if str(e) == 'time_limit' else 'budget'} "
            f"is used up; {len(rec.doc['pages'])} pages saved",
        }
    finally:
        if opened:
            for name, args in (("requests", ["requests", "--all"]), ("console", ["console", "error", "--all"])):
                ok, out = _pw(args, EVAL_SECONDS)
                core._write(rec.file.with_name(f"{name}-{slug}.log"), out if ok else "")
            ops._close_qa(ops._pw_env())
        rec.save(status=status, **fields)
    return rec


def _land(rec, cfg, hosts, where, state):
    """None when the page is a signed-in, allowed, loaded page; else ('blocked', fields) or ('skip', None)."""
    signed_in, status = ops._signed_in(cfg, state)
    if not signed_in:
        return "blocked", {
            "block_scope": "session",
            "stop_reason": "blocked",
            "summary": "session expired, run /qa-sdet:qa-login",
        }
    host = str(state.get("host") or "")
    if not core.url_allowed(f"https://{host}/", hosts):
        rec.notes.append(f"{where}: landed on {core.short(host, 80)}, outside allowedHosts")
        return "skip", None
    if status >= 400:
        rec.notes.append(f"{where}: HTTP {status}")
        return "skip", None
    return None


def _check(marker, idx, specs, target=None):
    """((found, invalid, first answer), None) of specs[idx] in the top document or a frame, or (None, error text)."""
    found, invalid, state = set(), set(), {}
    for k in range(0, len(idx), CHUNK):
        part = idx[k : k + CHUNK]
        marker.charge()
        ok, out = _pw(
            ["--raw", "eval", check_js([specs[i] for i in part], frame=bool(target))] + ([target] if target else []),
            EVAL_SECONDS,
        )
        r = _parsed(out) if ok else None
        if r is None:
            return None, out
        state = state or r
        pick = lambda key: {part[j] for j in r.get(key) or [] if isinstance(j, int) and 0 <= j < len(part)}  # noqa: B023 - called in this loop pass
        found, invalid = found | pick("found"), invalid | pick("invalid")
    return (found, invalid, state), None


# ---------- qa-mem crawl ----------
def crawl(con, run, routes=()):
    """`qa-mem crawl <run> [<route>...]`: open each static route (default: every one in the code map) and record which of
    its mapped locators exist, in the top document and its iframes, in <run>/.data/run-init-crawl.json. Prints one JSON
    line; exit 0 COMPLETED or INCOMPLETE, 1 BLOCKED."""
    run = _run_dir(run, "crawl")
    cfg, hosts = ops._qa_config("crawl")
    base = str(cfg.get("qaBaseUrl") or "").rstrip("/")
    if not core.url_allowed(base, hosts):
        raise core.BadInput(f"crawl: qaBaseUrl {base!r} must be an http(s) URL in allowedHosts ({sorted(hosts)})")
    if routes:
        bad = next(
            (r for r in routes if not core.norm_route(r).startswith(("/", "#")) or ":" in core.norm_route(r)), None
        )
        if bad is not None:
            raise core.BadInput(
                f"crawl: {bad!r} is not a static route (a path like /zones; a page with :id needs a record)"
            )
        routes = list(dict.fromkeys(core.norm_route(r) for r in routes))
    else:
        routes = [
            x[0]
            for x in con.execute(
                "SELECT DISTINCT page FROM codemap WHERE kind='route' AND area='frontend' AND page!='*' ORDER BY page"
            )
            if ":" not in x[0]
        ]
    plan, skipped = [], []
    for route in routes:
        locs = list(
            dict.fromkeys(
                (x["label"], x["value"])
                for x in codemap._codemap_rows(con, route)
                if x["kind"] == "locator" and x["value"]
            )
        )
        specs = [parse_locator(v) for _, v in locs]
        if any(specs):
            plan.append((route, locs, specs))
        else:
            skipped.append(
                f"{route}: " + (f"{len(locs)} locators, none checkable here" if locs else "no locators in the code map")
            )
    n = dict(routes=len(plan), loaded=0, locators_checked=0, locators_confirmed=0, unchecked=0)

    def walk(rec, marker):
        rec.notes += skipped
        fails = 0
        for k, (route, locs, specs) in enumerate(plan, 1):
            todo = [i for i, s in enumerate(specs) if s]
            marker.charge()
            ok, out = _pw(["goto", base + route], GOTO_SECONDS)
            res, err = _check(marker, todo, specs) if ok else (None, out)
            if res is None:
                fails += 1
                rec.notes.append(f"{route}: failed ({core.short(' '.join(err.split()), 120)})")
                rec.save()
                if fails >= FAILS_IN_A_ROW:
                    return "INCOMPLETE", {
                        "summary": f"{FAILS_IN_A_ROW} routes in a row failed to load or answer; "
                        f"{len(rec.doc['pages'])} routes saved (is the app up?)"
                    }
                continue
            fails = 0
            found, invalid, state = res
            landed = _land(rec, cfg, hosts, route, state)
            if landed and landed[0] == "blocked":
                return "BLOCKED", landed[1]
            if landed:
                rec.save()
                continue
            frames = state.get("frames") if isinstance(state.get("frames"), int) else 0
            for f in range(min(frames, MAX_FRAMES)):
                left = [i for i in todo if i not in found | invalid]
                if not left:
                    break
                res, err = _check(marker, left, specs, FRAME.format(f))
                if res is None:  # no such frame (it went away) is no failure
                    if "does not match any elements" not in err:
                        rec.notes.append(f"{route}: frame {f} check failed ({core.short(' '.join(err.split()), 120)})")
                    break
                found, invalid = found | res[0], invalid | res[1]
            checked, unchecked = len(todo) - len(invalid), len(specs) - len(todo) + len(invalid)
            n.update(
                loaded=n["loaded"] + 1,
                locators_checked=n["locators_checked"] + checked,
                locators_confirmed=n["locators_confirmed"] + len(found),
                unchecked=n["unchecked"] + unchecked,
            )
            rec.doc["pages"].append(
                {"route": route, "locators": [{"name": locs[i][0], "locator": locs[i][1]} for i in sorted(found)]}
            )
            said = f"{route}: {len(found)} of {checked} confirmed" + (f", {unchecked} unchecked" if unchecked else "")
            rec.notes.append(said)
            print(f"crawl {k}/{len(plan)} {said}" + ("" if unchecked else ", 0 unchecked"), file=sys.stderr)
            rec.save()
        return "COMPLETED", {
            "summary": f"{n['loaded']} of {len(plan)} routes loaded; {n['locators_confirmed']} of "
            f"{n['locators_checked']} locators confirmed, {n['unchecked']} unchecked"
        }

    rec = _session(run, "init-crawl", base, cfg, hosts, walk, page_blocks=False)
    d = rec.doc
    print(
        json.dumps(
            {
                "status": d["status"],
                "stop_reason": d.get("stop_reason"),
                "build": d["build"],
                "run_file": str(rec.file),
                **n,
                "skipped": len(skipped),
            }
        )
    )
    return 1 if d["status"] == "BLOCKED" else 0


# ---------- qa-mem discover ----------
def discover(run, url=None, depth=2, max_pages=25):
    """`qa-mem discover <run> [<url>] [--depth D] [--max-pages N]`: breadth-first over same-host links from url (default
    qaBaseUrl), one sample per detail pattern, recording each page's states and links in <run>/.data/run-discover.json.
    Never follows logout, delete or sign-in links; never clicks or types. Prints one JSON line; exit 0, or 1 BLOCKED."""
    run = _run_dir(run, "discover")
    cfg, hosts = ops._qa_config("discover")
    start = url or str(cfg.get("qaBaseUrl") or "").rstrip("/")
    if not core.url_allowed(start, hosts):
        raise core.BadInput(f"discover: {start!r} must be an http(s) URL in allowedHosts ({sorted(hosts)})")
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(start))
    n = dict(pages=0, states=0, links=0)

    def walk(rec, marker):
        first = urlsplit(start).path or "/"
        queue, seen, fails, opened = deque([(first, 0)]), {core.norm_route(first)}, 0, True
        while queue and len(rec.doc["pages"]) < max_pages:
            path, level = queue.popleft()
            ok, out = True, ""
            if not opened:  # page-start already opened the first page
                marker.charge()
                ok, out = _pw(["goto", origin + path], GOTO_SECONDS)
            opened = False
            if ok:
                marker.charge()
                ok, out = _pw(["--raw", "eval", HARVEST_JS], EVAL_SECONDS)
            h = _parsed(out) if ok else None
            if h is None:
                fails += 1
                rec.notes.append(f"{path}: failed ({core.short(' '.join(out.split()), 120)})")
                if fails >= FAILS_IN_A_ROW:
                    return "INCOMPLETE", {"summary": f"{FAILS_IN_A_ROW} pages in a row failed to load or answer"}
                continue
            fails = 0
            landed = _land(rec, cfg, hosts, path, h)
            if landed and landed[0] == "blocked":
                return "BLOCKED", landed[1]
            if landed:
                continue
            links, states = [], {}
            for link in h.get("links") if isinstance(h.get("links"), list) else []:
                if not (isinstance(link, list) and len(link) == 2 and all(isinstance(x, str) for x in link)):
                    continue
                raw, label = link
                if not raw.startswith("/") or raw.startswith("//"):  # a path on this host only
                    continue
                to = urlsplit(raw).path
                if (
                    not core.url_allowed(origin + to, hosts)
                    or ops._auth_segment(origin + to)
                    or SKIP_LINK_RE.search(f"{to} {label}")
                ):
                    continue
                links.append({"to": to, "trigger": label, "kind": "link", "sample_path": to})
                if (
                    level < depth and core.norm_route(to) not in seen
                ):  # one sample per pattern: /x/12 and /x/34 are /x/:id
                    seen.add(core.norm_route(to))
                    queue.append((to, level + 1))
            for st in h.get("states") if isinstance(h.get("states"), list) else []:
                if isinstance(st, list) and len(st) == 2 and all(isinstance(x, str) for x in st) and st[1].strip():
                    states.setdefault(
                        f"{st[0]}: {st[1]}",
                        {"key": f"{st[0]}: {st[1]}", "kind": st[0], "trigger": st[1], "status": "unvisited"},
                    )
            rec.doc["pages"].append({"route": path, "states": list(states.values()), "transitions": links})
            n.update(pages=n["pages"] + 1, states=n["states"] + len(states), links=n["links"] + len(links))
            print(f"discover {n['pages']} {path}: {len(links)} links, {len(states)} states", file=sys.stderr)
            rec.save()
        return "COMPLETED", {"summary": f"{n['pages']} pages mapped, {n['states']} states, {n['links']} links"}

    rec = _session(run, "discover", start, cfg, hosts, walk, page_blocks=True)
    d = rec.doc
    print(
        json.dumps(
            {
                "status": d["status"],
                "stop_reason": d.get("stop_reason"),
                "build": d["build"],
                "run_file": str(rec.file),
                **n,
            }
        )
    )
    return 1 if d["status"] == "BLOCKED" else 0
