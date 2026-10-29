"""Reports computed only from posted voucher lines (plus ledger opening balances)."""
from decimal import Decimal

from django.db.models import Sum

from .models import Ledger, Voucher, VoucherLine

ZERO = Decimal("0.00")


def _signed_opening(ledger):
    return ledger.opening_balance if ledger.opening_side == "Dr" else -ledger.opening_balance


def _sums(company, date_from=None, date_to=None):
    qs = VoucherLine.objects.filter(company=company, is_posted=True)
    if date_from:
        qs = qs.filter(voucher_date__gte=date_from)
    if date_to:
        qs = qs.filter(voucher_date__lte=date_to)
    return {r["ledger_id"]: (r["d"] or ZERO, r["c"] or ZERO)
            for r in qs.values("ledger_id").annotate(d=Sum("debit"), c=Sum("credit"))}


def trial_balance(company, date_from, date_to):
    from datetime import timedelta
    from .models import FinancialYear
    prior = _sums(company, date_to=date_from - timedelta(days=1))
    fy = FinancialYear.objects.filter(company=company, start_date__lte=date_from, end_date__gte=date_from).first()
    # nominal (income/expense) ledgers restart each financial year
    prior_in_year = _sums(company, fy.start_date, date_from - timedelta(days=1)) if fy and fy.start_date < date_from else {}
    period = _sums(company, date_from, date_to)
    rows, tot = [], dict(od=ZERO, oc=ZERO, pd=ZERO, pc=ZERO, cd=ZERO, cc=ZERO)
    bf = ZERO  # earlier years' net result, Dr positive
    for l in Ledger.objects.filter(company=company).select_related("group"):
        pd_, pc_ = prior.get(l.id, (ZERO, ZERO))
        net_open = _signed_opening(l) + pd_ - pc_
        if l.group.nature in ("income", "expense"):
            yd, yc = prior_in_year.get(l.id, (ZERO, ZERO))
            in_year_open = yd - yc
            bf += net_open - in_year_open
            net_open = in_year_open
        d, c = period.get(l.id, (ZERO, ZERO))
        net_close = net_open + d - c
        if not (net_open or d or c):
            continue
        row = dict(code=l.code, name=l.name, group=l.group.name, ledger_id=l.id,
                   opening_debit=max(net_open, ZERO), opening_credit=max(-net_open, ZERO),
                   period_debit=d, period_credit=c,
                   closing_debit=max(net_close, ZERO), closing_credit=max(-net_close, ZERO))
        rows.append(row)
        tot["od"] += row["opening_debit"]; tot["oc"] += row["opening_credit"]
        tot["pd"] += d; tot["pc"] += c
        tot["cd"] += row["closing_debit"]; tot["cc"] += row["closing_credit"]
    if bf:
        rows.append(dict(code="", name="Accumulated profit/loss brought forward", group="Reserves & Surplus", ledger_id=None,
                         opening_debit=max(bf, ZERO), opening_credit=max(-bf, ZERO), period_debit=ZERO, period_credit=ZERO,
                         closing_debit=max(bf, ZERO), closing_credit=max(-bf, ZERO)))
        tot["od"] += max(bf, ZERO); tot["oc"] += max(-bf, ZERO)
        tot["cd"] += max(bf, ZERO); tot["cc"] += max(-bf, ZERO)
    return {"rows": rows, "totals": tot,
            "is_balanced": tot["cd"] == tot["cc"] and tot["pd"] == tot["pc"],
            "difference": tot["cd"] - tot["cc"]}


def day_book(company, date_from, date_to, voucher_type=None):
    qs = Voucher.objects.filter(company=company, is_opening=False, status__in=["posted", "reversed"],
                                voucher_date__range=(date_from, date_to)).select_related("party_ledger")
    if voucher_type:
        qs = qs.filter(voucher_type=voucher_type)
    return [dict(id=v.id, date=v.voucher_date, number=v.voucher_number, type=v.voucher_type, status=v.status,
                 narration=v.narration, party=v.party_ledger.name if v.party_ledger else "",
                 amount=v.total_amount, source=f"{v.source_module}:{v.source_document_type}:{v.source_document_id}"
                 if v.source_document_id else "")
            for v in qs.order_by("voucher_date", "id")]


def general_ledger(company, ledger, date_from, date_to):
    from datetime import timedelta
    prior = VoucherLine.objects.filter(ledger=ledger, is_posted=True, voucher_date__lt=date_from).aggregate(
        d=Sum("debit"), c=Sum("credit"))
    opening = _signed_opening(ledger) + (prior["d"] or ZERO) - (prior["c"] or ZERO)
    running, rows = opening, []
    lines = (VoucherLine.objects.filter(ledger=ledger, is_posted=True, voucher_date__range=(date_from, date_to))
             .select_related("voucher").order_by("voucher_date", "voucher_id", "line_number"))
    for ln in lines:
        running += ln.debit - ln.credit
        rows.append(dict(date=ln.voucher_date, voucher_id=ln.voucher_id, number=ln.voucher.voucher_number,
                         type=ln.voucher.voucher_type, narration=ln.narration or ln.voucher.narration,
                         debit=ln.debit, credit=ln.credit, balance=running))
    return {"ledger": ledger.name, "opening": opening, "rows": rows, "closing": running}


def _group_balances(company, date_from, date_to, natures):
    period = _sums(company, date_from, date_to)
    out = {}
    for l in Ledger.objects.filter(company=company, group__nature__in=natures).select_related("group"):
        d, c = period.get(l.id, (ZERO, ZERO))
        out.setdefault(l.group.name, {"category": l.group.category, "nature": l.group.nature, "ledgers": [], "total": ZERO})
        amt = (c - d) if l.group.nature == "income" else (d - c)
        if amt:
            out[l.group.name]["ledgers"].append({"ledger_id": l.id, "name": l.name, "amount": amt})
            out[l.group.name]["total"] += amt
    return {k: v for k, v in out.items() if v["ledgers"]}


def profit_and_loss(company, date_from, date_to):
    groups = _group_balances(company, date_from, date_to, ["income", "expense"])
    inc = sum((g["total"] for g in groups.values() if g["nature"] == "income"), ZERO)
    exp = sum((g["total"] for g in groups.values() if g["nature"] == "expense"), ZERO)
    direct_inc = sum((g["total"] for g in groups.values() if g["nature"] == "income" and g["category"] == "direct"), ZERO)
    direct_exp = sum((g["total"] for g in groups.values() if g["nature"] == "expense" and g["category"] == "direct"), ZERO)
    return {"groups": groups, "income": inc, "expense": exp, "gross_profit": direct_inc - direct_exp,
            "net_profit": inc - exp}


def balance_sheet(company, as_of):
    """Assets = Liabilities + Capital + current-period (cumulative) profit."""
    all_sums = _sums(company, date_to=as_of)
    assets, liabilities, profit = ZERO, ZERO, ZERO
    groups = {}
    for l in Ledger.objects.filter(company=company).select_related("group"):
        d, c = all_sums.get(l.id, (ZERO, ZERO))
        net = _signed_opening(l) + d - c  # Dr positive
        n = l.group.nature
        if n in ("income", "expense"):
            profit += -net  # credit-positive profit contribution
            continue
        amt = net if n == "asset" else -net
        g = groups.setdefault(l.group.name, {"nature": n, "total": ZERO, "ledgers": []})
        g["total"] += amt
        g["ledgers"].append({"ledger_id": l.id, "name": l.name, "amount": amt})
        if n == "asset":
            assets += amt
        else:
            liabilities += amt
    return {"groups": groups, "assets": assets, "liabilities": liabilities, "profit_to_date": profit,
            "total_liabilities_and_equity": liabilities + profit,
            "is_balanced": assets == liabilities + profit}


def outstanding(company, control_type):
    """Ledger-level balances for receivable/payable party ledgers."""
    rows = []
    for l in Ledger.objects.filter(company=company, control_type=control_type):
        from .engine import ledger_balance
        bal = ledger_balance(l)
        if bal:
            rows.append({"ledger_id": l.id, "name": l.name, "balance": abs(bal), "side": "Dr" if bal > 0 else "Cr"})
    return rows


def gst_summary(company, date_from, date_to):
    from .models import VoucherTaxLine
    qs = VoucherTaxLine.objects.filter(voucher__company=company, voucher__status="posted",
                                       voucher__voucher_date__range=(date_from, date_to))
    out = {}
    for r in qs.values("voucher__voucher_type", "component", "applied_rate").annotate(
            taxable=Sum("taxable_value"), tax=Sum("tax_amount")):
        out.setdefault(r["voucher__voucher_type"], []).append(
            dict(component=r["component"], rate=r["applied_rate"], taxable_value=r["taxable"], tax=r["tax"]))
    return {"label": "Internal accounting summary - not a statutory return", "by_type": out}
