import csv
import io
import re
import time
from datetime import date
from urllib.parse import urlsplit

import pyotp

TOP_URL = "https://moneyforward.com/"
CF_URL = "https://moneyforward.com/cf"
CSV_HEADER = [
    "計算対象",
    "日付",
    "内容",
    "金額（円）",
    "保有金融機関",
    "大項目",
    "中項目",
    "メモ",
    "振替",
    "ID",
]
SUB_ACCOUNTS = {"楽天市場(my Rakuten)": "楽天市場"}
MAX_MONTH_MOVES = 36
TOTP_MIN_REMAINING = 5
LOGIN_WAIT_MS = 5 * 60 * 1000


class LoginRequired(RuntimeError):
    """MF returned its sign-in page instead of the requested content."""


def is_login_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.hostname == "id.moneyforward.com" or parts.path.startswith("/sign_in")

# 画面（JIS 系の対応）→ CSV（cp932 の対応）。両側に適用して比べる。
_CP932_CHARS = str.maketrans(
    {
        "〜": "～",
        "‖": "∥",
        "−": "－",
        "¢": "￠",
        "£": "￡",
        "¬": "￢",
        "—": "―",
    }
)

_SCREEN_ROWS_JS = """trs => trs.map(tr => [
    tr.id,
    (tr.querySelector('td.date')?.getAttribute('data-table-sortable-value') || '').slice(0, 10),
    tr.querySelector('td.content')?.innerText || '',
    tr.querySelector('td.amount')?.innerText || '',
])"""


def _day(text: str) -> date:
    return date.fromisoformat(text.replace("/", "-"))


def _totp_secret(secret: str) -> str:
    return "".join(secret.split())


def _totp_wait(now: float, interval: int = 30) -> float:
    remaining = interval - now % interval
    return remaining + 0.5 if remaining < TOTP_MIN_REMAINING else 0.0


def login(page, email, password, totp_secret) -> None:
    # Playwright の例外はコールログに入力値（fill("<value>")）を含むので、
    # どの段階で失敗したかと型名だけの固定メッセージに置き換える。
    step = "open"
    try:
        page.goto(TOP_URL)
        page.get_by_role("button", name="ログイン / 新規登録").click()
        page.get_by_role("link", name="ログイン", exact=True).first.click()
        step = "email"
        page.get_by_role("textbox", name="メールアドレス").fill(email)
        page.get_by_role("button", name="ログインする").click()
        step = "password"
        page.get_by_role("textbox", name="パスワード").fill(password)
        page.get_by_role("button", name="ログインする").click()
        step = "totp"
        totp = pyotp.TOTP(_totp_secret(totp_secret))
        code_box = page.get_by_role("textbox", name="認証コード（数字6桁）")
        code_box.wait_for()
        time.sleep(_totp_wait(time.time(), totp.interval))
        code_box.fill(totp.now())
        page.get_by_role("button", name="認証する").click()
        step = "wait for the top page"
        page.wait_for_url(TOP_URL, timeout=LOGIN_WAIT_MS)
    except Exception as error:
        raise RuntimeError(f"login failed at {step}: {type(error).__name__}") from None


def _months(start: date, end: date) -> list[tuple[int, int]]:
    first = start.year * 12 + start.month - 1 - 1
    last = end.year * 12 + end.month - 1 + 1
    return [(index // 12, index % 12 + 1) for index in range(first, last + 1)]


def _parse_csv(status: int, content_type: str, body: bytes) -> list[dict]:
    if status != 200:
        raise RuntimeError(f"CSV request failed: HTTP {status}")
    if not content_type.startswith("text/csv"):
        raise RuntimeError("CSV request returned a non-CSV response")
    try:
        text = body.decode("cp932")
    except UnicodeDecodeError:
        raise RuntimeError("CSV response is not cp932") from None
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames != CSV_HEADER:
        raise RuntimeError("CSV response has an unexpected header")
    return list(reader)


def _select(rows: list[dict], start: date, end: date) -> list[dict]:
    unique = {}
    for row in rows:
        unique.setdefault(row["ID"], row)
    return [row for row in unique.values() if start <= _day(row["日付"]) <= end]


def fetch_rows(context, start: date, end: date) -> list[dict]:
    rows = []
    for year, month in _months(start, end):
        response = context.request.get(
            f"{CF_URL}/csv?from={year}/{month:02d}/01&month={month}&year={year}"
        )
        if is_login_url(response.url):
            raise LoginRequired("MF returned the sign-in page for the CSV request")
        rows += _parse_csv(
            response.status,
            response.headers.get("content-type", ""),
            response.body(),
        )
    return _select(rows, start, end)


def _period(text: str) -> tuple[date, date]:
    found = re.fullmatch(
        r"\s*(\d{4})/(\d{1,2})/(\d{1,2})\s*-\s*(\d{4})/(\d{1,2})/(\d{1,2})\s*", text
    )
    if found is None:
        raise RuntimeError("unexpected calendar period")
    values = [int(value) for value in found.groups()]
    return date(*values[:3]), date(*values[3:])


def _needs_previous(text: str, target: date) -> bool:
    first, last = _period(text)
    if target > last:
        raise RuntimeError("target date is newer than the displayed month")
    return target < first


def _show_month(page, target: date) -> None:
    heading = page.locator("#calendar h2")
    for _ in range(MAX_MONTH_MOVES):
        text = heading.inner_text()
        if not _needs_previous(text, target):
            return
        page.locator("#calendar").get_by_text("◄").click()
        page.wait_for_function(
            "text => document.querySelector('#calendar h2')?.innerText !== text",
            arg=text,
        )
    raise RuntimeError("target month was not found")


def _normalize(text: str) -> str:
    return text.strip().translate(_CP932_CHARS)


def _amount(text: str) -> int | None:
    found = re.search(r"-?\d[\d,]*", text)
    return int(found.group().replace(",", "")) if found else None


def _find_rid(screen_rows: list, row: dict) -> str:
    content = _normalize(row["内容"])
    amount = int(row["金額（円）"])
    rids = [
        found.group(1)
        for row_id, day, text, amount_text in screen_rows
        if (found := re.fullmatch(r"js-transaction-(\d+)", row_id))
        and day == row["日付"]
        and _normalize(text) == content
        and _amount(amount_text) == amount
    ]
    if len(rids) != 1:
        raise RuntimeError(f"ID {row['ID']}: expected 1 matching row, found {len(rids)}")
    return rids[0]


def _option_value(options: list, label: str) -> str:
    values = [value for text, value in options if text.strip() == label]
    if len(values) != 1:
        raise RuntimeError(f"expected 1 option labeled {label}, found {len(values)}")
    return values[0]


def _select_label(select, label: str) -> None:
    options = select.locator("option").evaluate_all(
        "options => options.map(option => [option.textContent, option.value])"
    )
    select.select_option(value=_option_value(options, label))


def _has_class(locator, name: str) -> bool:
    return name in (locator.get_attribute("class") or "").split()


def set_transfer(page, row: dict, counterpart: str) -> None:
    sub_account = SUB_ACCOUNTS.get(counterpart)
    if sub_account is None:
        raise ValueError(f"unknown counterpart: {counterpart}")
    target = _day(row["日付"])

    page.goto(CF_URL)
    if is_login_url(page.url):
        raise LoginRequired("MF returned the sign-in page for /cf")
    _show_month(page, target)
    rid = _find_rid(
        page.locator("tr.transaction_list").evaluate_all(_SCREEN_ROWS_JS), row
    )
    line = page.locator(f"tr.transaction_list#js-transaction-{rid}")
    icon = line.locator("td:nth-child(9) > .icon-exchange")
    if _has_class(icon, "onchange") or _has_class(line, "mf-grayout"):
        raise RuntimeError(f"row {rid} is not a normal entry")

    icon.click()
    try:
        page.get_by_role("link", name="実行する").click()
        line.locator("td:nth-child(9) > .icon-exchange.onchange").wait_for()
        page.locator(f"#change_act_type_{rid}").click()

        modal = page.locator("#modal_change_act_type")
        modal.wait_for()
        _select_label(modal.locator("#user_asset_act_partner_account_id_hash"), counterpart)
        sub_select = modal.locator("#user_asset_act_partner_sub_account_id_hash")
        sub_select.locator("option", has_text=sub_account).first.wait_for(state="attached")
        _select_label(sub_select, sub_account)
        modal.get_by_role("button", name="設定を保存").click()

        line.locator(".transfer_account_box", has_text=counterpart).wait_for(
            state="attached"
        )
        done = _has_class(icon, "onchange")
    except Exception as error:
        # Playwright の例外は DOM（明細の内容・金額）を含みうるので、型名だけを残す。
        detail = str(error) if type(error) is RuntimeError else type(error).__name__
        raise RuntimeError(f"row {rid}: transfer failed after 実行する: {detail}") from error
    if not done:
        raise RuntimeError(f"row {rid} did not become a transfer")
