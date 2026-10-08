from collective.elastic.interfaces import IElasticSearchResults
from plone.app.querystring.querybuilder import QueryBuilder
from plone.restapi.deserializer import json_body
from plone.restapi.exceptions import DeserializationError
from plone.restapi.interfaces import ISerializeToJson
from plone.restapi.serializer.catalog import LazyCatalogResultSerializer
from zope.component import adapter
from zope.interface import implementer
from zope.interface import Interface


@implementer(ISerializeToJson)
@adapter(IElasticSearchResults, Interface)
class ElasticSearchResultsSerializer(LazyCatalogResultSerializer):
    """Adds the facet counts to serialized search results, e.g. of @search."""

    def __call__(self, fullobjects=False):
        results = super().__call__(fullobjects=fullobjects)
        facets = getattr(self.lazy_resultset, "facets", None)
        if facets:
            results["facets"] = facets
        return results


class FacetsQueryBuilder(QueryBuilder):
    """Takes the facets of a @querystring-search request into the catalog query.

    The request body of @querystring-search can name the facets to count next
    to the query, e.g. ``{"query": [...], "facets": ["Subject"]}``.
    """

    def _makequery(self, custom_query=None, **kwargs):
        facets = self._requested_facets()
        if facets:
            custom_query = dict(custom_query or {}, facets=facets)
        return super()._makequery(custom_query=custom_query, **kwargs)

    def _requested_facets(self):
        if not self.request.get("BODY"):
            return None
        try:
            data = json_body(self.request)
        except DeserializationError:
            return None
        if isinstance(data, dict):
            return data.get("facets")
        return None
