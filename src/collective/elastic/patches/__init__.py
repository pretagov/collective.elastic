from collective.elastic import interfaces
from collective.elastic.manager import ElasticSearchManager
from collective.elastic.utils import get_brain_from_path
from plone.folder.interfaces import IOrdering
from Products.CMFCore.indexing import processQueue
from Products.CMFCore.interfaces import IContentish
from time import process_time
from zope.globalrequest import getRequest
from zope.interface import alsoProvides
from zope.interface import noLongerProvides

import time
import urllib


PREVIOUS = "_elastic_previous_"


def preserve_previous(scope, original, replacement):
    """Monkey patch handler that also keeps the method it replaces.

    Like the ``preserveOriginal`` handler it keeps the unpatched method as
    ``_old_<name>``. The method in place when patching, which is
    collective.elasticsearch's patch while that is still installed, is kept as
    ``_elastic_previous_<name>``: the patches call it while this add-on isn't
    active, so collective.elasticsearch keeps working until the migration.
    """
    current = getattr(scope, original)
    if current is replacement:
        # Configuration loaded again, e.g. by another test layer
        return
    if not hasattr(scope, f"_old_{original}"):
        setattr(scope, f"_old_{original}", current)
    setattr(scope, f"{PREVIOUS}{original}", current)
    setattr(scope, original, replacement)


def previous(obj, name):
    """The implementation of ``name`` this add-on's patch replaced."""
    return getattr(obj, f"{PREVIOUS}{name}")


def unrestrictedSearchResults(self, REQUEST=None, **kw):
    manager = ElasticSearchManager()
    if manager.active:
        return manager.search_results(REQUEST, check_perms=False, **kw)
    return previous(self, "unrestrictedSearchResults")(REQUEST, **kw)


def safeSearchResults(self, REQUEST=None, **kw):
    manager = ElasticSearchManager()
    if manager.active:
        return manager.search_results(REQUEST, check_perms=True, **kw)
    return previous(self, "searchResults")(REQUEST, **kw)


def manage_catalogRebuild(self, RESPONSE=None, URL1=None):  # NOQA W0613
    """need to be publishable"""
    manager = ElasticSearchManager()
    if not manager.enabled:
        return previous(self, "manage_catalogRebuild")(RESPONSE=RESPONSE, URL1=URL1)
    manager._recreate_catalog()
    alsoProvides(getRequest(), interfaces.IReindexActive)

    elapse = time.time()
    c_elapse = process_time()

    self.clearFindAndRebuild()

    elapse = time.time() - elapse
    c_elapse = process_time() - c_elapse

    msg = f"Catalog Rebuilt\nTotal time: {elapse}\nTotal CPU time: {c_elapse}"

    processQueue()
    manager.flush_indices()
    noLongerProvides(getRequest(), interfaces.IReindexActive)
    if RESPONSE is not None:
        RESPONSE.redirect(
            URL1
            + "/manage_catalogAdvanced?manage_tabs_message="
            + urllib.parse.quote(msg)
        )


def manage_catalogClear(self, *args, **kwargs):
    """need to be publishable"""
    manager = ElasticSearchManager()
    if manager.enabled and not manager.active:
        manager._recreate_catalog()
    return previous(self, "manage_catalogClear")(*args, **kwargs)


def uncatalog_object(self, *args, **kwargs):
    manager = ElasticSearchManager()
    if manager.active:
        # If ES is active, we also remove the record from there
        zcatalog = self._catalog
        data = []
        for path in args:
            brain = get_brain_from_path(zcatalog, path)
            if not brain:
                # Path not in the catalog
                continue
            data.append(("delete", brain.UID, {}))
        manager.bulk(data=data)
    return previous(self, "uncatalog_object")(*args, **kwargs)


def get_ordered_ids(context) -> dict:
    """Return all object ids in a context, ordered."""
    if IOrdering.providedBy(context):
        return {oid: idx for idx, oid in enumerate(context.idsInOrder())}
    else:
        # For Plone 5.2, we care only about Dexterity content
        objects = [
            obj
            for obj in list(context._objects)
            if obj.get("meta_type").startswith("Dexterity")
        ]
        return {oid: idx for idx, oid in enumerate(context.getIdsSubset(objects))}


def moveObjectsByDelta(self, ids, delta, subset_ids=None, suppress_events=False):
    manager = ElasticSearchManager()
    ordered = self if IOrdering.providedBy(self) else None
    before = get_ordered_ids(self)
    res = previous(self, "moveObjectsByDelta")(
        ids, delta, subset_ids=subset_ids, suppress_events=suppress_events
    )
    if manager.active:
        after = get_ordered_ids(self)
        diff = [oid for oid, idx in after.items() if idx != before[oid]]
        context = self.context if ordered else self
        for oid in diff:
            obj = context[oid]
            # We only reindex content objects
            if not IContentish.providedBy(obj):
                continue
            obj.reindexObject(idxs=["getObjPositionInParent"])
    return res
