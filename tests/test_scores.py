"""Tests for optional, sourced critic scores."""
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from game_guide import scores
from game_guide.notes import Game


def item(qid):
    return {"datavalue": {"value": {"id": qid}}}


def string(text):
    return {"datavalue": {"value": text}}


def amount(n):
    return {"datavalue": {"value": {"amount": f"+{n}"}}}


def claim(mainsnak, rank="normal", **qualifiers):
    return {"mainsnak": mainsnak, "rank": rank, "qualifiers": qualifiers}


class ScoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.game = Game(name="Hades")

    def wikidata(self, search, entities):
        """urlopen answering Wikidata's search, then its entity lookup."""
        return mock.patch("urllib.request.urlopen", side_effect=[io.BytesIO(json.dumps({"search": search}).encode()),
                                                                 io.BytesIO(json.dumps({"entities": entities}).encode())])

    def test_without_igdb_keys_the_score_comes_from_wikidata(self):
        entities = {
            "Q1": {"claims": {"P31": [claim(item("Q7889"))], "P444": [
                claim(string("9/10"), P447=[item("Q207708")]),  # a single outlet's review, not an aggregate
                claim(string("97%"), P447=[item("Q21039459")]),  # OpenCritic's "% recommend"
                claim(string("88/100"), P447=[item("Q150248")], P7887=[amount(40)]),
                claim(string("89/100"), P447=[item("Q21039459")], P7887=[amount(72)]),
                claim(string("99/100"), P447=[item("Q21039459")], rank="deprecated"),
            ]}},
            "Q2": {"claims": {"P31": [claim(item("Q7058673"))]}},  # the series, not the game
        }
        search = [{"id": "Q2", "label": "Hades"}, {"id": "Q1", "label": "hades"}, {"id": "Q3", "label": "Hades II"}]
        with mock.patch.dict(os.environ, {"IGDB_CLIENT_ID": "", "IGDB_ACCESS_TOKEN": ""}), self.wikidata(search, entities) as call:
            result = scores.refresh_score(self.game, self.directory)
        self.assertEqual(result, {"score": 89, "count": 72, "source": "OpenCritic"})
        self.assertEqual(scores.score_for(self.game, scores.read_scores(self.directory)), result)
        request = call.call_args_list[0].args[0]
        self.assertTrue(request.full_url.startswith(scores.WIKIDATA_API))
        self.assertIn("GameGuider", request.get_header("User-agent"))
        self.assertIn("ids=Q2%7CQ1", call.call_args_list[1].args[0].full_url)  # only exact-title matches are looked up

    def test_a_wikidata_aggregate_without_a_review_count_still_counts(self):
        entities = {"Q1": {"claims": {"P31": [claim(item("Q7889"))], "P444": [claim(string("91 / 100"), P447=[item("Q150248")])]}}}
        with mock.patch.dict(os.environ, {"IGDB_CLIENT_ID": ""}), self.wikidata([{"id": "Q1", "label": "Hades"}], entities):
            self.assertEqual(scores.refresh_score(self.game, self.directory), {"score": 91, "count": None, "source": "Metacritic"})

    def test_no_wikidata_match_or_aggregate_is_explained_and_not_cached(self):
        cases = [
            ([{"id": "Q3", "label": "Hades II"}], {}, "exact title"),
            ([{"id": "Q1", "label": "Hades"}], {"Q1": {"claims": {"P31": [claim(item("Q7889"))]}}}, "no critic score"),
        ]
        for search, entities, message in cases:
            with self.subTest(message=message), mock.patch.dict(os.environ, {"IGDB_CLIENT_ID": ""}), self.wikidata(search, entities):
                with self.assertRaisesRegex(ValueError, message):
                    scores.refresh_score(self.game, self.directory)
        self.assertFalse((self.directory / scores.CACHE_NAME).exists())

    def test_old_cached_scores_without_a_source_read_as_igdb(self):
        self.assertEqual(scores.score_for(self.game, {"hades": {"name": "Hades", "score": 93, "count": 11}}),
                         {"score": 93, "count": 11, "source": "IGDB"})

    def test_exact_match_caches_critic_aggregate(self):
        response = io.BytesIO(json.dumps([{"name": "Hades II", "aggregated_rating": 99, "aggregated_rating_count": 3}, {"name": "Hades", "aggregated_rating": 93.2, "aggregated_rating_count": 11}]).encode())
        with mock.patch.dict(os.environ, {"IGDB_CLIENT_ID": "client", "IGDB_ACCESS_TOKEN": "token"}), mock.patch("urllib.request.urlopen", return_value=response) as call:
            result = scores.refresh_score(self.game, self.directory)
        self.assertEqual(result, {"score": 93.2, "count": 11, "source": "IGDB"})
        self.assertEqual(scores.score_for(self.game, scores.read_scores(self.directory)), result)
        self.assertEqual(call.call_args.args[0].full_url, "https://api.igdb.com/v4/games")
        self.assertIsNone(scores.score_for(Game(name="A different game"), scores.read_scores(self.directory)))

    def test_ambiguous_or_missing_aggregate_is_not_cached(self):
        for data in ([{"name": "Hades"}, {"name": "Hades"}], [{"name": "Hades", "aggregated_rating": 0, "aggregated_rating_count": 0}]):
            with self.subTest(data=data), mock.patch.dict(os.environ, {"IGDB_CLIENT_ID": "client", "IGDB_ACCESS_TOKEN": "token"}), mock.patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(data).encode())):
                with self.assertRaises(ValueError):
                    scores.refresh_score(self.game, self.directory)
                self.assertFalse((self.directory / scores.CACHE_NAME).exists())

    def test_rejects_invalid_cached_scores(self):
        for score in (float("nan"), 101, -1, True):
            self.assertIsNone(scores.score_for(self.game, {self.game.slug: {"name": "Hades", "score": score, "count": 1}}))
