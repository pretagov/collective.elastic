from collective.elastic.tests import BaseFunctionalTest
from plone import api


class TestKeywordIndexMapping(BaseFunctionalTest):
    """Every catalog KeywordIndex is mapped as an elasticsearch keyword, not
    only the ones in the built-in keyword_fields list. As text, values are
    analysed and an exact match on a multi-word or mixed case keyword fails."""

    def setUp(self):
        super().setUp()
        self.catalog.addIndex("custom_keywords", "KeywordIndex")
        self.catalog.manage_catalogRebuild()
        self.commit()

    def test_custom_keyword_index_is_mapped_as_keyword(self):
        mapping = self.es.connection.indices.get_mapping(index=self.es.index_name)
        properties = list(mapping.values())[0]["mappings"]["properties"]
        self.assertEqual(properties["custom_keywords"]["type"], "keyword")

    def test_exact_match_on_multi_word_keyword(self):
        doc = api.content.create(self.portal, "Document", "doc", title="Keyword")
        doc.custom_keywords = ["New South Wales", "other"]
        doc.reindexObject()
        self.commit(wait=1)

        results = self.search(
            {"SearchableText": "keyword", "custom_keywords": "New South Wales"}
        )
        self.assertEqual([b.getId for b in results], ["doc"])
        results = self.search({"SearchableText": "keyword", "custom_keywords": "New"})
        self.assertEqual(len(results), 0)
