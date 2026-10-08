from collections import Counter
from collective.elastic.indexes import EKeywordIndex
from collective.elastic.tests import BaseFunctionalTest
from plone import api
from Products.PluginIndexes.KeywordIndex.KeywordIndex import KeywordIndex

import unittest


class Dummy:
    def __init__(self, keywords):
        self.keywords = keywords


class TestKeywordIndexValue(unittest.TestCase):
    def get_value(self, keywords):
        index = EKeywordIndex(None, KeywordIndex("keywords"))
        return index.get_value(Dummy(keywords))

    def test_list_is_unchanged(self):
        self.assertEqual(self.get_value(["a", "b"]), ["a", "b"])

    def test_mapping_is_indexed_by_its_keys(self):
        value = self.get_value(Counter({"slate": 2, "image": 1}))
        self.assertIsInstance(value, list)
        self.assertEqual(sorted(value), ["image", "slate"])


class TestKeywordIndexMappingValue(BaseFunctionalTest):
    """plone.volto indexes block_types as a Counter of block type to count.
    The catalog's KeywordIndex indexes the keys; sending the mapping itself is
    rejected by elasticsearch, so the whole document was missing."""

    def setUp(self):
        super().setUp()
        self.catalog.addIndex("counted_keywords", "KeywordIndex")
        self.catalog.manage_catalogRebuild()
        self.commit()

    def test_document_with_mapping_value_is_indexed_and_found(self):
        doc = api.content.create(self.portal, "Document", "counted", title="Counted")
        doc.counted_keywords = Counter({"slate": 2, "image": 1})
        doc.reindexObject()
        self.commit(wait=1)

        source = self.es.connection.get(index=self.es.index_name, id=doc.UID())[
            "_source"
        ]
        self.assertEqual(sorted(source["counted_keywords"]), ["image", "slate"])
        results = self.search(
            {"SearchableText": "counted", "counted_keywords": "slate"}
        )
        self.assertEqual([b.getId for b in results], ["counted"])
