from collective.elastic import migration
from collective.elastic.manager import ElasticSearchManager
from collective.elastic.testing import ElasticSearch_FUNCTIONAL_TESTING
from collective.elastic.tests import BaseFunctionalTest
from collective.elastic.utils import get_settings
from elasticsearch import Elasticsearch
from importlib.util import find_spec
from plone import api
from plone.app.contenttypes.testing import PLONE_APP_CONTENTTYPES_FIXTURE
from plone.app.testing import applyProfile
from plone.app.testing import FunctionalTesting
from plone.app.testing import PloneSandboxLayer
from plone.app.testing import setRoles
from plone.app.testing import TEST_USER_ID
from plone.browserlayer.interfaces import ILocalBrowserLayerType
from plone.browserlayer.utils import register_layer
from plone.registry import field
from plone.registry import Record
from plone.registry.interfaces import IRegistry
from Products.CMFCore.indexing import processQueue
from Products.CMFPlone.utils import get_installer
from unittest import mock
from zope.component import getUtility
from zope.interface.interface import InterfaceClass

import time
import transaction
import unittest


HAS_OLD_PACKAGE = find_spec("collective.elasticsearch") is not None


def old_layer_names(portal):
    return [
        registration.name
        for registration in portal.getSiteManager().registeredUtilities()
        if registration.provided is ILocalBrowserLayerType
    ]


class TestMigrationWithoutOldPackage(BaseFunctionalTest):
    """A site that still has collective.elasticsearch's settings, profile
    version and browser layer, while the package itself may already be gone."""

    def setUp(self):
        super().setUp()
        self.registry = getUtility(IRegistry)
        self.settings = get_settings()
        old = migration.OLD_PREFIX
        records = {
            "enabled": Record(field.Bool(), True),
            "bulk_size": Record(field.Int(), 7),
            # Stored as an int, the setting is a float now
            "timeout": Record(field.Int(), 9),
            # Stored as a list, the setting is a set now
            "es_only_indexes": Record(
                field.List(value_type=field.TextLine()),
                ["Title", "Description", "SearchableText", "Subject"],
            ),
            "highlight_pre_tags": Record(field.Text(), "<mark>"),
            # A setting of another version, without a counterpart
            "search_fields": Record(
                field.Set(value_type=field.TextLine()), {"Title", "Description"}
            ),
            # A value that doesn't fit the setting of that name
            "highlight_threshold": Record(field.TextLine(), "six hundred"),
        }
        for name, record in records.items():
            self.registry.records[old + name] = record
        self.setup = api.portal.get_tool("portal_setup")
        self.setup.setLastVersionForProfile(migration.OLD_PROFILE, "6")
        # The interface of a layer whose package is gone unpickles as a stand-in
        layer = InterfaceClass(
            "IElasticSearchLayer", __module__="collective.elasticsearch.interfaces"
        )
        register_layer(layer, migration.OLD_LAYER)

    def test_settings_are_copied(self):
        report = migration.migrate()
        self.assertEqual(self.settings.bulk_size, 7)
        self.assertEqual(self.settings.timeout, 9.0)
        self.assertIsInstance(self.settings.timeout, float)
        self.assertEqual(
            self.settings.es_only_indexes,
            {"Title", "Description", "SearchableText", "Subject"},
        )
        self.assertEqual(self.settings.highlight_pre_tags, "<mark>")
        self.assertTrue(self.settings.enabled)
        self.assertEqual(
            sorted(report.copied),
            [
                "bulk_size",
                "enabled",
                "es_only_indexes",
                "highlight_pre_tags",
                "timeout",
            ],
        )
        self.assertEqual(report.not_migrated, ["search_fields"])
        self.assertEqual(report.invalid, ["highlight_threshold"])
        self.assertEqual(self.settings.highlight_threshold, 600)

    def test_old_add_on_is_removed(self):
        migration.migrate()
        self.assertEqual(migration.old_records(self.registry), {})
        self.assertEqual(
            self.setup.getLastVersionForProfile(migration.OLD_PROFILE), "unknown"
        )
        self.assertNotIn(migration.OLD_LAYER, old_layer_names(self.portal))
        self.assertIn("collective.elastic", old_layer_names(self.portal))
        self.assertFalse(migration.is_installed())

    def test_running_again_changes_nothing(self):
        migration.migrate()
        self.settings.bulk_size = 8
        report = migration.migrate()
        self.assertEqual(report, migration.MigrationReport())
        self.assertEqual(self.settings.bulk_size, 8)

    def test_site_without_old_add_on(self):
        for name in migration.old_records(self.registry):
            del self.registry.records[migration.OLD_PREFIX + name]
        self.setup.unsetLastVersionForProfile(migration.OLD_PROFILE)
        self.assertFalse(migration.is_installed())
        self.assertEqual(migration.migrate(), migration.MigrationReport())


class BothPackages(PloneSandboxLayer):
    """collective.elasticsearch installed in the site, collective.elastic
    deployed next to it but not installed yet, as before a migration. Both are
    configured in one go, as at Zope startup, so conflicts would show."""

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
        applyProfile(portal, migration.OLD_PROFILE)
        setRoles(portal, TEST_USER_ID, ("Member", "Manager"))


if HAS_OLD_PACKAGE:
    BOTH_PACKAGES_FUNCTIONAL = FunctionalTesting(
        bases=(BothPackages(),), name="BothPackages:Functional"
    )
else:
    # The tests are skipped; a layer that is set up anyway
    BOTH_PACKAGES_FUNCTIONAL = ElasticSearch_FUNCTIONAL_TESTING


@unittest.skipUnless(HAS_OLD_PACKAGE, "collective.elasticsearch is not installed")
class TestMigrationFromInstalledPackage(unittest.TestCase):
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
        # Not through either add-on: their settings change during the test
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

    def test_old_add_on_keeps_working_before_the_migration(self):
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

    def test_install_moves_the_site_over(self):
        old_names = set(migration.old_records(self.registry))
        self.install()
        installer = get_installer(self.portal)
        self.assertFalse(installer.is_product_installed("collective.elasticsearch"))
        self.assertTrue(installer.is_product_installed("collective.elastic"))
        self.assertFalse(self.old.enabled)
        manager = ElasticSearchManager()
        self.assertTrue(manager.enabled)
        self.assertTrue(manager.active, "The existing index is used as it is")
        new_names = {
            key.removeprefix(migration.NEW_PREFIX)
            for key in self.registry.records.keys()
            if key.startswith(migration.NEW_PREFIX)
        }
        self.assertEqual(migration.old_records(self.registry), {})
        # Whatever the old version had, the shared settings were carried over
        self.assertTrue(old_names & new_names)

    def test_search_and_indexing_after_the_migration(self):
        self.install()
        self.assertEqual(self.search("indexed"), ["before"])
        api.content.create(self.portal, "Document", "after", title="Indexed after")
        self.commit()
        self.assertEqual(sorted(self.search("indexed")), ["after", "before"])

    def test_nothing_refers_to_the_old_package(self):
        """After the migration the package can be removed: no persistent
        registration or setting refers to it."""
        self.install()
        site_manager = self.portal.getSiteManager()
        for registration in site_manager.registeredUtilities():
            for obj in (registration.component, registration.provided):
                module = getattr(obj, "__module__", "") or ""
                self.assertFalse(module.startswith("collective.elasticsearch"), obj)
        for key in self.registry.records.keys():
            self.assertFalse(key.startswith("collective.elasticsearch"), key)
            interface = self.registry.records[key].interfaceName or ""
            self.assertFalse(interface.startswith("collective.elasticsearch"), key)
        setup = api.portal.get_tool("portal_setup")
        self.assertEqual(
            setup.getLastVersionForProfile(migration.OLD_PROFILE), "unknown"
        )
