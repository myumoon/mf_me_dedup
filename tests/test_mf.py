from datetime import date
from unittest.mock import MagicMock

import pytest

from mf_me_dedup import mf

HEADER = "計算対象,日付,内容,金額（円）,保有金融機関,大項目,中項目,メモ,振替,ID\r\n"


def row(row_id="id-1", *, day="2026/09/15", description="ショップA", amount="-3000"):
    return {
        "計算対象": "1",
        "日付": day,
        "内容": description,
        "金額（円）": amount,
        "保有金融機関": "楽天カード",
        "大項目": "食費",
        "中項目": "食料品",
        "メモ": "",
        "振替": "0",
        "ID": row_id,
    }


def csv_body(*lines):
    return (HEADER + "".join(line + "\r\n" for line in lines)).encode("cp932")


def test_parse_csv_reads_cp932_rows_as_strings():
    body = csv_body('1,2026/09/15,ショップ～A,-3000,楽天カード,食費,食料品,"メモ, あり",0,id-1')
    rows = mf._parse_csv(200, "text/csv; charset=utf-8", body)
    assert rows == [
        {
            "計算対象": "1",
            "日付": "2026/09/15",
            "内容": "ショップ～A",
            "金額（円）": "-3000",
            "保有金融機関": "楽天カード",
            "大項目": "食費",
            "中項目": "食料品",
            "メモ": "メモ, あり",
            "振替": "0",
            "ID": "id-1",
        }
    ]


@pytest.mark.parametrize(
    ("status", "content_type", "body"),
    [
        (302, "text/csv", csv_body()),
        (500, "text/csv", csv_body()),
        (200, "text/html; charset=utf-8", "<html>ログイン</html>".encode()),
        (200, "text/csv", "<html>ログイン</html>".encode("cp932")),
        (200, "text/csv", "計算対象,日付,内容\r\n".encode("cp932")),
        (200, "text/csv", b""),
        (200, "text/csv", b"\xff\xff"),
    ],
)
def test_parse_csv_rejects_non_csv_responses(status, content_type, body):
    with pytest.raises(RuntimeError):
        mf._parse_csv(status, content_type, body)


def test_select_dedupes_by_id_and_keeps_both_ends():
    rows = [
        row("a", day="2026/08/31"),
        row("b", day="2026/09/01"),
        row("b", day="2026/09/01"),
        row("c", day="2026/09/30"),
        row("d", day="2026/10/01"),
    ]
    selected = mf._select(rows, date(2026, 9, 1), date(2026, 9, 30))
    assert [item["ID"] for item in selected] == ["b", "c"]


def test_months_adds_one_month_on_each_side():
    assert mf._months(date(2026, 9, 1), date(2026, 9, 29)) == [
        (2026, 8),
        (2026, 9),
        (2026, 10),
    ]


def test_months_crosses_years():
    assert mf._months(date(2025, 12, 20), date(2026, 1, 5)) == [
        (2025, 11),
        (2025, 12),
        (2026, 1),
        (2026, 2),
    ]
    assert mf._months(date(2026, 1, 3), date(2026, 1, 3)) == [
        (2025, 12),
        (2026, 1),
        (2026, 2),
    ]


class FakeResponse:
    def __init__(self, body, url="https://moneyforward.com/cf/csv"):
        self.url = url
        self.status = 200
        self.headers = {"content-type": "text/csv; charset=utf-8"}
        self._body = body

    def body(self):
        return self._body


class FakeRequest:
    def __init__(self):
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        return FakeResponse(
            csv_body("1,2026/01/10,ショップA,-100,楽天カード,食費,食料品,,0,same-id")
        )


def test_fetch_rows_requests_each_month_and_dedupes():
    request = FakeRequest()
    context = type("Context", (), {"request": request})()
    rows = mf.fetch_rows(context, date(2026, 1, 1), date(2026, 1, 31))
    assert request.urls == [
        "https://moneyforward.com/cf/csv?from=2025/12/01&month=12&year=2025",
        "https://moneyforward.com/cf/csv?from=2026/01/01&month=1&year=2026",
        "https://moneyforward.com/cf/csv?from=2026/02/01&month=2&year=2026",
    ]
    assert [item["ID"] for item in rows] == ["same-id"]


def test_fetch_rows_reports_sign_in_page_as_login_required():
    # 未ログインでは 302 → id.moneyforward.com/sign_in の HTML が 200 で返る（契約）。
    response = FakeResponse(b"<html></html>", "https://id.moneyforward.com/sign_in")
    response.headers = {"content-type": "text/html; charset=utf-8"}
    request = type("Request", (), {"get": lambda self, url: response})()
    context = type("Context", (), {"request": request})()

    with pytest.raises(mf.LoginRequired):
        mf.fetch_rows(context, date(2026, 1, 1), date(2026, 1, 31))


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://id.moneyforward.com/sign_in", True),
        ("https://id.moneyforward.com/sign_in/password?x=1", True),
        ("https://id.moneyforward.com/", True),
        ("https://moneyforward.com/sign_in", True),
        ("https://moneyforward.com/sign_in/email", True),
        ("https://moneyforward.com/cf", False),
        ("https://moneyforward.com/", False),
        ("https://moneyforward.com/cf?sign_in=1", False),
        ("https://id.moneyforward.com.example/cf", False),
    ],
)
def test_is_login_url(url, expected):
    assert mf.is_login_url(url) is expected


@pytest.mark.parametrize(
    ("now", "wait"),
    [(0.0, 0.0), (20.0, 0.0), (25.0, 0.0), (25.5, 5.0), (29.0, 1.5), (59.9, 0.6)],
)
def test_totp_wait(now, wait):
    assert mf._totp_wait(now) == pytest.approx(wait)


def test_totp_secret_removes_spaces():
    assert mf._totp_secret(" ABCD EFGH\tIJKL ") == "ABCDEFGHIJKL"


def test_period_parses_unpadded_dates():
    assert mf._period("2026/8/25 - 2026/9/24") == (date(2026, 8, 25), date(2026, 9, 24))
    assert mf._period(" 2025/12/25 - 2026/1/23\n") == (
        date(2025, 12, 25),
        date(2026, 1, 23),
    )


def test_period_rejects_other_text():
    with pytest.raises(RuntimeError):
        mf._period("2026年9月")


def test_needs_previous():
    period = "2026/8/25 - 2026/9/24"
    assert mf._needs_previous(period, date(2026, 8, 24))
    assert not mf._needs_previous(period, date(2026, 8, 25))
    assert not mf._needs_previous(period, date(2026, 9, 24))
    with pytest.raises(RuntimeError):
        mf._needs_previous(period, date(2026, 9, 25))


def screen(rid, *, day="2026/09/15", description="ショップA", amount="-3,000円"):
    return [f"js-transaction-{rid}", day, description, amount]


def test_find_rid_returns_the_unique_match():
    rows = [
        screen("1", day="2026/09/14"),
        screen("2", description=" ショップA \n"),
        screen("3", amount="-300"),
        screen("4", description="ショップB"),
    ]
    assert mf._find_rid(rows, row()) == "2"


def test_find_rid_reads_amounts_with_commas():
    rows = [screen("7", amount="-1,234,567")]
    assert mf._find_rid(rows, row(amount="-1234567")) == "7"


def test_find_rid_replaces_wave_dash():
    rows = [screen("5", description="ショップ〜A")]
    assert mf._find_rid(rows, row(description="ショップ～A")) == "5"


def test_find_rid_rejects_no_match():
    with pytest.raises(RuntimeError, match="found 0") as error:
        mf._find_rid([screen("1", amount="-2,000")], row("secret-free-id"))
    assert "ショップA" not in str(error.value)
    assert "3000" not in str(error.value)


def test_find_rid_rejects_duplicates():
    with pytest.raises(RuntimeError, match="found 2"):
        mf._find_rid([screen("1"), screen("2")], row())


def test_find_rid_ignores_rows_without_rid():
    assert mf._find_rid([["other", "2026/09/15", "ショップA", "-3,000"], screen("9")], row()) == "9"


def test_option_value_matches_trimmed_label():
    options = [["  楽天市場(my Rakuten) ", "hash-a"], ["楽天カード", "hash-b"]]
    assert mf._option_value(options, "楽天市場(my Rakuten)") == "hash-a"


@pytest.mark.parametrize(
    "options",
    [[["楽天市場 楽天ブックス", "x"]], [["楽天市場", "x"], [" 楽天市場 ", "y"]]],
)
def test_option_value_rejects_missing_or_duplicate(options):
    with pytest.raises(RuntimeError):
        mf._option_value(options, "楽天市場")


def test_set_transfer_rejects_unknown_counterpart_before_touching_page():
    with pytest.raises(ValueError):
        mf.set_transfer(object(), row(), "Amazon")


def transfer_page(url=mf.CF_URL):
    page = MagicMock()
    page.url = url
    listing = page.locator.return_value
    listing.inner_text.return_value = "2026/8/25 - 2026/9/24"
    listing.evaluate_all.return_value = [
        ["js-transaction-4242", "2026/09/15", "ショップA", "-3,000"]
    ]
    return page


def test_set_transfer_stops_on_sign_in_page_before_any_click():
    page = transfer_page("https://id.moneyforward.com/sign_in")

    with pytest.raises(mf.LoginRequired):
        mf.set_transfer(page, row(), "楽天市場(my Rakuten)")

    page.locator.return_value.locator.return_value.click.assert_not_called()
    page.get_by_role.assert_not_called()


def test_set_transfer_failure_after_execute_names_rid_only():
    page = transfer_page()
    icon = page.locator.return_value.locator.return_value
    icon.wait_for.side_effect = Exception("Timeout <tr>ショップA -3,000 円</tr>")

    with pytest.raises(RuntimeError) as error:
        mf.set_transfer(page, row(), "楽天市場(my Rakuten)")

    message = str(error.value)
    assert "4242" in message
    assert "ショップA" not in message
    assert "3,000" not in message and "3000" not in message
    page.get_by_role.assert_called_with("link", name="実行する")


def test_login_waits_five_minutes_for_the_top_page(monkeypatch):
    page = MagicMock()
    monkeypatch.setattr(mf.time, "sleep", lambda seconds: None)

    mf.login(page, "user@example.test", "password", "JBSWY3DPEHPK3PXP")

    page.wait_for_url.assert_called_once_with(mf.TOP_URL, timeout=5 * 60 * 1000)


def test_source_keeps_default_timeouts_and_has_no_hash_values():
    import inspect
    import re

    source = inspect.getsource(mf)
    # 例外はログイン完了の待ち時間だけ（契約 M4-4）。
    source = source.replace("page.wait_for_url(TOP_URL, timeout=LOGIN_WAIT_MS)", "", 1)
    assert "timeout=" not in source
    assert "set_default_timeout" not in source
    assert not re.search(r"[0-9a-f]{16,}|[A-Za-z0-9+/]{32,}", source)
