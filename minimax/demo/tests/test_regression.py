import unittest

from inventory import StockItem, restock


class RestockCompatibilityTest(unittest.TestCase):
    def test_restock_preserves_filter_and_order(self):
        items = [
            StockItem("z", 1),
            StockItem("a", 1),
            StockItem("low", 0),
            StockItem("high", 10),
            StockItem("closed", 0, discontinued=True),
            StockItem("closed-at-threshold", 5, discontinued=True),
        ]
        self.assertEqual(restock(items, threshold=5), ["low", "a", "z"])

    def test_restock_of_empty_inventory(self):
        self.assertEqual(restock([], threshold=5), [])
