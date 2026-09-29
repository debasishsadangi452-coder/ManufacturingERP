"""Level 1 QuickBooks integration: push ERP records into QuickBooks.

Each push_* function mirrors one ERP record into QuickBooks Online and stores
the returned Id/SyncToken back on the ERP record (and in QuickBooksEntityLink).
Transactional pushes (orders, invoices, bills, payments) first make sure the
master records they reference (customer, vendor, items) exist in QuickBooks.

QuickBooks Online has no Sales Order entity, so ERP sales orders are mirrored
as Estimates — the standard QBO equivalent of a non-posting sales commitment.
"""

import threading
from contextlib import contextmanager

from django.conf import settings
from django.db.models import Sum
from django.utils import timezone

from .models import QuickBooksConnection, QuickBooksEntityLink, QuickBooksSyncError, QuickBooksSyncRun
from .services import QuickBooksAPIError, quickbooks_get, quickbooks_post, query_entities


_local = threading.local()


@contextmanager
def suppress_auto_push():
    """Disable the post_save auto-push while ERP records are being written by
    the sync machinery itself (pull sync, bookkeeping saves) to avoid loops."""
    _local.depth = getattr(_local, "depth", 0) + 1
    try:
        yield
    finally:
        _local.depth -= 1


def auto_push_suppressed():
    return getattr(_local, "depth", 0) > 0


def get_active_connection(company):
    if not company:
        return None
    config = settings.QUICKBOOKS_CONFIG
    if not (config["CLIENT_ID"] and config["CLIENT_SECRET"]):
        return None
    return QuickBooksConnection.objects.filter(company=company, is_active=True).first()


# ---------------------------------------------------------------------------
# Low-level create/update helper
# ---------------------------------------------------------------------------

# QBO REST resource name -> response payload key
_RESPONSE_KEYS = {
    "customer": "Customer",
    "vendor": "Vendor",
    "item": "Item",
    "estimate": "Estimate",
    "purchaseorder": "PurchaseOrder",
    "invoice": "Invoice",
    "bill": "Bill",
    "payment": "Payment",
    "billpayment": "BillPayment",
    "journalentry": "JournalEntry",
}


def _write_entity(connection, resource, body, qb_id="", sync_token=""):
    """Create (no qb_id) or sparse-update (qb_id set) a QuickBooks entity.

    Returns the entity dict from the response. Recovers from a stale
    SyncToken by re-reading the entity and retrying once.
    """
    payload = dict(body)
    if qb_id:
        payload.update({"Id": qb_id, "SyncToken": sync_token or "0", "sparse": True})
    path = f"/v3/company/{connection.realm_id}/{resource}"
    try:
        response = quickbooks_post(connection, path, payload)
    except QuickBooksAPIError as exc:
        if qb_id and "Stale Object" in str(exc):
            fresh = quickbooks_get(
                connection,
                f"/v3/company/{connection.realm_id}/{resource}/{qb_id}",
                {"minorversion": "75"},
            ).get(_RESPONSE_KEYS[resource], {})
            payload["SyncToken"] = fresh.get("SyncToken", "0")
            response = quickbooks_post(connection, path, payload)
        else:
            raise
    return response.get(_RESPONSE_KEYS[resource], {})


def _adopt_duplicate(connection, entity_name, display_name):
    """When QBO rejects a create because the name already exists, find that
    record so the ERP row can adopt its Id instead of failing forever."""
    escaped = display_name.replace("'", "\\'")
    field = "Name" if entity_name == "Item" else "DisplayName"
    records = query_entities(connection, entity_name, f"{field} = '{escaped}'")
    return records[0] if records else None


def _store_result(connection, entity_type, obj, qb_entity):
    """Save the QuickBooks Id/SyncToken onto the ERP record and entity link."""
    fields = {
        "quickbooks_id": str(qb_entity.get("Id", "")),
        "quickbooks_sync_token": str(qb_entity.get("SyncToken", "")),
        "quickbooks_last_synced_at": timezone.now(),
    }
    # UPDATE rather than save(): no signals, and no model validation (a posted
    # journal entry in a since-closed period would refuse a plain save()).
    type(obj).objects.filter(pk=obj.pk).update(**fields)
    for name, value in fields.items():
        setattr(obj, name, value)
    QuickBooksEntityLink.objects.update_or_create(
        company=connection.company,
        entity_type=entity_type,
        quickbooks_id=obj.quickbooks_id,
        defaults={
            "local_object_id": obj.id,
            "sync_token": obj.quickbooks_sync_token,
            "last_synced_at": timezone.now(),
        },
    )


def _push_named_entity(connection, entity_type, resource, obj, body, name):
    """Shared create/update flow for Customer/Vendor/Item, including adopting
    an existing QBO record when the name is already taken (error 6240)."""
    try:
        result = _write_entity(connection, resource, body, obj.quickbooks_id, obj.quickbooks_sync_token)
    except QuickBooksAPIError as exc:
        if obj.quickbooks_id or "6240" not in str(exc):
            raise
        existing = _adopt_duplicate(connection, _RESPONSE_KEYS[resource], name)
        if not existing:
            raise
        result = _write_entity(connection, resource, body, existing["Id"], existing.get("SyncToken", "0"))
    _store_result(connection, entity_type, obj, result)
    return result


# ---------------------------------------------------------------------------
# Masters: customer, vendor, item
# ---------------------------------------------------------------------------

def push_customer(connection, customer):
    body = {"DisplayName": customer.name}
    if customer.email:
        body["PrimaryEmailAddr"] = {"Address": customer.email}
    if customer.phone:
        body["PrimaryPhone"] = {"FreeFormNumber": customer.phone}
    if customer.address:
        body["BillAddr"] = {"Line1": customer.address[:500]}
    return _push_named_entity(connection, "customer", "customer", customer, body, customer.name)


def push_vendor(connection, vendor):
    body = {"DisplayName": vendor.name}
    if vendor.email:
        body["PrimaryEmailAddr"] = {"Address": vendor.email}
    if vendor.phone:
        body["PrimaryPhone"] = {"FreeFormNumber": vendor.phone}
    if vendor.address:
        body["BillAddr"] = {"Line1": vendor.address[:500]}
    if vendor.tax_id:
        body["TaxIdentifier"] = vendor.tax_id
    return _push_named_entity(connection, "vendor", "vendor", vendor, body, vendor.name)


def _find_account(connection, account_type, prefer_subtype=""):
    cache = getattr(_local, "account_cache", None)
    if cache is None or cache.get("connection_id") != connection.id:
        cache = {"connection_id": connection.id}
        _local.account_cache = cache
    key = (account_type, prefer_subtype)
    if key not in cache:
        accounts = query_entities(connection, "Account", f"AccountType = '{account_type}'")
        chosen = None
        if prefer_subtype:
            chosen = next((a for a in accounts if a.get("AccountSubType") == prefer_subtype), None)
        chosen = chosen or (accounts[0] if accounts else None)
        cache[key] = chosen["Id"] if chosen else None
    return cache[key]


def _item_quantity(item):
    from inventory.models import Stock

    total = Stock.objects.filter(item=item, warehouse__company=item.company).aggregate(
        total=Sum("quantity")
    )["total"]
    return round(total or 0, 2)


def push_item(connection, item):
    income = _find_account(connection, "Income", "SalesOfProductIncome")
    expense = _find_account(connection, "Cost of Goods Sold", "SuppliesMaterialsCogs")
    asset = _find_account(connection, "Other Current Asset", "Inventory")
    is_raw_material = item.category == "raw_material"

    body = {
        "Name": item.name,
        "UnitPrice": 0 if is_raw_material else float(item.selling_price or 0),
        "PurchaseCost": float(item.purchase_cost or 0),
    }
    if item.sku:
        body["Sku"] = item.sku
    if is_raw_material:
        body["Description"] = "ERP raw material; purchase from vendors only."

    if is_raw_material and not (income and expense and asset):
        raise QuickBooksAPIError(
            "Raw materials must be created as QuickBooks Inventory items, but required income/expense/asset accounts could not be resolved."
        )

    if income and expense and asset:
        if not item.quickbooks_id:
            body.update(
                {
                    "Type": "Inventory",
                    "TrackQtyOnHand": True,
                    "IncomeAccountRef": {"value": income},
                    "ExpenseAccountRef": {"value": expense},
                    "AssetAccountRef": {"value": asset},
                    "QtyOnHand": _item_quantity(item),
                    "InvStartDate": timezone.localdate().isoformat(),
                }
            )
        elif float(item.purchase_cost or 0) > 0:
            # On updates, QuickBooks only keeps PurchaseCost when the item has an
            # expense account to attach it to. Service/NonInventory items pulled
            # from QB may lack one, so include the ExpenseAccountRef so the cost
            # actually persists instead of being silently dropped.
            body["ExpenseAccountRef"] = {"value": expense}
    elif income and expense:
        body.update(
            {
                "Type": "NonInventory",
                "IncomeAccountRef": {"value": income},
                "ExpenseAccountRef": {"value": expense},
            }
        )
    else:
        raise QuickBooksAPIError(
            "Could not resolve QuickBooks income/expense accounts required to create items."
        )
    return _push_named_entity(connection, "item", "item", item, body, item.name)


def push_item_quantity(connection, item):
    """Mirror the ERP stock level to QuickBooks (QtyOnHand on the item).

    QuickBooks Online ignores QtyOnHand on a *sparse* item update, and only
    Inventory-type items track quantity at all. So we read the current item,
    and if it's an Inventory item we send a full (non-sparse) update with the
    new QtyOnHand. Non-inventory/Service items can't hold a quantity in QB, so
    we no-op for them.
    """
    if not item.quickbooks_id:
        return push_item(connection, item)

    path = f"/v3/company/{connection.realm_id}/item"
    # Read the current QB item so we can send a complete (non-sparse) payload.
    current = quickbooks_get(
        connection, f"{path}/{item.quickbooks_id}", {"minorversion": "75"}
    ).get("Item", {})

    # Only Inventory items track quantity in QuickBooks.
    if current.get("Type") != "Inventory" or not current.get("TrackQtyOnHand"):
        return current

    body = dict(current)
    body["QtyOnHand"] = _item_quantity(item)
    body["sparse"] = False
    body["Id"] = item.quickbooks_id
    body["SyncToken"] = current.get("SyncToken", item.quickbooks_sync_token or "0")

    try:
        response = quickbooks_post(connection, path, body)
    except QuickBooksAPIError as exc:
        # Recover once from a stale SyncToken by re-reading and retrying.
        if "Stale Object" in str(exc):
            fresh = quickbooks_get(
                connection, f"{path}/{item.quickbooks_id}", {"minorversion": "75"}
            ).get("Item", {})
            body["SyncToken"] = fresh.get("SyncToken", "0")
            response = quickbooks_post(connection, path, body)
        else:
            raise
    result = response.get("Item", {})
    _store_result(connection, "item", item, result)
    return result


def _ensure_customer_ref(connection, customer):
    if not customer.quickbooks_id:
        push_customer(connection, customer)
    return {"value": customer.quickbooks_id}


def _ensure_vendor_ref(connection, vendor):
    if not vendor.quickbooks_id:
        push_vendor(connection, vendor)
    return {"value": vendor.quickbooks_id}


def _ensure_item_ref(connection, item):
    if not item.quickbooks_id:
        push_item(connection, item)
    return {"value": item.quickbooks_id}


# ---------------------------------------------------------------------------
# Transactions: sales order (estimate), purchase order, invoice, bill, payment
# ---------------------------------------------------------------------------

def _sales_lines(connection, rows):
    """rows: iterable of (item, quantity, unit_price, description)."""
    lines = []
    for item, quantity, unit_price, description in rows:
        amount = round(float(unit_price) * float(quantity), 2)
        lines.append(
            {
                "DetailType": "SalesItemLineDetail",
                "Amount": amount,
                "Description": description or item.name,
                "SalesItemLineDetail": {
                    "ItemRef": _ensure_item_ref(connection, item),
                    "Qty": float(quantity),
                    "UnitPrice": float(unit_price),
                },
            }
        )
    return lines


def _expense_lines(connection, rows):
    lines = []
    for item, quantity, unit_price, description in rows:
        amount = round(float(unit_price) * float(quantity), 2)
        lines.append(
            {
                "DetailType": "ItemBasedExpenseLineDetail",
                "Amount": amount,
                "Description": description or item.name,
                "ItemBasedExpenseLineDetail": {
                    "ItemRef": _ensure_item_ref(connection, item),
                    "Qty": float(quantity),
                    "UnitPrice": float(unit_price),
                },
            }
        )
    return lines


def push_sales_order(connection, order):
    rows = [
        (line.item, line.quantity, line.item.selling_price or 0, line.item.name)
        for line in order.salesorderitem_set.select_related("item")
    ]
    body = {
        "CustomerRef": _ensure_customer_ref(connection, order.customer),
        "TxnDate": order.created_at.date().isoformat(),
        "DocNumber": f"SO-{order.id}"[:21],
        "PrivateNote": f"ERP sales order SO-{order.id} (status: {order.status})",
        "Line": _sales_lines(connection, rows),
    }
    result = _write_entity(connection, "estimate", body, order.quickbooks_id, order.quickbooks_sync_token)
    _store_result(connection, "sales_order", order, result)
    return result


def push_purchase_order(connection, order):
    rows = [
        (line.item, line.quantity, line.unit_price or 0, line.item.name)
        for line in order.items.select_related("item")
    ]
    body = {
        "VendorRef": _ensure_vendor_ref(connection, order.vendor),
        "TxnDate": order.created_at.date().isoformat(),
        "DocNumber": f"PO-{order.id}"[:21],
        "PrivateNote": f"ERP purchase order PO-{order.id} (status: {order.status})",
        "Line": _expense_lines(connection, rows),
    }
    ap_account = _find_account(connection, "Accounts Payable")
    if ap_account:
        body["APAccountRef"] = {"value": ap_account}
    result = _write_entity(
        connection, "purchaseorder", body, order.quickbooks_id, order.quickbooks_sync_token
    )
    _store_result(connection, "purchase_order", order, result)
    return result


def push_invoice(connection, invoice):
    rows = [
        (line.item, line.quantity, line.unit_price, line.description)
        for line in invoice.lines.select_related("item")
    ]
    body = {
        "CustomerRef": _ensure_customer_ref(connection, invoice.customer),
        "TxnDate": invoice.invoice_date.isoformat(),
        "DocNumber": f"INV-{invoice.id}"[:21],
        "Line": _sales_lines(connection, rows),
    }
    if invoice.due_date:
        body["DueDate"] = invoice.due_date.isoformat()
    if invoice.sales_order_id:
        body["PrivateNote"] = f"ERP invoice for SO-{invoice.sales_order_id}"
    result = _write_entity(connection, "invoice", body, invoice.quickbooks_id, invoice.quickbooks_sync_token)
    _store_result(connection, "invoice", invoice, result)
    return result


def push_bill(connection, bill):
    rows = [
        (line.item, line.quantity, line.unit_price, line.description)
        for line in bill.lines.select_related("item")
    ]
    body = {
        "VendorRef": _ensure_vendor_ref(connection, bill.vendor),
        "TxnDate": bill.bill_date.isoformat(),
        "Line": _expense_lines(connection, rows),
    }
    if bill.bill_number:
        body["DocNumber"] = bill.bill_number[:21]
    if bill.due_date:
        body["DueDate"] = bill.due_date.isoformat()
    if bill.purchase_order_id:
        body["PrivateNote"] = f"ERP bill for PO-{bill.purchase_order_id}"
    result = _write_entity(connection, "bill", body, bill.quickbooks_id, bill.quickbooks_sync_token)
    _store_result(connection, "bill", bill, result)
    return result


def push_payment(connection, payment):
    body = {
        "CustomerRef": _ensure_customer_ref(connection, payment.customer),
        "TotalAmt": float(payment.amount),
        "TxnDate": payment.payment_date.isoformat(),
    }
    if payment.reference:
        body["PaymentRefNum"] = payment.reference[:21]
    if payment.invoice_id:
        invoice = payment.invoice
        if not invoice.quickbooks_id:
            push_invoice(connection, invoice)
        body["Line"] = [
            {
                "Amount": float(payment.amount),
                "LinkedTxn": [{"TxnId": invoice.quickbooks_id, "TxnType": "Invoice"}],
            }
        ]
    result = _write_entity(connection, "payment", body, payment.quickbooks_id, payment.quickbooks_sync_token)
    _store_result(connection, "payment", payment, result)
    return result


# ---------------------------------------------------------------------------
# Main books: bill payments, ERP-only journal entries, deletions
# ---------------------------------------------------------------------------
#
# QuickBooks is the company's main set of books. Every ERP ledger entry must
# reach it exactly once:
#   * invoices, bills, customer payments and bill payments are sent as those
#     QuickBooks documents, and QuickBooks books them itself;
#   * goods receipts, cost of goods sold, production and inventory valuation
#     are booked by QuickBooks from its Inventory items (the ERP mirrors stock
#     quantities to them), and year-end closing is done by QuickBooks itself,
#     so those ERP entries are NOT sent;
#   * entries QuickBooks cannot know about are sent as JournalEntry records.

QB_JOURNAL_SOURCES = {"manual", "expenses", "cash_bank", "tax_adjustment"}


def journal_entry_goes_to_quickbooks(entry):
    """Whether this ERP journal entry is mirrored to QuickBooks as a JournalEntry."""
    if entry.status not in ("posted", "reversed"):
        return False
    if entry.source_module in QB_JOURNAL_SOURCES:
        return True
    original = entry.reversal_of if entry.source_module == "reversal" else None
    return bool(original and original.source_module in QB_JOURNAL_SOURCES)


# ERP account type -> QuickBooks AccountType, used when auto-mapping accounts.
_QB_TYPE_FOR_ERP_TYPE = {
    "Cash & Cash Equivalents": "Bank",
    "Accounts Receivable": "Accounts Receivable",
    "Inventory": "Other Current Asset",
    "Prepaid Expenses & Other Current Assets": "Other Current Asset",
    "Property, Plant & Equipment": "Fixed Asset",
    "Accumulated Depreciation": "Fixed Asset",
    "Accounts Payable": "Accounts Payable",
    "Accrued Expenses & Other Current Liabilities": "Other Current Liability",
    "Long-Term Debt": "Long Term Liability",
    "Operating Sales Revenue": "Income",
    "Discounts & Returns": "Income",
    "Other Income": "Other Income",
}
_QB_TYPE_FOR_CATEGORY = {
    "asset": "Other Current Asset",
    "liability": "Other Current Liability",
    "equity": "Equity",
    "revenue": "Income",
    "expense": "Expense",
}


def list_quickbooks_accounts(connection):
    return query_entities(connection, "Account", "Active = true")


def _qb_type_for(account):
    type_name = account.account_type.name if account.account_type_id else ""
    if type_name in _QB_TYPE_FOR_ERP_TYPE:
        return _QB_TYPE_FOR_ERP_TYPE[type_name]
    if "cost of goods sold" in type_name.lower():
        return "Cost of Goods Sold"
    category = account.account_type.category if account.account_type_id else ""
    return _QB_TYPE_FOR_CATEGORY.get(category, "")


def auto_map_account(connection, account, qb_accounts=None, overwrite=False):
    """Map an ERP account to a QuickBooks account: same name first, then the
    first QuickBooks account of the matching type. Returns the mapping or None."""
    from .models import QuickBooksAccountMapping

    existing = QuickBooksAccountMapping.objects.filter(account=account).first()
    if existing and (not overwrite or not existing.auto_mapped):
        return existing
    qb_accounts = qb_accounts if qb_accounts is not None else list_quickbooks_accounts(connection)
    wanted_name = account.name.strip().lower()
    chosen = next((a for a in qb_accounts if (a.get("Name") or "").strip().lower() == wanted_name), None)
    if chosen is None:
        qb_type = _qb_type_for(account)
        chosen = next((a for a in qb_accounts if a.get("AccountType") == qb_type), None)
    if chosen is None:
        return existing
    mapping, _ = QuickBooksAccountMapping.objects.update_or_create(
        account=account,
        defaults={
            "company": account.company,
            "quickbooks_account_id": str(chosen["Id"]),
            "quickbooks_account_name": chosen.get("FullyQualifiedName") or chosen.get("Name", ""),
            "quickbooks_account_type": chosen.get("AccountType", ""),
            "auto_mapped": True,
        },
    )
    return mapping


def _quickbooks_account_ref(connection, account, cache):
    from .models import QuickBooksAccountMapping

    mapping = QuickBooksAccountMapping.objects.filter(account=account).first()
    if mapping is None:
        if "accounts" not in cache:
            cache["accounts"] = list_quickbooks_accounts(connection)
        mapping = auto_map_account(connection, account, cache["accounts"])
    if mapping is None:
        raise QuickBooksAPIError(
            f"ERP account {account.code} {account.name} is not mapped to a QuickBooks account. "
            "Map it in Accounting > QuickBooks > Account mapping, then retry."
        )
    return {"value": mapping.quickbooks_account_id}


def _bank_account_ref(connection):
    """The QuickBooks bank account payments are made from: the account mapped
    to ERP 1010 (Operating Bank) when set, else the first QuickBooks Bank account."""
    from accounting.models import Account
    from .models import QuickBooksAccountMapping

    mapping = QuickBooksAccountMapping.objects.filter(
        company=connection.company, account__in=Account.objects.filter(company=connection.company, code="1010")
    ).first()
    if mapping:
        return {"value": mapping.quickbooks_account_id}
    bank = _find_account(connection, "Bank")
    if not bank:
        raise QuickBooksAPIError("No QuickBooks Bank account found to pay the bill from.")
    return {"value": bank}


def push_bill_payment(connection, vendor_payment):
    """Mirror an ERP vendor payment as a QuickBooks BillPayment linked to its bill."""
    vendor_payment.refresh_from_db(fields=["quickbooks_id", "quickbooks_sync_token"])
    bill = vendor_payment.bill
    if bill is None:
        raise ValueError("Vendor payment is not linked to a bill, so it cannot be sent as a BillPayment.")
    if not bill.quickbooks_id:
        push_bill(connection, bill)
    body = {
        "VendorRef": _ensure_vendor_ref(connection, vendor_payment.vendor),
        "PayType": "Check",
        "CheckPayment": {"BankAccountRef": _bank_account_ref(connection)},
        "TotalAmt": float(vendor_payment.amount),
        "TxnDate": vendor_payment.payment_date.isoformat(),
        "Line": [{
            "Amount": float(vendor_payment.amount),
            "LinkedTxn": [{"TxnId": bill.quickbooks_id, "TxnType": "Bill"}],
        }],
    }
    if vendor_payment.reference:
        body["PrivateNote"] = f"ERP payment ref {vendor_payment.reference}"[:4000]
    result = _write_entity(
        connection, "billpayment", body, vendor_payment.quickbooks_id, vendor_payment.quickbooks_sync_token
    )
    _store_result(connection, "bill_payment", vendor_payment, result)
    return result


def push_journal_entry(connection, entry):
    """Mirror an ERP-only journal entry (manual, expenses, cash & bank, tax
    adjustments, or the reversal of one) as a QuickBooks JournalEntry."""
    entry.refresh_from_db()
    if not journal_entry_goes_to_quickbooks(entry):
        raise ValueError(
            f"Journal entry {entry.entry_number} ({entry.source_module}) is booked by QuickBooks from its own "
            "documents or inventory and is not sent as a journal entry."
        )
    cache = {}
    lines = []
    for line in entry.lines.select_related("account").order_by("line_number"):
        amount = line.debit if line.debit > 0 else line.credit
        if amount <= 0:
            continue
        lines.append({
            "Description": (line.description or entry.description or "")[:4000],
            "Amount": float(amount),
            "DetailType": "JournalEntryLineDetail",
            "JournalEntryLineDetail": {
                "PostingType": "Debit" if line.debit > 0 else "Credit",
                "AccountRef": _quickbooks_account_ref(connection, line.account, cache),
            },
        })
    body = {
        "TxnDate": entry.transaction_date.isoformat(),
        "DocNumber": (entry.entry_number or "")[:21],
        "PrivateNote": f"ERP {entry.entry_number}: {entry.description}"[:4000],
        "Line": lines,
    }
    result = _write_entity(connection, "journalentry", body, entry.quickbooks_id, entry.quickbooks_sync_token)
    _store_result(connection, "journal_entry", entry, result)
    return result


def delete_in_quickbooks(connection, resource, qb_id, sync_token=""):
    """Delete a QuickBooks transaction (used when an ERP payment is undone)."""
    path = f"/v3/company/{connection.realm_id}/{resource}"
    body = {"Id": qb_id, "SyncToken": sync_token or "0"}
    try:
        quickbooks_post(connection, path, body, query={"operation": "delete"})
    except QuickBooksAPIError as exc:
        if "Stale Object" not in str(exc):
            raise
        fresh = quickbooks_get(connection, f"{path}/{qb_id}", {"minorversion": "75"}).get(_RESPONSE_KEYS[resource], {})
        body["SyncToken"] = fresh.get("SyncToken", "0")
        quickbooks_post(connection, path, body, query={"operation": "delete"})


def safe_delete(connection, entity_type, resource, qb_id, sync_token="", local_id=None):
    try:
        delete_in_quickbooks(connection, resource, qb_id, sync_token)
        QuickBooksEntityLink.objects.filter(
            company=connection.company, entity_type=entity_type, quickbooks_id=qb_id
        ).delete()
        return True
    except Exception as exc:
        QuickBooksSyncError.objects.create(
            company=connection.company,
            entity_type=entity_type,
            quickbooks_id=qb_id,
            message=f"Could not delete in QuickBooks after the ERP record was removed: {exc}"[:5000],
            payload={"local_id": local_id, "operation": "delete", "resource": resource},
        )
        return False


# ---------------------------------------------------------------------------
# Dispatch + bulk push
# ---------------------------------------------------------------------------

PUSH_HANDLERS = {
    "customer": push_customer,
    "vendor": push_vendor,
    "item": push_item,
    "item_quantity": push_item_quantity,
    "sales_order": push_sales_order,
    "purchase_order": push_purchase_order,
    "invoice": push_invoice,
    "bill": push_bill,
    "payment": push_payment,
    "bill_payment": push_bill_payment,
    "journal_entry": push_journal_entry,
}


def push_object(connection, entity_type, obj):
    handler = PUSH_HANDLERS.get(entity_type)
    if not handler:
        raise ValueError(f"Unsupported push entity type '{entity_type}'.")
    return handler(connection, obj)


def safe_push(connection, entity_type, obj, sync_run=None):
    """Push and swallow errors into QuickBooksSyncError so ERP operations
    never fail because QuickBooks is unreachable. Returns True on success."""
    try:
        push_object(connection, entity_type, obj)
        return True
    except Exception as exc:
        QuickBooksSyncError.objects.create(
            company=connection.company,
            sync_run=sync_run,
            entity_type=entity_type,
            quickbooks_id=getattr(obj, "quickbooks_id", "") or "",
            message=str(exc)[:5000],
            payload={"local_id": obj.id},
        )
        return False


def push_all(connection):
    """Backfill records that do not yet have a QuickBooks link.

    Auto-push handles normal updates after a record is linked. Keeping the bulk
    path focused on unlinked records makes the Settings button finish quickly
    and avoids long-running HTTP requests that rewrite every QuickBooks row.
    """
    from django.db.models import Q
    from accounting.models import JournalEntry
    from inventory.models import Item
    from procurement.models import Bill, PurchaseOrder, Vendor, VendorPayment
    from sales.models import Customer, CustomerPayment, Invoice, SalesOrder

    company = connection.company
    QuickBooksSyncRun.objects.filter(
        company=company,
        sync_type="push_all",
        status="running",
        finished_at__isnull=True,
    ).update(
        status="failed",
        error_message="Bulk push was abandoned before it finished.",
        finished_at=timezone.now(),
    )
    run = QuickBooksSyncRun.objects.create(
        company=company, connection=connection, sync_type="push_all"
    )
    batches = [
        ("customer", Customer.objects.filter(company=company, quickbooks_id="")),
        ("vendor", Vendor.objects.filter(company=company, quickbooks_id="")),
        ("item", Item.objects.filter(company=company, quickbooks_id="")),
        (
            "sales_order",
            SalesOrder.objects.filter(customer__company=company, quickbooks_id="").exclude(status="cancelled"),
        ),
        (
            "purchase_order",
            # items__isnull=False: QuickBooks rejects a PurchaseOrder with no
            # Line (code 2020), so a line-less order would fail on every run.
            PurchaseOrder.objects.filter(vendor__company=company, quickbooks_id="")
            .exclude(status__in=["draft", "cancelled"])
            .filter(items__isnull=False)
            .distinct(),
        ),
        ("invoice", Invoice.objects.filter(company=company, quickbooks_id="").exclude(status="cancelled")),
        ("bill", Bill.objects.filter(company=company, quickbooks_id="").exclude(status="cancelled")),
        ("payment", CustomerPayment.objects.filter(company=company, quickbooks_id="")),
        (
            "bill_payment",
            VendorPayment.objects.filter(company=company, quickbooks_id="", bill__isnull=False)
            .exclude(bill__status="cancelled"),
        ),
        (
            "journal_entry",
            JournalEntry.objects.filter(company=company, quickbooks_id="", status__in=["posted", "reversed"])
            .filter(
                Q(source_module__in=QB_JOURNAL_SOURCES)
                | Q(source_module="reversal", reversal_of__source_module__in=QB_JOURNAL_SOURCES)
            )
            .order_by("id"),
        ),
    ]
    created = updated = seen = 0
    failed = False
    for entity_type, queryset in batches:
        for obj in queryset.iterator():
            seen += 1
            was_linked = bool(obj.quickbooks_id)
            if safe_push(connection, entity_type, obj, sync_run=run):
                if was_linked:
                    updated += 1
                else:
                    created += 1
            else:
                failed = True
            run.records_created = created
            run.records_updated = updated
            run.records_seen = seen
            run.save(update_fields=["records_created", "records_updated", "records_seen"])
    run.records_created = created
    run.records_updated = updated
    run.records_seen = seen
    run.status = "failed" if failed else "success"
    if failed:
        run.error_message = "Some records failed to push; see sync errors."
    run.finished_at = timezone.now()
    run.save()
    connection.last_synced_at = timezone.now()
    connection.save(update_fields=["last_synced_at"])
    return run
