from collective.elastic import utils
from collective.elastic.compat import IS_ES_8
from collective.elastic.manager import ElasticSearchManager
from collective.elastic.manager import get_connection
from collective.elastic.tests import BaseFunctionalTest

import unittest


def client_hosts(conn):
    if IS_ES_8:
        return [node.base_url for node in conn.transport.node_pool.all()]
    return [f"http://{host['host']}:{host['port']}" for host in conn.transport.hosts]


class TestGetConnection(unittest.TestCase):
    def test_same_settings_reuse_the_client(self):
        params = {"retry_on_timeout": True}
        conn = get_connection(["http://es-one:9200"], params)
        self.assertIs(get_connection(["http://es-one:9200"], dict(params)), conn)

    def test_changed_hosts_build_a_new_client(self):
        conn = get_connection(["http://es-one:9200"], {})
        rebuilt = get_connection(["http://es-two:9200"], {})
        self.assertIsNot(rebuilt, conn)
        self.assertEqual(client_hosts(rebuilt), ["http://es-two:9200"])

    def test_changed_params_build_a_new_client(self):
        conn = get_connection(["http://es-one:9200"], {"retry_on_timeout": True})
        rebuilt = get_connection(["http://es-one:9200"], {"retry_on_timeout": False})
        self.assertIsNot(rebuilt, conn)


class TestManagerConnection(BaseFunctionalTest):
    """The manager's client follows the registry settings, so saving new hosts
    in the control panel takes effect without a restart."""

    def test_connection_is_cached_while_settings_are_unchanged(self):
        es = ElasticSearchManager()
        self.assertIs(es.connection, es.connection)

    def test_connection_follows_changed_hosts(self):
        es = ElasticSearchManager()
        original = es.connection
        settings = utils.get_settings()
        previous_hosts = list(settings.hosts)
        try:
            settings.hosts = ["http://elasticsearch-elsewhere:9200"]
            self.assertEqual(
                client_hosts(es.connection), ["http://elasticsearch-elsewhere:9200"]
            )
        finally:
            settings.hosts = previous_hosts
        self.assertIsNot(es.connection, original)
        self.assertTrue(es.connection.ping())
