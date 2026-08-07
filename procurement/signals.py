"""Procurement signals.

Vendor emails are deliberately NOT drafted here. Drafting is tied to the
ordering action — see `PurchaseOrderViewSet.bulk_order` and `perform_update` —
so that orders placed together become one email and orders placed separately
become separate emails. A signal on line items cannot see that batching, and
would draft for orders that are never actually placed.
"""
