"""`core.record` and the guard's own copy: what `dataclasses.dataclass` is to the type checker, at run time without its
import. Fields only. Each case is checked against the real dataclass, so what mypy believes is what runs."""

import dataclasses
import inspect
import unittest

from support import load_hook

from qa_sdet import core

guard_paths = load_hook("guard_for_record").guard_paths
BOTH = (("core", core.record), ("guard_paths", guard_paths.record))


def canvas(make):
    @make
    class Canvas:
        """The image being annotated."""

        width: int
        height: int
        label: str = ""
        scale: float = 1.0

    return Canvas


class Record(unittest.TestCase):
    def each(self):
        for name, record in BOTH:
            with self.subTest(copy=name):
                yield canvas(record), canvas(dataclasses.dataclass)

    def test_a_record_takes_its_fields_by_position_or_by_name(self):
        for ours, real in self.each():
            for args, kw in (
                ((3, 4), {}),
                ((3,), {"height": 4}),
                ((), {"height": 4, "width": 3}),
                ((3, 4, "x", 2.0), {}),
            ):
                a, b = ours(*args, **kw), real(*args, **kw)
                self.assertEqual(vars(a), vars(b), (args, kw))
            self.assertEqual(ours.__doc__, "The image being annotated.")

    def test_a_field_without_a_default_must_be_given(self):
        for ours, real in self.each():
            for args, kw in (((), {}), ((3,), {}), ((), {"width": 3}), ((), {"label": "x"})):
                for cls in (ours, real):
                    with self.assertRaises(TypeError, msg=(cls, args, kw)):
                        cls(*args, **kw)

    def test_an_unknown_field_one_given_twice_or_one_too_many_is_refused(self):
        for ours, real in self.each():
            for args, kw in (((3, 4), {"depth": 1}), ((3, 4), {"width": 5}), ((3, 4, "x", 2.0, 9), {})):
                for cls in (ours, real):
                    with self.assertRaises(TypeError, msg=(cls, args, kw)):
                        cls(*args, **kw)

    def test_a_default_is_not_shared_state(self):  # the real one refuses a list, dict or set as a default: so do ours
        for name, record in BOTH:
            for bad in ([], {}, set()):
                for make in (record, dataclasses.dataclass):
                    with self.subTest(copy=name, default=bad, make=make.__name__), self.assertRaises(ValueError):

                        @make
                        class Shared:
                            items: list = bad

    def test_a_field_without_a_default_cannot_follow_one_with_a_default(self):
        for name, record in BOTH:
            for make in (record, dataclasses.dataclass):
                with self.subTest(copy=name, make=make.__name__), self.assertRaises(TypeError):

                    @make
                    class Wrong:
                        label: str = ""
                        width: int

    def test_a_field_can_be_set_after_the_record_is_made(self):
        for ours, real in self.each():
            a, b = ours(3, 4), real(3, 4)
            a.label = b.label = "later"
            self.assertEqual(vars(a), vars(b))

    def test_the_guard_keeps_the_same_code_as_lib(self):  # the guard imports nothing from lib: its copy must not drift
        self.assertEqual(inspect.getsource(guard_paths.record), inspect.getsource(core.record))


if __name__ == "__main__":
    unittest.main()
