"""Bug evidence images: `evidence` (a screenshot with the element boxed and labelled) and `annotate`."""

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from . import core, session

RED, WHITE = (224, 0, 0), (255, 255, 255)
NO_PILLOW = "annotate: Pillow not installed (python3 -m pip install --user pillow); image left as is"


def evidence_png(path, must_exist=True):
    """The real path of a .png inside qa-out/<run>/evidence/, or BadInput. The project is the folder holding the
    qa-out named in the path; the png's real path must stay inside that project (a link to another project's
    evidence, or a qa-out/<run> linked elsewhere, is refused). must_exist=False: an image about to be taken."""
    shape = lambda q: (
        q.suffix.lower() == ".png" and q.parent.name == "evidence" and q.parent.parent.parent.name == "qa-out"
    )
    given = Path(os.path.abspath(os.path.expanduser(str(path))))  # lexical: links not followed yet
    p = Path(os.path.realpath(given))
    project = Path(os.path.realpath(given.parents[3])) if len(given.parents) > 3 else None
    if not (shape(given) and shape(p) and project and p.is_relative_to(project)):
        raise core.BadInput(f"annotate: only .png files inside qa-out/<run>/evidence/ of this project ({path})")
    if must_exist and not p.is_file():
        raise core.BadInput(f"annotate: no such image {path}")
    return p


HIGHLIGHT_STYLE = "outline: 3px solid red"


def evidence(path, label, selector=None, ref=None):
    """The evidence image in one call (references/playwright-cli-tips.md, "Evidence image"): with a CSS selector, scroll
    to the element, take the screenshot and draw the box and label (Pillow); without Pillow, or for a snapshot ref (an
    element in a frame), the browser draws the box and `highlight --hide` always runs after, so no box leaks into the
    next image. Prints {path, annotated, method}; exit 1 when the element or a browser command fails."""
    if ref is not None and not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*", ref):  # an option would reach playwright-cli
        raise core.BadInput(f"evidence: --ref takes a snapshot ref such as e12 (from find or a snapshot), not {ref!r}")
    png = evidence_png(path, must_exist=False)
    env = session._pw_env()
    pw = lambda *a: subprocess.run(
        ["playwright-cli", "-s=qa", *a], env=env, capture_output=True, text=True, errors="replace", timeout=60
    )

    def fail(what, r=None):
        detail = core.short((r.stderr or r.stdout or "").strip(), 200) if r is not None else ""
        print(f"evidence: {what}" + (f": {detail}" if detail else ""), file=sys.stderr)
        return 1

    if selector is not None:  # the selector reaches the page as a JSON string, never as code
        js = (
            f"(() => {{ const el = document.querySelector({json.dumps(selector)}); if (!el) return ''; "
            "el.scrollIntoView({block: 'center'}); const r = el.getBoundingClientRect(); "
            "return [r.x, r.y, r.width, r.height].map(Math.round).join(','); })()"
        )
        r = pw("--raw", "eval", js)
        box = session._unwrap_raw(r.stdout) if r.returncode == 0 else None
        if r.returncode:
            return fail("the element lookup failed", r)
        if not isinstance(box, str) or not box.strip():
            return fail(f"no element matches {selector!r} (in a frame? use --ref from `find`)")
        r = pw("screenshot", f"--filename={png}")
        if r.returncode:
            return fail("screenshot failed", r)
        if annotate(png, [parse_box(box)], label):
            print(json.dumps({"path": str(png), "annotated": True, "method": "pillow"}))
            return 0
        target = selector  # no Pillow: take it again with the browser's own box
    else:
        r = pw("eval", "el => el.scrollIntoView({block: 'center'})", ref)
        if r.returncode:
            return fail(f"no element {ref!r} (take a fresh `find`)", r)
        target = ref
    try:
        r = pw("highlight", target, "--style", HIGHLIGHT_STYLE)
        if r.returncode:
            return fail("highlight failed", r)
        r = pw("screenshot", f"--filename={png}")
        if r.returncode:
            return fail("screenshot failed", r)
    finally:
        pw("highlight", "--hide")
    annotate(png, [], label)  # Pillow there: the label as a caption over the browser's box (an iframe element)
    print(json.dumps({"path": str(png), "annotated": True, "method": "highlight"}))
    return 0


def parse_box(text):
    try:
        x, y, w, h = (round(float(v)) for v in text.split(","))
    except (ValueError, OverflowError):  # OverflowError: inf / 1e400
        raise core.BadInput(f"annotate: --box must be x,y,w,h (got {text!r})") from None
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
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return False
    try:
        img = Image.open(path)
        img.load()
    except (
        OSError,
        ValueError,
        Image.DecompressionBombError,
    ) as e:  # not a PNG, truncated, huge text chunk, or bomb: left as is
        raise core.BadInput(f"annotate: can't read {path.name} as an image ({e}); image left as is") from e
    W, H = img.size
    for x, y, w, h in boxes:
        if x >= W or y >= H or x + w <= 0 or y + h <= 0:
            raise core.BadInput(
                f"annotate: --box {x},{y},{w},{h} is outside the {W}x{H} image; boxes are in the "
                "screenshot's pixels (the box eval's numbers as is); image left as is"
            )
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
        for r in (
            (ox0, oy0, ox1, by0 - 1),
            (ox0, by1 + 1, ox1, oy1),
            (ox0, by0, bx0 - 1, by1),
            (bx1 + 1, by0, ox1, by1),
        ):
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
        corners = [(0, 0), (max(0, W - tw), 0), (0, max(0, H - th)), (max(0, W - tw), max(0, H - th))]
        x0, y0, x1, y1 = drawn[0] if drawn else (0, 0, 0, 0)  # no box: a caption in the quietest corner
        ux0, uy0, uy1 = (
            (min(c[0] for c in drawn), min(c[1] for c in drawn), max(c[3] for c in drawn)) if drawn else (0, 0, 0)
        )
        fit = lambda tx, ty: (max(0, min(tx, W - tw)), max(0, min(ty, H - th)))
        hits = lambda tx, ty: any(
            tx < c[2] + gap and c[0] - gap < tx + tw and ty < c[3] + gap and c[1] - gap < ty + th for c in drawn
        )

        def busy(tx, ty):  # share of pixels that are page content (differ from the spot's commonest shade)
            hist = grey.crop((tx, ty, tx + tw, ty + th)).histogram()
            bg = max(range(256), key=hist.__getitem__)
            return 1 - sum(hist[max(0, bg - 24) : bg + 25]) / max(1, sum(hist))

        # around the first box (above, below, right, left; then above/below its right end), then above/below all
        spots = (
            [
                fit(x0, y0 - th - gap),
                fit(x0, y1 + gap + 1),
                fit(x1 + gap + 1, y0),
                fit(x0 - gap - tw, y0),
                fit(x1 - tw, y0 - th - gap),
                fit(x1 - tw, y1 + gap + 1),
                fit(ux0, uy0 - th - gap),
                fit(ux0, uy1 + gap + 1),
                fit(0, 0),
                fit(W, 0),
                fit(0, H),
                fit(W, H),
            ]
            if drawn
            else corners
        )  # corners: the last resort
        cover = lambda tx, ty: sum(
            max(0, min(tx + tw, c[2] + 1) - max(tx, c[0])) * max(0, min(ty + th, c[3] + 1) - max(ty, c[1]))
            for c in drawn
        )  # box area hidden (no free spot: hide the least)
        free = [xy for xy in spots if not hits(*xy)]
        quiet = [xy for xy in free if busy(*xy) < 0.01]
        tx, ty = (
            quiet[0]
            if quiet
            else min(free, key=lambda xy: busy(*xy))
            if free
            else min(spots, key=lambda xy: (cover(*xy), busy(*xy)))
        )
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
