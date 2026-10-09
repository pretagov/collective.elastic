"""Move a site from collective.elasticsearch to collective.elastic.

Both packages can be deployed side by side. Nothing happens on install: a
manager runs the steps from the ``@@ce-migration`` form, each on its own.

Copy the settings
    Every ``collective.elasticsearch.interfaces.IElasticSettings.<name>``
    value goes to the ``collective.elastic`` setting of the same name, if
    there is one and the value is valid for it. Nothing here names a setting
    (apart from ``enabled``, which only the switch changes), so this works
    for any version of collective.elasticsearch, including forks with
    settings of their own.

Switch over
    Checks that collective.elastic, with its settings, reaches the cluster and
    can search the index collective.elasticsearch built, then disables
    collective.elasticsearch and enables collective.elastic. The index is used
    as it is: the catalog attributes that record the conversion and the index
    name are the same in both packages.

Uninstall collective.elasticsearch
    Once it is disabled: uninstalls it and removes its settings, browser layer
    and profile version, so the package can be taken out of the deployment.
    Pending Redis jobs of collective.elasticsearch refer to its code; let the
    queue drain first.

The checks run before anything is written, with the settings as they will
be after the copy. If one fails, nothing changes.
"""

from collective.elastic import logger
from collective.elastic import utils
from collective.elastic.indexes import getIndex
from collective.elastic.interfaces import IElasticSettings
from collective.elastic.manager import ElasticSearchManager
from dataclasses import dataclass
from dataclasses import field
from elasticsearch import Elasticsearch
from plone import api
from plone.registry.interfaces import IRegistry
from Products.CMFPlone.utils import get_installer
from zope import schema
from zope.component import getUtility

import transaction


PACKAGE = "collective.elastic"
OLD_PACKAGE = "collective.elasticsearch"
OLD_PROFILE = f"{OLD_PACKAGE}:default"
OLD_LAYER = OLD_PACKAGE
OLD_PREFIX = f"{OLD_PACKAGE}.interfaces.IElasticSettings."
NEW_PREFIX = f"{IElasticSettings.__identifier__}."
_OLD_PREFIX_LENGTH = len(OLD_PREFIX)
# Changed by the switch only, never copied
ENABLED = "enabled"
UNKNOWN = "unknown"

COPY = "copy"
SAME = "same"
NO_COUNTERPART = "no counterpart"
INVALID = "invalid"


@dataclass
class SettingPlan:
    name: str
    old_value: object
    new_value: object = None
    action: str = COPY
    reason: str = ""


@dataclass
class MigrationReport:
    copied: list = field(default_factory=list)
    not_migrated: list = field(default_factory=list)
    invalid: list = field(default_factory=list)
    switched: bool = False
    uninstalled: bool = False
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def ok(self):
        return not self.errors


def _registry():
    return getUtility(IRegistry)


def old_records(registry=None) -> dict:
    """The collective.elasticsearch settings, by name."""
    registry = registry or _registry()
    return {
        key[_OLD_PREFIX_LENGTH:]: registry.records[key]
        for key in list(registry.records.keys())
        if key.startswith(OLD_PREFIX)
    }


def is_installed() -> bool:
    """Whether collective.elastic is installed in the site."""
    return (NEW_PREFIX + ENABLED) in _registry().records


def old_is_installed() -> bool:
    """Whether collective.elasticsearch is deployed and installed."""
    return get_installer(api.portal.get()).is_product_installed(OLD_PACKAGE)


def old_is_enabled() -> bool:
    """Whether collective.elasticsearch's Enabled setting is on."""
    record = _registry().records.get(OLD_PREFIX + ENABLED)
    return bool(record is not None and record.value)


def old_is_running() -> bool:
    """Whether collective.elasticsearch is installed and enabled. While it
    is, collective.elastic can't be enabled: both would index and search."""
    return old_is_installed() and old_is_enabled()


def is_pending() -> bool:
    """Whether the site has anything of collective.elasticsearch left."""
    setup = api.portal.get_tool("portal_setup")
    return bool(old_records()) or (
        setup.getLastVersionForProfile(OLD_PROFILE) != UNKNOWN
    )


def convert(field, value):
    """Fit a value to a field where the types merely differ, e.g. a list
    stored for what is now a set."""
    if value is None:
        return value
    if isinstance(value, bytes) and isinstance(field, schema.interfaces.IText):
        return value.decode("utf-8")
    if isinstance(value, (list, tuple, set, frozenset)):
        value_type = getattr(field, "value_type", None)
        if value_type is not None:
            value = [convert(value_type, item) for item in value]
        if isinstance(field, schema.FrozenSet):
            return frozenset(value)
        if isinstance(field, schema.Set):
            return set(value)
        if isinstance(field, schema.Tuple):
            return tuple(value)
        if isinstance(field, schema.List):
            return list(value)
        return value
    if isinstance(field, schema.Float) and isinstance(value, int):
        return float(value)
    if isinstance(field, schema.Int) and isinstance(value, float):
        if value.is_integer():
            return int(value)
    if isinstance(field, schema.interfaces.IText) and not isinstance(value, str):
        return str(value)
    return value


def plan_settings(registry=None) -> list:
    """What copying the settings would do, setting by setting."""
    registry = registry or _registry()
    plans = []
    for name, old in sorted(old_records(registry).items()):
        if name == ENABLED:
            continue
        plan = SettingPlan(name, old.value)
        plans.append(plan)
        new_key = NEW_PREFIX + name
        if new_key not in registry.records:
            plan.action = NO_COUNTERPART
            continue
        record = registry.records[new_key]
        plan.new_value = record.value
        try:
            value = convert(record.field, old.value)
            record.field.validate(value)
        except (schema.ValidationError, TypeError, ValueError) as exc:
            plan.action = INVALID
            plan.reason = repr(exc)
            continue
        if value == record.value:
            plan.action = SAME
    return plans


def copy_settings(report: MigrationReport, registry=None):
    registry = registry or _registry()
    for plan in plan_settings(registry):
        if plan.action == NO_COUNTERPART:
            report.not_migrated.append(plan.name)
            continue
        if plan.action == INVALID:
            logger.warning(
                f"Not copying setting {plan.name}={plan.old_value!r}: {plan.reason}"
            )
            report.invalid.append(plan.name)
            continue
        record = registry.records[NEW_PREFIX + plan.name]
        expected = convert(record.field, plan.old_value)
        record.value = expected
        if registry.records[NEW_PREFIX + plan.name].value != expected:
            report.errors.append(f"The setting {plan.name} wasn't copied.")
        else:
            report.copied.append(plan.name)


class _PlannedSettings:
    """collective.elastic's settings as they will be after the copy."""

    def __init__(self, copy):
        self._settings = utils.get_settings()
        registry = _registry()
        self._planned = {}
        if copy:
            for plan in plan_settings(registry):
                if plan.action == COPY:
                    record = registry.records[NEW_PREFIX + plan.name]
                    self._planned[plan.name] = convert(record.field, plan.old_value)

    def __getattr__(self, name):
        if name in self._planned:
            return self._planned[name]
        return getattr(self._settings, name)


class _Probe(ElasticSearchManager):
    """collective.elastic's manager on a client of its own, whatever an
    instance has cached."""

    def __init__(self, connection):
        self._connection = connection

    @property
    def connection(self):
        return self._connection


def check_index(report: MigrationReport, copy=True):
    """Check that collective.elastic, with its settings as they will be after
    the copy, can use the index collective.elasticsearch built. Failures go
    to the report's errors, differences that only mean a Convert or
    Synchronize is due afterwards to its warnings."""
    settings = _PlannedSettings(copy)
    hosts = utils.normalize_hosts(settings.hosts)
    params = utils.get_connection_params(settings)
    probe = _Probe(Elasticsearch(hosts, **params))
    catalog = probe.catalog
    if not probe.catalog_converted:
        report.warnings.append(
            "The catalog was never converted, so there is no index to take "
            "over: convert it in the Elastic Search control panel."
        )
        return
    conn = probe.connection
    try:
        conn.info()
    except Exception as exc:  # NOQA W0703
        report.errors.append(f"Can't connect to {', '.join(hosts)}: {exc}")
        return
    index_name = probe.real_index_name
    if not conn.indices.exists(index=index_name):
        report.errors.append(f"The index {index_name} doesn't exist.")
        return

    es_docs = conn.count(index=index_name)["count"]
    catalog_docs = len(catalog._catalog.getIndex("UID"))
    if es_docs != catalog_docs:
        report.warnings.append(
            f"The index has {es_docs} documents, the catalog {catalog_docs}: "
            "synchronize them in the Elastic Search control panel."
        )

    response = conn.indices.get_mapping(index=index_name)
    mappings = getattr(response, "body", response)
    properties = {}
    for value in mappings.values():
        properties.update(value.get("mappings", {}).get("properties", {}))
    zcatalog = catalog._catalog
    for name in sorted(zcatalog.indexes.keys()):
        index = getIndex(zcatalog, name)
        if index is None:
            continue
        expected = index.create_mapping(name).get("type")
        actual = properties.get(name, {}).get("type")
        if name not in properties:
            report.warnings.append(
                f"The index has no mapping for {name}: convert the catalog "
                "in the Elastic Search control panel."
            )
        elif expected and actual and expected != actual:
            report.warnings.append(
                f"{name} is mapped as {actual}, collective.elastic maps it "
                f"as {expected}: rebuild the index to change it."
            )

    path = "/".join(api.portal.get().getPhysicalPath())
    try:
        results = probe.search({"path": {"query": path}})
        found = len(results)
        if found:
            results[0]
    except Exception as exc:  # NOQA W0703
        logger.warning("Searching the index failed", exc_info=True)
        report.errors.append(f"Searching the index failed: {exc!r}")
        return
    if es_docs and not found:
        report.errors.append("Searching the index found nothing.")


def switch(report: MigrationReport, registry=None):
    """Disable collective.elasticsearch and enable collective.elastic."""
    registry = registry or _registry()
    old_enabled = registry.records.get(OLD_PREFIX + ENABLED)
    if old_enabled is not None:
        # First, or collective.elastic can't be enabled
        old_enabled.value = False
    registry.records[NEW_PREFIX + ENABLED].value = True
    manager = ElasticSearchManager()
    if manager.catalog_converted and not manager.active:
        report.errors.append("collective.elastic didn't become active.")
        return
    report.switched = True


def uninstall_old(report: MigrationReport, registry=None):
    """Uninstall collective.elasticsearch and remove what it leaves behind."""
    registry = registry or _registry()
    portal = api.portal.get()
    installer = get_installer(portal)
    if installer.is_product_installed(OLD_PACKAGE):
        # The package is still there: let it uninstall itself
        installer.uninstall_product(OLD_PACKAGE)
    _remove_layer(portal)
    setup = api.portal.get_tool("portal_setup")
    if setup.getLastVersionForProfile(OLD_PROFILE) != UNKNOWN:
        setup.unsetLastVersionForProfile(OLD_PROFILE)
    # Its uninstall profile leaves the settings in the registry
    for name in old_records(registry):
        del registry.records[OLD_PREFIX + name]
    report.uninstalled = True


def _remove_layer(portal):
    """Unregister the browser layer, also when the package (and with it the
    layer's interface) is gone already."""
    from plone.browserlayer.interfaces import ILocalBrowserLayerType

    site_manager = portal.getSiteManager()
    for registration in list(site_manager.registeredUtilities()):
        if (
            registration.provided is ILocalBrowserLayerType
            and registration.name == OLD_LAYER
        ):
            site_manager.unregisterUtility(
                component=registration.component,
                provided=ILocalBrowserLayerType,
                name=OLD_LAYER,
            )


def migrate(copy=True, switch_over=False, uninstall=False) -> MigrationReport:
    """Run the chosen steps, in order. The checks come first: if one fails,
    nothing is written and the report lists the errors."""
    report = MigrationReport()
    if not is_installed():
        report.errors.append(f"Install {PACKAGE} first.")
        return report
    if uninstall and not switch_over and old_is_running():
        report.errors.append(
            f"{OLD_PACKAGE} is still enabled: switch over before uninstalling it."
        )
    if switch_over and report.ok:
        check_index(report, copy=copy)
    if not report.ok:
        logger.warning(f"Migration from {OLD_PACKAGE} not run: {report.errors}")
        return report

    registry = _registry()
    if copy:
        copy_settings(report, registry)
    if switch_over and report.ok:
        switch(report, registry)
    if uninstall and report.ok:
        uninstall_old(report, registry)
    if not report.ok:
        # Keep none of it
        transaction.doom()
        logger.error(f"Migration from {OLD_PACKAGE} failed: {report.errors}")
        return report
    logger.info(
        f"Migrated from {OLD_PACKAGE}: copied {report.copied}, "
        f"no counterpart {report.not_migrated}, invalid {report.invalid}, "
        f"switched {report.switched}, uninstalled {report.uninstalled}"
    )
    return report
