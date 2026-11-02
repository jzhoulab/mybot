import tempfile
import unittest
from pathlib import Path

from app import pulse_figures as pf

PNG = pf.PNG_MAGIC + b"\x00" * 64


class PulseFiguresTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.src, self.dest = root / "work", root / "figures" / "2026-10-22"
        self.src.mkdir()
        (self.src / "flow.png").write_bytes(PNG)
        (self.src / "fake.png").write_bytes(b"not a png")

    def tearDown(self):
        self.tmp.cleanup()

    def test_collect_copies_valid_figures_and_drops_the_rest(self):
        text = ("Intro.\n\n![How it flows](figure:flow.png)\n\nMiddle.\n\n"
                "![Bad](figure:fake.png)\n![Missing](figure:none.png)\nEnd.")
        out = pf.collect(text, self.src, self.dest, "a2")
        self.assertIn("![How it flows](figure:a2-flow.png)", out)
        self.assertNotIn("fake", out)
        self.assertNotIn("none", out)
        self.assertTrue((self.dest / "a2-flow.png").exists())
        self.assertEqual(pf.figures_in(out), [{"caption": "How it flows", "name": "a2-flow.png"}])

    def test_collect_caps_figures_per_article(self):
        for name in ("b.png", "c.png"):
            (self.src / name).write_bytes(PNG)
        out = pf.collect("![1](figure:flow.png)\n![2](figure:b.png)\n![3](figure:c.png)", self.src, self.dest, "a1")
        self.assertEqual(len(pf.figures_in(out)), pf.MAX_PER_ARTICLE)

    def test_no_escape_from_the_work_folder(self):
        (self.src.parent / "secret.png").write_bytes(PNG)
        out = pf.collect("![x](figure:..secret.png)", self.src, self.dest, "a1")
        self.assertEqual(pf.figures_in(out), [])
        self.assertIsNone(pf.resolve(self.dest.parent, "2026-10-22", "../work/flow.png"))
        self.assertIsNone(pf.resolve(self.dest.parent, "../x", "flow.png"))

    def test_prune_and_captions(self):
        pf.collect("![Flow](figure:flow.png)", self.src, self.dest, "a1")
        text = "A\n\n![Flow](figure:a1-flow.png)\n\n![Invented](figure:a9-x.png)\n\nB"
        pruned = pf.prune(text, self.dest)
        self.assertIn("a1-flow.png", pruned)
        self.assertNotIn("Invented", pruned)
        self.assertEqual(pf.as_captions(pruned), "A\n\n*Figure: Flow*\n\nB")
        self.assertEqual(pf.without(pruned), "A\n\nB")
        self.assertEqual(pf.resolve(self.dest.parent, "2026-10-22", "a1-flow.png"), self.dest / "a1-flow.png")

    def test_inline_images_are_not_figures(self):
        text = "see ![x](figure:flow.png) inline, and ![web](https://example.com/a.png)"
        self.assertEqual(pf.figures_in(text), [])

    def test_instructions_only_when_available(self):
        self.assertEqual(pf.instructions("", self.src), "")
        self.assertIn(str(self.src), pf.instructions("/x/figure-python", self.src))


if __name__ == "__main__":
    unittest.main()


class HopFiguresTest(unittest.TestCase):
    """hop's web app draws the figures: the feed names them under
    /assets/pulse-figures/ and the files are copied beside pulse.json."""

    def test_sections_name_served_figures_and_publish_copies_them(self):
        from app import briefing
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            png = tmp / "a1-x.png"
            png.write_bytes(PNG)
            latest = {"date": "2026-11-02", "sections": [
                {"id": "a1", "kind": "article", "body": "Before.\n\n![A sketch.](figure:a1-x.png)\n\nAfter."},
                {"id": "a2", "kind": "article", "body": "![Gone](figure:gone.png)"}]}
            copies = {}
            secs = briefing._hop_sections("claude", latest, lambda d, n: png if n == "a1-x.png" else None, copies)
            self.assertIn("*Figure: A sketch.*", secs[0]["body"])
            self.assertEqual(secs[0]["figures"], [{"caption": "A sketch.", "name": "a1-x.png",
                                                   "src": "/assets/pulse-figures/2026-11-02-claude/a1-x.png"}])
            self.assertNotIn("src", secs[1]["figures"][0])
            root = tmp / "assets"
            (root / "pulse-figures" / "2026-10-01-claude").mkdir(parents=True)
            (root / "pulse-figures" / "2026-10-01-claude" / "old.png").write_bytes(PNG)
            briefing._publish_figures(root, copies)
            self.assertEqual((root / "pulse-figures/2026-11-02-claude/a1-x.png").read_bytes(), PNG)
            self.assertFalse((root / "pulse-figures/2026-10-01-claude").exists())
