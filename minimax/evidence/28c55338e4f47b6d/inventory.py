from dataclasses import dataclass


@dataclass(frozen=True)
class StockItem:
    sku: str
    quantity: int
    discontinued: bool = False


def restock(items: list[StockItem], threshold: int) -> list[str]:
    """Return eligible SKUs, ordered by quantity and then by SKU."""
    eligible = [item for item in items if not item.discontinued and item.quantity <= threshold]
    return [item.sku for item in sorted(eligible, key=lambda item: (item.quantity, item.sku))]
