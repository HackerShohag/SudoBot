import tempfile
import unittest
from pathlib import Path

import fitz

from bot.utils import split_for_manual_color


class PdfSplittingTests(unittest.TestCase):
    def make_pdf(self, path, page_colors):
        doc = fitz.open()
        for color in page_colors:
            page = doc.new_page(width=200, height=200)
            page.draw_rect(
                fitz.Rect(20, 20, 80, 80),
                color=color,
                fill=color,
            )
        doc.save(path)
        doc.close()

    def test_simplex_splits_bw_and_color_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "mixed.pdf"
            self.make_pdf(source, [(0, 0, 0), (1, 0, 0), (0, 0, 0)])

            result = split_for_manual_color(source, output_dir=root / "out")

            self.assertEqual(result.bw_pages, 2)
            self.assertEqual(result.color_pages, 1)
            self.assertTrue(result.bw_path.is_file())
            self.assertTrue(result.color_path.is_file())
            self.assertIn("insert as document page 2", result.guide)

    def test_duplex_preserves_positions_and_pads_odd_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "mixed.pdf"
            self.make_pdf(source, [(0, 0, 0), (1, 0, 0), (0, 0, 0)])

            result = split_for_manual_color(
                source, duplex=True, output_dir=root / "out"
            )

            self.assertEqual(result.bw_pages, 4)
            self.assertEqual(result.color_pages, 1)
            self.assertIn("sheet 1 (Back)", result.guide)

    def test_all_color_pdf_does_not_try_to_save_an_empty_bw_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "color.pdf"
            self.make_pdf(source, [(1, 0, 0)])

            result = split_for_manual_color(source, output_dir=root / "out")

            self.assertEqual(result.bw_pages, 0)
            self.assertIsNone(result.bw_path)
            self.assertEqual(result.color_pages, 1)
            self.assertTrue(result.color_path.is_file())

    def test_reports_page_progress_during_split(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "mixed.pdf"
            self.make_pdf(source, [(0, 0, 0), (1, 0, 0), (0, 0, 0)])
            progress = []

            split_for_manual_color(
                source,
                output_dir=root / "out",
                progress_callback=lambda processed, total: progress.append(
                    (processed, total)
                ),
            )

            self.assertEqual(
                progress,
                [(0, 3), (1, 3), (2, 3), (3, 3)],
            )


if __name__ == "__main__":
    unittest.main()
