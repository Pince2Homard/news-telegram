"""Collecte directe ; les anciens caches restent des données historiques."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from newsbot import collection, persistence


class DirectFeedTests(unittest.TestCase):
    def test_configured_youtube_feed_uses_one_direct_request(self):
        url = "https://www.youtube.com/feeds/videos.xml?channel_id=UCexample"
        response = Mock(content=b"synthetic-feed")
        parser = Mock(return_value=Mock(entries=[{"id": "video"}]))
        transport = Mock(get=Mock(return_value=response))
        self.assertEqual(collection.entries_for_feed(url, http=transport, parser=parser),
                         [{"id": "video"}])
        transport.get.assert_called_once_with(
            url, headers={"User-Agent": collection.UA}, timeout=(5, 20),
        )
        response.raise_for_status.assert_called_once_with()
        parser.assert_called_once_with(b"synthetic-feed")

    def test_old_gdata_state_remains_readable_and_unchanged_on_save(self):
        legacy_feed = "https://gdata.youtube.com/feeds/base/users/old/uploads"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps({
                "feeds": [legacy_feed], "yt": {"old": "UCarchived"},
                "file": [{"text": "pending", "link": "https://example.org/pending"}],
                "vus": ["historical-guid"],
            }), encoding="utf-8")
            state = persistence.load_state(path)
            persistence.save_state(state, dict.fromkeys(state["vus"]), path)
            resumed = persistence.load_state(path)
        self.assertEqual(resumed["feeds"], [legacy_feed])
        self.assertEqual(resumed["yt"], {"old": "UCarchived"})
        self.assertEqual(resumed["file"], state["file"])
        self.assertEqual(resumed["vus"], ["historical-guid"])
