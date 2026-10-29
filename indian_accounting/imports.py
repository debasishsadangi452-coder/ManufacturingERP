"""
Controlled data imports (CSV or Excel): upload -> validate every row -> preview -> explicit commit.

Kinds: opening_balances, chart_of_accounts, tax_codes, open_invoices, opening_stock.
Nothing is written until commit. Each batch keeps its parsed rows, row-level errors and warnings, the file hash
(a committed file can never be imported twice) and an audit entry. Opening-type imports are one-time and only
allowed before the first real voucher is posted; they must reconcile to the ledger openings.
"""
import csv
import hashlib
import io
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .engine import audit
from .models import (AccountGroup, FinancialYear, ImportBatch, InventoryMovement, Ledger, LedgerMapping, TaxCode, Voucher)

ZERO = Decimal("0.00")
MAX_ROWS = 5000

TEMPLATES = {
    "opening_balances": "code,name,group,side,amount,gstin,state_code\n1001,Cash,Cash-in-Hand,Dr,50000,,\n2001,Capital,Capital Account,Cr,50000,,\n",
    "chart_of_accounts": "type,name,parent,code,nature,category,control_type\ngroup,Plant & Machinery,Fixed Assets,,,,\nledger,Lathe Machine,Plant & Machinery,1501,,,\n",
    "tax_codes": "code,name,rate,treatment,hsn,itc_eligible,effective_from,effective_to\nGST18,GST 18%,18,taxable,,yes,2017-07-01,\n",
    "open_invoices": "type,party_code,number,date,due_date,amount\nsales,C000001,INV-0001,2026-03-10,2026-04-10,11800\npurchase,S000001,B-77,2026-03-20,2026-04-20,5900\n",
    "opening_stock": "item,class,qty,value\nSugar,raw_material,100,200\n",
}
ONCE = {"opening_balances", "open_invoices", "opening_stock"}      # one-time loads
CLEAN_BOOKS = {"opening_balances", "open_invoices", "opening_stock"}  # only before real vouchers exist


def normalise_kind(kind):
    k = (kind or "").replace("-", "_")
    if k not in TEMPLATES:
        raise ValidationError("Unknown import type.")
    return k


# ---------------------------------------------------------------- reading files
def read_table(raw: bytes, filename: str):
    """List of {lower-case header: stripped text} dicts from a .csv or .xlsx upload."""
    name = (filename or "").lower()
    if name.endswith(".xlsx"):
        try:
            import openpyxl
        except ImportError:
            raise ValidationError("Excel import is unavailable on this server; please upload a CSV file.")
        try:
            wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
        except Exception:
            raise ValidationError("Could not read the Excel file.")
        ws = wb.active
        it = ws.iter_rows(values_only=True)
        header = next(it, None)
        if not header:
            raise ValidationError("The spreadsheet is empty.")
        keys = [str(h).strip().lower() if h is not None else "" for h in header]
        out = []
        for values in it:
            if all(v is None or str(v).strip() == "" for v in values):
                continue
            rec = {}
            for k, v in zip(keys, values):
                if isinstance(v, (datetime, date)):
                    v = v.date().isoformat() if isinstance(v, datetime) else v.isoformat()
                rec[k] = "" if v is None else str(v).strip()
            out.append(rec)
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ValidationError("File must be UTF-8 CSV or an .xlsx workbook.")
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            raise ValidationError("The file is empty.")
        out = [{(k or "").strip().lower(): (v or "").strip() for k, v in rec.items()} for rec in reader
               if any((v or "").strip() for v in rec.values())]
    if len(out) > MAX_ROWS:
        raise ValidationError(f"Too many rows (limit {MAX_ROWS}).")
    return out


def _need(table, cols):
    have = set(table[0].keys()) if table else set()
    missing = [c for c in cols if c not in have]
    if table and missing:
        raise ValidationError(f"Missing column(s): {', '.join(missing)}.")


def _dec(v, label):
    try:
        return Decimal(str(v).replace(",", "")).quantize(ZERO)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{label} is not a number.")


def _date(v, label):
    v = (v or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(v[:10] if fmt == "%Y-%m-%d" else v, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"{label} must be a date (YYYY-MM-DD or DD/MM/YYYY).")


def _yes(v):
    return str(v).strip().lower() in ("yes", "y", "true", "1", "")


# ---------------------------------------------------------------- per-kind validation
def parse_opening_balances(company, table):
    _need(table, ["code", "side", "amount"])
    groups = {g.name.lower(): g for g in AccountGroup.objects.filter(company=company)}
    existing = {l.code: l for l in Ledger.objects.filter(company=company)}
    rows, errors, seen = [], [], set()
    for n, r in enumerate(table, start=2):
        err, code = None, r.get("code", "")
        try:
            amount = _dec(r.get("amount", ""), "Amount")
        except ValueError as e:
            amount, err = ZERO, str(e)
        side = r.get("side", "").capitalize()
        if err:
            pass
        elif not code:
            err = "Code is required."
        elif code in seen:
            err = f"Duplicate code {code} in file."
        elif amount < 0:
            err = "Amount cannot be negative."
        elif side not in ("Dr", "Cr"):
            err = "Side must be Dr or Cr."
        elif code not in existing:
            if not r.get("name"):
                err = f"Ledger {code} does not exist; a name and group are required to create it."
            elif groups.get(r.get("group", "").lower()) is None:
                err = f"Unknown group '{r.get('group', '')}'."
        if not err and r.get("state_code") and not (r["state_code"].isdigit() and len(r["state_code"]) == 2):
            err = "state_code must be 2 digits."
        seen.add(code)
        if err:
            errors.append({"row": n, "message": err})
        else:
            rows.append({"row": n, "code": code, "name": r.get("name", ""), "group": r.get("group", ""), "side": side,
                         "amount": str(amount), "gstin": r.get("gstin", ""), "state_code": r.get("state_code", ""),
                         "new": code not in existing})
    return rows, errors, []


def _opening_totals(company, rows):
    incoming = {r["code"] for r in rows}
    dr = cr = ZERO
    for l in Ledger.objects.filter(company=company):
        if l.code in incoming:
            continue
        if l.opening_side == "Dr":
            dr += l.opening_balance
        else:
            cr += l.opening_balance
    for r in rows:
        if r["side"] == "Dr":
            dr += Decimal(r["amount"])
        else:
            cr += Decimal(r["amount"])
    return dr, cr


def parse_chart(company, table):
    _need(table, ["type", "name", "parent"])
    nature_ok = {"asset", "liability", "income", "expense"}
    controls = {c for c, _ in Ledger.CONTROL if c}
    db_groups = {g.name.lower(): g for g in AccountGroup.objects.filter(company=company)}
    file_groups = {}
    codes = {l.code for l in Ledger.objects.filter(company=company)}
    rows, errors = [], []
    for n, r in enumerate(table, start=2):
        t, name, parent = r.get("type", "").lower(), r.get("name", ""), r.get("parent", "")
        err = None
        if not name:
            err = "Name is required."
        elif t == "group":
            pg = db_groups.get(parent.lower()) or file_groups.get(parent.lower())
            if name.lower() in db_groups or name.lower() in file_groups:
                err = f"Group '{name}' already exists."
            elif parent and pg is None:
                err = f"Parent group '{parent}' not found (list parents before children)."
            else:
                nature = r.get("nature", "").lower() or (pg["nature"] if isinstance(pg, dict) else pg.nature if pg else "")
                if nature not in nature_ok:
                    err = "nature must be asset, liability, income or expense for a top-level group."
                else:
                    file_groups[name.lower()] = {"nature": nature}
                    rows.append({"row": n, "type": "group", "name": name, "parent": parent, "nature": nature,
                                 "category": r.get("category", "")})
        elif t == "ledger":
            code = r.get("code", "")
            if not code:
                err = "Ledger code is required."
            elif code in codes:
                err = f"Ledger code {code} already exists or is repeated."
            elif db_groups.get(parent.lower()) is None and parent.lower() not in file_groups:
                err = f"Group '{parent}' not found."
            elif r.get("control_type") and r["control_type"] not in controls:
                err = f"control_type must be one of: {', '.join(sorted(controls))}."
            else:
                codes.add(code)
                rows.append({"row": n, "type": "ledger", "name": name, "parent": parent, "code": code,
                             "control_type": r.get("control_type", "")})
        else:
            err = "type must be 'group' or 'ledger'."
        if err:
            errors.append({"row": n, "message": err})
    return rows, errors, []


def parse_tax_codes(company, table):
    _need(table, ["code", "name", "rate", "effective_from"])
    treatments = {t for t, _ in TaxCode.TREATMENT}
    existing = list(TaxCode.objects.filter(company=company))
    rows, errors = [], []
    ranges = [(t.code, t.effective_from, t.effective_to) for t in existing]
    for n, r in enumerate(table, start=2):
        try:
            rate = _dec(r.get("rate", ""), "Rate")
            start = _date(r.get("effective_from", ""), "effective_from")
            end = _date(r["effective_to"], "effective_to") if r.get("effective_to") else None
            code, treat = r.get("code", ""), (r.get("treatment") or "taxable").lower()
            if not code or not r.get("name"):
                raise ValueError("Code and name are required.")
            if not (ZERO <= rate <= 100):
                raise ValueError("Rate must be between 0 and 100.")
            if treat not in treatments:
                raise ValueError(f"treatment must be one of: {', '.join(sorted(treatments))}.")
            if end and end <= start:
                raise ValueError("effective_to must be after effective_from.")
            for c, s, e in ranges:
                if c == code and start <= (e or date.max) and s <= (end or date.max):
                    raise ValueError(f"{code} already has a rate in force for part of this period; end-date it first.")
            ranges.append((code, start, end))
            rows.append({"row": n, "code": code, "name": r["name"], "rate": str(rate), "treatment": treat,
                         "hsn": r.get("hsn", ""), "itc_eligible": _yes(r.get("itc_eligible", "")),
                         "effective_from": start.isoformat(), "effective_to": end.isoformat() if end else ""})
        except ValueError as e:
            errors.append({"row": n, "message": str(e)})
    return rows, errors, []


def parse_open_invoices(company, table):
    _need(table, ["type", "party_code", "number", "date", "amount"])
    ledgers = {l.code: l for l in Ledger.objects.filter(company=company)}
    rows, errors, warnings, seen, per_party = [], [], [], set(), {}
    for n, r in enumerate(table, start=2):
        try:
            kind = r.get("type", "").lower()
            if kind not in ("sales", "purchase"):
                raise ValueError("type must be sales or purchase.")
            party = ledgers.get(r.get("party_code", ""))
            want = "receivable" if kind == "sales" else "payable"
            if party is None or party.control_type != want:
                raise ValueError(f"party_code must be an existing {want} ledger.")
            number = r.get("number", "")
            if not number:
                raise ValueError("number is required.")
            key = (kind, party.code, number)
            if key in seen or Voucher.objects.filter(company=company, voucher_type=kind, party_ledger=party,
                                                     voucher_number=number).exists():
                raise ValueError(f"{kind} document {number} for {party.code} already exists.")
            amount = _dec(r.get("amount", ""), "Amount")
            if amount <= 0:
                raise ValueError("Amount must be positive.")
            d = _date(r.get("date", ""), "date")
            due = _date(r["due_date"], "due_date") if r.get("due_date") else d
            seen.add(key)
            per_party[(kind, party.code)] = per_party.get((kind, party.code), ZERO) + amount
            rows.append({"row": n, "type": kind, "party_code": party.code, "party": party.name, "number": number,
                         "date": d.isoformat(), "due_date": due.isoformat(), "amount": str(amount)})
        except ValueError as e:
            errors.append({"row": n, "message": str(e)})
    # reconcile to the party ledgers' opening balances (a receivable is Dr, a payable is Cr)
    for (kind, code), total in per_party.items():
        l = ledgers[code]
        expected_side = "Dr" if kind == "sales" else "Cr"
        have = l.opening_balance if l.opening_side == expected_side else -l.opening_balance
        if have != total:
            errors.append({"row": 0, "message": (
                f"{l.name} ({code}): listed documents total {total} but the ledger's opening balance is "
                f"{l.opening_balance} {l.opening_side}. Import opening balances first and make them agree.")})
    listed = {c for _, c in per_party}
    for l in ledgers.values():
        if l.control_type in ("receivable", "payable") and l.opening_balance and l.code not in listed:
            warnings.append({"row": 0, "message": f"{l.name} ({l.code}) has an opening balance of {l.opening_balance} "
                                                  f"{l.opening_side} but no outstanding documents listed."})
    return rows, errors, warnings


def parse_opening_stock(company, table):
    from inventory.models import Item
    _need(table, ["item", "class", "qty", "value"])
    items = {}
    for it in Item.objects.filter(company=company):
        items.setdefault(it.name.lower(), []).append(it)
    rows, errors, seen, totals = [], [], set(), {"raw_material": ZERO, "finished_goods": ZERO}
    for n, r in enumerate(table, start=2):
        try:
            cands = items.get(r.get("item", "").lower(), [])
            if len(cands) != 1:
                raise ValueError(f"Item '{r.get('item', '')}' " + ("not found." if not cands else "matches more than one item."))
            cls = r.get("class", "").lower()
            if cls not in totals:
                raise ValueError("class must be raw_material or finished_goods.")
            qty, value = _dec(r.get("qty", ""), "qty"), _dec(r.get("value", ""), "value")
            if qty <= 0 or value < 0:
                raise ValueError("qty must be positive and value cannot be negative.")
            if cands[0].id in seen:
                raise ValueError(f"Item '{r['item']}' is listed twice.")
            seen.add(cands[0].id)
            totals[cls] += value
            rows.append({"row": n, "item_id": cands[0].id, "item": cands[0].name, "class": cls, "qty": str(qty),
                         "value": str(value)})
        except ValueError as e:
            errors.append({"row": n, "message": str(e)})
    warnings = []
    for cls, total in totals.items():
        try:
            l = LedgerMapping.objects.get(company=company, key=cls).ledger
        except LedgerMapping.DoesNotExist:
            continue
        opening = l.opening_balance if l.opening_side == "Dr" else -l.opening_balance
        if total != opening:
            errors.append({"row": 0, "message": (
                f"{cls.replace('_', ' ').title()}: stock listed is worth {total} but the '{l.name}' ledger opens at "
                f"{opening}. Import opening balances first and make them agree.")})
    return rows, errors, warnings


PARSERS = {"opening_balances": parse_opening_balances, "chart_of_accounts": parse_chart, "tax_codes": parse_tax_codes,
           "open_invoices": parse_open_invoices, "opening_stock": parse_opening_stock}


# ---------------------------------------------------------------- gates, preview, commit
def _real_vouchers(company):
    return Voucher.objects.filter(company=company, is_opening=False, status__in=["posted", "reversed"]).exists()


def _gate(company, kind, digest=None):
    if digest and ImportBatch.objects.filter(company=company, file_hash=digest, status="committed").exists():
        raise ValidationError("This file has already been imported.")
    if kind in ONCE and ImportBatch.objects.filter(company=company, kind=kind, status="committed").exists():
        raise ValidationError("This has already been imported for this company and can only be loaded once.")
    if kind in CLEAN_BOOKS and _real_vouchers(company):
        raise ValidationError("Opening data can only be imported before any voucher is posted.")


@transaction.atomic
def create_preview(company, user, raw, filename, kind="opening_balances"):
    kind = normalise_kind(kind)
    digest = hashlib.sha256(raw).hexdigest()
    _gate(company, kind, digest)
    table = read_table(raw, filename)
    if not table:
        rows, errors, warnings = [], [{"row": 0, "message": "The file has no data rows."}], []
    else:
        rows, errors, warnings = PARSERS[kind](company, table)
    dr = cr = ZERO
    if kind == "opening_balances" and not errors:
        dr, cr = _opening_totals(company, rows)
        if dr != cr:
            errors.append({"row": 0, "message": f"Opening balances do not balance: debit {dr} != credit {cr}."})
    return ImportBatch.objects.create(
        company=company, kind=kind, source_file=(filename or "upload")[:200],
        source_system="xlsx" if (filename or "").lower().endswith(".xlsx") else "csv", file_hash=digest,
        rows=rows, errors=errors, warnings=warnings, total_debit=dr, total_credit=cr,
        created_by=user if getattr(user, "is_authenticated", False) else None)


def _fy_start(company):
    fy = FinancialYear.objects.filter(company=company).order_by("start_date").first()
    return fy


@transaction.atomic
def commit(company, user, batch_id):
    b = ImportBatch.objects.select_for_update().filter(company=company, pk=batch_id).first()
    if b is None:
        raise ValidationError("Import batch not found.")
    if b.status != "preview":
        raise ValidationError(f"Batch is already {b.status}.")
    if b.errors:
        raise ValidationError("Fix the errors in the file and upload it again; nothing was imported.")
    _gate(company, b.kind)
    # re-validate against the data as it is now (it may have changed since the preview)
    fresh = PARSERS[b.kind](company, read_rows_for_recheck(b))
    if fresh[1]:
        raise ValidationError("The data changed since the preview and no longer validates; upload the file again.")
    APPLY[b.kind](company, user, b)
    b.status, b.committed_at = "committed", timezone.now()
    b.committed_by = user if getattr(user, "is_authenticated", False) else None
    b.save()
    audit(company, user, "import_batch", b.pk, "commit", after={"kind": b.kind, "rows": len(b.rows)})
    return b


def read_rows_for_recheck(b):
    """Rebuild the parser's input from the stored, already-validated rows."""
    if b.kind == "opening_balances":
        return [{"code": r["code"], "name": r["name"], "group": r["group"], "side": r["side"], "amount": r["amount"],
                 "gstin": r["gstin"], "state_code": r["state_code"]} for r in b.rows]
    if b.kind == "chart_of_accounts":
        return [{"type": r["type"], "name": r["name"], "parent": r["parent"], "code": r.get("code", ""),
                 "nature": r.get("nature", ""), "category": r.get("category", ""), "control_type": r.get("control_type", "")}
                for r in b.rows]
    if b.kind == "tax_codes":
        return [{"code": r["code"], "name": r["name"], "rate": r["rate"], "treatment": r["treatment"], "hsn": r["hsn"],
                 "itc_eligible": "yes" if r["itc_eligible"] else "no", "effective_from": r["effective_from"],
                 "effective_to": r["effective_to"]} for r in b.rows]
    if b.kind == "open_invoices":
        return [{"type": r["type"], "party_code": r["party_code"], "number": r["number"], "date": r["date"],
                 "due_date": r["due_date"], "amount": r["amount"]} for r in b.rows]
    return [{"item": r["item"], "class": r["class"], "qty": r["qty"], "value": r["value"]} for r in b.rows]


# ---------------------------------------------------------------- applying a batch
def apply_opening_balances(company, user, b):
    dr, cr = _opening_totals(company, b.rows)
    if dr != cr:
        raise ValidationError(f"Opening balances do not balance: debit {dr} != credit {cr}.")
    groups = {g.name.lower(): g for g in AccountGroup.objects.filter(company=company)}
    fy = _fy_start(company)
    day = fy.start_date if fy else timezone.localdate()
    for r in b.rows:
        l = Ledger.objects.filter(company=company, code=r["code"]).first()
        if l is None:
            l = Ledger(company=company, code=r["code"], name=r["name"], group=groups[r["group"].lower()])
        if r["gstin"]:
            l.gstin = r["gstin"]
        if r["state_code"]:
            l.state_code, l.state_source = r["state_code"], "manual"
        l.opening_balance, l.opening_side, l.opening_date = Decimal(r["amount"]), r["side"], day
        l.full_clean()
        l.save()
    b.total_debit, b.total_credit = dr, cr


def apply_chart(company, user, b):
    for r in b.rows:
        if r["type"] == "group":
            parent = AccountGroup.objects.filter(company=company, name__iexact=r["parent"]).first() if r["parent"] else None
            AccountGroup.objects.create(company=company, name=r["name"], parent=parent, nature=r["nature"],
                                        category=r["category"])
    for r in b.rows:
        if r["type"] == "ledger":
            g = AccountGroup.objects.get(company=company, name__iexact=r["parent"])
            l = Ledger(company=company, code=r["code"], name=r["name"], group=g, control_type=r["control_type"])
            l.full_clean()
            l.save()


def apply_tax_codes(company, user, b):
    for r in b.rows:
        TaxCode.objects.create(
            company=company, code=r["code"], name=r["name"], gst_rate=Decimal(r["rate"]), treatment=r["treatment"],
            hsn_sac=r["hsn"], itc_eligible=r["itc_eligible"], effective_from=date.fromisoformat(r["effective_from"]),
            effective_to=date.fromisoformat(r["effective_to"]) if r["effective_to"] else None)


def apply_open_invoices(company, user, b):
    fy = _fy_start(company)
    if fy is None:
        raise ValidationError("No financial year is set up.")
    now = timezone.now()
    for r in b.rows:
        party = Ledger.objects.get(company=company, code=r["party_code"])
        v = Voucher(company=company, financial_year=fy, voucher_type=r["type"], voucher_number=r["number"],
                    voucher_date=date.fromisoformat(r["date"]), due_date=date.fromisoformat(r["due_date"]),
                    status="posted", is_opening=True, party_ledger=party, total_amount=Decimal(r["amount"]),
                    narration="Opening outstanding document (imported)", posted_at=now,
                    posted_by=user if getattr(user, "is_authenticated", False) else None,
                    created_by=user if getattr(user, "is_authenticated", False) else None,
                    supplier_invoice_no=r["number"] if r["type"] == "purchase" else "",
                    source_module="import", source_document_type="open_invoice",
                    source_document_id=f"{b.pk}-{r['row']}", source_event="opening")
        v._allow_status_change = True
        v.save()


def apply_opening_stock(company, user, b):
    from inventory.models import Item
    fy = _fy_start(company)
    day = fy.start_date if fy else timezone.localdate()
    for r in b.rows:
        InventoryMovement.objects.create(company=company, item=Item.objects.get(pk=r["item_id"]), qty=Decimal(r["qty"]),
                                         value=Decimal(r["value"]), kind="opening", voucher=None, movement_date=day)


APPLY = {"opening_balances": apply_opening_balances, "chart_of_accounts": apply_chart, "tax_codes": apply_tax_codes,
         "open_invoices": apply_open_invoices, "opening_stock": apply_opening_stock}
