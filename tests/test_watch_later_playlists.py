import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ytcli


class WatchLaterPlaylistTests(unittest.TestCase):
    def test_load_open_and_remove_playlist_preserving_other_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'later.txt'
            path.write_text('abcdefghijk | Video | 2024-01-01 | 1:00\n'
                            'https://www.youtube.com/playlist?list=PLexample123456789 | Playlist\n'
                            'unrecognized entry\n')
            with patch.object(ytcli, 'watch_later_path', return_value=str(path)):
                video, playlist = ytcli.load_watch_later()
                self.assertEqual((video['type'], playlist['type']), ('video', 'playlist'))
                self.assertIn('type=playlist', ytcli._video_entry_lines(playlist, [], set(), False, True)[1])
                with patch.object(ytcli.webbrowser, 'open', return_value=True) as browser:
                    ytcli.open_entry(playlist)
                    browser.assert_called_once_with('https://www.youtube.com/playlist?list=PLexample123456789')
                ytcli.remove_from_watch_later(playlist)
                self.assertEqual(path.read_text(), 'abcdefghijk | Video | 2024-01-01 | 1:00\nunrecognized entry\n')

    def test_playlist_id_and_invalid_url(self):
        self.assertEqual(ytcli.parse_watch_later_line('PLexample123456789')['type'], 'playlist')
        self.assertIsNone(ytcli.parse_watch_later_line('https://example.com/playlist?list=PLexample123456789'))


if __name__ == '__main__':
    unittest.main()

class PlaylistMetadataTests(unittest.TestCase):
    def test_metadata_and_unavailable_fallback(self):
        entries = [ytcli.parse_watch_later_line('PLexample123456789 | Playlist'),
                   ytcli.parse_watch_later_line('PLmissing123456789 | Missing')]
        response = {'items': [{'id': 'PLexample123456789',
                               'snippet': {'publishedAt': '2022-12-01T00:00:00Z'},
                               'contentDetails': {'itemCount': 12}}]}
        with patch.object(ytcli, 'make_session'), patch.object(ytcli, 'api_get', return_value=response):
            ytcli.populate_playlist_metadata(entries)
        self.assertEqual((entries[0]['published'], entries[0]['duration']), ('2022-12-01', '12 videos'))
        self.assertEqual((entries[1]['published'], entries[1]['duration']), ('date unavailable', 'video count unavailable'))

class AddWatchLaterTests(unittest.TestCase):
    def test_add_valid_playlist_and_reject_duplicate_and_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'later.txt'
            entries = []
            response = {'items': [{'snippet': {'title': 'A playlist', 'publishedAt': '2023-01-01T00:00:00Z'},
                                   'contentDetails': {'itemCount': 2}}]}
            with patch.object(ytcli, 'watch_later_path', return_value=str(path)), \
                 patch.object(ytcli, 'make_session'), \
                 patch.object(ytcli, 'api_get', return_value=response) as api:
                self.assertIn('Saved', ytcli.add_watch_later_entry('PLexample123456789', entries))
                self.assertEqual(entries[0]['duration'], '2 videos')
                self.assertIn('Already saved', ytcli.add_watch_later_entry('PLexample123456789', entries))
                self.assertEqual(len(entries), 1)
                original = path.read_text()
                api.return_value = {'items': []}
                self.assertIn('not found', ytcli.add_watch_later_entry('PLmissing123456789', entries))
                self.assertEqual(path.read_text(), original)

    def test_numbered_empty_list_adds_without_lowercasing_id(self):
        entries = []
        with patch.object(ytcli, 'read_input', side_effect=['a', 'PLMixedCase123456', '']), \
             patch.object(ytcli, 'add_watch_later_entry', return_value='added') as add:
            ytcli._video_list_view_numbered(entries, 'Watch later', allow_remove=True)
        add.assert_called_once_with('PLMixedCase123456', entries)
