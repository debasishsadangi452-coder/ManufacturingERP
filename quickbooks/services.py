import base64
import json
from datetime import datetime, timedelta
from decimal import Decimal
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from inventory.models import Item
from procurement.models import PurchaseOrder, PurchaseOrderItem, Vendor, Bill, BillLine, VendorPriceList
from sales.models import Customer, CustomerPayment, Invoice, InvoiceLine, SalesOrder, SalesOrderItem

from .models import QuickBooksEntityLink, QuickBooksSyncError, QuickBooksSyncRun


AUTHORIZATION_URL = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"


class QuickBooksConfigError(Exception):
    pass


class QuickBooksAPIError(Exception):
    pass


def get_config():
    config = settings.QUICKBOOKS_CONFIG
    environment = config.get("ENVIRONMENT", "sandbox")
    return {
        **config,
        "API_BASE_URL": (
            "https://sandbox-quickbooks.api.intuit.com"
            if environment == "sandbox"
            else "https://quickbooks.api.intuit.com"
        ),
    }


def require_credentials():
    config = get_config()
    if not config["CLIENT_ID"] or not config["CLIENT_SECRET"]:
        raise QuickBooksConfigError("QuickBooks development client ID/secret are not configured.")
    return config


def build_authorization_url(state):
    config = require_credentials()
    params = {
        "client_id": config["CLIENT_ID"],
        "response_type": "code",
        "scope": "com.intuit.quickbooks.accounting",
        "redirect_uri": config["REDIRECT_URI"],
        "state": state,
    }
    return f"{AUTHORIZATION_URL}?{urlencode(params)}"


def exchange_code_for_tokens(code):
    config = require_credentials()
    return _token_request(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": config["REDIRECT_URI"],
        }
    )


def refresh_access_token(connection):
    payload = _token_request(
        {
            "grant_type": "refresh_token",
            "refresh_token": connection.get_refresh_token(),
        }
    )
    apply_token_payload(connection, payload)
    connection.save()
    return connection.get_access_token()


def apply_token_payload(connection, payload):
    now = timezone.now()
    connection.set_access_token(payload.get("access_token", ""))
    if payload.get("refresh_token"):
        connection.set_refresh_token(payload["refresh_token"])
    connection.access_token_expires_at = now + timedelta(seconds=int(payload.get("expires_in", 3600)))
    if payload.get("x_refresh_token_expires_in"):
        connection.refresh_token_expires_at = now + timedelta(
            seconds=int(payload["x_refresh_token_expires_in"])
        )


def _token_request(form):
    config = require_credentials()
    credentials = f"{config['CLIENT_ID']}:{config['CLIENT_SECRET']}".encode("utf-8")
    basic = base64.b64encode(credentials).decode("ascii")
    data = urlencode(form).encode("utf-8")
    request = Request(
        TOKEN_URL,
        data=data,
        headers={
            "Authorization": f"Basic {basic}",
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    return _open_json(request)


def quickbooks_get(connection, path, query=None, retry=True):
    config = get_config()
    if connection.access_token_expires_at and connection.access_token_expires_at <= timezone.now():
        refresh_access_token(connection)

    url = f"{config['API_BASE_URL']}{path}"
    if query:
        url = f"{url}?{urlencode(query)}"
    request = Request(
        url,
        headers={
            "Authorization": f"Bearer {connection.get_access_token()}",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        return _open_json(request)
    except QuickBooksAPIError as exc:
        if retry and "401" in str(exc):
            refresh_access_token(connection)
            return quickbooks_get(connection, path, query, retry=False)
        raise


def quickbooks_post(connection, path, body, query=None, retry=True):
    config = get_config()
    if connection.access_token_expires_at and connection.access_token_expires_at <= timezone.now():
        refresh_access_token(connection)

    url = f"{config['API_BASE_URL']}{path}"
    params = {"minorversion": "75", **(query or {})}
    url = f"{url}?{urlencode(params)}"
    request = Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {connection.get_access_token()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        return _open_json(request)
    except QuickBooksAPIError as exc:
        if retry and "401" in str(exc):
            refresh_access_token(connection)
            return quickbooks_post(connection, path, body, query, retry=False)
        raise


def _open_json(request):
    try:
        with urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise QuickBooksAPIError(f"QuickBooks API error {exc.code}: {body}") from exc
    except OSError as exc:
        raise QuickBooksAPIError(f"QuickBooks API request failed: {exc}") from exc


def query_entities(connection, entity_name, where=""):
    query = f"select * from {entity_name}"
    if where:
        query = f"{query} where {where}"
    payload = quickbooks_get(
        connection,
        f"/v3/company/{connection.realm_id}/query",
        {"query": query, "minorversion": "75"},
    )
    return payload.get("QueryResponse", {}).get(entity_name, [])


def fetch_company_info(connection):
    payload = quickbooks_get(
        connection,
        f"/v3/company/{connection.realm_id}/companyinfo/{connection.realm_id}",
        {"minorversion": "75"},
    )
    info = payload.get("CompanyInfo", {})
    connection.company_name = info.get("CompanyName") or info.get("LegalName") or connection.company_name
    connection.last_synced_at = timezone.now()
    connection.save(update_fields=["company_name", "last_synced_at"])
    return info


def sync_master_data(connection, sync_type="all"):
    # Local import: push.py imports from this module.
    from .push import suppress_auto_push

    allowed = {"company", "customers", "vendors", "items", "sales", "estimates", "invoices", "payments", "procurement", "purchase_orders", "bills", "all"}
    if sync_type not in allowed:
        raise ValueError(f"Unsupported sync type '{sync_type}'.")

    run = QuickBooksSyncRun.objects.create(
        company=connection.company,
        connection=connection,
        sync_type=sync_type,
    )
    totals = {"created": 0, "updated": 0, "seen": 0}
    try:
        # Suppress auto-push while writing pulled records so the pull sync
        # does not immediately push the same data back to QuickBooks.
        with suppress_auto_push():
            if sync_type in {"company", "all"}:
                fetch_company_info(connection)

            if sync_type in {"customers", "all"}:
                result = sync_customers(connection, run)
                _add_totals(totals, result)

            if sync_type in {"vendors", "all"}:
                result = sync_vendors(connection, run)
                _add_totals(totals, result)

            if sync_type in {"items", "all"}:
                result = sync_items(connection, run)
                _add_totals(totals, result)

            if sync_type in {"sales", "estimates", "all"}:
                result = sync_estimates(connection, run)
                _add_totals(totals, result)

            if sync_type in {"sales", "invoices", "all"}:
                result = sync_invoices(connection, run)
                _add_totals(totals, result)

            if sync_type in {"sales", "payments", "all"}:
                result = sync_payments(connection, run)
                _add_totals(totals, result)

            if sync_type in {"procurement", "purchase_orders", "all"}:
                result = sync_purchase_orders(connection, run)
                _add_totals(totals, result)

            if sync_type in {"procurement", "bills", "all"}:
                result = sync_bills(connection, run)
                _add_totals(totals, result)

        run.status = "success"
        connection.last_synced_at = timezone.now()
        connection.save(update_fields=["last_synced_at"])
    except Exception as exc:
        run.status = "failed"
        run.error_message = str(exc)
        QuickBooksSyncError.objects.create(
            company=connection.company,
            sync_run=run,
            message=str(exc),
        )
        raise
    finally:
        run.records_created = totals["created"]
        run.records_updated = totals["updated"]
        run.records_seen = totals["seen"]
        run.finished_at = timezone.now()
        run.save()
    return run


def sync_customers(connection, run):
    records = query_entities(connection, "Customer")
    return _sync_records(records, "customer", connection, run, _upsert_customer)


def sync_vendors(connection, run):
    records = query_entities(connection, "Vendor")
    return _sync_records(records, "vendor", connection, run, _upsert_vendor)


def sync_items(connection, run):
    records = query_entities(connection, "Item")
    return _sync_records(records, "item", connection, run, _upsert_item)


def sync_estimates(connection, run):
    records = query_entities(connection, "Estimate")
    return _sync_records(records, "sales_order", connection, run, _upsert_estimate)


def sync_invoices(connection, run):
    records = query_entities(connection, "Invoice")
    return _sync_records(records, "invoice", connection, run, _upsert_invoice)


def sync_payments(connection, run):
    records = query_entities(connection, "Payment")
    return _sync_records(records, "payment", connection, run, _upsert_payment)


def sync_purchase_orders(connection, run):
    records = query_entities(connection, "PurchaseOrder")
    return _sync_records(records, "purchase_order", connection, run, _upsert_purchase_order)


def sync_bills(connection, run):
    records = query_entities(connection, "Bill")
    return _sync_records(records, "bill", connection, run, _upsert_bill)


def _sync_records(records, entity_type, connection, run, upsert):
    result = {"created": 0, "updated": 0, "seen": len(records)}
    for payload in records:
        try:
            created = upsert(connection, payload)
            result["created" if created else "updated"] += 1
        except Exception as exc:
            QuickBooksSyncError.objects.create(
                company=connection.company,
                sync_run=run,
                entity_type=entity_type,
                quickbooks_id=str(payload.get("Id", "")),
                message=str(exc),
                payload=payload,
            )
    return result


@transaction.atomic
def _upsert_customer(connection, payload):
    qb_id = str(payload["Id"])
    defaults = {
        "name": payload.get("DisplayName") or payload.get("FullyQualifiedName") or f"QuickBooks Customer {qb_id}",
        "email": _email(payload.get("PrimaryEmailAddr")),
        "phone": _phone(payload.get("PrimaryPhone")),
        "address": _address(payload.get("BillAddr") or payload.get("ShipAddr")),
        "quickbooks_sync_token": payload.get("SyncToken", ""),
        "quickbooks_last_synced_at": timezone.now(),
        "payment_terms": _ref_name(payload.get("SalesTermRef")),
        "tax_status": str(payload.get("Taxable", "")),
        "balance_due": _decimal(payload.get("Balance")),
    }
    obj, created = Customer.objects.update_or_create(
        company=connection.company,
        quickbooks_id=qb_id,
        defaults=defaults,
    )
    _link(connection, "customer", obj.id, qb_id, payload.get("SyncToken", ""))
    return created


@transaction.atomic
def _upsert_vendor(connection, payload):
    qb_id = str(payload["Id"])
    defaults = {
        "name": payload.get("DisplayName") or payload.get("PrintOnCheckName") or f"QuickBooks Vendor {qb_id}",
        "email": _email(payload.get("PrimaryEmailAddr")),
        "phone": _phone(payload.get("PrimaryPhone")),
        "address": _address(payload.get("BillAddr")),
        "quickbooks_sync_token": payload.get("SyncToken", ""),
        "quickbooks_last_synced_at": timezone.now(),
        "payment_terms": _ref_name(payload.get("TermRef")),
        "tax_id": payload.get("TaxIdentifier", ""),
        "outstanding_balance": _decimal(payload.get("Balance")),
    }
    obj, created = Vendor.objects.update_or_create(
        company=connection.company,
        quickbooks_id=qb_id,
        defaults=defaults,
    )
    _link(connection, "vendor", obj.id, qb_id, payload.get("SyncToken", ""))
    return created


@transaction.atomic
def _upsert_item(connection, payload):
    qb_id = str(payload["Id"])
    item_type = payload.get("Type", "")
    category = "raw_material" if item_type == "Inventory" else "finished_good"
    purchase_cost = _decimal(payload.get("PurchaseCost"))
    defaults = {
        "name": payload.get("Name") or payload.get("FullyQualifiedName") or f"QuickBooks Item {qb_id}",
        "category": category,
        "unit": "unit",
        "selling_price": _decimal(payload.get("UnitPrice")),
        "quickbooks_sync_token": payload.get("SyncToken", ""),
        "quickbooks_last_synced_at": timezone.now(),
        "sku": payload.get("Sku", ""),
        "purchase_cost": purchase_cost,
        "reorder_point": payload.get("ReorderPoint"),
    }
    obj, created = Item.objects.update_or_create(
        company=connection.company,
        quickbooks_id=qb_id,
        defaults=defaults,
    )

    # Ensure QB Inventory items are always marked as raw_material
    # (in case they were previously synced with wrong category)
    if item_type == "Inventory" and obj.category != "raw_material":
        obj.category = "raw_material"
        obj.save(update_fields=["category"])

    # Add to QuickBooks catalogue with the purchase cost
    if purchase_cost > 0:
        catalogue_vendor = _get_or_create_catalogue_vendor(connection.company)
        VendorPriceList.objects.update_or_create(
            vendor=catalogue_vendor,
            item=obj,
            defaults={
                "unit_price": purchase_cost,
                "currency": "USD",
                "min_order_qty": 1,
                "lead_time_days": 7,
                "is_active": True,
            },
        )

    _link(connection, "item", obj.id, qb_id, payload.get("SyncToken", ""))
    return created


@transaction.atomic
def _upsert_estimate(connection, payload):
    qb_id = str(payload["Id"])
    customer = _customer_for_ref(connection, payload.get("CustomerRef"))
    total = _decimal(payload.get("TotalAmt"))
    defaults = {
        "customer": customer,
        "total_amount": total,
        "quickbooks_sync_token": payload.get("SyncToken", ""),
        "quickbooks_last_synced_at": timezone.now(),
        "status": _estimate_status(payload),
    }
    order, created = SalesOrder.objects.update_or_create(
        quickbooks_id=qb_id,
        customer__company=connection.company,
        defaults=defaults,
    )
    _replace_sales_order_lines(connection, order, payload.get("Line", []))
    _link(connection, "sales_order", order.id, qb_id, payload.get("SyncToken", ""))
    return created


@transaction.atomic
def _upsert_invoice(connection, payload):
    qb_id = str(payload["Id"])
    customer = _customer_for_ref(connection, payload.get("CustomerRef"))
    total = _decimal(payload.get("TotalAmt"))
    balance = _decimal(payload.get("Balance"))
    amount_paid = max(Decimal("0"), total - balance)
    defaults = {
        "company": connection.company,
        "customer": customer,
        "invoice_date": _date(payload.get("TxnDate")),
        "due_date": _date(payload.get("DueDate")) if payload.get("DueDate") else None,
        "total_amount": total,
        "amount_paid": amount_paid,
        "status": _invoice_status(total, balance),
        "quickbooks_sync_token": payload.get("SyncToken", ""),
        "quickbooks_last_synced_at": timezone.now(),
    }
    invoice, created = Invoice.objects.update_or_create(
        company=connection.company,
        quickbooks_id=qb_id,
        defaults=defaults,
    )
    _replace_invoice_lines(connection, invoice, payload.get("Line", []))
    _link(connection, "invoice", invoice.id, qb_id, payload.get("SyncToken", ""))
    return created


@transaction.atomic
def _upsert_payment(connection, payload):
    qb_id = str(payload["Id"])
    customer = _customer_for_ref(connection, payload.get("CustomerRef"))
    invoice = _linked_invoice(connection, payload.get("Line", []))
    defaults = {
        "company": connection.company,
        "customer": customer,
        "invoice": invoice,
        "amount": _decimal(payload.get("TotalAmt")),
        "payment_date": _date(payload.get("TxnDate")),
        "method": "other",
        "reference": payload.get("PaymentRefNum", "")[:100],
        "quickbooks_sync_token": payload.get("SyncToken", ""),
        "quickbooks_last_synced_at": timezone.now(),
    }
    payment, created = CustomerPayment.objects.update_or_create(
        company=connection.company,
        quickbooks_id=qb_id,
        defaults=defaults,
    )
    _link(connection, "payment", payment.id, qb_id, payload.get("SyncToken", ""))
    return created


@transaction.atomic
def _upsert_purchase_order(connection, payload):
    qb_id = str(payload["Id"])
    vendor = _vendor_for_ref(connection, payload.get("VendorRef"))
    total = _decimal(payload.get("TotalAmt"))
    defaults = {
        "vendor": vendor,
        "total_amount": total,
        "quickbooks_sync_token": payload.get("SyncToken", ""),
        "quickbooks_last_synced_at": timezone.now(),
        "status": _purchase_order_status(payload),
    }
    order, created = PurchaseOrder.objects.update_or_create(
        quickbooks_id=qb_id,
        vendor__company=connection.company,
        defaults=defaults,
    )
    _replace_purchase_order_lines(connection, order, payload.get("Line", []))
    _link(connection, "purchase_order", order.id, qb_id, payload.get("SyncToken", ""))
    return created


@transaction.atomic
def _upsert_bill(connection, payload):
    qb_id = str(payload["Id"])
    vendor = _vendor_for_ref(connection, payload.get("VendorRef"))
    total = _decimal(payload.get("TotalAmt"))
    balance = _decimal(payload.get("Balance"))
    amount_paid = max(Decimal("0"), total - balance)
    defaults = {
        "company": connection.company,
        "vendor": vendor,
        "bill_date": _date(payload.get("TxnDate")),
        "due_date": _date(payload.get("DueDate")) if payload.get("DueDate") else None,
        "total_amount": total,
        "status": _bill_status(total, balance),
        "bill_number": payload.get("DocNumber", "")[:100],
        "quickbooks_sync_token": payload.get("SyncToken", ""),
        "quickbooks_last_synced_at": timezone.now(),
    }
    bill, created = Bill.objects.update_or_create(
        company=connection.company,
        quickbooks_id=qb_id,
        defaults=defaults,
    )
    _replace_bill_lines(connection, bill, payload.get("Line", []))
    _link(connection, "bill", bill.id, qb_id, payload.get("SyncToken", ""))
    return created


def _link(connection, entity_type, local_id, qb_id, sync_token):
    QuickBooksEntityLink.objects.update_or_create(
        company=connection.company,
        entity_type=entity_type,
        quickbooks_id=qb_id,
        defaults={
            "local_object_id": local_id,
            "sync_token": sync_token,
            "last_synced_at": timezone.now(),
        },
    )


def _add_totals(totals, result):
    totals["created"] += result["created"]
    totals["updated"] += result["updated"]
    totals["seen"] += result["seen"]


def _email(value):
    return (value or {}).get("Address", "")


def _phone(value):
    return (value or {}).get("FreeFormNumber", "")


def _ref_name(value):
    return (value or {}).get("name", "") or (value or {}).get("value", "")


def _decimal(value):
    return Decimal(str(value or "0"))


def _date(value):
    return datetime.strptime(value, "%Y-%m-%d").date() if value else timezone.localdate()


def _customer_for_ref(connection, ref):
    qb_id = str((ref or {}).get("value") or "")
    name = (ref or {}).get("name") or f"QuickBooks Customer {qb_id or 'Unknown'}"
    if qb_id:
        customer = Customer.objects.filter(company=connection.company, quickbooks_id=qb_id).first()
        if customer:
            return customer
    customer, _ = Customer.objects.update_or_create(
        company=connection.company,
        quickbooks_id=qb_id,
        defaults={
            "name": name,
            "quickbooks_last_synced_at": timezone.now(),
        },
    )
    return customer


def _item_for_ref(connection, ref, fallback_name="QuickBooks Sales Item"):
    qb_id = str((ref or {}).get("value") or "")
    name = (ref or {}).get("name") or fallback_name
    if qb_id:
        item = Item.objects.filter(company=connection.company, quickbooks_id=qb_id).first()
        if item:
            return item
    item, _ = Item.objects.update_or_create(
        company=connection.company,
        quickbooks_id=qb_id,
        defaults={
            "name": name,
            "category": "finished_good",
            "unit": "unit",
            "quickbooks_last_synced_at": timezone.now(),
        },
    )
    return item


def _sales_line_rows(connection, lines):
    for line in lines or []:
        detail = line.get("SalesItemLineDetail")
        if not detail:
            continue
        item = _item_for_ref(connection, detail.get("ItemRef"), line.get("Description") or "QuickBooks Sales Item")
        quantity = float(detail.get("Qty") or 1)
        unit_price = _decimal(detail.get("UnitPrice"))
        amount = _decimal(line.get("Amount"))
        if not unit_price and quantity:
            unit_price = amount / Decimal(str(quantity))
        yield item, quantity, unit_price, amount, line.get("Description") or item.name


def _replace_sales_order_lines(connection, order, lines):
    order.salesorderitem_set.all().delete()
    for item, quantity, unit_price, amount, description in _sales_line_rows(connection, lines):
        SalesOrderItem.objects.create(sales_order=order, item=item, quantity=quantity)


def _replace_invoice_lines(connection, invoice, lines):
    invoice.lines.all().delete()
    for item, quantity, unit_price, amount, description in _sales_line_rows(connection, lines):
        InvoiceLine.objects.create(
            invoice=invoice,
            item=item,
            description=description,
            quantity=quantity,
            unit_price=unit_price,
            amount=amount,
        )


def _linked_invoice(connection, lines):
    for line in lines or []:
        for txn in line.get("LinkedTxn", []) or []:
            if txn.get("TxnType") == "Invoice":
                return Invoice.objects.filter(
                    company=connection.company,
                    quickbooks_id=str(txn.get("TxnId")),
                ).first()
    return None


def _invoice_status(total, balance):
    if balance <= 0:
        return "paid"
    if balance < total:
        return "partial"
    return "open"


def _estimate_status(payload):
    status = (payload.get("TxnStatus") or "").lower()
    if status in {"rejected", "closed"}:
        return "cancelled"
    if status in {"accepted", "converted"}:
        return "confirmed"
    return "pending"


def _address(value):
    if not value:
        return ""
    parts = [
        value.get("Line1"),
        value.get("Line2"),
        value.get("City"),
        value.get("CountrySubDivisionCode"),
        value.get("PostalCode"),
        value.get("Country"),
    ]
    return ", ".join(part for part in parts if part)


def _vendor_for_ref(connection, ref):
    qb_id = str((ref or {}).get("value") or "")
    name = (ref or {}).get("name") or f"QuickBooks Vendor {qb_id or 'Unknown'}"
    if qb_id:
        vendor = Vendor.objects.filter(company=connection.company, quickbooks_id=qb_id).first()
        if vendor:
            return vendor
    vendor, _ = Vendor.objects.update_or_create(
        company=connection.company,
        quickbooks_id=qb_id,
        defaults={
            "name": name,
            "quickbooks_last_synced_at": timezone.now(),
        },
    )
    return vendor


def _get_or_create_catalogue_vendor(company):
    """Get or create a generic QuickBooks Catalogue vendor for price list entries."""
    vendor, _ = Vendor.objects.get_or_create(
        company=company,
        name="QuickBooks Catalogue",
        defaults={
            "category": "raw_material",
        },
    )
    return vendor


def _purchase_order_lines(connection, lines):
    for line in lines or []:
        detail = line.get("PurchaseOrderItemLineDetail")
        if not detail:
            continue
        item = _item_for_ref(connection, detail.get("ItemRef"), line.get("Description") or "QuickBooks Item")
        quantity = float(detail.get("Qty") or 1)
        unit_price = _decimal(detail.get("UnitPrice"))
        amount = _decimal(line.get("Amount"))
        if not unit_price and quantity:
            unit_price = amount / Decimal(str(quantity))
        yield item, quantity, unit_price


def _replace_purchase_order_lines(connection, order, lines):
    order.items.all().delete()
    for item, quantity, unit_price in _purchase_order_lines(connection, lines):
        PurchaseOrderItem.objects.create(purchase_order=order, item=item, quantity=quantity, unit_price=unit_price)


def _bill_lines(connection, lines):
    for line in lines or []:
        detail = line.get("LineDetail")
        if not detail:
            continue
        item = _item_for_ref(connection, detail.get("ItemRef"), line.get("Description") or "QuickBooks Item")
        quantity = float(detail.get("Qty") or 1)
        unit_price = _decimal(detail.get("UnitPrice"))
        amount = _decimal(line.get("Amount"))
        if not unit_price and quantity:
            unit_price = amount / Decimal(str(quantity))
        yield item, quantity, unit_price, amount, line.get("Description") or item.name


def _replace_bill_lines(connection, bill, lines):
    bill.lines.all().delete()
    for item, quantity, unit_price, amount, description in _bill_lines(connection, lines):
        BillLine.objects.create(
            bill=bill,
            item=item,
            description=description,
            quantity=quantity,
            unit_price=unit_price,
            amount=amount,
        )


def _purchase_order_status(payload):
    status = (payload.get("TxnStatus") or "").lower()
    if status in {"rejected", "closed"}:
        return "cancelled"
    if status in {"accepted"}:
        return "ordered"
    return "pending"


def _bill_status(total, balance):
    if balance <= 0:
        return "paid"
    if balance < total:
        return "open"
    return "open"
