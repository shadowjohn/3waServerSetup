import unittest
from unittest.mock import patch


class MascotTests(unittest.TestCase):
    def test_pixel_grid_and_terminal_cell_dimensions(self):
        from server_setting.mascot import render_mascot
        from rich.cells import cell_len
        picture = render_mascot()
        self.assertEqual(len(picture.plain.splitlines()), 20)
        self.assertTrue(all(cell_len(row) == 20 for row in picture.plain.splitlines()))
        self.assertTrue(any('\u2801' <= char <= '\u28ff' for char in picture.plain))
        self.assertEqual({str(span.style) for span in picture.spans}, {'#ffffff on #000000'})

    def test_ascii_fallback_has_no_block_characters_or_color(self):
        from server_setting.mascot import render_mascot
        fallback = render_mascot(True)
        self.assertTrue(fallback.plain.isascii())
        self.assertEqual(fallback.spans, [])
        self.assertNotIn('PASS', fallback.plain)
        self.assertIn('3wa', fallback.plain)
        self.assertNotIn('snack', fallback.plain)

    def test_explicit_and_no_color_fallback(self):
        from server_setting.mascot import ascii_fallback
        with patch.dict('os.environ', {'NO_COLOR': '1'}):
            self.assertTrue(ascii_fallback())
        self.assertTrue(ascii_fallback(True))
