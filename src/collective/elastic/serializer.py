"""JSON serializers that accept the set values found in catalog data."""

from elasticsearch.serializer import JSONSerializer


try:
    from elasticsearch.serializer import NdjsonSerializer
except ImportError:  # elasticsearch 7 serializes every body with JSONSerializer
    NdjsonSerializer = None


class SetEncodingMixin:
    """Encode set and frozenset as lists.

    Indexers can return sets, e.g. plone.volto's ``block_types`` or an empty
    ``Subject``, which the stock serializers reject.
    """

    def default(self, data):
        if isinstance(data, (set, frozenset)):
            return list(data)
        return super().default(data)


class SetJSONSerializer(SetEncodingMixin, JSONSerializer):
    pass


if NdjsonSerializer is not None:

    class SetNdjsonSerializer(SetEncodingMixin, NdjsonSerializer):
        """Serializer for bulk requests, which elasticsearch 8 sends as ndjson."""

else:
    SetNdjsonSerializer = None
