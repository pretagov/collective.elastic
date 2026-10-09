"""Keep collective.elastic off while collective.elasticsearch is running."""

from collective.elastic.interfaces import IElasticSettings
from collective.elastic.migration import ENABLED
from collective.elastic.migration import NEW_PREFIX
from collective.elastic.migration import old_is_running
from collective.elastic.migration import OLD_PACKAGE


class OldPackageRunning(ValueError):
    """collective.elastic can't be enabled while collective.elasticsearch is."""


def disable_enabled_widget(event):
    """Grey out the control panel's Enabled checkbox."""
    widget = event.widget
    field = getattr(widget, "field", None)
    if (
        getattr(field, "interface", None) is IElasticSettings
        and field.__name__ == ENABLED
        and old_is_running()
    ):
        widget.disabled = "disabled"


def refuse_enabling(event):
    """Whatever sets the setting: the control panel, the REST API or a
    profile."""
    if (
        event.record.__name__ == NEW_PREFIX + ENABLED
        and event.newValue
        and old_is_running()
    ):
        raise OldPackageRunning(
            f"{OLD_PACKAGE} is enabled: switch over with @@ce-migration "
            "instead of enabling collective.elastic next to it."
        )
