from collective.elastic import manager
from collective.elastic.interfaces import IElasticSearchResults
from collective.elastic.tests import BaseAPITest
from collective.elastic.tests import BaseFunctionalTest
from collective.elastic.utils import get_settings
from plone import api
from plone.app.testing import logout
from plone.app.testing import SITE_OWNER_NAME
from plone.app.testing import SITE_OWNER_PASSWORD
from plone.restapi.testing import RelativeSession
from unittest import mock

import json


def create_documents(portal):
    for id_, subjects, publish in (
        ("doc1", ("alpha", "beta"), True),
        ("doc2", ("alpha",), True),
        ("doc3", ("gamma",), False),
    ):
        doc = api.content.create(portal, "Document", id_, title=f"Document {id_}")
        doc.setSubject(subjects)
        doc.reindexObject()
        # plone_workflow starts in "visible", which anonymous may view
        api.content.transition(obj=doc, transition="publish" if publish else "hide")


class TestFacets(BaseFunctionalTest):
    def setUp(self):
        super().setUp()
        create_documents(self.portal)
        self.commit(wait=1)

    def es_bodies(self, query):
        with mock.patch.object(
            manager, "es_search", wraps=manager.es_search
        ) as es_search:
            results = self.search(query)
            list(results)
        return [call.args[2] for call in es_search.call_args_list]

    def test_counts_values_of_the_matching_documents(self):
        results = self.search({"SearchableText": "document", "facets": ["Subject"]})
        self.assertTrue(IElasticSearchResults.providedBy(results))
        self.assertEqual(len(results), 3)
        self.assertEqual(
            results.facets, {"Subject": {"alpha": 2, "beta": 1, "gamma": 1}}
        )

    def test_counts_follow_the_filters(self):
        results = self.search(
            {"SearchableText": "document", "Subject": ["alpha"], "facets": ["Subject"]}
        )
        self.assertEqual(results.facets, {"Subject": {"alpha": 2, "beta": 1}})

    def test_facets_alone_query_elasticsearch(self):
        """Without an elasticsearch only index the catalog would be queried,
        which can't count facets."""
        results = self.search(
            {"portal_type": "Document", "facets": ["Subject", "review_state"]}
        )
        self.assertEqual(
            results.facets,
            {
                "Subject": {"alpha": 2, "beta": 1, "gamma": 1},
                "review_state": {"published": 2, "private": 1},
            },
        )

    def test_counts_only_what_the_user_may_see(self):
        logout()
        results = self.search({"SearchableText": "document", "facets": ["Subject"]})
        self.assertEqual(results.facets, {"Subject": {"alpha": 2, "beta": 1}})

    def test_only_keyword_indexes_are_counted(self):
        """Text indexes can't be counted, and allowedRolesAndUsers would
        disclose users and groups."""
        results = self.search(
            {
                "SearchableText": "document",
                "facets": [
                    "SearchableText",
                    "allowedRolesAndUsers",
                    "no_such_index",
                    "Subject",
                ],
            }
        )
        self.assertEqual(len(results), 3)
        self.assertEqual(list(results.facets), ["Subject"])

    def test_number_of_values_per_facet(self):
        results = self.search({"SearchableText": "document", "facets": {"Subject": 1}})
        self.assertEqual(results.facets, {"Subject": {"alpha": 2}})

    def test_no_facets_requested(self):
        (body,) = self.es_bodies({"SearchableText": "document"})
        self.assertNotIn("aggs", body)
        self.assertEqual(self.search({"SearchableText": "document"}).facets, {})

    def test_facets_are_counted_once(self):
        get_settings().bulk_size = 1
        bodies = self.es_bodies({"SearchableText": "document", "facets": ["Subject"]})
        self.assertEqual(len(bodies), 3)
        self.assertIn("aggs", bodies[0])
        self.assertNotIn("aggs", bodies[1])
        self.assertNotIn("aggs", bodies[2])


class TestFacetsAPI(BaseAPITest):
    def setUp(self):
        super().setUp()
        create_documents(self.layer["portal"])
        self.commit(wait=1)
        self.api_session = RelativeSession(self.layer["portal"].absolute_url())
        self.api_session.headers.update({"Accept": "application/json"})
        self.api_session.auth = (SITE_OWNER_NAME, SITE_OWNER_PASSWORD)

    def tearDown(self):
        self.api_session.close()
        super().tearDown()

    query = [
        {
            "i": "SearchableText",
            "o": "plone.app.querystring.operation.string.contains",
            "v": "document",
        }
    ]
    expected = {"Subject": {"alpha": 2, "beta": 1, "gamma": 1}}

    def test_querystring_search_post(self):
        response = self.api_session.post(
            "/@querystring-search", json={"query": self.query, "facets": ["Subject"]}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["items_total"], 3)
        self.assertEqual(response.json()["facets"], self.expected)

    def test_querystring_search_get(self):
        query = json.dumps({"query": self.query, "facets": ["Subject"]})
        response = self.api_session.get("/@querystring-search", params={"query": query})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["facets"], self.expected)

    def test_querystring_search_without_facets(self):
        response = self.api_session.post(
            "/@querystring-search", json={"query": self.query}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["items_total"], 3)
        self.assertNotIn("facets", response.json())

    def test_search(self):
        response = self.api_session.get(
            "/@search", params={"SearchableText": "document", "facets": "Subject"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["facets"], self.expected)
