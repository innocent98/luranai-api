"""Import parsing: amounts, value aliases, CSV dialects and the paste-from-WhatsApp lines format."""

import pytest

from app.services.offering_import import (
    ImportUnreadable,
    parse_amount,
    parse_csv,
    parse_lines,
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("12,000", 12000),
        ("₦12000", 12000),
        ("₦ 12,000", 12000),
        ("12k", 12000),
        ("12K", 12000),
        ("2.5k", 2500),
        ("1.5m", 1_500_000),
        ("N2,500", 2500),
        ("n2500", 2500),
        ("NGN 3,000", 3000),
        ("12,000 naira", 12000),
        ("12000.00", 12000),
        ("  900  ", 900),
        ("0", 0),
        ("-500", -500),
    ],
)
def test_amounts(text, expected):
    assert parse_amount(text) == expected


@pytest.mark.parametrize("text", ["abc", "free", "12000.50", "1e3", "2.5.5k", "12 000", "k", "₦"])
def test_unparseable_amounts_raise(text):
    with pytest.raises(ValueError):
        parse_amount(text)


def test_csv_header_is_required_and_extra_columns_are_ignored():
    rows = parse_csv("name,price,colour\nSneaker,12000,black\n")
    assert rows == [(1, {"name": "Sneaker", "price": "12000"})]


def test_csv_header_aliases_and_case():
    rows = parse_csv("Item , Amount , Stock\nSneaker,12k,sold out\n")
    assert rows == [(1, {"name": "Sneaker", "price": "12k", "availability": "sold out"})]


def test_csv_blank_cells_are_dropped_and_blank_rows_skipped():
    rows = parse_csv("name,price,category\nA,,\n\n,,\nB,5k,Bags\n")
    assert rows == [(1, {"name": "A"}), (2, {"name": "B", "price": "5k", "category": "Bags"})]


def test_csv_quoted_commas_crlf_and_bom():
    rows = parse_csv('﻿name,description\r\n"Sneaker, black","Fits 40, 41"\r\n')
    assert rows == [(1, {"name": "Sneaker, black", "description": "Fits 40, 41"})]


@pytest.mark.parametrize("sep", ["\t", ";"])
def test_csv_detects_tab_and_semicolon(sep):
    rows = parse_csv(f"name{sep}price\nSneaker{sep}12000\n")
    assert rows == [(1, {"name": "Sneaker", "price": "12000"})]


@pytest.mark.parametrize(
    "content", ["", "   \n  ", "price,category\n5k,Bags\n", "name\n", "a\x00b,price\n1,2\n"]
)
def test_unreadable_csv(content):
    with pytest.raises(ImportUnreadable):
        parse_csv(content)


def test_lines_format():
    rows = parse_lines(
        "Canvas sneaker - 12000 - in stock\nTote bag - ₦8,500\n\nHaircut - from 3k - limited\n"
    )
    assert rows == [
        (1, {"name": "Canvas sneaker", "price": "12000", "availability": "in stock"}),
        (2, {"name": "Tote bag", "price": "₦8,500"}),
        (3, {"name": "Haircut", "price": "from 3k", "availability": "limited"}),
    ]


def test_lines_keep_hyphens_inside_a_name():
    rows = parse_lines("T-shirt - 5000\nWedding dress – 150k – on request")
    assert rows[0] == (1, {"name": "T-shirt", "price": "5000"})
    assert rows[1] == (2, {"name": "Wedding dress", "price": "150k", "availability": "on request"})


def test_a_line_with_only_a_name_has_no_price():
    assert parse_lines("Consultation") == [(1, {"name": "Consultation"})]


def test_empty_lines_input_is_unreadable():
    with pytest.raises(ImportUnreadable):
        parse_lines("\n  \n")
