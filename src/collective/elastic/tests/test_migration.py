from collective.elastic import migration
from collective.elastic.manager import ElasticSearchManager
from collective.elastic.migration.subscribers import OldPackageRunning
from collective.elastic.tests import BaseFunctionalTest
from collective.elastic.tests.test_coexistence import BothPackagesTest
from collective.elastic.tests.test_coexistence import HAS_OLD_PACKAGE
from collective.elastic.utils import get_settings
from contextlib import contextmanager
from plone import api
from plone.app.testing import SITE_OWNER_NAME
from plone.app.testing import SITE_OWNER_PASSWORD
from plone.browserlayer.interfaces import ILocalBrowserLayerType
from plone.browserlayer.utils import register_layer
from plone.registry import field
from plone.registry import Record
from plone.registry.interfaces import IRegistry
from plone.testing.zope import Browser
from Products.CMFPlone.utils import get_installer
from zope.component import getUtility
from zope.interface import Interface

import re
import transaction
import unittest


class IOldLayer(Interface):
    """Stands in for collective.elasticsearch's browser layer."""


def layer_names(portal):
    return [
        registration.name
        for registration in portal.getSiteManager().registeredUtilities()
        if registration.provided is ILocalBrowserLayerType
    ]


def manager_browser(layer):
    browser = Browser(layer["app"])
    browser.handleErrors = False
    browser.addHeader("Authorization", f"Basic {SITE_OWNER_NAME}:{SITE_OWNER_PASSWORD}")
    return browser


def run_form(browser, portal, copy=True, switch_over=False, uninstall=False):
    browser.open(f"{portal.absolute_url()}/@@ce-migration")
    for name, value in (
        ("copy_settings", copy),
        ("switch_over", switch_over),
        ("uninstall", uninstall),
    ):
        control = browser.getControl(name=f"form.widgets.{name}:list")
        control.value = ["selected"] if value else []
    browser.getControl("Run the migration").click()
    # See what the request committed
    transaction.begin()


class TestMigrationWithoutOldPackage(BaseFunctionalTest):
    """A site with collective.elasticsearch's settings, profile version and
    browser layer, while the package itself isn't deployed (any more)."""

    def setUp(self):
        super().setUp()
        self.registry = getUtility(IRegistry)
        self.settings = get_settings()
        self.settings.enabled = False
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
            # The same value in both
            "highlight": Record(field.Bool(), False),
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
        # Whatever the interface, the layer is found by its name
        register_layer(IOldLayer, migration.OLD_LAYER)
        self.commit()

    def old_enabled(self):
        return self.registry.records[migration.OLD_PREFIX + "enabled"].value

    @contextmanager
    def missing_index(self):
        self.catalog._elasticcustomindex = "no-such-index"
        self.commit()
        try:
            yield
        finally:
            # For the tear down
            transaction.abort()
            self.catalog._elasticcustomindex = "plone-test-index"
            self.commit()

    def test_plan(self):
        plans = {plan.name: plan.action for plan in migration.plan_settings()}
        self.assertEqual(
            plans,
            {
                "bulk_size": migration.COPY,
                "es_only_indexes": migration.COPY,
                "highlight": migration.SAME,
                "highlight_pre_tags": migration.COPY,
                "highlight_threshold": migration.INVALID,
                "search_fields": migration.NO_COUNTERPART,
                "timeout": migration.COPY,
            },
        )
        self.assertEqual(self.settings.bulk_size, 50, "The plan changes nothing")

    def test_settings_are_copied(self):
        report = migration.migrate(copy=True)
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(self.settings.bulk_size, 7)
        self.assertEqual(self.settings.timeout, 9.0)
        self.assertIsInstance(self.settings.timeout, float)
        self.assertEqual(
            self.settings.es_only_indexes,
            {"Title", "Description", "SearchableText", "Subject"},
        )
        self.assertEqual(self.settings.highlight_pre_tags, "<mark>")
        self.assertEqual(
            sorted(report.copied),
            [
                "bulk_size",
                "es_only_indexes",
                "highlight",
                "highlight_pre_tags",
                "timeout",
            ],
        )
        self.assertEqual(report.not_migrated, ["search_fields"])
        self.assertEqual(report.invalid, ["highlight_threshold"])
        self.assertEqual(self.settings.highlight_threshold, 600)
        # Only the switch changes them
        self.assertFalse(self.settings.enabled)
        self.assertTrue(self.old_enabled())
        self.assertFalse(report.switched)
        self.assertFalse(report.uninstalled)

    def test_switch_over_uses_the_existing_index(self):
        self.es.connection.indices.refresh(index="plone-test-index")
        report = migration.migrate(copy=False, switch_over=True)
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.warnings, [])
        self.assertTrue(report.switched)
        self.assertFalse(self.old_enabled())
        self.assertTrue(self.settings.enabled)
        self.assertTrue(ElasticSearchManager().active)

    def test_failed_check_changes_nothing(self):
        with self.missing_index():
            report = migration.migrate(copy=True, switch_over=True, uninstall=True)
        self.assertFalse(report.ok)
        self.assertEqual(report.errors, ["The index no-such-index doesn't exist."])
        self.assertEqual(self.settings.bulk_size, 50)
        self.assertFalse(self.settings.enabled)
        self.assertTrue(self.old_enabled())
        self.assertTrue(migration.is_pending())

    def test_unreachable_cluster_changes_nothing(self):
        self.registry.records[migration.OLD_PREFIX + "hosts"] = Record(
            field.List(value_type=field.TextLine()), ["http://127.0.0.1:1"]
        )
        self.registry.records[migration.OLD_PREFIX + "timeout"].value = 1
        report = migration.migrate(copy=True, switch_over=True)
        self.assertFalse(report.ok)
        self.assertTrue(report.errors[0].startswith("Can't connect to"), report.errors)
        self.assertEqual(self.settings.hosts, ["127.0.0.1"])
        self.assertFalse(self.settings.enabled)

    def test_uninstall_removes_the_old_add_on(self):
        report = migration.migrate(copy=False, uninstall=True)
        self.assertTrue(report.uninstalled)
        self.assertEqual(migration.old_records(), {})
        self.assertEqual(
            self.setup.getLastVersionForProfile(migration.OLD_PROFILE), "unknown"
        )
        self.assertNotIn(migration.OLD_LAYER, layer_names(self.portal))
        self.assertIn("collective.elastic", layer_names(self.portal))
        self.assertFalse(migration.is_pending())

    def test_running_again_changes_nothing(self):
        migration.migrate(copy=True, switch_over=True, uninstall=True)
        self.settings.bulk_size = 8
        report = migration.migrate(copy=True, switch_over=True, uninstall=True)
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.copied, [])
        self.assertEqual(self.settings.bulk_size, 8)
        self.assertTrue(self.settings.enabled)

    def test_form(self):
        browser = manager_browser(self.layer)
        browser.open(f"{self.portal.absolute_url()}/@@ce-migration")
        settings = browser.contents.split('id="ce-migration-settings"')[1]
        self.assertIn("search_fields", settings)
        self.assertIn("no such setting, not copied", settings)
        run_form(browser, self.portal, copy=True, switch_over=True)
        self.assertIn("The migration ran.", browser.contents)
        self.assertIn("Switched over", browser.contents)
        self.assertEqual(self.settings.bulk_size, 7)
        self.assertTrue(self.settings.enabled)
        self.assertFalse(self.old_enabled())

    def test_form_reports_a_failure(self):
        browser = manager_browser(self.layer)
        with self.missing_index():
            run_form(browser, self.portal, copy=True, switch_over=True)
        self.assertIn("nothing was changed", browser.contents)
        self.assertIn("The index no-such-index doesn", browser.contents)
        self.assertEqual(self.settings.bulk_size, 50)
        self.assertTrue(self.old_enabled())

    def test_controlpanel_mentions_the_settings_left(self):
        browser = manager_browser(self.layer)
        browser.open(f"{self.portal.absolute_url()}/@@elastic-controlpanel")
        self.assertIn("still has collective.elasticsearch settings", browser.contents)
        self.assertIn("/@@ce-migration", browser.contents)
        # The package isn't running, so collective.elastic can be enabled
        enabled = browser.getControl(name="form.widgets.enabled:list")
        self.assertFalse(enabled.disabled)

    def test_site_without_old_add_on(self):
        migration.migrate(copy=False, uninstall=True)
        self.commit()
        browser = manager_browser(self.layer)
        browser.open(f"{self.portal.absolute_url()}/@@elastic-controlpanel")
        self.assertNotIn("ce-migration-warning", browser.contents)
        browser.open(f"{self.portal.absolute_url()}/@@ce-migration")
        self.assertIn("nothing of collective.elasticsearch left", browser.contents)


@unittest.skipUnless(HAS_OLD_PACKAGE, "collective.elasticsearch is not installed")
class TestMigrationFromInstalledPackage(BothPackagesTest):
    def setUp(self):
        super().setUp()
        self.install()

    def test_enabling_is_refused_while_the_old_add_on_runs(self):
        with self.assertRaises(OldPackageRunning):
            get_settings().enabled = True

    def test_controlpanel_points_to_the_migration(self):
        browser = manager_browser(self.layer)
        browser.open(f"{self.portal.absolute_url()}/@@elastic-controlpanel")
        warning = re.search(
            r'<div class="portalMessage warning"\s+id="ce-migration-warning".*?</div>',
            browser.contents,
            re.S,
        )
        self.assertIsNotNone(warning, browser.contents)
        self.assertIn("can't be enabled", warning.group())
        self.assertIn(
            f'href="{self.portal.absolute_url()}/@@ce-migration"', warning.group()
        )
        enabled = browser.getControl(name="form.widgets.enabled:list")
        self.assertTrue(enabled.disabled)

    def test_migration_moves_the_site_over(self):
        old_names = set(migration.old_records())
        report = migration.migrate(copy=True, switch_over=True, uninstall=True)
        self.commit()
        self.assertTrue(report.ok, report.errors)
        self.assertEqual(report.warnings, [])
        # Whatever the old version had, the shared settings were carried over
        self.assertTrue(set(report.copied) & old_names)
        installer = get_installer(self.portal)
        self.assertFalse(installer.is_product_installed("collective.elasticsearch"))
        self.assertTrue(installer.is_product_installed("collective.elastic"))
        self.assertFalse(self.old.enabled)
        self.assertTrue(ElasticSearchManager().active)
        self.assertEqual(self.search("indexed"), ["before"])
        api.content.create(self.portal, "Document", "after", title="Indexed after")
        self.commit()
        self.assertEqual(sorted(self.search("indexed")), ["after", "before"])

    def test_nothing_refers_to_the_old_package(self):
        """After the migration the package can be removed: no persistent
        registration or setting refers to it."""
        migration.migrate(copy=True, switch_over=True, uninstall=True)
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

    def test_uninstall_waits_for_the_switch(self):
        report = migration.migrate(copy=True, uninstall=True)
        self.assertFalse(report.ok)
        installer = get_installer(self.portal)
        self.assertTrue(installer.is_product_installed("collective.elasticsearch"))
        self.assertOldAddOnWorks()

    def test_form_runs_the_migration(self):
        browser = manager_browser(self.layer)
        run_form(browser, self.portal, copy=True, switch_over=True, uninstall=True)
        self.assertIn("The migration ran.", browser.contents)
        installer = get_installer(self.portal)
        self.assertFalse(installer.is_product_installed("collective.elasticsearch"))
        self.assertTrue(ElasticSearchManager().active)
