from collective.elastic import migration
from plone import api
from plone.app.layout.viewlets import ViewletBase
from plone.z3cform import layout
from Products.Five.browser.pagetemplatefile import ViewPageTemplateFile
from Products.statusmessages.interfaces import IStatusMessage
from z3c.form import button
from z3c.form import field
from z3c.form import form
from zope import schema
from zope.interface import Interface


class IMigrationSteps(Interface):

    copy_settings = schema.Bool(
        title="Copy the settings",
        description=(
            "Copy every collective.elasticsearch setting to the "
            "collective.elastic setting of the same name, as listed above. "
            "Enabled is left to the switch."
        ),
        default=True,
        required=False,
    )

    switch_over = schema.Bool(
        title="Switch over",
        description=(
            "Check that collective.elastic reaches the cluster and can search "
            "the existing index, then disable collective.elasticsearch and "
            "enable collective.elastic."
        ),
        default=False,
        required=False,
    )

    uninstall = schema.Bool(
        title="Uninstall collective.elasticsearch",
        description=(
            "Once it is disabled: uninstall it and remove its settings, "
            "browser layer and profile version, so that the package can be "
            "removed from the deployment."
        ),
        default=False,
        required=False,
    )


class MigrationForm(form.Form):
    fields = field.Fields(IMigrationSteps)
    ignoreContext = True
    label = "Migrate from collective.elasticsearch"
    report = None

    @button.buttonAndHandler("Run the migration", name="run")
    def handle_run(self, action):
        data, errors = self.extractData()
        if errors:
            self.status = self.formErrorsMessage
            return
        self.report = migration.migrate(
            copy=data.get("copy_settings"),
            switch_over=data.get("switch_over"),
            uninstall=data.get("uninstall"),
        )
        messages = IStatusMessage(self.request)
        if self.report.ok:
            messages.add("The migration ran.", type="info")
        else:
            messages.add("The migration failed and nothing was changed.", type="error")


class MigrationFormWrapper(layout.FormWrapper):
    index = ViewPageTemplateFile("templates/migration.pt")

    @property
    def report(self):
        return self.form_instance.report

    @property
    def installed(self):
        return migration.is_installed()

    @property
    def pending(self):
        return migration.is_pending()

    def state(self):
        manager = migration.ElasticSearchManager()
        return [
            (
                "collective.elasticsearch installed",
                migration.old_is_installed(),
            ),
            ("collective.elasticsearch enabled", migration.old_is_enabled()),
            ("collective.elastic installed", self.installed),
            ("collective.elastic enabled", self.installed and manager.enabled),
            ("Catalog converted", manager.catalog_converted),
            ("Index", manager.real_index_name),
        ]

    def settings(self):
        if not self.installed:
            return []
        return migration.plan_settings()

    @property
    def controlpanel_url(self):
        return f"{api.portal.get().absolute_url()}/@@elastic-controlpanel"


MigrationView = layout.wrap_form(MigrationForm, MigrationFormWrapper)


class ControlPanelWarning(ViewletBase):
    """Points to the migration from the Elastic Search control panel."""

    index = ViewPageTemplateFile("templates/warning.pt")

    def update(self):
        super().update()
        self.running = migration.old_is_running()
        self.available = self.running or migration.is_pending()
