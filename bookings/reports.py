"""
Reports admin page (canvas: DjReports): funnel, enquiry sources, abandonment,
accommodation uptake, experience sales and referral attribution for a date
range, with CSV export. Staff-only via admin_site.admin_view.
"""

import csv
from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import Decimal

from django.contrib import admin
from django.http import HttpResponse
from django.template.response import TemplateResponse
from django.utils import timezone

from .models import Booking, BookingTraveller, Lead

PAID = (Booking.Status.CONFIRMED, Booking.Status.COMPLETED)


def _range(request):
    today = timezone.localdate()
    try:
        start = date.fromisoformat(request.GET.get("from", ""))
    except ValueError:
        start = today - timedelta(days=89)
    try:
        end = date.fromisoformat(request.GET.get("to", ""))
    except ValueError:
        end = today
    return start, end


def build_report(start, end) -> dict:
    leads = Lead.objects.filter(created_at__date__range=(start, end))
    bookings = Booking.objects.filter(created_at__date__range=(start, end))
    paid = bookings.filter(status__in=PAID)
    abandoned = [b for b in bookings.filter(status=Booking.Status.PENDING, amount_paid=0) if b.is_expired]
    n_leads, n_quotes, n_started, n_paid = leads.count(), leads.exclude(quote={}).count(), bookings.count(), paid.count()

    sources = []
    for value, label in Lead.Source.choices:
        group = leads.filter(source=value)
        if group.exists():
            won = group.filter(stage=Lead.Stage.WON).count()
            sources.append({"label": label, "leads": group.count(), "won": won,
                            "rate": round(won / group.count() * 100)})

    travellers = BookingTraveller.objects.filter(booking__in=paid).select_related("unit__stay_type")
    stays = Counter(t.unit.stay_type.name if t.unit_id else "Own stay" for t in travellers)

    sales = defaultdict(lambda: {"count": 0, "revenue": Decimal("0")})
    for b in paid.filter(mode__gt=""):
        for line in b.addons or []:
            if line.get("unit") == "per_person":
                sales[line["name"]]["count"] += line.get("quantity", 0)
                sales[line["name"]]["revenue"] += Decimal(str(line.get("line_total", "0")))

    referrals = []
    for code in leads.exclude(referral_code="").values_list("referral_code", flat=True).distinct():
        group = leads.filter(referral_code=code)
        referrals.append({"code": code, "leads": group.count(), "quotes": group.exclude(quote={}).count(),
                          "bookings": group.filter(stage=Lead.Stage.WON).count()})

    pct = lambda a, b: round(a / b * 100) if b else 0
    return {
        "start": start, "end": end,
        "kpis": [
            {"label": "Enquiries", "value": n_leads, "note": f"{n_quotes} saved quotes"},
            {"label": "Checkouts started", "value": n_started, "note": f"{len(abandoned)} abandoned"},
            {"label": "Paid bookings", "value": n_paid, "note": f"{pct(n_paid, n_started)}% of checkouts"},
            {"label": "Abandonment", "value": f"{pct(len(abandoned), n_started)}%", "note": "Unpaid after the checkout window"},
        ],
        "funnel": [
            {"label": "Enquiries", "n": n_leads}, {"label": "Saved quotes", "n": n_quotes},
            {"label": "Checkouts started", "n": n_started}, {"label": "Paid", "n": n_paid},
        ],
        "funnel_max": max(n_leads, n_quotes, n_started, n_paid, 1),
        "sources": sources,
        "stays": [{"label": k, "n": v, "pct": pct(v, sum(stays.values()))} for k, v in stays.most_common()],
        "sales": [{"label": k, **v} for k, v in sorted(sales.items(), key=lambda kv: -kv[1]["revenue"])],
        "referrals": referrals,
    }


def _csv(report) -> HttpResponse:
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="azura-report-{report["start"]}-{report["end"]}.csv"'
    w = csv.writer(response)
    w.writerow(["Azura report", report["start"], report["end"]])
    w.writerow([]); w.writerow(["Funnel", "Count"])
    w.writerows([[r["label"], r["n"]] for r in report["funnel"]])
    w.writerow([]); w.writerow(["Enquiry source", "Leads", "Converted", "Conversion %"])
    w.writerows([[r["label"], r["leads"], r["won"], r["rate"]] for r in report["sources"]])
    w.writerow([]); w.writerow(["Accommodation", "Travellers", "%"])
    w.writerows([[r["label"], r["n"], r["pct"]] for r in report["stays"]])
    w.writerow([]); w.writerow(["Experience", "Sold", "Revenue"])
    w.writerows([[r["label"], r["count"], r["revenue"]] for r in report["sales"]])
    w.writerow([]); w.writerow(["Referral code", "Leads", "Quotes", "Bookings"])
    w.writerows([[r["code"], r["leads"], r["quotes"], r["bookings"]] for r in report["referrals"]])
    return response


def reports_view(request):
    start, end = _range(request)
    report = build_report(start, end)
    if request.GET.get("export") == "csv":
        return _csv(report)
    today = timezone.localdate()
    presets = [("Last 30 days", today - timedelta(days=29)), ("Last 90 days", today - timedelta(days=89)),
               ("This year", date(today.year, 1, 1))]
    return TemplateResponse(request, "admin/reports.html", {
        **admin.site.each_context(request), "title": "Reports", "r": report,
        "presets": [{"label": l, "from": s.isoformat(), "to": today.isoformat(),
                     "on": s == start and end == today} for l, s in presets],
    })
