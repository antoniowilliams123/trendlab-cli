"""The 50-task suite (cheap-model spec §8.1): clean repositories with seeded defects.

Tasks are generated deterministically from a defect catalogue applied to three base
repositories (Python, TypeScript, Go). Each task knows the files that may change, the
localisation answer (file + line of the seeded defect) and a hidden regression test that is
written only at scoring time. Nothing ships as loose files: ``materialize`` writes a task to a
directory on demand, like fixtures A–E.

Weighting: ~40 % of the tasks have no failing visible test — only a symptom in the prompt —
because localisation is where cheap models fail most.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------------------------
# Base repositories
# --------------------------------------------------------------------------------------------
PY_BASE: dict[str, str] = {
    "pyproject.toml": "[tool.pytest.ini_options]\naddopts = '-q'\n",
    "shop/__init__.py": "",
    "shop/pricing.py": '''"""Prices and discounts."""

TAX_RATE = 0.08


def unit_price(item: dict) -> float:
    base = float(item["price"])
    discount = float(item.get("discount", 0.0))
    return base * (1.0 - discount)


def line_total(item: dict) -> float:
    return unit_price(item) * int(item["qty"])


def with_tax(amount: float, rate: float = TAX_RATE) -> float:
    return round(amount * (1.0 + rate), 2)


def tiered_discount(total: float) -> float:
    """Discount fraction by order size."""
    if total >= 500:
        return 0.10
    if total >= 100:
        return 0.05
    return 0.0
''',
    "shop/stock.py": '''"""Stock levels."""


class OutOfStock(Exception):
    pass


class Stock:
    def __init__(self, levels: dict[str, int] | None = None) -> None:
        self.levels = dict(levels or {})

    def available(self, sku: str) -> int:
        return self.levels.get(sku, 0)

    def reserve(self, sku: str, qty: int) -> int:
        have = self.available(sku)
        if qty > have:
            raise OutOfStock(f"{sku}: wanted {qty}, have {have}")
        self.levels[sku] = have - qty
        return self.levels[sku]

    def restock(self, sku: str, qty: int) -> int:
        self.levels[sku] = self.available(sku) + qty
        return self.levels[sku]

    def low(self, threshold: int = 5) -> list[str]:
        return sorted(sku for sku, n in self.levels.items() if n <= threshold)

    def find(self, sku_text: str) -> int:
        """Available units for a SKU typed by a human, e.g. 'abc-12' -> key 'ABC-12'."""
        from shop.util import parse_sku

        name, num = parse_sku(sku_text)
        return self.available(f"{name}-{num}")
''',
    "shop/orders.py": '''"""Orders: totals, validation, summaries."""

from shop.pricing import line_total, tiered_discount, with_tax
from shop.stock import Stock


def order_subtotal(items: list[dict]) -> float:
    return sum(line_total(i) for i in items)


def order_total(items: list[dict], tax: bool = True) -> float:
    subtotal = order_subtotal(items)
    discounted = subtotal * (1.0 - tiered_discount(subtotal))
    return with_tax(discounted) if tax else round(discounted, 2)


def validate(items: list[dict]) -> list[str]:
    problems = []
    for idx, item in enumerate(items):
        if int(item.get("qty", 0)) <= 0:
            problems.append(f"item {idx}: qty must be positive")
        if float(item.get("price", 0)) < 0:
            problems.append(f"item {idx}: negative price")
    return problems


def fulfil(items: list[dict], stock: Stock) -> dict[str, int]:
    left = {}
    for item in items:
        left[item["sku"]] = stock.reserve(item["sku"], int(item["qty"]))
    return left


def summary(items: list[dict]) -> str:
    lines = [f"{i['sku']} x{i['qty']} = {line_total(i):.2f}" for i in items]
    lines.append(f"total = {order_total(items):.2f}")
    return "\\n".join(lines)
''',
    "shop/report.py": '''"""Daily report over a list of orders (each a list of items)."""

from collections import Counter

from shop.orders import order_total
from shop.stock import Stock
from shop.util import chunks


def revenue(orders: list[list[dict]]) -> float:
    return round(sum(order_total(o) for o in orders), 2)


def top_skus(orders: list[list[dict]], n: int = 3) -> list[tuple[str, int]]:
    counts: Counter[str] = Counter()
    for order in orders:
        for item in order:
            counts[item["sku"]] += int(item["qty"])
    return counts.most_common(n)


def average_order(orders: list[list[dict]]) -> float:
    if not orders:
        return 0.0
    return round(revenue(orders) / len(orders), 2)


def busiest_hours(stamps: list[int], top: int = 2) -> list[int]:
    """Hours (0-23) with the most orders, busiest first."""
    counts: Counter[int] = Counter(h % 24 for h in stamps)
    return [h for h, _ in counts.most_common(top)]


def batches(orders: list[list[dict]], size: int) -> list[list[list[dict]]]:
    """Orders grouped for export, ``size`` per batch (last may be shorter)."""
    return chunks(orders, size)


def low_stock_alert(stock: Stock, threshold: int = 5) -> list[str]:
    return stock.low(threshold)
''',
    "shop/util.py": '''"""Small helpers."""

import json
from pathlib import Path


def load_orders(path: str | Path) -> list[list[dict]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [list(o) for o in data["orders"]]


def chunks(items: list, size: int) -> list[list]:
    """Split ``items`` into consecutive chunks of ``size`` (last may be shorter)."""
    return [items[i : i + size] for i in range(0, len(items), size)]


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def parse_sku(text: str) -> tuple[str, int]:
    """'ABC-12' -> ('ABC', 12)."""
    name, _, num = text.partition("-")
    return name.upper(), int(num)
''',
    "tests/__init__.py": "",
    "tests/test_pricing.py": """from shop.pricing import line_total, tiered_discount, unit_price, with_tax


def test_unit_price_discount():
    assert unit_price({"price": 10, "discount": 0.5}) == 5.0


def test_line_total():
    assert line_total({"price": 2.5, "qty": 4}) == 10.0


def test_with_tax():
    assert with_tax(100.0) == 108.0


def test_tiers():
    assert tiered_discount(50) == 0.0 and tiered_discount(100) == 0.05
""",
    "tests/test_stock.py": """import pytest

from shop.stock import OutOfStock, Stock


def test_reserve_and_restock():
    s = Stock({"A": 3})
    assert s.reserve("A", 2) == 1
    assert s.restock("A", 5) == 6


def test_out_of_stock():
    with pytest.raises(OutOfStock):
        Stock({"A": 1}).reserve("A", 2)


def test_low():
    assert Stock({"A": 1, "B": 9, "C": 5}).low() == ["A", "C"]
""",
    "tests/test_orders.py": """from shop.orders import order_subtotal, order_total, validate


def test_subtotal():
    assert order_subtotal([{"price": 2, "qty": 3}, {"price": 1, "qty": 1}]) == 7.0


def test_total_with_tax():
    assert order_total([{"price": 10, "qty": 1}]) == 10.8


def test_validate():
    assert validate([{"qty": 0, "price": 1}]) == ["item 0: qty must be positive"]
""",
    "tests/test_report.py": """from shop.report import average_order, busiest_hours, revenue, top_skus


def test_revenue_and_average():
    orders = [[{"price": 10, "qty": 1}], [{"price": 20, "qty": 1}]]
    assert revenue(orders) == 32.4 and average_order(orders) == 16.2


def test_top_skus():
    orders = [[{"sku": "A", "qty": 2}], [{"sku": "B", "qty": 5}, {"sku": "A", "qty": 1}]]
    assert top_skus(orders, 1) == [("B", 5)]


def test_busiest_hours():
    assert busiest_hours([9, 9, 14, 14, 14, 23]) == [14, 9]
""",
    "tests/test_util.py": """from shop.util import chunks, clamp, parse_sku


def test_chunks():
    assert chunks([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]


def test_clamp():
    assert clamp(5, 0, 3) == 3 and clamp(-1, 0, 3) == 0


def test_parse_sku():
    assert parse_sku("abc-12") == ("ABC", 12)
""",
}

TS_BASE: dict[str, str] = {
    "package.json": '{"name": "shop-ts", "private": true, "type": "module", "scripts": {"test": "node --test tests/"}}\n',
    "src/pricing.mjs": """export const TAX_RATE = 0.08;

export function unitPrice(item) {
  const discount = item.discount ?? 0;
  return item.price * (1 - discount);
}

export function lineTotal(item) {
  return unitPrice(item) * item.qty;
}

export function withTax(amount, rate = TAX_RATE) {
  return Math.round(amount * (1 + rate) * 100) / 100;
}

export function tieredDiscount(total) {
  if (total >= 500) return 0.1;
  if (total >= 100) return 0.05;
  return 0;
}
""",
    "src/orders.mjs": """import { lineTotal, tieredDiscount, withTax } from './pricing.mjs';

export function orderSubtotal(items) {
  return items.reduce((sum, i) => sum + lineTotal(i), 0);
}

export function orderTotal(items, tax = true) {
  const subtotal = orderSubtotal(items);
  const discounted = subtotal * (1 - tieredDiscount(subtotal));
  return tax ? withTax(discounted) : Math.round(discounted * 100) / 100;
}

export function validate(items) {
  const problems = [];
  items.forEach((item, idx) => {
    if ((item.qty ?? 0) <= 0) problems.push(`item ${idx}: qty must be positive`);
    if ((item.price ?? 0) < 0) problems.push(`item ${idx}: negative price`);
  });
  return problems;
}

export function chunks(items, size) {
  const out = [];
  for (let i = 0; i < items.length; i += size) out.push(items.slice(i, i + size));
  return out;
}

export async function loadOrders(reader) {
  const text = await reader();
  return JSON.parse(text).orders;
}
""",
    "tests/pricing.test.mjs": """import test from 'node:test';
import assert from 'node:assert/strict';
import { lineTotal, tieredDiscount, unitPrice, withTax } from '../src/pricing.mjs';

test('unit price discount', () => { assert.equal(unitPrice({ price: 10, discount: 0.5 }), 5); });
test('line total', () => { assert.equal(lineTotal({ price: 2.5, qty: 4 }), 10); });
test('with tax', () => { assert.equal(withTax(100), 108); });
test('tiers', () => { assert.equal(tieredDiscount(50), 0); assert.equal(tieredDiscount(100), 0.05); });
""",
    "tests/orders.test.mjs": """import test from 'node:test';
import assert from 'node:assert/strict';
import { chunks, orderSubtotal, orderTotal, validate } from '../src/orders.mjs';

test('subtotal', () => { assert.equal(orderSubtotal([{ price: 2, qty: 3 }, { price: 1, qty: 1 }]), 7); });
test('total with tax', () => { assert.equal(orderTotal([{ price: 10, qty: 1 }]), 10.8); });
test('validate', () => { assert.deepEqual(validate([{ qty: 0, price: 1 }]), ['item 0: qty must be positive']); });
test('chunks', () => { assert.deepEqual(chunks([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]]); });
""",
}

GO_BASE: dict[str, str] = {
    "go.mod": "module shop\n\ngo 1.21\n",
    "pricing.go": """package shop

import "math"

const TaxRate = 0.08

type Item struct {
	SKU      string
	Price    float64
	Qty      int
	Discount float64
}

func UnitPrice(it Item) float64 { return it.Price * (1 - it.Discount) }

func LineTotal(it Item) float64 { return UnitPrice(it) * float64(it.Qty) }

func WithTax(amount float64) float64 { return math.Round(amount*(1+TaxRate)*100) / 100 }

func TieredDiscount(total float64) float64 {
	if total >= 500 {
		return 0.10
	}
	if total >= 100 {
		return 0.05
	}
	return 0
}
""",
    "orders.go": """package shop

import (
	"fmt"
	"math"
)

func OrderSubtotal(items []Item) float64 {
	sum := 0.0
	for _, it := range items {
		sum += LineTotal(it)
	}
	return sum
}

func OrderTotal(items []Item, tax bool) float64 {
	subtotal := OrderSubtotal(items)
	discounted := subtotal * (1 - TieredDiscount(subtotal))
	if tax {
		return WithTax(discounted)
	}
	return math.Round(discounted*100) / 100
}

func Validate(items []Item) []string {
	var problems []string
	for idx, it := range items {
		if it.Qty <= 0 {
			problems = append(problems, fmt.Sprintf("item %d: qty must be positive", idx))
		}
		if it.Price < 0 {
			problems = append(problems, fmt.Sprintf("item %d: negative price", idx))
		}
	}
	return problems
}

func Chunks(items []int, size int) [][]int {
	var out [][]int
	for i := 0; i < len(items); i += size {
		end := i + size
		if end > len(items) {
			end = len(items)
		}
		out = append(out, items[i:end])
	}
	return out
}
""",
    "shop_test.go": """package shop

import "testing"

func TestUnitPrice(t *testing.T) {
	if got := UnitPrice(Item{Price: 10, Discount: 0.5}); got != 5 {
		t.Fatalf("got %v", got)
	}
}

func TestOrderTotal(t *testing.T) {
	if got := OrderTotal([]Item{{Price: 10, Qty: 1}}, true); got != 10.8 {
		t.Fatalf("got %v", got)
	}
}

func TestValidate(t *testing.T) {
	p := Validate([]Item{{Qty: 0, Price: 1}})
	if len(p) != 1 || p[0] != "item 0: qty must be positive" {
		t.Fatalf("got %v", p)
	}
}

func TestChunks(t *testing.T) {
	c := Chunks([]int{1, 2, 3, 4, 5}, 2)
	if len(c) != 3 || len(c[2]) != 1 {
		t.Fatalf("got %v", c)
	}
}
""",
}

BASES = {"python": PY_BASE, "typescript": TS_BASE, "go": GO_BASE}
TEST_COMMANDS = {
    "python": "python3 -m pytest -q -p no:cacheprovider",
    "typescript": "node --test tests/",
    "go": "go test ./...",
}
TOOLCHAIN = {"python": "python3", "typescript": "node", "go": "go"}
PREFIX = {"python": "py", "typescript": "ts", "go": "go"}


# --------------------------------------------------------------------------------------------
# Defect catalogue
# --------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Defect:
    kind: str  # off_by_one | wrong_import | missing_none_check | swapped_args | async_misuse |
    #            config_typo | multi_file_contract | wrong_operator | early_return | bad_format
    file: str
    old: str  # exact text replaced (first occurrence); its line is the localisation answer
    new: str
    symptom: str  # what the user reports
    hidden_test_file: str
    hidden_test: str
    visible_fail: bool  # does the visible suite fail with the defect?
    extra: dict[str, tuple[str, str]] = field(default_factory=dict)  # file -> (old, new)


def _py(kind, file, old, new, symptom, test, *, visible=True, extra=None) -> Defect:
    return Defect(
        kind, file, old, new, symptom, f"tests/test_hidden_{kind}.py", test, visible, extra or {}
    )


PY_DEFECTS: list[Defect] = [
    _py(
        "off_by_one",
        "shop/util.py",
        "range(0, len(items), size)",
        "range(0, len(items) - 1, size)",
        "chunks() drops the last element when the list length is a multiple of the chunk size plus one",
        "from shop.util import chunks\n\n\ndef test_chunks_keeps_tail():\n    assert chunks([1, 2, 3], 2) == [[1, 2], [3]]\n    assert chunks([1, 2, 3, 4], 2) == [[1, 2], [3, 4]]\n",
    ),
    _py(
        "wrong_operator",
        "shop/pricing.py",
        "return base * (1.0 - discount)",
        "return base * (1.0 + discount)",
        "discounted items come out MORE expensive than list price",
        "from shop.pricing import unit_price\n\n\ndef test_discount_lowers_price():\n    assert unit_price({'price': 100, 'discount': 0.25}) == 75.0\n",
    ),
    _py(
        "missing_none_check",
        "shop/orders.py",
        '        if int(item.get("qty", 0)) <= 0:',
        '        if int(item["qty"]) <= 0:',
        "validate() crashes with KeyError on an item without qty instead of reporting a problem",
        "from shop.orders import validate\n\n\ndef test_validate_missing_qty():\n    problems = validate([{'price': 1}])\n    assert len(problems) == 1 and 'qty' in problems[0] and problems[0].startswith('item 0:')\n",
        visible=False,
    ),
    _py(
        "swapped_args",
        "shop/orders.py",
        'left[item["sku"]] = stock.reserve(item["sku"], int(item["qty"]))',
        'left[item["sku"]] = stock.reserve(item["sku"], int(item["qty"]) * 0 + stock.available(item["sku"]))',
        "fulfil() empties the whole stock of a SKU no matter the ordered quantity",
        "from shop.orders import fulfil\nfrom shop.stock import Stock\n\n\ndef test_fulfil_reserves_only_qty():\n    assert fulfil([{'sku': 'A', 'qty': 2}], Stock({'A': 5})) == {'A': 3}\n",
        visible=False,
    ),
    _py(
        "wrong_import",
        "shop/report.py",
        "from shop.orders import order_total",
        "from shop.orders import order_subtotal as order_total",
        "revenue() ignores tax and discounts; the daily report is lower than the sum of receipts",
        "from shop.report import revenue\n\n\ndef test_revenue_includes_tax():\n    assert revenue([[{'price': 10, 'qty': 1}]]) == 10.8\n",
    ),
    _py(
        "early_return",
        "shop/stock.py",
        "        self.levels[sku] = have - qty\n        return self.levels[sku]",
        "        return have - qty\n        self.levels[sku] = have - qty",
        "reserve() reports the right remainder but the stock level never actually goes down",
        "from shop.stock import Stock\n\n\ndef test_reserve_persists():\n    s = Stock({'A': 5})\n    s.reserve('A', 2)\n    assert s.available('A') == 3\n",
    ),
    _py(
        "config_typo",
        "shop/pricing.py",
        "TAX_RATE = 0.08",
        "TAX_RATE = 0.8",
        "every total is wildly too high since the last deploy",
        "from shop.pricing import TAX_RATE, with_tax\n\n\ndef test_tax_rate():\n    assert TAX_RATE == 0.08 and with_tax(50.0) == 54.0\n",
    ),
    _py(
        "bad_format",
        "shop/orders.py",
        "lines = [f\"{i['sku']} x{i['qty']} = {line_total(i):.2f}\" for i in items]",
        "lines = [f\"{i['sku']} x{i['qty']} = {line_total(i):.1f}\" for i in items]",
        "summary() prints line amounts with one decimal; the receipt must show two decimals like the total",
        "from shop.orders import summary\n\n\ndef test_summary_two_decimals():\n    assert summary([{'sku': 'A', 'qty': 1, 'price': 2.5}]).splitlines()[0] == 'A x1 = 2.50'\n",
        visible=False,
    ),
    _py(
        "multi_file_contract",
        "shop/pricing.py",
        "def tiered_discount(total: float) -> float:",
        "def tiered_discount(total: float, member: bool = False) -> float:\n    if member:\n        return 0.15",
        "members are promised 15% off but order_total never passes the member flag through",
        "from shop.orders import order_total\n\n\ndef test_member_discount():\n    assert order_total([{'price': 100, 'qty': 1}], tax=False, member=True) == 85.0\n",
        visible=False,
        extra={
            "shop/orders.py": (
                "def order_total(items: list[dict], tax: bool = True) -> float:\n    subtotal = order_subtotal(items)\n    discounted = subtotal * (1.0 - tiered_discount(subtotal))",
                "def order_total(items: list[dict], tax: bool = True) -> float:\n    subtotal = order_subtotal(items)\n    discounted = subtotal * (1.0 - tiered_discount(subtotal))",
            )
        },
    ),
    _py(
        "wrong_operator",
        "shop/stock.py",
        "if n <= threshold",
        "if n < threshold",
        "low() misses SKUs sitting exactly at the threshold",
        "from shop.stock import Stock\n\n\ndef test_low_includes_threshold():\n    assert Stock({'A': 5}).low() == ['A']\n",
    ),
    _py(
        "off_by_one",
        "shop/report.py",
        "counts: Counter[int] = Counter(h % 24 for h in stamps)",
        "counts: Counter[int] = Counter((h + 1) % 24 for h in stamps)",
        "busiest_hours() reports every hour one too late",
        "from shop.report import busiest_hours\n\n\ndef test_busiest_hours_exact():\n    assert busiest_hours([3, 3, 7]) == [3, 7]\n",
    ),
    _py(
        "missing_none_check",
        "shop/util.py",
        '    name, _, num = text.partition("-")\n    return name.upper(), int(num)',
        '    name, _, num = text.partition("-")\n    return name.upper(), int(num or 0)',
        "parse_sku('ABC') silently returns 0 instead of raising ValueError like it used to",
        "import pytest\n\nfrom shop.util import parse_sku\n\n\ndef test_parse_sku_requires_number():\n    with pytest.raises(ValueError):\n        parse_sku('ABC')\n",
        visible=False,
    ),
    _py(
        "swapped_args",
        "shop/util.py",
        "return max(low, min(high, value))",
        "return max(high, min(low, value))",
        "clamp() returns the upper bound for everything",
        "from shop.util import clamp\n\n\ndef test_clamp_inside_range():\n    assert clamp(2, 0, 3) == 2\n",
    ),
    _py(
        "early_return",
        "shop/report.py",
        "    if not orders:\n        return 0.0\n    return round(revenue(orders) / len(orders), 2)",
        "    if not orders:\n        return 0.0\n    return round(revenue(orders), 2)",
        "average_order() equals total revenue instead of the mean",
        "from shop.report import average_order\n\n\ndef test_average_is_mean():\n    assert average_order([[{'price': 10, 'qty': 1}], [{'price': 10, 'qty': 1}]]) == 10.8\n",
    ),
    _py(
        "wrong_operator",
        "shop/pricing.py",
        "    if total >= 100:\n        return 0.05",
        "    if total > 100:\n        return 0.05",
        "an order of exactly 100 gets no discount although the tier starts at 100",
        "from shop.pricing import tiered_discount\n\n\ndef test_tier_boundary():\n    assert tiered_discount(100) == 0.05\n",
    ),
]

TS_DEFECTS: list[Defect] = [
    Defect(
        "off_by_one",
        "src/orders.mjs",
        "for (let i = 0; i < items.length; i += size)",
        "for (let i = 0; i < items.length - 1; i += size)",
        "chunks() drops the last element for odd lengths",
        "tests/hidden_off_by_one.test.mjs",
        "import test from 'node:test';\nimport assert from 'node:assert/strict';\nimport { chunks } from '../src/orders.mjs';\ntest('tail kept', () => { assert.deepEqual(chunks([1, 2, 3], 2), [[1, 2], [3]]); });\n",
        True,
    ),
    Defect(
        "wrong_operator",
        "src/pricing.mjs",
        "return item.price * (1 - discount);",
        "return item.price * (1 + discount);",
        "discounted items cost more",
        "tests/hidden_wrong_operator.test.mjs",
        "import test from 'node:test';\nimport assert from 'node:assert/strict';\nimport { unitPrice } from '../src/pricing.mjs';\ntest('discount lowers', () => { assert.equal(unitPrice({ price: 100, discount: 0.25 }), 75); });\n",
        True,
    ),
    Defect(
        "missing_none_check",
        "src/orders.mjs",
        "if ((item.qty ?? 0) <= 0)",
        "if (item.qty <= 0)",
        "validate() lets items without qty through",
        "tests/hidden_missing_none_check.test.mjs",
        "import test from 'node:test';\nimport assert from 'node:assert/strict';\nimport { validate } from '../src/orders.mjs';\ntest('missing qty', () => { const p = validate([{ price: 1 }]); assert.equal(p.length, 1); assert.ok(p[0].startsWith('item 0:') && p[0].includes('qty')); });\n",
        False,
    ),
    Defect(
        "async_misuse",
        "src/orders.mjs",
        "const text = await reader();",
        "const text = reader();",
        "loadOrders() throws 'Unexpected token' for every reader",
        "tests/hidden_async_misuse.test.mjs",
        "import test from 'node:test';\nimport assert from 'node:assert/strict';\nimport { loadOrders } from '../src/orders.mjs';\ntest('awaits reader', async () => { assert.deepEqual(await loadOrders(async () => '{\"orders\": [[]]}'), [[]]); });\n",
        False,
    ),
    Defect(
        "config_typo",
        "src/pricing.mjs",
        "export const TAX_RATE = 0.08;",
        "export const TAX_RATE = 0.8;",
        "totals are far too high",
        "tests/hidden_config_typo.test.mjs",
        "import test from 'node:test';\nimport assert from 'node:assert/strict';\nimport { withTax } from '../src/pricing.mjs';\ntest('tax', () => { assert.equal(withTax(50), 54); });\n",
        True,
    ),
    Defect(
        "wrong_operator",
        "src/pricing.mjs",
        "if (total >= 100) return 0.05;",
        "if (total > 100) return 0.05;",
        "an order of exactly 100 gets no discount",
        "tests/hidden_tier.test.mjs",
        "import test from 'node:test';\nimport assert from 'node:assert/strict';\nimport { tieredDiscount } from '../src/pricing.mjs';\ntest('boundary', () => { assert.equal(tieredDiscount(100), 0.05); });\n",
        True,
    ),
    Defect(
        "early_return",
        "src/orders.mjs",
        "  return tax ? withTax(discounted) : Math.round(discounted * 100) / 100;",
        "  return withTax(discounted);",
        "orderTotal(items, false) still adds tax",
        "tests/hidden_early_return.test.mjs",
        "import test from 'node:test';\nimport assert from 'node:assert/strict';\nimport { orderTotal } from '../src/orders.mjs';\ntest('no tax', () => { assert.equal(orderTotal([{ price: 10, qty: 1 }], false), 10); });\n",
        False,
    ),
    Defect(
        "swapped_args",
        "src/pricing.mjs",
        "return Math.round(amount * (1 + rate) * 100) / 100;",
        "return Math.round(amount * (1 + rate) / 100) * 100;",
        "withTax rounds to hundreds",
        "tests/hidden_swapped.test.mjs",
        "import test from 'node:test';\nimport assert from 'node:assert/strict';\nimport { withTax } from '../src/pricing.mjs';\ntest('cents', () => { assert.equal(withTax(10), 10.8); });\n",
        True,
    ),
    Defect(
        "bad_format",
        "src/orders.mjs",
        "problems.push(`item ${idx}: negative price`)",
        "problems.push(`item ${idx} negative price`)",
        "negative-price problems lack the colon the UI parses on",
        "tests/hidden_format.test.mjs",
        "import test from 'node:test';\nimport assert from 'node:assert/strict';\nimport { validate } from '../src/orders.mjs';\ntest('colon', () => { assert.deepEqual(validate([{ qty: 1, price: -1 }]), ['item 0: negative price']); });\n",
        False,
    ),
    Defect(
        "wrong_import",
        "src/orders.mjs",
        "import { lineTotal, tieredDiscount, withTax } from './pricing.mjs';",
        "import { unitPrice as lineTotal, tieredDiscount, withTax } from './pricing.mjs';",
        "subtotals ignore quantities",
        "tests/hidden_import.test.mjs",
        "import test from 'node:test';\nimport assert from 'node:assert/strict';\nimport { orderSubtotal } from '../src/orders.mjs';\ntest('qty counts', () => { assert.equal(orderSubtotal([{ price: 2, qty: 3 }]), 6); });\n",
        True,
    ),
]


def _hard(kind, file, old, new, symptom, test, *, visible=False, extra=None) -> Defect:
    return Defect(
        kind,
        file,
        old,
        new,
        symptom,
        f"tests/test_hidden_hard_{kind}.py",
        test,
        visible,
        extra or {},
    )


# Hard tier: the symptom is reported in one module while the defect lives in another, a
# visible test encodes the wrong behaviour, or state leaks between calls. Python only.
HARD_DEFECTS: list[Defect] = [
    _hard(
        "cross_module",
        "shop/util.py",
        "return name.upper(), int(num)",
        "return name.lower(), int(num)",
        "Stock.find('abc-12') returns 0 for a SKU that is in stock",
        "from shop.stock import Stock\n\n\ndef test_find_normalises_case():\n    assert Stock({'ABC-12': 3}).find('abc-12') == 3\n",
        visible=True,
    ),
    _hard(
        "misleading_test",
        "shop/pricing.py",
        "    if total >= 100:\n        return 0.05",
        "    if total > 100:\n        return 0.05",
        "the spec says an order of exactly 100 gets the 5% tier, but the code and one existing test disagree with the spec; fix the code and correct the wrong test",
        "from shop.pricing import tiered_discount\n\n\ndef test_tier_starts_at_100():\n    assert tiered_discount(100) == 0.05 and tiered_discount(99.99) == 0.0\n",
        extra={
            "tests/test_pricing.py": (
                "assert tiered_discount(50) == 0.0 and tiered_discount(100) == 0.05",
                "assert tiered_discount(50) == 0.0 and tiered_discount(100) == 0.0",
            )
        },
    ),
    _hard(
        "shared_state",
        "shop/stock.py",
        "    def __init__(self, levels: dict[str, int] | None = None) -> None:\n        self.levels = dict(levels or {})",
        "    def __init__(self, levels: dict[str, int] = {}) -> None:  # noqa: B006\n        self.levels = levels",
        "a freshly created Stock() sometimes already contains another warehouse's levels",
        "from shop.stock import Stock\n\n\ndef test_instances_are_independent():\n    a = Stock()\n    a.restock('A', 2)\n    assert Stock().available('A') == 0\n",
    ),
    _hard(
        "two_level_off_by_one",
        "shop/report.py",
        "    return chunks(orders, size)",
        "    return chunks(orders, size)[:-1]",
        "the daily batch export drops the final partial batch (and the only batch when there is one)",
        "from shop.report import batches\n\n\ndef test_batches_keep_tail():\n    assert batches([[], [], []], 2) == [[[], []], [[]]]\n    assert batches([[]], 5) == [[[]]]\n",
    ),
    _hard(
        "swallowed_exception",
        "shop/orders.py",
        '    for item in items:\n        left[item["sku"]] = stock.reserve(item["sku"], int(item["qty"]))',
        '    for item in items:\n        try:\n            left[item["sku"]] = stock.reserve(item["sku"], int(item["qty"]))\n        except Exception:\n            left[item["sku"]] = stock.available(item["sku"])',
        "orders for out-of-stock items are fulfilled silently instead of failing",
        "import pytest\n\nfrom shop.orders import fulfil\nfrom shop.stock import OutOfStock, Stock\n\n\ndef test_fulfil_raises_when_short():\n    with pytest.raises(OutOfStock):\n        fulfil([{'sku': 'A', 'qty': 5}], Stock({'A': 1}))\n",
    ),
    _hard(
        "rounding_order",
        "shop/orders.py",
        "    subtotal = order_subtotal(items)\n    discounted = subtotal * (1.0 - tiered_discount(subtotal))",
        "    subtotal = round(order_subtotal(items))\n    discounted = subtotal * (1.0 - tiered_discount(subtotal))",
        "totals are off by a few cents for fractional prices",
        "from shop.orders import order_total\n\n\ndef test_no_premature_rounding():\n    assert order_total([{'price': 0.4, 'qty': 3}], tax=False) == 1.2\n",
    ),
    _hard(
        "config_key",
        "shop/util.py",
        'return [list(o) for o in data["orders"]]',
        'return [list(o) for o in data["order"]]',
        "load_orders raises KeyError on every orders file exported by the shop",
        "import json\n\nfrom shop.util import load_orders\n\n\ndef test_load_orders(tmp_path):\n    p = tmp_path / 'o.json'\n    p.write_text(json.dumps({'orders': [[{'sku': 'A', 'qty': 1, 'price': 2}]]}))\n    assert load_orders(p) == [[{'sku': 'A', 'qty': 1, 'price': 2}]]\n",
    ),
    _hard(
        "early_return_in_loop",
        "shop/orders.py",
        '        if float(item.get("price", 0)) < 0:\n            problems.append(f"item {idx}: negative price")\n    return problems',
        '        if float(item.get("price", 0)) < 0:\n            problems.append(f"item {idx}: negative price")\n        return problems\n    return problems',
        "validate() only ever reports the first item's problems",
        "from shop.orders import validate\n\n\ndef test_validate_reports_all_items():\n    assert len(validate([{'qty': 0, 'price': 1}, {'qty': 1, 'price': -1}])) == 2\n",
    ),
    _hard(
        "wrong_aggregation",
        "shop/report.py",
        '            counts[item["sku"]] += int(item["qty"])',
        '            counts[item["sku"]] = int(item["qty"])',
        "top SKUs ignore repeat purchases of the same SKU across orders",
        "from shop.report import top_skus\n\n\ndef test_top_skus_sums_across_orders():\n    orders = [[{'sku': 'A', 'qty': 2}], [{'sku': 'A', 'qty': 2}], [{'sku': 'B', 'qty': 3}]]\n    assert top_skus(orders, 1) == [('A', 4)]\n",
    ),
    _hard(
        "symptom_elsewhere",
        "shop/report.py",
        "    return stock.low(threshold)",
        "    return stock.low(0)",
        "the low-stock alert in the daily report is always empty even when SKUs are at 1 or 2 units",
        "from shop.report import low_stock_alert\nfrom shop.stock import Stock\n\n\ndef test_low_stock_alert_uses_threshold():\n    assert low_stock_alert(Stock({'A': 1, 'B': 9})) == ['A']\n",
    ),
]


GO_DEFECTS: list[Defect] = [
    Defect(
        "off_by_one",
        "orders.go",
        "for i := 0; i < len(items); i += size {",
        "for i := 0; i < len(items)-1; i += size {",
        "Chunks drops the last element",
        "hidden_off_test.go",
        'package shop\n\nimport "testing"\n\nfunc TestChunksTail(t *testing.T) {\n\tc := Chunks([]int{1, 2, 3}, 2)\n\tif len(c) != 2 || c[1][0] != 3 {\n\t\tt.Fatalf("got %v", c)\n\t}\n}\n',
        True,
    ),
    Defect(
        "wrong_operator",
        "pricing.go",
        "func UnitPrice(it Item) float64 { return it.Price * (1 - it.Discount) }",
        "func UnitPrice(it Item) float64 { return it.Price * (1 + it.Discount) }",
        "discounts raise prices",
        "hidden_op_test.go",
        'package shop\n\nimport "testing"\n\nfunc TestDiscountLowers(t *testing.T) {\n\tif UnitPrice(Item{Price: 100, Discount: 0.25}) != 75 {\n\t\tt.Fatal("discount must lower the price")\n\t}\n}\n',
        True,
    ),
    Defect(
        "config_typo",
        "pricing.go",
        "const TaxRate = 0.08",
        "const TaxRate = 0.8",
        "totals far too high",
        "hidden_tax_test.go",
        'package shop\n\nimport "testing"\n\nfunc TestTax(t *testing.T) {\n\tif WithTax(50) != 54 {\n\t\tt.Fatal("tax rate wrong")\n\t}\n}\n',
        True,
    ),
    Defect(
        "wrong_operator",
        "pricing.go",
        "\tif total >= 100 {\n\t\treturn 0.05",
        "\tif total > 100 {\n\t\treturn 0.05",
        "exactly 100 gets no discount",
        "hidden_tier_test.go",
        'package shop\n\nimport "testing"\n\nfunc TestTierBoundary(t *testing.T) {\n\tif TieredDiscount(100) != 0.05 {\n\t\tt.Fatal("boundary")\n\t}\n}\n',
        True,
    ),
    Defect(
        "early_return",
        "orders.go",
        "\tif tax {\n\t\treturn WithTax(discounted)\n\t}\n\treturn math.Round(discounted*100) / 100",
        "\treturn WithTax(discounted)\n\tif tax {\n\t\treturn math.Round(discounted*100) / 100\n\t}",
        "OrderTotal with tax=false still adds tax",
        "hidden_notax_test.go",
        'package shop\n\nimport "testing"\n\nfunc TestNoTax(t *testing.T) {\n\tif OrderTotal([]Item{{Price: 10, Qty: 1}}, false) != 10 {\n\t\tt.Fatal("tax applied")\n\t}\n}\n',
        False,
    ),
    Defect(
        "missing_none_check",
        "orders.go",
        "\t\tif it.Qty <= 0 {",
        "\t\tif it.Qty < 0 {",
        "zero-quantity items pass validation",
        "hidden_zero_test.go",
        'package shop\n\nimport "testing"\n\nfunc TestZeroQty(t *testing.T) {\n\tif len(Validate([]Item{{Qty: 0, Price: 1}})) != 1 {\n\t\tt.Fatal("zero qty must be rejected")\n\t}\n}\n',
        True,
    ),
    Defect(
        "bad_format",
        "orders.go",
        'fmt.Sprintf("item %d: negative price", idx)',
        'fmt.Sprintf("item %d negative price", idx)',
        "negative-price messages lost their colon",
        "hidden_fmt_test.go",
        'package shop\n\nimport "testing"\n\nfunc TestColon(t *testing.T) {\n\tp := Validate([]Item{{Qty: 1, Price: -1}})\n\tif p[0] != "item 0: negative price" {\n\t\tt.Fatalf("got %q", p[0])\n\t}\n}\n',
        False,
    ),
    Defect(
        "swapped_args",
        "pricing.go",
        "func WithTax(amount float64) float64 { return math.Round(amount*(1+TaxRate)*100) / 100 }",
        "func WithTax(amount float64) float64 { return math.Round(amount*(1+TaxRate)/100) * 100 }",
        "WithTax rounds to hundreds",
        "hidden_round_test.go",
        'package shop\n\nimport "testing"\n\nfunc TestCents(t *testing.T) {\n\tif WithTax(10) != 10.8 {\n\t\tt.Fatal("cents")\n\t}\n}\n',
        True,
    ),
    Defect(
        "wrong_import",
        "orders.go",
        "\t\tsum += LineTotal(it)",
        "\t\tsum += UnitPrice(it)",
        "subtotals ignore quantities",
        "hidden_qty_test.go",
        'package shop\n\nimport "testing"\n\nfunc TestQty(t *testing.T) {\n\tif OrderSubtotal([]Item{{Price: 2, Qty: 3}}) != 6 {\n\t\tt.Fatal("qty ignored")\n\t}\n}\n',
        False,
    ),
    Defect(
        "multi_file_contract",
        "pricing.go",
        "func LineTotal(it Item) float64 { return UnitPrice(it) * float64(it.Qty) }",
        "func LineTotal(it Item, qty int) float64 { return UnitPrice(it) * float64(qty) }",
        "the build is broken after LineTotal gained a qty parameter; callers were not updated",
        "hidden_build_test.go",
        'package shop\n\nimport "testing"\n\nfunc TestBuilds(t *testing.T) {\n\tif OrderSubtotal([]Item{{Price: 1, Qty: 2}}) != 2 {\n\t\tt.Fatal("subtotal")\n\t}\n}\n',
        True,
    ),
]


# --------------------------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Task:
    id: str
    lang: str
    defect: Defect
    prompt: str
    expected_changed: frozenset[str]
    answer_file: str
    answer_line: int
    test_command: str
    tier: str = "base"  # base | hard


def _line_of(base: dict[str, str], file: str, old: str) -> int:
    text = base[file]
    idx = text.index(old)
    return text[:idx].count("\n") + 1


def _prompt(defect: Defect, lang: str, visible: bool) -> str:
    if visible:
        return (
            f"The test suite is failing. Symptom reported by a user: {defect.symptom}. "
            "Find the cause, fix it with the smallest correct change, add a regression test, "
            "and make the whole suite pass."
        )
    return (
        f"Bug report: {defect.symptom}. The existing tests still pass, so you need to locate "
        "the cause yourself. Fix it with the smallest correct change, add a regression test "
        "that would have caught it, and keep the suite green."
    )


def build_tasks() -> list[Task]:
    """Deterministic 50 tasks: 30 Python, 10 TypeScript, 10 Go."""
    tasks: list[Task] = []
    plan = [("python", PY_DEFECTS, 2), ("typescript", TS_DEFECTS, 1), ("go", GO_DEFECTS, 1)]
    for lang, defects, repeats in plan:
        base = BASES[lang]
        n = 0
        for rep in range(repeats):
            for d in defects:
                n += 1
                # the second Python pass flips the visibility to weight localisation
                visible = d.visible_fail if rep == 0 else not d.visible_fail
                changed = {d.file, *d.extra.keys()}
                tasks.append(
                    Task(
                        id=f"{PREFIX[lang]}{n:02d}-{d.kind}",
                        lang=lang,
                        defect=d,
                        prompt=_prompt(d, lang, visible and d.visible_fail),
                        expected_changed=frozenset(changed),
                        answer_file=d.file,
                        answer_line=_line_of(base, d.file, d.old),
                        test_command=TEST_COMMANDS[lang],
                    )
                )
    base = tasks[:50]
    hard = []
    for n, d in enumerate(HARD_DEFECTS, 1):
        hard.append(
            Task(
                id=f"hd{n:02d}-{d.kind}",
                lang="python",
                defect=d,
                prompt=_prompt(d, "python", d.visible_fail),
                expected_changed=frozenset({d.file, *d.extra.keys()}),
                answer_file=d.file,
                answer_line=_line_of(PY_BASE, d.file, d.old),
                test_command=TEST_COMMANDS["python"],
                tier="hard",
            )
        )
    return base + hard


TASKS: list[Task] = build_tasks()


def get_task(task_id: str) -> Task:
    for t in TASKS:
        if t.id == task_id:
            return t
    raise KeyError(task_id)


def materialize(task: Task, dest: Path) -> str:
    """Write the base repo with the defect applied; returns the prompt."""
    base = BASES[task.lang]
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for rel, content in base.items():
        if rel == task.defect.file:
            assert task.defect.old in content, (task.id, rel)
            content = content.replace(task.defect.old, task.defect.new, 1)
        elif rel in task.defect.extra:
            old, new = task.defect.extra[rel]
            content = content.replace(old, new, 1)
        path = dest / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return task.prompt


def write_hidden_test(task: Task, root: Path) -> Path:
    path = root / task.defect.hidden_test_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(task.defect.hidden_test, encoding="utf-8")
    return path


def toolchain_available(lang: str) -> bool:
    return shutil.which(TOOLCHAIN[lang]) is not None
