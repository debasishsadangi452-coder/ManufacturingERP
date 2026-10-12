"""Replay operational events into the General Ledger for one company.

Operational flows post to the ledger automatically, but only once the company
has a chart of accounts. Events from before that (e.g. a company created by a
demo seeder that never set up accounting) were never posted. This command
replays them through the same auto-posting handlers, which skip anything that
is already posted, so it is safe to run more than once.

    python manage.py backfill_accounting --company smartiesfoods            # dry run
    python manage.py backfill_accounting --company smartiesfoods --setup --apply

--setup   seed the standard chart of accounts, current fiscal year and turn
          auto-posting on (only what is missing)
--apply   actually post; without it the command only lists what it would post
"""
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q


class Command(BaseCommand):
    help = "Post operational history (receipts, bills, production, dispatches, invoices, payments) to the GL."

    def add_arguments(self, parser):
        parser.add_argument("--company", required=True, help="Company slug")
        parser.add_argument("--setup", action="store_true", help="Seed chart of accounts / fiscal year / enable auto-post if missing")
        parser.add_argument("--apply", action="store_true", help="Post for real (default is a dry run)")

    def handle(self, *args, **opts):
        from accounts.models import Company
        from accounting.auto_posting import execute_auto_post, is_auto_posting_enabled
        from accounting.models import Account, AccountingSettings

        company = Company.objects.filter(slug=opts["company"]).first()
        if company is None:
            raise CommandError(f"No company with slug '{opts['company']}'.")
        apply = opts["apply"]

        if opts["setup"]:
            from accounting.seeds import ensure_current_fiscal_year, seed_standard_chart_of_accounts
            if apply:
                created = seed_standard_chart_of_accounts(company) if not Account.objects.filter(company=company).exists() else 0
                ensure_current_fiscal_year(company)
                st, _ = AccountingSettings.objects.get_or_create(company=company)
                if not st.auto_post_enabled:
                    st.auto_post_enabled = True
                    st.save(update_fields=["auto_post_enabled"])
                self.stdout.write(f"Setup: {created} account(s) created; fiscal year ensured; auto-post on.")
            else:
                self.stdout.write(f"Setup (dry run): accounts now {Account.objects.filter(company=company).count()} — would seed the standard chart if 0.")

        if apply and not is_auto_posting_enabled(company):
            raise CommandError("Auto-posting is not enabled or there is no chart of accounts. Run with --setup.")

        events = list(self._events(company))
        self.stdout.write(f"{len(events)} operational event(s) found for {company.name}.")
        if not apply:
            from collections import Counter
            for ev, n in sorted(Counter(e for e, _, _ in events).items()):
                self.stdout.write(f"  {ev:22s} {n}")
            self.stdout.write("Dry run — nothing posted. Re-run with --apply (and --setup if accounting is not set up).")
            return

        tally = {"posted": 0, "skipped": 0, "failed": 0}
        for event, source_id, payload in events:
            log = execute_auto_post(event, company, source_id, user=None, payload=payload)
            tally[log.status] = tally.get(log.status, 0) + 1
            if log.status == "failed":
                self.stdout.write(self.style.WARNING(f"  FAILED {event} #{source_id}: {log.message[:160]}"))
        self.stdout.write(self.style.SUCCESS(
            f"Done: {tally['posted']} posted, {tally['skipped']} skipped (already posted or nothing to post), {tally['failed']} failed."
        ))

    def _events(self, company):
        """(event, source_id, payload) in a sensible order: stock in, bills,
        production, scrap, stock out, invoices, payments."""
        from django.db.models import Sum
        from fulfillment.models import Dispatch
        from procurement.models import Bill, GoodsReceipt
        from production.models import ProductionOrder, ScrapRecord
        from sales.models import CustomerPayment, Invoice, SalesOrder

        for r in GoodsReceipt.objects.filter(purchase_order__vendor__company=company).order_by("id"):
            yield "goods_receipt", r.id, {}
        for b in Bill.objects.filter(company=company).exclude(status="cancelled").order_by("id"):
            yield "vendor_bill", b.id, {}

        for o in ProductionOrder.objects.filter(recipe__product__company=company, status__in=["completed", "closed"],
                                                rework_of__isnull=True).order_by("id"):
            payload = self._production_payload(o)
            if payload is not None:
                yield "production_completed", o.id, payload
        for s in ScrapRecord.objects.filter(production_order__recipe__product__company=company).order_by("id"):
            yield "scrap_write_off", s.id, {}

        for d in Dispatch.objects.filter(company=company, status__in=["dispatched", "delivered"]).order_by("id"):
            yield "dispatch_shipment", d.id, {}
        # Legacy direct shipments (orders shipped without the dispatch flow).
        dispatched = set(Dispatch.objects.filter(company=company).values_list("sales_order_id", flat=True))
        for so in SalesOrder.objects.filter(customer__company=company).exclude(id__in=dispatched).order_by("id"):
            lines = [{"item_id": i.item_id, "quantity": i.shipped_quantity}
                     for i in so.salesorderitem_set.all() if (i.shipped_quantity or 0) > 0]
            if lines:
                yield "sales_shipment", so.id, {"lines": lines}

        for inv in Invoice.objects.filter(company=company).exclude(status="cancelled").order_by("id"):
            yield "sales_invoice", inv.id, {}
        for p in CustomerPayment.objects.filter(Q(company=company) | Q(invoice__company=company)).order_by("id"):
            yield "customer_payment", p.id, {}

    @staticmethod
    def _production_payload(order):
        """Same quantities quality.services.maybe_post_manufacturing_accounting
        uses; None while QA is still pending."""
        from django.db.models import Sum
        if order.qualitycheck_set.filter(status="pending").exists():
            return None
        agg = order.qualitycheck_set.aggregate(
            accepted=Sum("accepted_quantity"), rejected=Sum("rejected_quantity"),
            quarantined=Sum("quarantined_quantity"), rework=Sum("rework_quantity"),
        )
        good = (agg["accepted"] or 0) + (agg["quarantined"] or 0) + (agg["rework"] or 0)
        legacy = order.qualitycheck_set.filter(status="approved", accepted_quantity__isnull=True).exists()
        if legacy and good <= 1e-9:
            good = order.produced_quantity or order.quantity
        if not order.qualitycheck_set.exists():
            good = order.produced_quantity
        scrap = (agg["rejected"] or 0) + sum(op.scrap_quantity for op in order.operations.all())
        scrap = min(scrap, max(order.quantity - good, 0))
        return {"completed_qty": round(good, 6), "scrap_qty": round(scrap, 6)}
