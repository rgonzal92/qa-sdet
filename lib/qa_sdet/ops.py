"""Run folders, markers, `browser-open`, prune and annotate."""
import json
import os
import re
import stat
import sys
import time
from datetime import datetime
from pathlib import Path
from . import core


# ---------- v0.4 wrappers: ticket keys, run folders, run marker, browser, prune ----------
KEY_RE = core._LazyRe(r"[A-Z][A-Z0-9]+-\d+", re.ASCII)  # ASCII: Jira keys never hold other digits


def config():
    try:
        cfg = json.loads(core.CONFIG.read_text())
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def expand_keys(args, prefix=None):
    """['1234', 'ops-7,1250'] -> ['STUDIO-1234', 'OPS-7', 'STUDIO-1250'] (prefix from config jiraKeyPrefix)."""
    prefix = str(prefix or "").strip().rstrip("-")
    prefix = prefix.upper() if prefix.isascii() else prefix
    out = []
    for tok in (t for a in args for t in re.split(r"[,\s]+", a) if t):
        if tok.isascii() and tok.isdigit():
            if not prefix:
                raise core.BadInput(f"set jiraKeyPrefix in {core.CONFIG} (e.g. \"STUDIO\") to use bare numbers like {tok}, "
                               "or give full keys")
            if not re.fullmatch(r"[A-Z][A-Z0-9]+", prefix, re.ASCII):
                raise core.BadInput(f"jiraKeyPrefix in {core.CONFIG} must be a Jira project key like \"STUDIO\" (got "
                               f"{core.short(prefix, 40)!r}); fix it, or give full keys")
            key = f"{prefix}-{tok}"
        else:
            key = tok.upper() if tok.isascii() else tok  # "ß-12".upper() is "SS-12": non-ASCII must fail, not morph
        if not KEY_RE.fullmatch(key):
            raise core.BadInput(f"not a ticket number or key: {tok!r} (use 1234 or STUDIO-1234)")
        if key not in out:
            out.append(key)
    if not out:
        raise core.BadInput("give ticket numbers or keys, e.g. 1234 1250 or STUDIO-1234")
    return out


def route_slug(route):
    """C3 slug: '/form.html' -> form, '/incidents/4521' -> incidents-id, '/#/zones' -> zones, '/' -> home."""
    r = core.norm_route(route)
    path, _, frag = r.partition("#")
    segs = [x for x in (path + "/" + frag.lstrip("!")).split("/") if x]
    if segs and "." in segs[-1]:
        segs[-1] = segs[-1].rsplit(".", 1)[0] or segs[-1]  # '/.well-known' keeps its name
    segs = ["id" if x == ":id" else x for x in segs if x]
    return re.sub(r"[^A-Za-z0-9._-]+", "-", "-".join(segs)).strip("-") or "home"


def run_label(keys=None, route=None, label=None, prefix=""):
    if keys:
        text = "+".join(keys[:3]) + (f"+{len(keys) - 3}more" if len(keys) > 3 else "")
    elif route is not None:
        text = route_slug(route)
    else:
        text = str(label or "").strip() or "run"
    # capped: a folder name over 255 bytes can't be created (a long qa-plan text label or route)
    return re.sub(r"[^A-Za-z0-9+._-]", "-", f"{prefix or ''}{text}")[:80]


def git_exclude(project, entries=("qa-out/", ".playwright-cli/", ".playwright-mcp/")):
    """Keep run output out of commits: list it in the repo's info/exclude (works in worktrees and submodules)."""
    out = core._git(project, "rev-parse", "--git-path", "info/exclude")
    if not out or not out.strip():
        return
    path = Path(out.strip())
    path = path if path.is_absolute() else Path(project) / path
    try:  # bytes: a Latin-1 comment in someone's exclude file must not stop the run
        text = path.read_text(encoding="utf-8", errors="surrogateescape") if path.exists() else ""
    except OSError:
        return
    have = {x.strip().strip("/") for x in text.splitlines()}
    missing = [e for e in entries if e.strip("/") not in have]
    if missing:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(("\n" if text and not text.endswith("\n") else "") + "".join(e + "\n" for e in missing))
        except OSError:
            pass  # an unwritable .git: the run still works, the output just isn't git-ignored


def _project(project):
    """The project folder, or BadInput (a typo must not create a stray folder or report a silent no-op)."""
    project = Path(project).resolve()
    if not project.is_dir():
        raise core.BadInput(f"not a folder: {project}")
    return project


def make_rundir(project, label, day=None):
    """<project>/qa-out/<YYYY-MM-DD>-<label>[-n]/ with bugs/ evidence/ repro/ .data/; a fresh folder every run."""
    project = _project(project)
    root = project / "qa-out"
    root.mkdir(exist_ok=True)
    base = f"{day or datetime.now().strftime('%Y-%m-%d')}-{label}"
    for n in range(1, 1000):
        run = root / (base if n == 1 else f"{base}-{n}")
        try:
            run.mkdir(mode=0o700)
            break
        except FileExistsError:
            continue
    else:
        raise core.BadInput(f"too many run folders named {base}")
    for sub in ("bugs", "evidence", "repro", ".data"):
        (run / sub).mkdir()
    git_exclude(project)
    return run


MARKER_HOURS = 8  # the guard ignores a marker after this (a crashed run); also caps --minutes


def marker_start(project, budget=0, minutes=0, faults=False, t=None):
    """qa-out/.active for the guard: 0 = unlimited budget / no deadline. Written atomically."""
    t = time.time() if t is None else t
    root = _project(project) / "qa-out"
    root.mkdir(exist_ok=True)
    data = {"expires": int(t + MARKER_HOURS * 3600), "deadline": int(t + minutes * 60) if minutes else 0,
            "budget": int(budget or 0), "faults": bool(faults), "used": 0}
    core._write(root / ".active", json.dumps(data) + "\n")
    return data


def marker_stop(project):
    for name in (".active", ".active.lock"):
        (Path(project) / "qa-out" / name).unlink(missing_ok=True)


def profile_dir():
    """The persistent `main` browser profile (the QA sign-in lives here; the guard protects it)."""
    return core.DATA_DIR / "profiles" / "main"


def signed_in_stamp():
    """Written by `qa-mem login-ok` after qa-login verified the sign-in; removed when a new qa-login starts."""
    return core.DATA_DIR / "profiles" / ".signed-in"


def ensure_profile_dir():
    p = profile_dir()
    core.DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    for d in (p.parent, p):
        d.mkdir(mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
    return p


def browser_cmd(session, headed, url, browser, profile):
    cmd = ["playwright-cli", f"-s={session}", "open", f"--browser={browser}", "--profile", str(profile)]
    return cmd + (["--headed"] if headed else []) + ([url] if url else [])


def browser_open(args):
    import subprocess
    session, headed, url = "qa", False, None
    it = iter(args)
    for a in it:
        if a == "--headed":
            headed = True
        elif a == "--session" or a.startswith("--session="):
            session = a.split("=", 1)[1] if "=" in a else next(it, "")
        elif a.startswith("-") or url:
            raise core.BadOption(f"browser-open: unexpected argument {a!r}")
        else:
            url = a
    if session not in ("qa", "qa-login"):
        raise core.BadInput("browser-open: --session must be qa or qa-login")
    browser = config().get("browser")
    if browser not in core.BROWSERS:
        try:
            json.loads(core.CONFIG.read_text())
            where = f'set "browser" in {core.CONFIG}'
        except OSError:
            where = f'{core.CONFIG} is missing; create it with a "browser" key'
        except ValueError as e:
            where = f"{core.CONFIG} isn't valid JSON ({e}); fix it"
        raise core.BadInput(f"browser-open: config browser must be one of {', '.join(core.BROWSERS)} (got {browser!r}): "
                       f"{where} (`qa-mem browsers` lists what's installed)")
    # every PLAYWRIGHT_MCP_* var (storage state, isolated, browser, user data dir…) would override the profile or break the launch
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PLAYWRIGHT_MCP_", "PWTEST_"))}
    profile = ensure_profile_dir()
    if session == "qa-login":  # a new sign-in starts: until it is verified (login-ok), doctor says to sign in
        signed_in_stamp().unlink(missing_ok=True)
    other = "qa-login" if session == "qa" else "qa"
    try:  # both sessions use profiles/main, which opens in one browser only: stop the other one (no-op if it's closed)
        subprocess.run(["playwright-cli", f"-s={other}", "close"], env=env, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        pass  # the open below reports a missing playwright-cli or a profile still in use
    try:  # captured (the daemon gets its own stdio, so this returns) to translate the profile-in-use error
        r = subprocess.run(browser_cmd(session, headed, url, browser, profile), env=env, capture_output=True, text=True,
                           errors="replace")
    except OSError as e:
        raise core.BadInput(f"browser-open: can't run playwright-cli ({e}); npm i -g @playwright/cli")
    code_ = r.returncode if r.returncode >= 0 else 128 - r.returncode  # killed by signal N: exit 128+N like a shell
    if "already in use" in f"{r.stdout}\n{r.stderr}":
        # playwright-cli's hint (--isolated) would drop the sign-in; say what actually holds the profile
        print(f"browser-open: Browser is already in use: another qa-sdet browser holds the main profile (a "
              f"/qa-sdet:qa-login window not finished, or a run in another project folder). Close the other qa-sdet "
              f"browser (or run `playwright-cli close-all` in that folder) and retry; never use --isolated, it has no "
              f"login.", file=sys.stderr)
        code_ = code_ or 1
    sys.stdout.write(r.stdout or "")
    sys.stderr.write(r.stderr or "")
    return code_


def prune(project, days=7, t=None, run=None):
    """Delete page snapshots older than `days` (regular files only; links are never followed). With a run folder also
    that run's temp files: <run>/.data/snapshot*.md and map-*.png, and the snapshots taken since the run began (its
    oldest file). Evidence, bug drafts, repro specs, the JSON files and REPORT.md stay. Returns the count."""
    cutoff, n, since = (time.time() if t is None else t) - days * 86400, 0, None
    if run is not None:
        stamps = [os.lstat(os.path.join(d, fn)).st_mtime for d, _, fs in os.walk(run) for fn in fs]
        since = min(stamps) if stamps else None
        data = Path(run) / ".data"
        for f in [*data.glob("snapshot*.md"), *data.glob("map-*.png")] if data.is_dir() and not data.is_symlink() else []:
            try:
                if stat.S_ISREG(f.lstat().st_mode):
                    f.unlink()
                    n += 1
            except OSError:
                continue
    for name in (".playwright-cli", ".playwright-mcp"):
        top = Path(project) / name
        if top.is_symlink() or not top.is_dir():
            continue
        for dirpath, _, files in os.walk(top):
            for fn in files:
                f = Path(dirpath) / fn
                try:
                    st = f.lstat()
                    if stat.S_ISREG(st.st_mode) and (st.st_mtime < cutoff or (since is not None and st.st_mtime >= since)):
                        f.unlink()
                        n += 1
                except OSError:
                    continue
    return n


# ---------- annotated bug images ----------
RED, WHITE = (224, 0, 0), (255, 255, 255)
NO_PILLOW = "annotate: Pillow not installed (python3 -m pip install --user pillow); image left as is"


def evidence_png(path):
    """The real path of a .png inside qa-out/<run>/evidence/, or BadInput. The project is the folder holding the
    qa-out named in the path; the png's real path must stay inside that project (a link to another project's
    evidence, or a qa-out/<run> linked elsewhere, is refused)."""
    shape = lambda q: q.suffix.lower() == ".png" and q.parent.name == "evidence" and q.parent.parent.parent.name == "qa-out"
    given = Path(os.path.abspath(os.path.expanduser(str(path))))  # lexical: links not followed yet
    p = Path(os.path.realpath(given))
    project = Path(os.path.realpath(given.parents[3])) if len(given.parents) > 3 else None
    if not (shape(given) and shape(p) and project and p.is_relative_to(project)):
        raise core.BadInput(f"annotate: only .png files inside qa-out/<run>/evidence/ of this project ({path})")
    if not p.is_file():
        raise core.BadInput(f"annotate: no such image {path}")
    return p


def parse_box(text):
    try:
        x, y, w, h = (int(round(float(v))) for v in text.split(","))
    except (ValueError, OverflowError):  # OverflowError: inf / 1e400
        raise core.BadInput(f"annotate: --box must be x,y,w,h (got {text!r})")
    if w <= 0 or h <= 0:
        raise core.BadInput(f"annotate: --box width and height must be > 0 ({text!r})")
    return x, y, w, h


def _font(ImageFont, size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1: fixed-size bitmap font
        return ImageFont.load_default()


def _wrap(draw, text, font, width):
    """Lines of text no wider than width px (words kept whole unless one alone is too wide)."""
    lines, cur = [], ""
    for word in text.split(" "):
        while draw.textlength(word, font=font) > width and len(word) > 1:  # break an over-long word
            n = max(1, next((i for i in range(len(word), 0, -1) if draw.textlength(word[:i], font=font) <= width), 1))
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(word[:n])
            word = word[n:]
        cand = f"{cur} {word}" if cur else word
        if cur and draw.textlength(cand, font=font) > width:
            lines.append(cur)
            cur = word
        else:
            cur = cand
    return lines + ([cur] if cur else [])


def annotate(path, boxes, label):
    """Red outline drawn just OUTSIDE each box (so the element's own text stays readable) and a red label tag
    with white text next to the first box, on the quietest nearby spot that covers no box (with no free spot, the one
    hiding the least of the boxes; a label taller than the image keeps the lines that fit, ending in …). Line width and font
    grow with the image (a HiDPI 2560px screenshot gets 2x), long labels wrap. Boxes are in the screenshot's
    pixels; a box wholly outside the image is refused. Returns False when Pillow is missing (image untouched)."""
    import tempfile
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return False
    try:
        img = Image.open(path)
        img.load()
    except (OSError, Image.DecompressionBombError) as e:  # not a PNG, truncated, or too many pixels: left as is
        raise core.BadInput(f"annotate: can't read {path.name} as an image ({e}); image left as is")
    W, H = img.size
    for x, y, w, h in boxes:
        if x >= W or y >= H or x + w <= 0 or y + h <= 0:
            raise core.BadInput(f"annotate: --box {x},{y},{w},{h} is outside the {W}x{H} image; boxes are in the "
                           "screenshot's pixels (the box eval's numbers as is); image left as is")
    if img.mode.startswith("I"):  # 16/32-bit grey: convert("RGB") clips every value > 255 to white
        img = img.convert("I").point(lambda v: v / 256).convert("L")
    alpha = "A" in img.getbands() or "transparency" in img.info  # palette/grey PNGs keep transparency in info
    img = img.convert("RGBA" if alpha else "RGB")
    grey = img.convert("L")  # the page before drawing: where the label would hide the least
    scale = min(max(W / 1280, 1.0), 4.0)
    lw, gap = max(2, round(3 * scale)), max(2, round(3 * scale))
    draw = ImageDraw.Draw(img)
    drawn = []
    for x, y, w, h in boxes:  # four bands just outside the element, each clipped to the image: at an image edge
        bx0, by0, bx1, by1 = x, y, x + w - 1, y + h - 1  # the band there is thinner, never moved onto the element
        ox0, oy0, ox1, oy1 = bx0 - lw, by0 - lw, bx1 + lw, by1 + lw
        for r in ((ox0, oy0, ox1, by0 - 1), (ox0, by1 + 1, ox1, oy1), (ox0, by0, bx0 - 1, by1), (bx1 + 1, by0, ox1, by1)):
            cx0, cy0, cx1, cy1 = max(r[0], 0), max(r[1], 0), min(r[2], W - 1), min(r[3], H - 1)
            if cx0 <= cx1 and cy0 <= cy1:
                draw.rectangle([cx0, cy0, cx1, cy1], fill=RED)
        drawn.append((max(ox0, 0), max(oy0, 0), min(ox1, W - 1), min(oy1, H - 1)))
    text = core.short(" ".join(str(label).split()), 80)
    if text:
        font, pad = _font(ImageFont, round(18 * scale)), max(4, round(4 * scale))
        lines = _wrap(draw, text, font, max(1, W - 2 * pad))
        asc, desc = font.getmetrics() if hasattr(font, "getmetrics") else (11, 0)
        lh = asc + desc + max(1, round(2 * scale))
        room = max(1, (H - 2 * pad) // lh)  # a short screenshot: keep the lines that fit, say the rest was cut
        if len(lines) > room:
            lines = lines[:room]
            while len(lines[-1]) > 1 and draw.textlength(lines[-1] + "…", font=font) > W - 2 * pad:
                lines[-1] = lines[-1][:-1]
            lines[-1] = lines[-1].rstrip() + "…"
        tw = min(max(round(draw.textlength(t, font=font)) for t in lines) + 2 * pad, W)
        th = min(lh * len(lines) + 2 * pad, H)
        x0, y0, x1, y1 = drawn[0]
        ux0, uy0, uy1 = min(c[0] for c in drawn), min(c[1] for c in drawn), max(c[3] for c in drawn)
        fit = lambda tx, ty: (max(0, min(tx, W - tw)), max(0, min(ty, H - th)))
        hits = lambda tx, ty: any(tx < c[2] + gap and c[0] - gap < tx + tw and ty < c[3] + gap and c[1] - gap < ty + th
                                  for c in drawn)

        def busy(tx, ty):  # share of pixels that are page content (differ from the spot's commonest shade)
            hist = grey.crop((tx, ty, tx + tw, ty + th)).histogram()
            bg = max(range(256), key=hist.__getitem__)
            return 1 - sum(hist[max(0, bg - 24):bg + 25]) / max(1, sum(hist))
        # around the first box (above, below, right, left; then above/below its right end), then above/below all
        spots = [fit(x0, y0 - th - gap), fit(x0, y1 + gap + 1), fit(x1 + gap + 1, y0), fit(x0 - gap - tw, y0),
                 fit(x1 - tw, y0 - th - gap), fit(x1 - tw, y1 + gap + 1), fit(ux0, uy0 - th - gap), fit(ux0, uy1 + gap + 1),
                 fit(0, 0), fit(W, 0), fit(0, H), fit(W, H)]  # corners: the last resort when nothing near is free
        cover = lambda tx, ty: sum(max(0, min(tx + tw, c[2] + 1) - max(tx, c[0])) * max(0, min(ty + th, c[3] + 1) - max(ty, c[1]))
                                   for c in drawn)  # box area hidden (no free spot: hide the least)
        free = [xy for xy in spots if not hits(*xy)]
        quiet = [xy for xy in free if busy(*xy) < 0.01]
        tx, ty = (quiet[0] if quiet else min(free, key=lambda xy: busy(*xy)) if free
                  else min(spots, key=lambda xy: (cover(*xy), busy(*xy))))
        draw.rectangle([tx, ty, tx + tw - 1, ty + th - 1], fill=RED)
        for i, t in enumerate(lines):
            draw.text((tx + pad, ty + pad + i * lh), t, fill=WHITE, font=font)
    # a fresh temp file (O_EXCL): a planted .tmp symlink must not redirect the write outside evidence/
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            img.save(f, format="PNG")
        os.chmod(tmp, stat.S_IMODE(path.stat().st_mode))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return True
