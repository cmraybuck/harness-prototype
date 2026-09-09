import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from harness_prototype.files import MAX_FILE_BYTES, read_file
from harness_prototype.tools import execute_tool


class FileReaderTests(unittest.TestCase):
    def setUp(self):
        """Create a temporary UTF-8 sample file for each reader test."""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'sample.txt'
        self.path.write_bytes(b'\xef\xbb\xbfalpha\r\nbeta\r\ngamma')

    def test_pages_and_line_numbers(self):
        """Check numbered text and continuation from one page to the next."""
        first = read_file(str(self.path), max_lines=2)
        self.assertEqual(first['content'], '1: alpha\n2: beta\n')
        self.assertTrue(first['truncated'])
        last = read_file(str(self.path), start_line=first['next_line'])
        self.assertEqual(last['content'], '3: gamma\n')
        self.assertIsNone(last['next_line'])

    def test_empty_and_past_end(self):
        """Check that empty files and reads past the last line return no text."""
        self.assertEqual(read_file(str(self.path), start_line=99)['lines_read'], 0)
        self.path.write_text('')
        result = read_file(str(self.path))
        self.assertEqual(result['total_lines'], 0)
        self.assertFalse(result['truncated'])

    def test_unsupported_content(self):
        """Check that binary, invalid UTF-8, and oversized content return errors."""
        for data in (b'\x00abc', b'\xff', b'x' * (MAX_FILE_BYTES + 1), b'x' * 8001):
            self.path.write_bytes(data)
            self.assertFalse(read_file(str(self.path))['ok'])

    def test_output_limit_preserves_paging(self):
        """Check that the output cap leaves unread lines available on the next page."""
        self.path.write_text(('a' * 4000) + '\n' + ('b' * 4000))
        result = read_file(str(self.path))
        self.assertEqual(result['next_line'], 2)
        self.assertLessEqual(len(result['content']), 8000)
        self.assertTrue(read_file(str(self.path), start_line=2)['ok'])

    def test_preserves_non_newline_separators(self):
        """Keep form feeds and Unicode separators on their source line."""
        data = 'alpha\x0bbeta\x0cgamma\u2028delta\r\nlast\n'.encode()
        with patch('pathlib.Path.is_file', return_value=True), patch(
            'pathlib.Path.open', return_value=io.BytesIO(data)
        ):
            result = read_file('sample.txt')
        self.assertEqual(result['total_lines'], 2)
        self.assertEqual(result['content'], '1: alpha\x0bbeta\x0cgamma\u2028delta\n2: last\n')

    def test_invalid_requests(self):
        """Check that invalid reader arguments and unusable paths return errors."""
        for args in ({}, {'path': 3}, {'path': str(self.path), 'start_line': 0},
                     {'path': str(self.path), 'max_lines': True}):
            self.assertFalse(execute_tool('read_file', args)['ok'])
        self.assertFalse(read_file(str(self.path.parent))['ok'])
        self.assertFalse(read_file(str(self.path.parent / 'missing'))['ok'])


if __name__ == '__main__':
    unittest.main()
