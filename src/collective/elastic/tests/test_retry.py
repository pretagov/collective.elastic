from collective.elastic import manager
from collective.elastic.tests import BaseFunctionalTest
from collective.elastic.tests.stale_proxy import StaleProxy
from collective.elastic.utils import get_settings
from elasticsearch import exceptions
from plone import api
from unittest import mock


# The error elasticsearch answers a bad request with
RequestError = getattr(exceptions, "ApiError", None) or exceptions.RequestError


class TestSearchRetry(BaseFunctionalTest):
    """Searches are retried after a delay when the cluster can't be reached,
    e.g. while the network recovers after the host resumed from suspension."""

    def setUp(self):
        super().setUp()
        api.content.create(self.portal, "Document", "doc", title="Retry me")
        self.commit(wait=1)
        self.settings = get_settings()
        self.previous_hosts = list(self.settings.hosts)
        self.proxy = StaleProxy()
        self.settings.hosts = [self.proxy.url]
        self.settings.timeout = 0.5
        self.settings.retry_delay = 0.0
        self.es._reset_connection()
        # Leave an open connection in the client's pool
        self.assertEqual(self.ids(), ["doc"])

    def tearDown(self):
        self.settings.hosts = self.previous_hosts
        self.es._reset_connection()
        self.proxy.close()
        super().tearDown()

    def ids(self):
        return [brain.getId for brain in self.search({"SearchableText": "retry"})]

    def count_searches(self):
        return mock.patch.object(manager, "es_search", wraps=manager.es_search)

    def test_retrying_is_off_by_default(self):
        self.assertEqual(self.es.retry_attempts, 0)
        self.proxy.outage(60)
        with self.count_searches() as es_search:
            with self.assertRaises(
                (exceptions.ConnectionError, exceptions.ConnectionTimeout)
            ):
                self.ids()
        self.assertEqual(es_search.call_count, 1)

    def test_outage_shorter_than_the_delay_is_bridged(self):
        """The client's own retries come straight away and fail as well."""
        self.settings.retry_attempts = 1
        self.settings.retry_delay = 2.0
        self.proxy.outage(1.5)
        with self.count_searches() as es_search:
            self.assertEqual(self.ids(), ["doc"])
        self.assertEqual(es_search.call_count, 2)

    def test_last_error_is_raised_when_all_attempts_fail(self):
        self.settings.retry_attempts = 2
        self.proxy.outage(60)
        with self.count_searches() as es_search:
            with self.assertRaises(
                (exceptions.ConnectionError, exceptions.ConnectionTimeout)
            ):
                self.ids()
        self.assertEqual(es_search.call_count, 3)

    def test_retry_uses_a_new_client(self):
        """A retry starts from a new client: new sockets, and no node list
        learned by sniffing, which may be out of date after a resume."""
        self.settings.retry_on_timeout = False
        self.settings.retry_attempts = 1
        self.es._reset_connection()
        self.assertEqual(self.ids(), ["doc"])
        client = self.es.connection
        self.proxy.make_existing_stale("blackhole")
        with self.count_searches() as es_search:
            self.assertEqual(self.ids(), ["doc"])
        self.assertEqual(es_search.call_count, 2)
        self.assertIs(es_search.call_args_list[0].args[0], client)
        self.assertIsNot(es_search.call_args_list[1].args[0], client)

    def test_request_errors_are_not_retried(self):
        self.settings.retry_attempts = 2
        with self.count_searches() as es_search:
            with self.assertRaises(RequestError):
                # Title is a text field, which elasticsearch can't sort on
                self.search({"SearchableText": "retry", "sort_on": "Title"})
        self.assertEqual(es_search.call_count, 1)
