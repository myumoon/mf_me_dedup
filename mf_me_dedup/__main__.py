import argparse
import msvcrt
import os
import subprocess
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from mf_me_dedup import mf, rakuten


JST = timezone(timedelta(hours=9))
CREDENTIAL_NAMES = ("MF_EMAIL", "MF_PASSWORD", "MF_TOTP_SECRET")
COUNTERPART = "楽天市場(my Rakuten)"

# 通知の文言は固定。明細の内容・秘密情報を入れない。PowerShell の単一引用符で囲むので ' を含めない。
LOGIN_NEEDED_TEXT = "MoneyForward ME へのログインが必要です。1Password の承認画面で承認してください。"
EXIT_TEXTS = {
    1: "エラーまたは金額の不一致がありました。last-run.md を確認してください。",
    2: "MoneyForward ME にログインできませんでした。--login でログインしてください。",
}
# PowerShell の AppUserModelID（登録なしでトーストを出せる）。
_TOAST_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
_TOAST_SCRIPT = (
    "$ErrorActionPreference = 'Stop'; "
    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null; "
    "$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
    "$text = $xml.GetElementsByTagName('text'); "
    "$text.Item(0).AppendChild($xml.CreateTextNode('mf_me_dedup')) > $null; "
    "$text.Item(1).AppendChild($xml.CreateTextNode('%s')) > $null; "
    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('%s')"
    ".Show([Windows.UI.Notifications.ToastNotification]::new($xml))"
)


class AlreadyRunning(Exception):
    pass


class LoginCancelled(Exception):
    pass


def _jst_today() -> date:
    return datetime.now(JST).date()


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from error


def _app_dir() -> Path:
    return Path(os.environ["LOCALAPPDATA"]) / "mf_me_dedup"


@contextmanager
def _run_lock():
    """Hold an OS byte-range lock; the OS drops it when the process exits."""
    path = _app_dir() / "run.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT)
    try:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            raise AlreadyRunning from None
        try:
            yield
        finally:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(fd)


@contextmanager
def _browser_session(minimized: bool):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(_app_dir() / "profile"),
            channel="chrome",
            headless=False,
            args=["--start-minimized"] if minimized else [],
        )
        try:
            yield (context.pages[0] if context.pages else context.new_page()), context
        finally:
            context.close()


def _notify(text: str) -> None:
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _TOAST_SCRIPT % (text, _TOAST_APP_ID)],
            capture_output=True,
            timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception:
        pass


def _op_error_kind(stderr: str) -> str:
    if "authorization timeout" in stderr:
        return "retry"
    if "authorization prompt dismissed" in stderr:
        return "cancelled"
    return "error"


def _op_read(reference: str) -> str:
    while True:
        try:
            completed = subprocess.run(
                ["op", "read", reference],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
        except FileNotFoundError:
            raise RuntimeError("1Password CLI (op) was not found on PATH") from None
        if completed.returncode == 0:
            return completed.stdout.removesuffix("\n")
        kind = _op_error_kind(completed.stderr)
        if kind == "cancelled":
            raise LoginCancelled("1Password authorization was dismissed")
        if kind == "error":
            raise RuntimeError(f"op read failed: {completed.stderr.strip()}")
        # authorization timeout: 承認されるかキャンセルされるまで待ち続ける。


def _references() -> dict[str, str]:
    references = {name: os.environ.get(name, "") for name in CREDENTIAL_NAMES}
    invalid = [name for name, value in references.items() if not value.startswith("op://")]
    if invalid:
        # 値そのものは出さない（誤って平文の秘密情報が入っている場合があるため）。
        raise RuntimeError(f"Set {', '.join(invalid)} to op:// references")
    return references


def _login(page, references: dict[str, str], secrets: list[str]) -> None:
    values = []
    for name in CREDENTIAL_NAMES:
        values.append(_op_read(references[name]))
        secrets.append(values[-1])
    mf.login(page, *values)


def _redact(message: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret:
            message = message.replace(secret, "[REDACTED]")
    return message


def _table(results: list[rakuten.Result]) -> str:
    def cell(value) -> str:
        return str(value).replace("|", r"\|").replace("\r", " ").replace("\n", " ")

    lines = [
        "| 日付 | 内容 | 金額（円） | 判定 | 市場明細日 |",
        "| --- | --- | ---: | --- | --- |",
    ]
    for result in results:
        card = result.card
        values = (
            card.get("日付", ""),
            card.get("内容", ""),
            card.get("金額（円）", ""),
            result.kind,
            result.market_date or "",
        )
        lines.append("| " + " | ".join(cell(value) for value in values) + " |")
    return "\n".join(lines) + "\n"


def _counts(results: list[rakuten.Result], changed: int) -> str:
    matches = sum(result.kind == "MATCH" for result in results)
    mismatches = sum(result.kind == "AMOUNT_MISMATCH" for result in results)
    return f"MATCH={matches} AMOUNT_MISMATCH={mismatches} changed={changed}"


def _write_report(kind: str, state, error: str) -> None:
    lines = [kind, _counts(state.results, state.changed)]
    if error:
        lines.append(error)
    content = "\n".join(lines) + "\n\n" + _table(state.results)
    (_app_dir() / "last-run.md").write_text(content, encoding="utf-8")


def _outcome(results: list[rakuten.Result]) -> tuple[int, str]:
    if any(result.kind == "AMOUNT_MISMATCH" for result in results):
        return 1, "AMOUNT_MISMATCH"
    return 0, "SUCCESS"


def _process(args, state, secrets: list[str]) -> tuple[int, str]:
    references = _references()
    today = _jst_today()
    since = args.since or today - timedelta(days=30)
    start = since - timedelta(days=90)
    with _browser_session(minimized=not args.login) as (page, context):
        try:
            if args.login:
                _login(page, references, secrets)
                return 0, "SUCCESS"

            page.goto(mf.CF_URL)
            if mf.is_login_url(page.url):
                _notify(LOGIN_NEEDED_TEXT)
                _login(page, references, secrets)

            rows = mf.fetch_rows(context, start, today)
            state.results = rakuten.match(rows, since)
            if args.dry_run:
                print(_counts(state.results, state.changed))
                return _outcome(state.results)

            matches = [result for result in state.results if result.kind == "MATCH"]
            if len(matches) > args.max_changes:
                print(_counts(state.results, state.changed))
                raise RuntimeError("MATCH count exceeds --max-changes")

            before_ids = {row["ID"] for row in rows}
            before_transfer = {row["ID"] for row in rows if row["振替"] == "1"}
            for result in matches:
                mf.set_transfer(page, result.card, COUNTERPART)
                state.changed += 1

            if matches:
                after = mf.fetch_rows(context, start, today)
                after_ids = {row["ID"] for row in after}
                common_ids = before_ids & after_ids
                after_transfer = {row["ID"] for row in after if row["振替"] == "1"}
                delta = (before_transfer & common_ids) ^ (after_transfer & common_ids)
                target_ids = {result.card["ID"] for result in matches}
                if delta != target_ids:
                    raise RuntimeError("Transfer ID verification failed")

            print(_counts(state.results, state.changed))
            return _outcome(state.results)
        except Exception:
            try:
                page.screenshot(path=str(_app_dir() / "error.png"))
            except Exception:
                pass
            raise


def _run(args) -> int:
    state = SimpleNamespace(results=[], changed=0)
    secrets: list[str] = []
    error = ""
    try:
        code, kind = _process(args, state, secrets)
    except (LoginCancelled, mf.LoginRequired) as caught:
        code, kind, error = 2, "LOGIN_REQUIRED", str(caught)
    except Exception as caught:
        code, kind, error = 1, "ERROR", str(caught) or type(caught).__name__
    error = _redact(error, secrets)
    if error:
        print(error, file=sys.stderr)
    _write_report(kind, state, error)
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mf_me_dedup")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--since", type=_parse_date)
    parser.add_argument("--max-changes", type=int, default=10)
    parser.add_argument("--login", action="store_true")
    args = parser.parse_args(argv)

    try:
        with _run_lock():
            code = _run(args)
    except AlreadyRunning:
        print("Another mf_me_dedup run is using the profile", file=sys.stderr)
        code = 1
    if code:
        _notify(EXIT_TEXTS[code])
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
