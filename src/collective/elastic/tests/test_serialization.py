from collective.elastic.compat import get_serializer_params
from collective.elastic.compat import IS_ES_8
from collective.elastic.serializer import SetJSONSerializer
from collective.elastic.serializer import SetNdjsonSerializer
from collective.elastic.tests import BaseFunctionalTest

import json
import unittest


class TestSetSerializers(unittest.TestCase):
    def test_json_serializer_encodes_sets_as_lists(self):
        data = json.loads(
            SetJSONSerializer().dumps(
                {"keywords": {"a", "b"}, "empty": frozenset(), "text": "x"}
            )
        )
        self.assertEqual(sorted(data["keywords"]), ["a", "b"])
        self.assertEqual(data["empty"], [])
        self.assertEqual(data["text"], "x")

    def test_ndjson_serializer_encodes_sets_as_lists(self):
        if SetNdjsonSerializer is None:
            self.skipTest("Only elasticsearch 8 has a separate ndjson serializer")
        lines = SetNdjsonSerializer().dumps(
            [{"index": {"_id": "1"}}, {"keywords": {"a"}}]
        )
        if isinstance(lines, bytes):
            lines = lines.decode("utf-8")
        self.assertEqual(
            [json.loads(line) for line in lines.splitlines()],
            [{"index": {"_id": "1"}}, {"keywords": ["a"]}],
        )

    def test_client_params_cover_the_bulk_mimetype(self):
        params = get_serializer_params()
        if IS_ES_8:
            self.assertIsInstance(
                params["serializers"]["application/x-ndjson"], SetNdjsonSerializer
            )
            self.assertIsInstance(
                params["serializers"]["application/json"], SetJSONSerializer
            )
        else:
            self.assertIsInstance(params["serializer"], SetJSONSerializer)


class TestSetValuesAreIndexed(BaseFunctionalTest):
    """Index data can hold sets, e.g. plone.volto's block_types or an empty
    Subject; the stock serializers raise on them."""

    def get_source(self, uid):
        return self.es.connection.get(index=self.es.index_name, id=uid)["_source"]

    def test_bulk_index_with_set_values(self):
        self.es.bulk(
            [
                (
                    "index",
                    "set-values",
                    {
                        "Title": "Set values",
                        "Subject": frozenset(),
                        "block_types": {"slate", "image"},
                    },
                )
            ]
        )
        self.es.flush_indices()
        source = self.get_source("set-values")
        self.assertEqual(source["Subject"], [])
        self.assertEqual(sorted(source["block_types"]), ["image", "slate"])

    def test_bulk_update_with_set_values(self):
        self.es.bulk([("index", "set-update", {"Title": "Set update"})])
        self.es.bulk([("update", "set-update", {"Subject": {"one"}})])
        self.es.flush_indices()
        self.assertEqual(self.get_source("set-update")["Subject"], ["one"])
