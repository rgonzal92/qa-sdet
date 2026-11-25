"""`qa-mem annotate`: red boxes and a label drawn on a bug's evidence image."""

import builtins
import importlib.util
import os
import stat
import struct
import unittest
import zlib
from unittest import mock

from support import Base, cli, core

from qa_sdet import ops


def white_png(w, h):
    """A plain white RGB PNG, built without Pillow (it is optional at runtime)."""
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    raw = b"".join(b"\0" + b"\xff" * 3 * w for _ in range(h))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


HAVE_PIL = importlib.util.find_spec("PIL") is not None


class Annotate(Base):
    """`qa-mem annotate` draws on evidence/*.png only, in place, and says so when Pillow is missing."""

    def png(self, w=200, h=120):
        p = self.rundir("r") / "evidence" / "BUG-1.png"
        p.write_bytes(white_png(w, h))
        return p

    @unittest.skipUnless(HAVE_PIL, "Pillow not installed")
    def test_a_label_alone_is_a_caption_in_a_corner(self):  # an iframe shot the browser boxed: the label still fits
        from PIL import Image

        p = self.png()
        code, out, _ = cli("annotate", str(p), "--label", "POST /api/zones 500 shown as Saved")
        self.assertEqual(code, 0)
        img = Image.open(p).convert("RGB")
        self.assertEqual(img.getpixel((2, 2)), ops.RED)  # the quiet top-left corner of a blank page
        self.assertIn(ops.WHITE, [img.getpixel((x, y)) for x in range(0, 120) for y in range(0, 30)])

    @unittest.skipUnless(HAVE_PIL, "Pillow not installed")
    def test_box_and_label(self):
        from PIL import Image

        p = self.png()
        code, out, _ = cli(
            "annotate", str(p), "--box", "60,60,50,30", "--box", "5.4,5,10,10", "--label", "Speed accepts 999"
        )
        self.assertEqual(code, 0)
        img = Image.open(p).convert("RGB")
        self.assertEqual(img.getpixel((57, 75)), ops.RED)  # outline just outside the first box's left edge
        self.assertEqual(img.getpixel((59, 75)), ops.RED)  # 3px wide
        self.assertEqual(img.getpixel((60, 75)), (255, 255, 255))  # the element itself stays clear
        self.assertEqual(img.getpixel((85, 75)), (255, 255, 255))
        self.assertEqual(img.getpixel((3, 10)), ops.RED)  # second box
        tag = [img.getpixel((x, y)) for x in range(60, 200) for y in range(30, 58)]
        self.assertGreater(tag.count(ops.RED), 200)  # the label tag sits above the first box
        self.assertIn(ops.WHITE, tag)  # with white text on it
        self.assertEqual(list(p.parent.iterdir()), [p])  # written in place, no temp file left

    @unittest.skipUnless(HAVE_PIL, "Pillow not installed")
    def test_label_below_when_no_room_and_clamped(self):
        from PIL import Image

        p = self.png()
        ops.annotate(p, [(150, 0, 100, 20)], "x" * 200)  # top edge, sticks out on the right
        img = Image.open(p).convert("RGB")
        self.assertEqual(img.getpixel((148, 10)), ops.RED)  # outline just outside the box
        self.assertNotEqual(img.getpixel((199, 10)), ops.RED)  # the box runs off the image: no band there, element kept
        self.assertEqual(img.getpixel((170, 21)), ops.RED)  # the band under it is drawn
        below = [img.getpixel((x, y)) for x in range(0, 200) for y in range(23, 45)]
        self.assertGreater(below.count(ops.RED), 200)
        self.assertEqual(len(core.short(" ".join(("x" * 200).split()), 80)), 80)

    @unittest.skipUnless(HAVE_PIL, "Pillow not installed")
    def test_label_never_covers_another_box(self):
        from PIL import Image

        p = self.png(300, 160)  # the 46-char label wraps to two lines at 300px
        # live case: the 2nd box sits right above the 1st, where the label would go
        ops.annotate(p, [(10, 60, 280, 16), (40, 30, 60, 20)], "0 closures shown although the API returned 404")
        img = Image.open(p).convert("RGB")
        inside = [img.getpixel((x, y)) for x in range(44, 97) for y in range(34, 47)]
        self.assertEqual(inside.count(ops.RED), 0)  # inside of the 2nd box stays clear
        below = [img.getpixel((x, y)) for x in range(10, 290) for y in range(83, 140)]
        self.assertGreater(below.count(ops.RED), 200)  # the label went below the boxes

    @unittest.skipUnless(HAVE_PIL, "Pillow not installed")
    def test_outline_outside_text_label_off_page_content_and_hidpi(self):  # outline hid the first glyph,
        from PIL import Image, ImageDraw  # label covered the Layer dropdown, 18px label unreadable at 2x

        for s in (1, 2):
            p = self.png(1280 * s, 720 * s)
            img = Image.open(p).convert("RGB")
            d = ImageDraw.Draw(img)
            d.rectangle([49 * s, 106 * s, 124 * s, 125 * s], outline=(128, 128, 128), fill=(200, 200, 200))  # dropdown
            d.rectangle([8 * s, 141 * s, 9 * s, 157 * s], fill=(0, 0, 0))  # first glyph of "0 closures shown"
            img.save(p)
            ops.annotate(p, [(8 * s, 141 * s, 110 * s, 17 * s)], 'Closures API 404 shown as a plain "0 closures shown"')
            img = Image.open(p).convert("RGB")
            self.assertEqual(img.getpixel((8 * s, 149 * s)), (0, 0, 0), s)  # the glyph is not painted over
            self.assertEqual(img.getpixel((8 * s - 1, 149 * s)), ops.RED, s)  # the outline hugs it from outside
            drop = [img.getpixel((x, y)) for x in range(49 * s, 125 * s) for y in range(106 * s, 126 * s)]
            self.assertEqual(drop.count(ops.RED), 0, s)  # dropdown left visible
            rows = [
                y
                for y in range(img.height)
                if ops.WHITE in [img.getpixel((x, y)) for x in range(125 * s, 700 * s)]
                and ops.RED in [img.getpixel((x, y)) for x in range(125 * s, 700 * s)]
            ]
            tag = max(rows) - min(rows) + 1
            self.assertGreaterEqual(tag, 20 * s, s)  # tag grows with the image
            lw = sum(img.getpixel((x, 149 * s)) == ops.RED for x in range(0, 8 * s))
            self.assertGreaterEqual(lw, 2 * s, s)

    @unittest.skipUnless(HAVE_PIL, "Pillow not installed")
    def test_long_label_wraps_on_a_narrow_image_and_box_off_image_is_refused(self):
        from PIL import Image

        p = self.png(390, 844)
        label = "Saved at 131 km/h; max is 130 and the error message never appears on mobile view"
        ops.annotate(p, [(8, 400, 250, 20)], label)
        img = Image.open(p).convert("RGB")
        reds = [x for x in range(390) for y in range(0, 844, 2) if img.getpixel((x, y)) == ops.RED]
        self.assertLessEqual(max(reds), 389)
        tag_rows = [y for y in range(844) if sum(img.getpixel((x, y)) == ops.RED for x in range(20, 200)) > 90]
        self.assertGreater(len(tag_rows), 40)  # two lines tall, not one line clipped at the right edge
        before = p.read_bytes()
        code, _, err = cli("annotate", str(p), "--box", "800,1700,100,40", "--label", "x")  # a 2x box on a 1x image
        self.assertEqual((code, p.read_bytes()), (2, before))
        self.assertIn("outside the 390x844 image", err)

    def test_no_pillow(self):
        p = self.png()
        before = p.read_bytes()
        real = builtins.__import__

        def fake(name, *a, **k):
            if name == "PIL" or name.startswith("PIL."):
                raise ImportError(name)
            return real(name, *a, **k)

        with mock.patch.object(builtins, "__import__", fake):
            code, out, _ = cli("annotate", str(p), "--box", "1,1,5,5", "--label", "x")
        self.assertEqual((code, out.strip()), (0, ops.NO_PILLOW))
        self.assertEqual(p.read_bytes(), before)

    def test_refuses_paths_outside_evidence(self):
        p = self.png()
        other = self.t / "x.png"
        other.write_bytes(p.read_bytes())
        (p.parent.parent / "bugs" / "a.png").write_bytes(p.read_bytes())
        (p.parent / "notes.txt").write_text("x")
        (p.parent / "link.png").symlink_to(other)
        for target in (
            other,
            p.parent.parent / "bugs" / "a.png",
            p.parent / "notes.txt",
            p.parent / "link.png",
            p.parent / "missing.png",
        ):
            self.assertEqual(cli("annotate", str(target), "--box", "1,1,5,5", "--label", "x")[0], 2, target)
        for argv in (("--box", "1,1,5"), ("--box", "1,1,0,5")):  # a label alone is a caption (its own test)
            self.assertEqual(
                cli("annotate", str(p), *argv, *(() if "--label" in argv else ("--label", "x")))[0], 2, argv
            )
        self.assertEqual(cli("annotate", str(p), "--box", "1,1,5,5")[0], 2)  # no label

    def test_refuses_another_projects_evidence(self):  # a link to another project's evidence png was drawn on
        p = self.png()
        other = self.t / "other" / "qa-out" / "r" / "evidence"
        other.mkdir(parents=True)
        (other / "a.png").write_bytes(p.read_bytes())
        (p.parent / "a.png").symlink_to(other / "a.png")
        (self.proj / "qa-out" / "r2").symlink_to(other.parent)  # a whole run folder linked to the other project
        before = (other / "a.png").read_bytes()
        for target in (p.parent / "a.png", self.proj / "qa-out" / "r2" / "evidence" / "a.png"):
            code, _, err = cli("annotate", str(target), "--box", "1,1,5,5", "--label", "x")
            self.assertEqual(code, 2, target)
            self.assertIn("of this project", err)
        self.assertEqual((other / "a.png").read_bytes(), before)
        self.assertEqual(ops.evidence_png(other / "a.png"), (other / "a.png").resolve())  # its own project: fine
        (p.parent / "same.png").symlink_to(p)  # a link within the same project stays allowed
        self.assertEqual(ops.evidence_png(p.parent / "same.png"), p.resolve())

    def test_refuses_all_but_a_png_in_a_run_evidence_folder(self):
        p = self.png()
        ev, other, loose = p.parent, self.t / "other", self.proj / "qa-out" / "evidence"
        for d in (other, ev / "sub", loose):
            d.mkdir()
        (ev / "linkdir").symlink_to(other)
        for f in (other / "x.png", ev / "BUG-1.jpg", ev / "BUG-1.png.sh", ev / "sub" / "x.png", loose / "x.png"):
            f.write_bytes(p.read_bytes())
        for target in (
            ev / "BUG-1.jpg",
            ev / "BUG-1.png.sh",
            ev / "sub" / "x.png",
            ev / "linkdir" / "x.png",
            ev,
            loose / "x.png",
            f"{ev}/../../../x.png",
            ev / "*.png",
            "-x.png",
        ):
            self.assertEqual(cli("annotate", str(target), "--box", "1,1,5,5", "--label", "x")[0], 2, target)

    @unittest.skipUnless(HAVE_PIL, "Pillow not installed")
    def test_bad_input_is_exit_2_and_image_kept(self):  # inf box, non-PNG, truncated, huge -> tracebacks
        from PIL import Image

        p = self.png()
        for box in ("inf,1,5,5", "1e400,1,5,5", "nan,1,5,5"):
            self.assertEqual(cli("annotate", str(p), "--box", box, "--label", "x")[0], 2, box)
        fake, cut = p.parent / "fake.png", p.parent / "cut.png"
        fake.write_text("<html>not a png</html>")
        cut.write_bytes(p.read_bytes()[:60])
        for f in (fake, cut):
            before = f.read_bytes()
            code, _, err = cli("annotate", str(f), "--box", "1,1,5,5", "--label", "x")
            self.assertEqual((code, f.read_bytes()), (2, before), f)
            self.assertIn("can't read", err)
        with mock.patch.object(Image, "MAX_IMAGE_PIXELS", 100):  # decompression-bomb guard
            self.assertEqual(cli("annotate", str(p), "--box", "1,1,5,5", "--label", "x")[0], 2)
        self.assertEqual(sorted(x.name for x in p.parent.iterdir()), ["BUG-1.png", "cut.png", "fake.png"])

    @unittest.skipUnless(HAVE_PIL, "Pillow not installed")
    def test_planted_temp_link_is_not_written_through(self):  # .BUG-1.png.tmp -> outside file was overwritten
        p = self.png()
        victim = self.t / "victim.txt"
        victim.write_text("keep")
        (p.parent / ".BUG-1.png.tmp").symlink_to(victim)
        os.chmod(p, 0o644)
        self.assertEqual(cli("annotate", str(p), "--box", "1,1,5,5", "--label", "x")[0], 0)
        self.assertEqual(victim.read_text(), "keep")
        self.assertFalse(p.is_symlink())
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o644)  # mode kept

    @unittest.skipUnless(HAVE_PIL, "Pillow not installed")
    def test_palette_and_16_bit_pngs(self):  # transparent palette went black, 16-bit grey went all white
        from PIL import Image

        p = self.png()
        im = Image.new("P", (40, 40), 0)
        im.save(p, transparency=0)
        ops.annotate(p, [(1, 1, 5, 5)], "x")
        self.assertEqual(Image.open(p).convert("RGBA").getpixel((30, 30))[3], 0)  # still transparent
        Image.new("I;16", (40, 40), 32768).save(p)
        ops.annotate(p, [(1, 1, 5, 5)], "x")
        self.assertEqual(Image.open(p).convert("RGB").getpixel((30, 30)), (128, 128, 128))


if __name__ == "__main__":
    unittest.main()
