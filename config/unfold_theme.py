"""Single source of truth for admin theming.

Color ramps and status->badge-color maps used by:
  - UNFOLD["COLORS"] in settings.py (admin chrome, light + dark mode)
  - config/dashboard.py (chart palettes)
  - templates/admin/ (status badges via the shared maps)
  - static/admin/admin-aurora.css mirrors AURORA_GREY / GOLD as CSS variables

The neutral ramp is the Aurora dashboard grey scale; the primary ramp is the
Azura brand gold (same as travelfrontend --color-primary).
"""

# Aurora cool-tinted grey scale (aurora-free-1.0.0/src/theme/palette/colors.ts)
AURORA_GREY = {
    "50": "#F7FAFC",
    "100": "#EBF2F5",
    "200": "#DBE6EB",
    "300": "#C3D3DB",
    "400": "#9CAEB8",
    "500": "#77878F",
    "600": "#4D595E",
    "700": "#262D30",
    "800": "#1B2124",
    "900": "#111417",
    "950": "#06080A",
}

# Azura brand gold ramp (500 == frontend --color-primary #bd8f3a)
GOLD = {
    "50": "#FDF8EE",
    "100": "#F9EFD1",
    "200": "#F2DCA3",
    "300": "#E7C36C",
    "400": "#D6A84F",
    "500": "#BD8F3A",
    "600": "#A17529",
    "700": "#805B1E",
    "800": "#634615",
    "900": "#49340F",
    "950": "#2F2109",
}

# Aurora semantic 500s — used for charts and anywhere a literal color is needed
SEMANTIC = {
    "success": "#099F69",
    "warning": "#F68D2A",
    "danger": "#D02241",
    "info": "#0DA6D6",
    "blue": "#3385F0",
    "purple": "#A641FA",
    "neutral": "#77878F",
    "gold": GOLD["500"],
}

# Translucent gold for line-chart fills
GOLD_SOFT = "rgba(189, 143, 58, 0.12)"

# ---------------------------------------------------------------------------
# Chart colors are emitted as CSS custom-property references — Unfold's chart
# runtime resolves var() at render time, so light and dark mode each get the
# palette validated for their surface (values + validation notes live in
# static/admin/admin-aurora.css).
# ---------------------------------------------------------------------------
CHART_PALETTE = [f"var(--chart-cat-{i})" for i in range(1, 8)]
CHART_GOLD = "var(--chart-gold)"
CHART_GOLD_SOFT = "var(--chart-gold-soft)"
# Card background — used as the 2px separator ring between doughnut segments
CHART_SURFACE = "var(--chart-surface)"

# ---------------------------------------------------------------------------
# Status -> unfold badge type ("success" | "warning" | "danger" | "info" |
# "primary" | anything else falls back to the neutral grey badge).
# Keys are the raw model choice values; display methods return
# (raw_value, human_label) tuples so these maps drive the badge color.
# ---------------------------------------------------------------------------

BOOKING_STATUS_BADGE = {
    "pending": "warning",
    "confirmed": "success",
    "cancelled": "danger",
    "completed": "info",
}

PAYMENT_STATUS_BADGE = {
    "pending": "warning",
    "success": "success",
    "failed": "danger",
    "abandoned": "danger",
    "refunded": "info",
}

REFUND_STATUS_BADGE = {
    "pending": "warning",
    "processed": "success",
    "rejected": "danger",
}

BLOG_STATUS_BADGE = {
    "draft": "warning",
    "published": "success",
}

# Booking.payment_state_display keys (derived state, not a model field)
PAYMENT_STATE_BADGE = {
    "paid": "success",
    "overdue": "danger",
    "partial": "warning",
    "expired": "danger",
    "unpaid": "warning",
}

# Hex used by dashboard charts for booking statuses (matches badge semantics)
BOOKING_STATUS_CHART_COLORS = {
    "pending": SEMANTIC["warning"],
    "confirmed": SEMANTIC["success"],
    "cancelled": SEMANTIC["danger"],
    "completed": SEMANTIC["info"],
}
