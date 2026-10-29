"""Authenticated, company-scoped API. No generic CRUD on posted accounting data."""
from datetime import date

from django.core.exceptions import ValidationError
from rest_framework import serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permission import IsAdmin, IsFinanceOrAdmin

from . import engine, gst, reports, seeds
from .models import InventoryMovement, AccountingPeriod, FinancialYear, ImportBatch, AccountGroup, AccountingMode, AuditLog, BillTaxDetail, IndianCompanyProfile, Ledger, PostingEvent, TaxCode, Voucher


def _err(e):
    msgs = e.messages if hasattr(e, "messages") else [str(e)]
    return Response({"detail": msgs[0], "errors": msgs}, status=status.HTTP_400_BAD_REQUEST)


def _company(request):
    return request.user.company


def _dates(request):
    try:
        today = date.today()
        fy_from = date.fromisoformat(request.query_params.get("from") or seeds.financial_year_bounds(today)[1].isoformat())
        fy_to = date.fromisoformat(request.query_params.get("to") or today.isoformat())
    except ValueError:
        raise ValidationError("Dates must be YYYY-MM-DD.")
    return fy_from, fy_to


def mode_for(company):
    row = AccountingMode.objects.filter(company=company).first() if company else None
    return row.mode if row else "standard"


class IsIndianMode(IsFinanceOrAdmin):
    def has_permission(self, request, view):
        return super().has_permission(request, view) and mode_for(request.user.company) == "indian"


class ModeView(APIView):
    """GET: current mode (defaults to the existing system). POST: choose at onboarding (admin, once)."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        c = _company(request)
        row = AccountingMode.objects.filter(company=c).first() if c else None
        return Response({"mode": row.mode if row else "standard", "chosen": row is not None})

    def post(self, request):
        if request.user.role != "admin":
            return Response({"detail": "Only an admin can choose the accounting system."}, status=403)
        c = _company(request)
        mode = request.data.get("mode")
        if mode not in ("standard", "indian"):
            return Response({"detail": "mode must be 'standard' or 'indian'."}, status=400)
        existing = AccountingMode.objects.filter(company=c).first()
        if existing:
            if existing.mode != mode:
                return Response({"detail": "The accounting system is already chosen and cannot be changed."}, status=409)
            return Response({"mode": existing.mode, "chosen": True})
        state = str(request.data.get("state_code", "")).strip()
        if mode == "indian" and not state.isdigit():
            return Response({"detail": "state_code (GST state code) is required for the Indian system."}, status=400)
        from django.db import transaction
        with transaction.atomic():
            AccountingMode.objects.create(company=c, mode=mode, chosen_by=request.user)
            if mode == "indian":
                seeds.seed_company(c, state_code=state.zfill(2), gstin=request.data.get("gstin", ""),
                                   pan=request.data.get("pan", ""), legal_name=request.data.get("legal_name", ""))
        return Response({"mode": mode, "chosen": True}, status=201)


class LineSerializer(serializers.Serializer):
    ledger = serializers.IntegerField()
    debit = serializers.DecimalField(max_digits=18, decimal_places=2, required=False, default=0)
    credit = serializers.DecimalField(max_digits=18, decimal_places=2, required=False, default=0)
    narration = serializers.CharField(required=False, allow_blank=True, default="")


class VoucherSerializer(serializers.Serializer):
    voucher_type = serializers.ChoiceField(choices=["journal", "payment", "receipt", "contra"])
    voucher_date = serializers.DateField()
    narration = serializers.CharField(required=False, allow_blank=True, default="")
    reference = serializers.CharField(required=False, allow_blank=True, default="")
    idempotency_key = serializers.CharField(required=False, allow_null=True, default=None)
    party_ledger = serializers.IntegerField(required=False, allow_null=True, default=None)
    due_date = serializers.DateField(required=False, allow_null=True, default=None)
    lines = LineSerializer(many=True)
    post = serializers.BooleanField(required=False, default=False)


class VoucherListCreateView(APIView):
    permission_classes = [IsIndianMode]

    def get(self, request):
        c = _company(request)
        f, t = _dates(request)
        return Response(reports.day_book(c, f, t, request.query_params.get("type")))

    def post(self, request):
        s = VoucherSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        try:
            v = engine.create_voucher(
                _company(request), request.user, voucher_type=d["voucher_type"], voucher_date=d["voucher_date"],
                lines=d["lines"], narration=d["narration"], reference=d["reference"],
                idempotency_key=d["idempotency_key"], post=d["post"], due_date=d["due_date"],
                party_ledger=Ledger.objects.filter(company=_company(request), pk=d["party_ledger"]).first() if d["party_ledger"] else None)
        except ValidationError as e:
            return _err(e)
        return Response({"id": v.id, "voucher_number": v.voucher_number, "status": v.status}, status=201)


class VoucherActionView(APIView):
    permission_classes = [IsIndianMode]

    def post(self, request, pk, action):
        c = _company(request)
        try:
            if action == "post":
                v = engine.post_voucher(pk, request.user, c)
            elif action == "submit":
                v = engine.submit_voucher(pk, request.user, c)
            elif action == "approve":
                v = engine.approve_voucher(pk, request.user, c)
            elif action == "reverse":
                v = engine.reverse_voucher(pk, request.user, c, request.data.get("reason", ""))
            else:
                return Response({"detail": "Unknown action."}, status=404)
        except (ValidationError, Voucher.DoesNotExist) as e:
            return _err(e) if isinstance(e, ValidationError) else Response({"detail": "Voucher not found."}, status=404)
        return Response({"id": v.id, "voucher_number": v.voucher_number, "status": v.status})


class InvoiceView(APIView):
    """Sales invoice / purchase bill with server-side GST calculation."""
    permission_classes = [IsIndianMode]

    def post(self, request, kind):
        c = _company(request)
        if kind not in ("sales", "purchase"):
            return Response({"detail": "Unknown invoice kind."}, status=404)
        try:
            profile = IndianCompanyProfile.objects.get(company=c)
            party = Ledger.objects.get(company=c, pk=request.data.get("party_ledger"))
            vdate = date.fromisoformat(request.data["voucher_date"])
            pos = str(request.data.get("place_of_supply") or "")
            lines = request.data.get("lines") or []
            if not lines:
                raise ValidationError("At least one line is required.")
            tc = gst.resolve_tax_code(c, request.data.get("tax_code", "GST18"), vdate)
            doc = gst.calculate_document(
                [dict(quantity=l["quantity"], unit_price=l["unit_price"], rate=tc.gst_rate,
                      discount=l.get("discount", 0), charges=l.get("charges", 0)) for l in lines],
                seller_state=profile.state_code if kind == "sales" else (party.state_code or pos),
                place_of_supply=pos, treatment=tc.treatment)
            if request.data.get("preview"):
                return Response({k: (str(v) if not isinstance(v, (list, dict)) else v) for k, v in doc.items()
                                 if k not in ("lines",)} | {"component_totals": {k: str(v) for k, v in doc["component_totals"].items()}})
            src = request.data.get("source") or {}
            v = engine.post_tax_invoice(
                c, request.user, kind=kind, voucher_date=vdate, party_ledger=party, place_of_supply=pos, doc=doc,
                seller_state=profile.state_code if kind == "sales" else (party.state_code or pos),
                source={"module": src.get("module", ""), "document_type": src.get("document_type", ""),
                        "document_id": src.get("document_id", ""), "event": kind},
                supplier_invoice_no=request.data.get("supplier_invoice_no", ""), tax_code=tc,
                narration=request.data.get("narration", ""), post=bool(request.data.get("post", True)),
                idempotency_key=request.data.get("idempotency_key"))
        except (ValidationError, KeyError, ValueError) as e:
            return _err(e) if isinstance(e, ValidationError) else Response({"detail": f"Invalid request: {e}"}, status=400)
        except (IndianCompanyProfile.DoesNotExist, Ledger.DoesNotExist):
            return Response({"detail": "Company profile or party ledger not found."}, status=400)
        return Response({"id": v.id, "voucher_number": v.voucher_number, "status": v.status, "total": str(v.total_amount)}, status=201)


class ReportView(APIView):
    permission_classes = [IsIndianMode]

    def get(self, request, name):
        c = _company(request)
        try:
            f, t = _dates(request)
            if name == "trial-balance":
                return Response(reports.trial_balance(c, f, t))
            if name == "day-book":
                return Response(reports.day_book(c, f, t, request.query_params.get("type")))
            if name == "profit-loss":
                return Response(reports.profit_and_loss(c, f, t))
            if name == "balance-sheet":
                return Response(reports.balance_sheet(c, t))
            if name == "gst-summary":
                return Response(reports.gst_summary(c, f, t))
            if name == "receivables":
                return Response(reports.outstanding(c, "receivable"))
            if name == "payables":
                return Response(reports.outstanding(c, "payable"))
            if name in ("receivables-ageing", "payables-ageing"):
                from . import allocations
                a = allocations.ageing(c, "sales" if name.startswith("receivables") else "purchase", t)
                return Response(a)
            if name == "open-items":
                from . import allocations
                return Response(allocations.open_items(c, request.query_params.get("kind", "sales"), t))
            if name == "statement":
                from . import allocations
                return Response(allocations.statement(c, Ledger.objects.get(company=c, pk=request.query_params.get("ledger")), f, t))
            if name == "ledger":
                ledger = Ledger.objects.get(company=c, pk=request.query_params.get("ledger"))
                return Response(reports.general_ledger(c, ledger, f, t))
        except ValidationError as e:
            return _err(e)
        except (Ledger.DoesNotExist, ValueError, TypeError):
            return Response({"detail": "Ledger not found."}, status=404)
        return Response({"detail": "Unknown report."}, status=404)


class LedgerListView(APIView):
    permission_classes = [IsIndianMode]

    def get(self, request):
        c = _company(request)
        return Response([{"id": l.id, "code": l.code, "name": l.name, "group": l.group.name, "nature": l.group.nature,
                          "control_type": l.control_type, "state_code": l.state_code, "gstin": l.gstin, "state_source": l.state_source} for l in Ledger.objects.filter(company=c, is_active=True).select_related("group")])

    def post(self, request):
        c = _company(request)
        try:
            group = AccountGroup.objects.get(company=c, pk=request.data.get("group"))
            l = Ledger(company=c, group=group, code=request.data["code"], name=request.data["name"],
                       opening_balance=request.data.get("opening_balance", 0),
                       opening_side=request.data.get("opening_side", "Dr"),
                       control_type=request.data.get("control_type", ""))
            l.full_clean()
            l.save()
            engine.audit(c, request.user, "ledger", l.pk, "create", after={"code": l.code})
        except (AccountGroup.DoesNotExist, KeyError):
            return Response({"detail": "Valid group, code and name are required."}, status=400)
        except ValidationError as e:
            return _err(e)
        return Response({"id": l.id}, status=201)


class AuditLogView(APIView):
    permission_classes = [IsIndianMode]

    def get(self, request):
        rows = AuditLog.objects.filter(company=_company(request))[:200]
        return Response([{"timestamp": r.timestamp, "user": r.user.username if r.user else None, "entity": r.entity_type,
                          "entity_id": r.entity_id, "action": r.action, "reason": r.reason} for r in rows])


class LedgerDetailView(APIView):
    """Edit safe master fields only (balances are never edited here)."""
    permission_classes = [IsIndianMode]

    def patch(self, request, pk):
        c = _company(request)
        l = Ledger.objects.filter(company=c, pk=pk).first()
        if l is None:
            return Response({"detail": "Ledger not found."}, status=404)
        before = {"state_code": l.state_code, "gstin": l.gstin, "name": l.name}
        for f in ("state_code", "gstin", "name"):
            if f in request.data:
                setattr(l, f, str(request.data[f]).strip())
        if "state_code" in request.data:
            l.state_source = "manual"
        if l.state_code and not (l.state_code.isdigit() and len(l.state_code) == 2):
            return Response({"detail": "state_code must be a 2-digit GST state code."}, status=400)
        l.save()
        engine.audit(c, request.user, "ledger", l.pk, "update", before=before,
                     after={"state_code": l.state_code, "gstin": l.gstin, "name": l.name})
        return Response({"id": l.id})


class PostingEventView(APIView):
    """List operational events routed to the Indian ledger; POST <id>/retry replays a failed one."""
    permission_classes = [IsIndianMode]

    def get(self, request):
        rows = PostingEvent.objects.filter(company=_company(request))[:200]
        return Response([{"id": r.id, "event": r.event, "source_id": r.source_id, "status": r.status,
                          "message": r.message, "attempts": r.attempts, "voucher": r.voucher_id,
                          "updated_at": r.updated_at} for r in rows])

    def post(self, request, pk=None):
        from . import hooks
        c = _company(request)
        r = PostingEvent.objects.filter(company=c, pk=pk).first()
        if r is None:
            return Response({"detail": "Event not found."}, status=404)
        if r.status == "posted":
            return Response({"detail": "Already posted."}, status=400)
        r = hooks.execute(r.event, c, r.source_id, request.user, r.payload)
        return Response({"status": r.status, "message": r.message})


class TaxCodeListView(APIView):
    permission_classes = [IsIndianMode]

    def get(self, request):
        rows = TaxCode.objects.filter(company=_company(request), is_active=True).order_by("gst_rate")
        return Response([{"id": t.id, "code": t.code, "name": t.name, "rate": str(t.gst_rate),
                          "treatment": t.treatment, "itc_eligible": t.itc_eligible} for t in rows])


class BillTaxView(APIView):
    """GST details for a vendor bill. Must be set before the bill posts; a posted bill needs reversal first."""
    permission_classes = [IsIndianMode]

    def get(self, request):
        d = BillTaxDetail.objects.filter(company=_company(request), bill_id=request.query_params.get("bill")).first()
        if d is None:
            return Response({"detail": "No GST details for this bill."}, status=404)
        return Response({"bill": d.bill_id, "tax_code": d.tax_code_id, "taxable_value": str(d.taxable_value),
                         "supplier_state": d.supplier_state, "itc_eligible": d.itc_eligible})

    def post(self, request):
        from decimal import Decimal, InvalidOperation
        from procurement.models import Bill
        c = _company(request)
        try:
            bill = Bill.objects.get(company=c, pk=request.data.get("bill"))
            tc = TaxCode.objects.get(company=c, pk=request.data.get("tax_code"), is_active=True)
            taxable = Decimal(str(request.data.get("taxable_value")))
            state = str(request.data.get("supplier_state", "")).strip()
        except (Bill.DoesNotExist, TaxCode.DoesNotExist):
            return Response({"detail": "Bill or tax code not found."}, status=404)
        except (InvalidOperation, TypeError):
            return Response({"detail": "taxable_value must be a number."}, status=400)
        if taxable <= 0 or not (state.isdigit() and len(state) == 2):
            return Response({"detail": "Positive taxable_value and a 2-digit supplier_state are required."}, status=400)
        if Voucher.objects.filter(company=c, source_module="procurement", source_document_type="bill",
                                  source_document_id=str(bill.id), status="posted").exists():
            return Response({"detail": "This bill is already posted. Reverse its voucher before changing GST details."}, status=409)
        d, _ = BillTaxDetail.objects.update_or_create(
            bill=bill, defaults=dict(company=c, tax_code=tc, taxable_value=taxable, supplier_state=state,
                                     itc_eligible=bool(request.data.get("itc_eligible", tc.itc_eligible)),
                                     created_by=request.user))
        engine.audit(c, request.user, "bill_tax", bill.id, "set", after={"taxable": str(taxable), "code": tc.code})
        return Response({"id": d.id}, status=201)


class SettingsView(APIView):
    permission_classes = [IsIndianMode]

    def get(self, request):
        p = IndianCompanyProfile.objects.get(company=_company(request))
        return Response({"cogs_on": p.cogs_on, "require_bill_gst": p.require_bill_gst, "state_code": p.state_code,
                         "gstin": p.gstin, "posting_enabled": p.posting_enabled,
                         "cogs_locked": Voucher.objects.filter(company=p.company, source_event="cogs").exists(),
                         "costing_method": p.costing_method,
                         "costing_locked": InventoryMovement.objects.filter(company=p.company).exclude(kind__in=["opening", "grn"]).exists()})

    def patch(self, request):
        if request.user.role != "admin":
            return Response({"detail": "Only an admin can change accounting settings."}, status=403)
        c = _company(request)
        p = IndianCompanyProfile.objects.get(company=c)
        before = {"cogs_on": p.cogs_on, "require_bill_gst": p.require_bill_gst}
        if "cogs_on" in request.data:
            if request.data["cogs_on"] not in ("invoice", "dispatch"):
                return Response({"detail": "cogs_on must be 'invoice' or 'dispatch'."}, status=400)
            if request.data["cogs_on"] != p.cogs_on and Voucher.objects.filter(company=c, source_event="cogs").exists():
                return Response({"detail": "Cost of goods sold has already been posted; the policy can no longer change."}, status=409)
            p.cogs_on = request.data["cogs_on"]
        if "costing_method" in request.data:
            if request.data["costing_method"] not in ("standard", "weighted_average"):
                return Response({"detail": "costing_method must be 'standard' or 'weighted_average'."}, status=400)
            if request.data["costing_method"] != p.costing_method and \
                    InventoryMovement.objects.filter(company=c).exclude(kind__in=["opening", "grn"]).exists():
                return Response({"detail": "Stock has already been issued or produced; the costing method can no longer change."}, status=409)
            p.costing_method = request.data["costing_method"]
        if "require_bill_gst" in request.data:
            p.require_bill_gst = bool(request.data["require_bill_gst"])
        p.save()
        engine.audit(c, request.user, "settings", c.pk, "update", before=before,
                     after={"cogs_on": p.cogs_on, "require_bill_gst": p.require_bill_gst,
                                                          "costing_method": p.costing_method})
        return Response({"cogs_on": p.cogs_on, "require_bill_gst": p.require_bill_gst, "costing_method": p.costing_method})


class AllocationView(APIView):
    """POST allocates a receipt/payment to an invoice; POST <id>/release/ releases it (with a reason)."""
    permission_classes = [IsIndianMode]

    def get(self, request):
        from .models import Allocation
        qs = Allocation.objects.filter(company=_company(request)).select_related("payment_voucher", "invoice_voucher")
        if request.query_params.get("invoice"):
            qs = qs.filter(invoice_voucher_id=request.query_params["invoice"])
        if request.query_params.get("payment"):
            qs = qs.filter(payment_voucher_id=request.query_params["payment"])
        return Response([{"id": a.id, "payment": a.payment_voucher.voucher_number, "invoice": a.invoice_voucher.voucher_number,
                          "amount": str(a.amount), "date": a.allocation_date, "status": a.status}
                         for a in qs.order_by("-id")[:200]])

    def post(self, request, pk=None, action=None):
        from . import allocations
        c = _company(request)
        try:
            if action == "release":
                a = allocations.release(c, request.user, pk, request.data.get("reason", ""))
            else:
                a = allocations.allocate(c, request.user, payment_id=int(request.data["payment"]),
                                         invoice_id=int(request.data["invoice"]), amount=request.data["amount"])
        except ValidationError as e:
            return _err(e)
        except (KeyError, ValueError, TypeError, ArithmeticError):
            return Response({"detail": "payment, invoice and a numeric amount are required."}, status=400)
        return Response({"id": a.id, "status": a.status}, status=201 if action is None else 200)


class NoteView(APIView):
    """Credit note (sales) / debit note (purchase) against a posted document."""
    permission_classes = [IsIndianMode]

    def post(self, request):
        from . import notes
        try:
            v = notes.post_note(_company(request), request.user, original_id=int(request.data["against"]),
                                taxable_value=request.data["taxable_value"],
                                voucher_date=date.fromisoformat(request.data["voucher_date"]),
                                reason=str(request.data.get("reason", "")).strip())
        except ValidationError as e:
            return _err(e)
        except (KeyError, ValueError, TypeError, ArithmeticError):
            return Response({"detail": "against, taxable_value, voucher_date and reason are required."}, status=400)
        return Response({"id": v.id, "voucher_number": v.voucher_number, "total": str(v.total_amount)}, status=201)


class _AdminOnlyIndian(IsIndianMode):
    def has_permission(self, request, view):
        return super().has_permission(request, view) and request.user.role == "admin"


class PeriodListView(APIView):
    permission_classes = [IsIndianMode]

    def get(self, request):
        from . import closing
        c = _company(request)
        out = []
        for fy in FinancialYear.objects.filter(company=c):
            out.append({"id": fy.id, "name": fy.name, "start": fy.start_date, "end": fy.end_date, "status": fy.status,
                        "blockers": closing.close_checks(c, fy) if fy.status == "open" else [],
                        "periods": [{"id": p.id, "start": p.start_date, "end": p.end_date, "status": p.status}
                                    for p in fy.periods.all()]})
        return Response(out)


class PeriodActionView(APIView):
    """Lock/unlock a period, close/reopen a financial year. Admin only; every action is audited."""
    permission_classes = [_AdminOnlyIndian]

    def post(self, request, kind, pk, action):
        from . import closing
        c = _company(request)
        reason = str(request.data.get("reason", "")).strip()
        try:
            if kind == "period" and action in ("lock", "unlock"):
                closing.set_period_lock(c, request.user, pk, action == "lock", reason)
            elif kind == "year" and action == "close":
                closing.close_year(c, request.user, pk)
            elif kind == "year" and action == "reopen":
                closing.reopen_year(c, request.user, pk, reason)
            else:
                return Response({"detail": "Unknown action."}, status=404)
        except ValidationError as e:
            return _err(e)
        return Response({"ok": True})


class ImportView(APIView):
    """Upload (CSV or Excel) -> preview with row errors -> commit. One endpoint for every import kind."""
    permission_classes = [_AdminOnlyIndian]

    @staticmethod
    def _data(b):
        return {"id": b.id, "kind": b.kind, "status": b.status, "file": b.source_file, "rows": b.rows,
                "errors": b.errors, "warnings": b.warnings, "total_debit": str(b.total_debit),
                "total_credit": str(b.total_credit)}

    def get(self, request, kind):
        from django.http import HttpResponse
        from . import imports
        try:
            kind = imports.normalise_kind(kind)
        except ValidationError as e:
            return _err(e)
        if request.query_params.get("template"):
            r = HttpResponse(imports.TEMPLATES[kind], content_type="text/csv")
            r["Content-Disposition"] = f'attachment; filename="{kind}_template.csv"'
            return r
        b = ImportBatch.objects.filter(company=_company(request), kind=kind).first()
        return Response(None if b is None else self._data(b))

    def post(self, request, kind, pk=None, action=None):
        from . import imports
        c = _company(request)
        try:
            kind = imports.normalise_kind(kind)
            if action == "commit":
                b = imports.commit(c, request.user, pk)
            else:
                f = request.FILES.get("file")
                if f is None:
                    return Response({"detail": "Upload a CSV or Excel file in the 'file' field."}, status=400)
                if f.size > 5_000_000:
                    return Response({"detail": "File is too large."}, status=400)
                b = imports.create_preview(c, request.user, f.read(), f.name, kind)
        except ValidationError as e:
            return _err(e)
        return Response(self._data(b), status=201 if action is None else 200)

SOURCE_PAGES = {
    ("sales", "invoice"): ("Sales invoice", "/sales"), ("sales", "payment"): ("Customer payment", "/sales"),
    ("sales", "shipment"): ("Sales shipment", "/sales"), ("procurement", "bill"): ("Vendor bill", "/procurement"),
    ("procurement", "receipt"): ("Goods receipt", "/procurement"), ("procurement", "payment"): ("Vendor payment", "/procurement"),
    ("fulfillment", "dispatch"): ("Dispatch", "/fulfilment"), ("production", "order"): ("Production order", "/production"),
    ("production", "scrap"): ("Scrap record", "/production"), ("quality", "rejection"): ("QA rejection", "/quality"),
    ("import", "open_invoice"): ("Imported opening document", "/accounting"),
}


class VoucherDetailView(APIView):
    """Everything behind a voucher: lines, GST snapshot, audit trail, settlements, links and the ERP source document."""
    permission_classes = [IsIndianMode]

    def get(self, request, pk):
        from .models import Allocation
        c = _company(request)
        v = Voucher.objects.filter(company=c, pk=pk).select_related("party_ledger", "reversal_of", "against", "financial_year").first()
        if v is None:
            return Response({"detail": "Voucher not found."}, status=404)
        label, page = SOURCE_PAGES.get((v.source_module, v.source_document_type), (None, None))
        reversed_by = Voucher.objects.filter(company=c, reversal_of=v).first()
        return Response({
            "id": v.id, "number": v.voucher_number, "type": v.voucher_type, "status": v.status, "date": v.voucher_date,
            "due_date": v.due_date, "narration": v.narration, "reference": v.reference, "total": v.total_amount,
            "party": v.party_ledger.name if v.party_ledger else None, "party_id": v.party_ledger_id,
            "financial_year": v.financial_year.name, "is_opening": v.is_opening, "place_of_supply": v.place_of_supply,
            "lines": [{"ledger_id": l.ledger_id, "ledger": l.ledger.name, "debit": l.debit, "credit": l.credit,
                       "narration": l.narration} for l in v.lines.select_related("ledger")],
            "tax_lines": [{"component": t.component, "rate": t.applied_rate, "taxable_value": t.taxable_value,
                           "tax": t.tax_amount, "place_of_supply": t.place_of_supply, "itc_eligible": t.itc_eligible}
                          for t in v.tax_lines.all()],
            "source": None if not v.source_document_id else {
                "module": v.source_module, "document_type": v.source_document_type, "document_id": v.source_document_id,
                "label": f"{label or v.source_module + ' ' + v.source_document_type} #{v.source_document_id}", "page": page},
            "reversal_of": {"id": v.reversal_of_id, "number": v.reversal_of.voucher_number} if v.reversal_of_id else None,
            "reversed_by": {"id": reversed_by.id, "number": reversed_by.voucher_number} if reversed_by else None,
            "against": {"id": v.against_id, "number": v.against.voucher_number} if v.against_id else None,
            "notes": [{"id": n.id, "number": n.voucher_number, "type": n.voucher_type, "total": n.total_amount}
                      for n in v.notes.filter(status="posted")],
            "allocations": [{"id": a.id, "with": (a.invoice_voucher if a.payment_voucher_id == v.id else a.payment_voucher).voucher_number,
                             "with_id": a.invoice_voucher_id if a.payment_voucher_id == v.id else a.payment_voucher_id,
                             "amount": a.amount, "status": a.status}
                            for a in Allocation.objects.filter(company=c).filter(
                                payment_voucher=v).select_related("invoice_voucher", "payment_voucher")
                            | Allocation.objects.filter(company=c, invoice_voucher=v).select_related("invoice_voucher", "payment_voucher")],
            "audit": [{"timestamp": a.timestamp, "user": a.user.username if a.user else None, "action": a.action,
                       "reason": a.reason} for a in AuditLog.objects.filter(company=c, entity_type="voucher", entity_id=str(v.id))
                      .select_related("user")],
        })


class StockView(APIView):
    """Stock quantity and value per item as held by the Indian ledger (reconciles to the stock ledgers)."""
    permission_classes = [IsIndianMode]

    def get(self, request):
        from . import valuation
        rows = valuation.stock_report(_company(request))
        return Response({"items": rows, "total_value": sum((r["value"] for r in rows), 0)})
