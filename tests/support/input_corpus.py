"""Accepted and rejected corpora, one per parser, written from the SP1 grammar.

Each corpus is specific to its parser: sentinels and percentages appear only
where the grammar defines them, nickname text such as ``NaN`` is accepted, and a
URL longer than 64 characters is valid. Expected values are literals here, never
computed by the code under test.
"""

from __future__ import annotations

from datetime import time
from decimal import Decimal
from typing import Any

from price_tracker.app.inputs import (
    Absolute,
    AnyDrop,
    Cancel,
    ClearTarget,
    Forever,
    IntervalMinutes,
    Off,
    Percentage,
    QuietHours,
    ResetInterval,
    SetTarget,
)

BIDI = "\u202e\u2066"
LONG_URL = "https://shop.example/" + "a" * 80

NUMERIC_HOSTILE = [
    "NaN",
    "nan",
    "Infinity",
    "-Infinity",
    "inf",
    "1e999",
    "-1e999",
    "1E3",
    "+5",
    "-5",
    "--10",
    "1,299.99",
    "1.299,99",
    "1..5",
    "1,,5",
    "1.2.3",
    "\uff15",
    "\u0665",
    "5" + BIDI,
    BIDI + "5",
    "5\u200b",
    "5\x00",
    BIDI,
    "",
    "   ",
    "1" * 65,
    "abc",
    "0x10",
    "1_000",
]

ACCEPTED: dict[str, list[tuple[str, Any]]] = {
    "threshold": [
        ("20%", Percentage(20)),
        ("1%", Percentage(1)),
        ("99%", Percentage(99)),
        ("007%", Percentage(7)),
        ("5.50", Absolute(Decimal("5.50"))),
        ("5,50", Absolute(Decimal("5.50"))),
        ("1.299", Absolute(Decimal("1.299"))),
        ("1,299", Absolute(Decimal("1.299"))),
        (" 5 ", Absolute(Decimal("5"))),
        ("\t12\n", Absolute(Decimal("12"))),
        ("999999999.9999", Absolute(Decimal("999999999.9999"))),
        ("any", AnyDrop()),
        ("ANY", AnyDrop()),
        ("ogni", AnyDrop()),
        ("sempre", AnyDrop()),
        ("all", AnyDrop()),
        ("-", Cancel()),
        ("no", Cancel()),
        ("skip", Cancel()),
        ("salta", Cancel()),
        ("annulla", Cancel()),
    ],
    "target": [
        ("49.90", SetTarget(Decimal("49.90"))),
        ("1,299", SetTarget(Decimal("1.299"))),
        (" 3 ", SetTarget(Decimal("3"))),
        ("0", ClearTarget()),
        ("0.00", ClearTarget()),
        ("-", Cancel()),
        ("skip", Cancel()),
    ],
    "product_interval": [
        ("0", ResetInterval()),
        ("5", IntervalMinutes(5)),
        ("10080", IntervalMinutes(10080)),
        (" 60 ", IntervalMinutes(60)),
    ],
    "global_interval": [("5", 5), ("10080", 10080), (" 360 ", 360)],
    "digest_interval": [("5", 5), ("1440", 1440)],
    "throttle": [("1", 1), ("999999999", 999999999), ("off", Off()), ("OFF", Off())],
    "mute_hours": [("1", 1), ("8760", 8760), ("forever", Forever())],
    "user_id": [
        ("1", 1),
        ("1000000000", 1000000000),
        ("9223372036854775807", 9223372036854775807),
        (" 42 ", 42),
    ],
    "nickname": [
        ("NaN", "NaN"),
        ("Infinity", "Infinity"),
        ("-", "-"),
        ("<b>Bob</b>", "<b>Bob</b>"),
        ("  Ana  ", "Ana"),
        ("\u674e\u96f7", "\u674e\u96f7"),
        ("x" * 64, "x" * 64),
        ("e\u0301" * 64, "e\u0301" * 64),
    ],
    "currency_code": [("usd", "USD"), (" EUR ", "EUR"), ("jpy", "JPY")],
    "timezone": [("Europe/Rome", "Europe/Rome"), (" UTC ", "UTC")],
    "quiet_hours": [
        ("22:00-08:00", QuietHours(time(22, 0), time(8, 0))),
        ("00:00-23:59", QuietHours(time(0, 0), time(23, 59))),
        ("off", Off()),
    ],
    "url": [
        ("https://shop.example/item/1", "https://shop.example/item/1"),
        (LONG_URL, LONG_URL),
        (" http://93.184.216.34/x ", "http://93.184.216.34/x"),
    ],
}

REJECTED: dict[str, list[str]] = {
    "threshold": [*NUMERIC_HOSTILE, "0", "0%", "100%", "999%", "10%%", "%5", "5 %", "0.00"],
    "target": [*NUMERIC_HOSTILE, "any", "10%", "1.12345"],
    "product_interval": [*NUMERIC_HOSTILE, "4", "10081", "1.5", "-", "any", "5%"],
    "global_interval": [*NUMERIC_HOSTILE, "0", "4", "10081", "off"],
    "digest_interval": [*NUMERIC_HOSTILE, "4", "1441"],
    "throttle": [*NUMERIC_HOSTILE, "0", "1000000000"],
    "mute_hours": [*NUMERIC_HOSTILE, "0", "8761", "all"],
    "user_id": [
        *NUMERIC_HOSTILE,
        "0",
        "9223372036854775808",
        "99999999999999999999",
    ],
    "nickname": ["", "   ", "x" * 65, "a\u202eb", "a\x00b", "line\u2028break", "\u0301\u0301"],
    "currency_code": ["us$", "US", "USDD", "XXQ", "\uff35SD", "", "1234", "U" + BIDI + "SD"],
    "timezone": ["Europe/Nowhere", "europe/rome", "", "UTC" + BIDI, "../etc/passwd"],
    "quiet_hours": ["22:00-22:00", "24:00-08:00", "22:60-08:00", "2200-0800", "22:00", "on"],
    "url": [
        "",
        "ftp://shop.example/x",
        "shop.example/x",
        "https://",
        "https://shop.example/a b",
        "https://shop.example/\u202e",
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest",
        "https://" + "a" * 2100 + ".example",
        "https://shop.example:99999/x",
    ],
}

# Inputs a user can actually type as a message: Telegram never delivers an
# empty or whitespace-only text message.
FLOW_REJECTED = {
    kind: [text for text in REJECTED[kind] if text.strip()]
    for kind in ("threshold", "target", "product_interval")
}
