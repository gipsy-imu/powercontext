import unittest

from inventory import StockItem, restock


class RestockBoundaryTest(unittest.TestCase):
    def test_includes_an_active_sku_at_the_threshold(self):
        items = [StockItem("over", 6), StockItem("at", 5), StockItem("below", 2)]
        self.assertEqual(restock(items, threshold=5), ["below", "at"])
