from datetime import date

from mf_me_dedup.rakuten import match


def row(
    account: str,
    day: str,
    description: str,
    amount: int,
    *,
    calculated: str = "1",
    transfer: str = "0",
    row_id: str | None = None,
) -> dict[str, str]:
    return {
        "計算対象": calculated,
        "日付": day.replace("-", "/"),
        "内容": description,
        "金額（円）": str(amount),
        "保有金融機関": account,
        "大項目": "",
        "中項目": "",
        "メモ": "",
        "振替": transfer,
        "ID": row_id if row_id is not None else f"{account}:{day}:{description}:{amount}",
    }


def card(day: str, description: str, amount: int = -3000, **kwargs) -> dict[str, str]:
    return row("楽天カード", day, description, amount, **kwargs)


def market(day: str, description: str, amount: int, **kwargs) -> dict[str, str]:
    return row("楽天市場(my Rakuten)", day, description, amount, **kwargs)


def test_case_01_exact_store_prefix_match():
    result = match(
        [
            card("2025-09-12", "ショップA 楽天市場店 ラクテンイチバ700001"),
            market("2025-09-10", "ショップA 楽天市場店 商品X", -3000),
        ],
        date(2025, 9, 1),
    )

    assert [(item.kind, item.market_date) for item in result] == [
        ("MATCH", date(2025, 9, 10))
    ]


def test_case_02_market_rows_sum_to_card_amount():
    result = match(
        [
            card("2025-07-08", "ショップC", -2100),
            market("2025-07-05", "ショップC 商品Y", -1600),
            market("2025-07-05", "ショップC 送料", -500),
        ],
        date(2025, 7, 1),
    )

    assert [(item.kind, item.market_date) for item in result] == [
        ("MATCH", date(2025, 7, 5))
    ]


def test_case_03_coupon_is_included_in_market_group_sum():
    result = match(
        [
            card("2025-07-07", "ショップD", -2700),
            market("2025-07-05", "ショップD 商品Z", -3000),
            market("2025-07-05", "ショップD クーポン利用", 300),
        ],
        date(2025, 7, 1),
    )

    assert [(item.kind, item.market_date) for item in result] == [
        ("MATCH", date(2025, 7, 5))
    ]


def test_case_04_truncated_card_store_key_matches_market_prefix():
    result = match(
        [
            card("2025-09-09", "サンプル雑貨ストア公式 楽 ラクテンイチバ700002", -5000),
            market("2025-09-04", "サンプル雑貨ストア公式 楽天市場店 商品", -5000),
        ],
        date(2025, 9, 1),
    )

    assert [(item.kind, item.market_date) for item in result] == [
        ("MATCH", date(2025, 9, 4))
    ]


def test_case_05_processed_card_reserves_its_market_group():
    result = match(
        [
            card("2025-09-05", "ショップE", -4000),
            market("2025-08-06", "ショップE 消耗品", -4000),
            market("2025-08-29", "ショップE 消耗品", -4000),
            card(
                "2025-08-08",
                "ショップE",
                -4000,
                calculated="0",
                transfer="1",
                row_id="processed-card",
            ),
        ],
        date(2025, 9, 1),
    )

    assert [(item.kind, item.market_date) for item in result] == [
        ("MATCH", date(2025, 8, 29))
    ]


def test_case_06_match_with_54_day_delay():
    result = match(
        [
            card("2025-03-10", "ショップF", -19000),
            market("2025-01-15", "ショップF 家具", -20000),
            market("2025-01-15", "ショップF クーポン利用", 1000),
        ],
        date(2025, 3, 1),
    )

    assert [(item.kind, item.market_date) for item in result] == [
        ("MATCH", date(2025, 1, 15))
    ]


def test_case_07_non_rakuten_card_purchase_is_omitted():
    result = match(
        [card("2025-09-20", "楽天SP コンビニG 700100", -500)],
        date(2025, 9, 1),
    )

    assert result == []


def test_case_08_prefix_candidate_with_wrong_amount_is_mismatch():
    result = match(
        [
            card("2025-09-15", "ショップH", -2000),
            market("2025-09-10", "ショップH 商品", -2500),
        ],
        date(2025, 9, 1),
    )

    assert [(item.kind, item.market_date) for item in result] == [
        ("AMOUNT_MISMATCH", None)
    ]


def test_case_09_equal_amount_outside_lookback_is_mismatch():
    result = match(
        [
            card("2025-09-15", "ショップI", -1000),
            market("2025-05-01", "ショップI 商品", -1000),
        ],
        date(2025, 9, 1),
    )

    assert [(item.kind, item.market_date) for item in result] == [
        ("AMOUNT_MISMATCH", None)
    ]


def test_used_equal_amount_group_does_not_hide_mismatch():
    result = match(
        [
            card(
                "2025-08-08",
                "ショップE",
                -4000,
                calculated="0",
                transfer="1",
            ),
            market("2025-08-06", "ショップE 消耗品", -4000),
            card("2025-09-05", "ショップE", -4000),
            market("2025-09-03", "ショップE 消耗品", -3500),
        ],
        date(2025, 9, 1),
    )

    assert [(item.kind, item.market_date) for item in result] == [
        ("AMOUNT_MISMATCH", None)
    ]


def test_case_10_excluded_market_row_is_not_a_candidate():
    result = match(
        [
            card("2025-09-15", "ショップJ", -1000),
            market("2025-09-10", "ショップJ 商品", -1000, calculated="0"),
        ],
        date(2025, 9, 1),
    )

    assert result == []


def test_case_11_processed_card_is_omitted():
    result = match(
        [
            card(
                "2025-09-15",
                "ショップK",
                -1000,
                calculated="0",
                transfer="1",
            ),
            market("2025-09-10", "ショップK 商品", -1000),
        ],
        date(2025, 9, 1),
    )

    assert result == []


def test_old_unprocessed_card_reserves_group_and_later_card_is_mismatch():
    rows = [
        card("2025-09-16", "ショップL", -1000),
        market("2025-08-30", "ショップL 商品", -1000),
        card("2025-09-01", "ショップL", -1000),
    ]

    for since in (date(2025, 9, 1), date(2025, 9, 16)):
        result = match(rows, since)

        assert [
            (item.kind, item.market_date)
            for item in result
            if item.card["日付"] == "2025/09/16"
        ] == [("AMOUNT_MISMATCH", None)]
