import argparse
import os
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from mf_me_dedup import mf, rakuten


JST = timezone(timedelta(hours=9))


def _jst_today() -> date:
    return datetime.now(JST).date()


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from error


@contextmanager
def _browser_session(headed: bool):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not headed)
        try:
            context = browser.new_context()
            yield context.new_page(), context
        finally:
            browser.close()


def _write_table(results: list[rakuten.Result]) -> None:
    def cell(value) -> str:
        return str(value).replace("|", r"\|").replace("\r", " ").replace("\n", " ")

    lines = [
        "| 日付 | 内容 | 金額（円） | 判定 | 市場明細日 |",
        "| --- | --- | ---: | --- | --- |",
    ]
    for result in results:
        card = result.card
        lines.append(
            "| "
            + " | ".join(
                cell(value)
                for value in (
                    card.get("日付", ""),
                    card.get("内容", ""),
                    card.get("金額（円）", ""),
                    result.kind,
                    result.market_date or "",
                )
            )
            + " |"
        )
    table = "\n".join(lines) + "\n"
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as summary:
            summary.write(table)
    else:
        print(table, end="")


def _print_counts(results: list[rakuten.Result], changed: int) -> None:
    matches = sum(result.kind == "MATCH" for result in results)
    mismatches = sum(result.kind == "AMOUNT_MISMATCH" for result in results)
    print(f"MATCH={matches} AMOUNT_MISMATCH={mismatches} changed={changed}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mf_me_dedup")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--since", type=_parse_date)
    parser.add_argument("--max-changes", type=int, default=10)
    parser.add_argument("--login-only", action="store_true")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args(argv)

    credentials = {
        name: os.environ.get(name, "")
        for name in ("MF_EMAIL", "MF_PASSWORD", "MF_TOTP_SECRET")
    }
    missing = [name for name, value in credentials.items() if not value]
    if missing:
        print(f"Missing environment variables: {', '.join(missing)}", file=sys.stderr)
        return 1

    today = _jst_today()
    since = args.since or today - timedelta(days=30)
    start = since - timedelta(days=90)
    page = None
    results = []
    changed = 0
    try:
        with _browser_session(args.headed) as (page, context):
            mf.login(page, credentials["MF_EMAIL"], credentials["MF_PASSWORD"], credentials["MF_TOTP_SECRET"])
            if args.login_only:
                return 0

            rows = mf.fetch_rows(context, start, today)
            results = rakuten.match(rows, since)
            _write_table(results)
            if args.dry_run:
                _print_counts(results, changed)
                return int(any(result.kind == "AMOUNT_MISMATCH" for result in results))

            matches = [result for result in results if result.kind == "MATCH"]
            if len(matches) > args.max_changes:
                _print_counts(results, changed)
                print("MATCH count exceeds --max-changes", file=sys.stderr)
                return 1

            before_ids = {row["ID"] for row in rows}
            before_transfer = {row["ID"] for row in rows if row["振替"] == "1"}
            for result in matches:
                mf.set_transfer(page, result.card, "楽天市場(my Rakuten)")
                changed += 1

            if matches:
                after = mf.fetch_rows(context, start, today)
                after_ids = {row["ID"] for row in after}
                common_ids = before_ids & after_ids
                after_transfer = {row["ID"] for row in after if row["振替"] == "1"}
                delta = (before_transfer & common_ids) ^ (after_transfer & common_ids)
                target_ids = {result.card["ID"] for result in matches}
                if delta != target_ids:
                    raise RuntimeError("Transfer ID verification failed")

            _print_counts(results, changed)
            return int(any(result.kind == "AMOUNT_MISMATCH" for result in results))
    except Exception as error:
        if page is not None:
            try:
                Path("artifacts").mkdir(parents=True, exist_ok=True)
                page.screenshot(path="artifacts/error.png")
            except Exception:
                pass
        message = str(error)
        for secret in credentials.values():
            if secret:
                message = message.replace(secret, "[REDACTED]")
        raise RuntimeError(message or "MF processing failed") from None


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
