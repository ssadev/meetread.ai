from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from caption_scraper import CaptionScraper, StaleElementReferenceException


class FakeElement:
    def __init__(self, text="", children=None):
        self.text = text
        self.children = children or {}

    def find_elements(self, by, selector=None):
        return self.children.get(selector or by, [])

    def is_displayed(self):
        return True


class StaleCaptionElement(FakeElement):
    def find_elements(self, by, selector=None):
        raise StaleElementReferenceException("stale")


class CaptionScraperTests(unittest.TestCase):
    def test_caption_dedup_commits_only_finalized_utterances(self):
        scraper = CaptionScraper()
        start = datetime(2026, 5, 23, 10, 0, tzinfo=timezone.utc)
        scraper._meeting_start_time = start

        scraper.ingest_caption("Alice", "hello", start + timedelta(seconds=1))
        scraper.ingest_caption("Alice", "hello team", start + timedelta(seconds=2))
        self.assertEqual(scraper.get_lines(), [])

        scraper.ingest_caption("Bob", "sure", start + timedelta(seconds=4))
        lines = scraper.get_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["speaker"], "Alice")
        self.assertEqual(lines[0]["text"], "hello team")
        self.assertEqual(lines[0]["timestamp"], "00:00:04")

        scraper.commit_buffer(start + timedelta(seconds=6))
        self.assertEqual(scraper.get_lines()[1]["text"], "sure")

    def test_caption_dedup_handles_case_changes_in_incremental_text(self):
        scraper = CaptionScraper()
        start = datetime(2026, 5, 23, 10, 0, tzinfo=timezone.utc)
        scraper._meeting_start_time = start

        scraper.ingest_caption("Alice", "Video. And then any", start + timedelta(seconds=1))
        scraper.ingest_caption("Alice", "video and then any two foreign ministers", start + timedelta(seconds=2))
        scraper.ingest_caption("Bob", "yes", start + timedelta(seconds=3))

        lines = scraper.get_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["text"], "video and then any two foreign ministers")

    def test_scrape_current_caption_uses_selector_fallbacks(self):
        caption = FakeElement(
            text="Alice hello",
            children={
                'div[jsname="W3Gkyd"]': [FakeElement("Alice")],
                'span[jsname="dfnsle"]': [FakeElement("hello")],
            },
        )
        driver = FakeElement(children={'div[jsname="tgaKEf"]': [caption]})
        scraper = CaptionScraper()

        self.assertEqual(scraper.scrape_current_caption(driver), ("Alice", "hello"))
        self.assertEqual(scraper.get_status()["last_selector"], 'div[jsname="tgaKEf"]')

    def test_scrape_current_caption_treats_stale_dom_as_transient(self):
        driver = FakeElement(children={'div[jsname="tgaKEf"]': [StaleCaptionElement("old caption")]})
        scraper = CaptionScraper()

        self.assertIsNone(scraper.scrape_current_caption(driver))
        self.assertEqual(scraper.get_status()["stale_dom_polls"], 1)
        self.assertEqual(scraper.get_status()["errors"], [])

    def test_scrape_current_caption_splits_embedded_speaker_name(self):
        caption = FakeElement(text="Sarfaraz Ahamed\nVideo. And then any two foreign ministers")
        driver = FakeElement(children={"div.a4cQT": [caption]})
        scraper = CaptionScraper()

        self.assertEqual(
            scraper.scrape_current_caption(driver),
            ("Sarfaraz Ahamed", "Video. And then any two foreign ministers"),
        )

    def test_ingest_caption_splits_embedded_speaker_when_unknown(self):
        scraper = CaptionScraper()
        start = datetime(2026, 5, 23, 10, 0, tzinfo=timezone.utc)
        scraper._meeting_start_time = start

        scraper.ingest_caption("Unknown", "Sarfaraz Ahamed Video. And then any", start + timedelta(seconds=1))
        scraper.commit_buffer(start + timedelta(seconds=2))

        line = scraper.get_lines()[0]
        self.assertEqual(line["speaker"], "Sarfaraz Ahamed")
        self.assertEqual(line["text"], "Video. And then any")
