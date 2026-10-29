"""Exact currency formatting used by the home page."""

from decimal import Decimal

from ui.home import _money


def test_home_currency_formatting_keeps_minor_units_exact():
    assert _money(Decimal(100_000_000_00) / 100, 'MNT') == '100,000,000.00 ₮'
    assert _money(Decimal('-1250.5'), 'USD') == '−1,250.50 USD'
