from collective.elastic import local
from collective.elastic import utils
from collective.elastic.browser.controlpanel import ElasticControlPanelView
from collective.elastic.manager import ElasticSearchManager
from collective.elastic.tests import BaseFunctionalTest
from collective.elastic.tests import BaseRedisTest
from unittest import mock

import os
import socket


ENV_FOR_REDIS = {
    "PLONE_REDIS_DSN": "",
    "PLONE_BACKEND": "",
    "PLONE_USERNAME": "",
    "PLONE_PASSWORD": "",
}


class TestControlPanel(BaseRedisTest):
    def test_use_redis_checkbox_is_disabled_enabled(self):
        controlpanel = ElasticControlPanelView(self.portal, self.request)
        controlpanel.update()

        self.assertIsNone(controlpanel.form_instance.widgets["use_redis"].disabled)

        with mock.patch.dict(os.environ, ENV_FOR_REDIS):
            controlpanel.update()
            self.assertEqual(
                "disabled", controlpanel.form_instance.widgets["use_redis"].disabled
            )


class UnresponsiveServer:
    """A socket that accepts connections but never answers, like a cluster
    behind a firewall that drops packets."""

    def __enter__(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(16)
        return f"http://127.0.0.1:{self.socket.getsockname()[1]}"

    def __exit__(self, *exc):
        self.socket.close()


def closed_port_url():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


class TestControlPanelWithoutCluster(BaseFunctionalTest):
    """The control panel is where the hosts are fixed, so it has to render when
    the configured cluster is down or doesn't answer."""

    def setUp(self):
        super().setUp()
        self.settings = utils.get_settings()
        self.previous = (list(self.settings.hosts), self.settings.timeout)

    def tearDown(self):
        self.settings.hosts, self.settings.timeout = self.previous
        self.reset_connection()
        super().tearDown()

    def reset_connection(self):
        local.set_local(ElasticSearchManager.connection_key, None)

    def use_hosts(self, url):
        self.settings.hosts = [url]
        self.settings.timeout = 0.5
        self.settings.retry_on_timeout = False
        self.reset_connection()

    def render(self):
        return ElasticControlPanelView(self.portal, self.request)()

    def test_renders_when_cluster_does_not_answer(self):
        with UnresponsiveServer() as url:
            self.use_hosts(url)
            self.assertIn("Could not connect", self.render())

    def test_renders_when_cluster_refuses_connections(self):
        self.use_hosts(closed_port_url())
        self.assertIn("Could not connect", self.render())

    def test_info_is_empty_when_cluster_does_not_answer(self):
        with UnresponsiveServer() as url:
            self.use_hosts(url)
            self.assertEqual(ElasticSearchManager().info, [])

    def test_data_sync_without_index_stats(self):
        """Without stats for the index, info has no document counts."""
        info = [
            ("Cluster Name", "cluster"),
            ("Elastic Search Version", "8"),
            ("Number of docs (Catalog)", 3),
        ]
        with mock.patch.object(
            ElasticSearchManager, "info", new_callable=mock.PropertyMock
        ) as mock_info:
            mock_info.return_value = info
            wrapper = ElasticControlPanelView(self.portal, self.request)
            self.assertEqual(
                wrapper.enable_data_sync, {"elastic_docs": 0, "catalog_objs": 3}
            )
