"""Migrate a site from collective.elasticsearch to collective.elastic.

The settings are copied record by record: every
``collective.elasticsearch.interfaces.IElasticSettings.<name>`` value goes to
the ``collective.elastic`` record of the same name, if there is one and the
value is valid for it. Nothing here names a setting, so this works for any
version of collective.elasticsearch, including forks with settings of their
own; settings without a counterpart are reported and dropped.

collective.elasticsearch is then disabled and uninstalled: its registry
records, browser layer and profile version are removed, so the package can be
taken out of the deployment afterwards. Its index in elasticsearch is kept and
used as it is: the catalog attributes that record the conversion and the index
name are the same in both packages.

Pending Redis jobs of collective.elasticsearch refer to its code; let the queue
drain before removing the package.
"""

from collective.elastic import logger
from collective.elastic.interfaces import IElasticSettings
from dataclasses import dataclass
from dataclasses import field
from plone import api
from plone.registry.interfaces import IRegistry
from Products.CMFPlone.utils import get_installer
from zope import schema
from zope.component import getUtility


OLD_PACKAGE = "collective.elasticsearch"
OLD_PROFILE = f"{OLD_PACKAGE}:default"
OLD_LAYER = OLD_PACKAGE
OLD_PREFIX = f"{OLD_PACKAGE}.interfaces.IElasticSettings."
NEW_PREFIX = f"{IElasticSettings.__identifier__}."
_OLD_PREFIX_LENGTH = len(OLD_PREFIX)
UNKNOWN = "unknown"


@dataclass
class MigrationReport:
    copied: list = field(default_factory=list)
    not_migrated: list = field(default_factory=list)
    invalid: list = field(default_factory=list)
    uninstalled: bool = False


def old_records(registry) -> dict:
    """The collective.elasticsearch settings, by name."""
    return {
        key[_OLD_PREFIX_LENGTH:]: registry.records[key]
        for key in list(registry.records.keys())
        if key.startswith(OLD_PREFIX)
    }


def is_installed() -> bool:
    """Whether the site has collective.elasticsearch settings or profile."""
    setup = api.portal.get_tool("portal_setup")
    return bool(old_records(getUtility(IRegistry))) or (
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


def copy_settings(registry, report: MigrationReport):
    for name, old in sorted(old_records(registry).items()):
        new_key = NEW_PREFIX + name
        if new_key not in registry.records:
            report.not_migrated.append(name)
            continue
        record = registry.records[new_key]
        try:
            record.value = convert(record.field, old.value)
        except (schema.ValidationError, TypeError, ValueError) as exc:
            logger.warning(f"Not migrating setting {name}={old.value!r}: {exc!r}")
            report.invalid.append(name)
            continue
        report.copied.append(name)


def uninstall_old(portal, registry, report: MigrationReport):
    # Disable it first, for the rest of this process, where its code is still
    # loaded, until the records are gone
    enabled = OLD_PREFIX + "enabled"
    if enabled in registry.records:
        registry.records[enabled].value = False

    installer = get_installer(portal)
    if installer.is_product_installed(OLD_PACKAGE):
        # The package is still there: let it uninstall itself
        installer.uninstall_product(OLD_PACKAGE)
        report.uninstalled = True
    _remove_layer()
    setup = api.portal.get_tool("portal_setup")
    if setup.getLastVersionForProfile(OLD_PROFILE) != UNKNOWN:
        setup.unsetLastVersionForProfile(OLD_PROFILE)
        report.uninstalled = True
    # Its uninstall profile leaves the settings in the registry
    for name in old_records(registry):
        del registry.records[OLD_PREFIX + name]


def _remove_layer():
    """Unregister the browser layer, also when the package (and with it the
    layer's interface) is gone already."""
    from plone.browserlayer.interfaces import ILocalBrowserLayerType

    site_manager = api.portal.get().getSiteManager()
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


def migrate(portal=None) -> MigrationReport:
    """Move the site from collective.elasticsearch to collective.elastic.

    collective.elastic's profile has to be installed already, so that its
    settings exist. Running it again, or on a site without
    collective.elasticsearch, changes nothing.
    """
    portal = portal or api.portal.get()
    report = MigrationReport()
    if not is_installed():
        return report
    registry = getUtility(IRegistry)
    copy_settings(registry, report)
    uninstall_old(portal, registry, report)
    logger.info(
        f"Migrated from {OLD_PACKAGE}: copied {report.copied}, "
        f"not migrated (no such setting) {report.not_migrated}, "
        f"invalid {report.invalid}"
    )
    return report
