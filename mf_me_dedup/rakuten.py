import re
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Result:
    kind: str
    card: dict
    market_date: date | None


def _day(row: dict) -> date:
    return date.fromisoformat(row["日付"].replace("/", "-"))


def _key(row: dict) -> str:
    return re.sub(r"\s*ラクテンイチバ\d+$", "", row["内容"]).strip()


def _reserve_group(
    card: dict,
    key: str,
    market_rows: list[tuple[date, str, int]],
    used: set[tuple[str, date]],
    lookback_days: int,
) -> tuple[date | None, bool]:
    card_day = _day(card)
    amount = int(card["金額（円）"])
    groups: dict[date, int] = {}
    for market_day, description, value in market_rows:
        if description.startswith(key):
            groups[market_day] = groups.get(market_day, 0) + value

    amount_matches = [
        market_day
        for market_day, group_amount in groups.items()
        if 0 <= (card_day - market_day).days <= lookback_days
        and group_amount == amount
    ]
    available = [
        market_day
        for market_day in amount_matches
        if (key, market_day) not in used
    ]
    if available:
        match_day = min(available, key=lambda day: (card_day - day).days)
        used.add((key, match_day))
        return match_day, False
    return None, bool(groups) and not amount_matches


def match(rows: list[dict], since: date, lookback_days: int = 90) -> list[Result]:
    cards = [row for row in rows if row["保有金融機関"] == "楽天カード"]
    markets = [
        (_day(row), row["内容"], int(row["金額（円）"]))
        for row in rows
        if row["保有金融機関"] == "楽天市場(my Rakuten)"
        and row["計算対象"] == "1"
        and row["振替"] == "0"
    ]
    used: set[tuple[str, date]] = set()

    processed = [
        row
        for row in cards
        if int(row["金額（円）"]) < 0
        and (row["振替"] == "1" or row["計算対象"] == "0")
    ]
    for row in sorted(processed, key=_day):
        key = _key(row)
        if key:
            _reserve_group(row, key, markets, used, lookback_days)

    pending = [
        row
        for row in cards
        if row["計算対象"] == "1"
        and row["振替"] == "0"
        and int(row["金額（円）"]) < 0
        and _key(row)
    ]
    results = []
    for row in sorted(pending, key=_day):
        key = _key(row)
        market_day, mismatch = _reserve_group(row, key, markets, used, lookback_days)
        if _day(row) < since:
            continue
        if market_day is not None:
            results.append(Result("MATCH", row, market_day))
        elif mismatch:
            results.append(Result("AMOUNT_MISMATCH", row, None))
    return results
