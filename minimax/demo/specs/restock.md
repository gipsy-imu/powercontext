# Reorder threshold

`inventory.restock` returns the SKU of each active item whose quantity is **at or below** the threshold. The threshold applies to each item independently. A discontinued item is never eligible.

Keep the established order of increasing quantity, then alphabetical SKU. Items above the threshold stay excluded. An empty inventory returns an empty list.

For task SDD-001, bring the implementation in line with the inclusive boundary. Preserve the public signature and update the implementation only.
