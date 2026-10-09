from collective.elastic import manager
from collective.elastic.tests import BaseFunctionalTest
from collective.elastic.upgrades import update_registry
from collective.elastic.utils import get_settings
from collective.elastic.utils import getESOnlyIndexes
from plone import api
from plone.registry.interfaces import IRegistry
from unittest import mock
from zope.component import getUtility


class TestMinScore(BaseFunctionalTest):
    def setUp(self):
        super().setUp()
        api.content.create(self.portal, "Document", "strong", title="Apple apple apple")
        api.content.create(
            self.portal,
            "Document",
            "weak",
            title="Apple banana cherry damson elderberry fig grape",
        )
        self.commit(wait=1)

    def ids(self, query):
        return {brain.getId for brain in self.search(query)}

    def es_bodies(self, query):
        with mock.patch.object(
            manager, "es_search", wraps=manager.es_search
        ) as es_search:
            self.search(query)
        return [call.args[2] for call in es_search.call_args_list]

    def scores(self, query):
        """Scores of the documents for the query the search sends."""
        (body,) = self.es_bodies(query)
        hits = self.es._search(body["query"])["hits"]["hits"]
        return {
            hit["fields"]["path.path"][0].split("/")[-1]: hit["_score"] for hit in hits
        }

    def test_default_does_not_set_min_score(self):
        self.assertEqual(get_settings().min_score, 0.0)
        for body in self.es_bodies({"SearchableText": "apple"}):
            self.assertNotIn("min_score", body)
        self.assertEqual(self.ids({"SearchableText": "apple"}), {"strong", "weak"})

    def test_low_scoring_results_are_left_out(self):
        scores = self.scores({"SearchableText": "apple"})
        self.assertGreater(scores["strong"], scores["weak"])
        get_settings().min_score = (scores["strong"] + scores["weak"]) / 2
        self.assertEqual(self.ids({"SearchableText": "apple"}), {"strong"})

    def test_filter_only_query_ignores_min_score(self):
        """Filter clauses don't score; min_score would leave out everything."""
        settings = get_settings()
        settings.es_only_indexes = set(getESOnlyIndexes()) | {"portal_type"}
        settings.min_score = 1000.0
        self.assertEqual(self.ids({"portal_type": "Document"}), {"strong", "weak"})
        self.assertEqual(self.ids({"SearchableText": "apple"}), set())

    def test_upgrade_adds_the_record(self):
        registry = getUtility(IRegistry)
        record = "collective.elastic.interfaces.IElasticSettings.min_score"
        del registry.records[record]
        self.assertEqual(self.es.min_score, 0.0)
        update_registry(self.portal)
        self.assertIn(record, registry.records)
