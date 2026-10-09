from collective.elastic.manager import ElasticSearchManager
from collective.elastic.testing import ElasticSearch_FUNCTIONAL_TESTING
from elasticsearch import Elasticsearch
from importlib.util import find_spec
from plone import api
from plone.app.contenttypes.testing import PLONE_APP_CONTENTTYPES_FIXTURE
from plone.app.testing import applyProfile
from plone.app.testing import FunctionalTesting
from plone.app.testing import PloneSandboxLayer
from plone.app.testing import setRoles
from plone.app.testing import TEST_USER_ID
from plone.registry.interfaces import IRegistry
from Products.CMFCore.indexing import processQueue
from Products.CMFPlone.utils import get_installer
from unittest import mock
from zope.component import getUtility

import time
import transaction
import unittest


OLD_PACKAGE = "collective.elasticsearch"
HAS_OLD_PACKAGE = find_spec(OLD_PACKAGE) is not None


class BothPackages(PloneSandboxLayer):
    """collective.elasticsearch installed in the site, collective.elastic
    deployed next to it but not installed. Both are configured in one go, as
    at Zope startup, so conflicts would show."""

    defaultBases = (PLONE_APP_CONTENTTYPES_FIXTURE,)

    def setUpZope(self, app, configurationContext):
        from zope.configuration import xmlconfig

        xmlconfig.string(
            """<configure xmlns="http://namespaces.zope.org/zope">
            <include package="collective.elasticsearch" />
            <include package="collective.elastic" />
            </configure>""",
            context=configurationContext,
        )

    def setUpPloneSite(self, portal):
        applyProfile(portal, f"{OLD_PACKAGE}:default")
        setRoles(portal, TEST_USER_ID, ("Member", "Manager"))


if HAS_OLD_PACKAGE:
    BOTH_PACKAGES_FUNCTIONAL = FunctionalTesting(
        bases=(BothPackages(),), name="BothPackages:Functional"
    )
else:
    # The tests are skipped; a layer that is set up anyway
    BOTH_PACKAGES_FUNCTIONAL = ElasticSearch_FUNCTIONAL_TESTING


class BothPackagesTest(unittest.TestCase):
    """collective.elasticsearch enabled, with its index built."""

    layer = BOTH_PACKAGES_FUNCTIONAL

    def setUp(self):
        from collective.elasticsearch import utils as old_utils
        from collective.elasticsearch.manager import ElasticSearchManager as Old

        self.portal = self.layer["portal"]
        self.catalog = api.portal.get_tool("portal_catalog")
        self.registry = getUtility(IRegistry)
        settings = old_utils.get_settings()
        settings.enabled = True
        settings.sniffer_timeout = 0.0
        settings.raise_search_exception = True
        self.old = Old()
        for _ in range(20):
            try:
                if self.old.connection.ping():
                    break
            except Exception:  # NOQA W0703
                pass
            time.sleep(1)
        self.catalog._elasticcustomindex = "plone-test-index"
        self.catalog.manage_catalogRebuild()
        api.content.create(self.portal, "Document", "before", title="Indexed before")
        self.commit()

    def tearDown(self):
        # Not through either add-on: their settings change during the tests
        conn = Elasticsearch(["http://127.0.0.1:9200"])
        for name in conn.indices.get(index="plone-test-index*"):
            conn.indices.delete(index=name)

    def commit(self):
        processQueue()
        transaction.commit()
        Elasticsearch(["http://127.0.0.1:9200"]).indices.refresh(
            index="plone-test-index*"
        )

    def install(self):
        get_installer(self.portal).install_product("collective.elastic")
        self.commit()

    def search(self, text):
        return [brain.getId for brain in self.catalog(SearchableText=text)]

    def assertOldAddOnWorks(self):
        """collective.elastic's patches are applied last, and fall through to
        collective.elasticsearch's while collective.elastic isn't active."""
        from collective.elasticsearch.manager import ElasticSearchManager as Old

        self.assertFalse(ElasticSearchManager().enabled)
        self.assertTrue(self.old.active)
        # Indexed by collective.elasticsearch's queue processor
        api.content.create(self.portal, "Document", "more", title="Indexed more")
        self.commit()
        with mock.patch.object(
            Old, "search_results", autospec=True, side_effect=Old.search_results
        ) as search_results:
            self.assertEqual(sorted(self.search("indexed")), ["before", "more"])
        self.assertEqual(search_results.call_count, 1)
        # Found in elasticsearch, not only in the catalog
        count = self.old.connection.count(index="plone-test-index", q="Title:more")
        self.assertEqual(count["count"], 1)


@unittest.skipUnless(HAS_OLD_PACKAGE, f"{OLD_PACKAGE} is not installed")
class TestCoexistence(BothPackagesTest):
    def test_old_add_on_keeps_working_next_to_it(self):
        self.assertOldAddOnWorks()

    def test_installing_leaves_the_old_add_on_alone(self):
        self.install()
        installer = get_installer(self.portal)
        self.assertTrue(installer.is_product_installed(OLD_PACKAGE))
        self.assertTrue(installer.is_product_installed("collective.elastic"))
        self.assertOldAddOnWorks()
