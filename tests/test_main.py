import os
import subprocess
import sys
import time
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mf_me_dedup import __main__ as cli
from mf_me_dedup import mf
from mf_me_dedup.rakuten import Result

REAL_NOTIFY = cli._notify
SIGN_IN_URL = "https://id.moneyforward.com/sign_in"
REFERENCES = {
    "MF_EMAIL": "op://vault/item/username",
    "MF_PASSWORD": "op://vault/item/password",
    "MF_TOTP_SECRET": "op://vault/item/totp",
}
SECRETS = {
    "op://vault/item/username": "user@example.test",
    "op://vault/item/password": "password-secret",
    "op://vault/item/totp": "totp-secret",
}


def row(row_id, *, transfer="0", description="ショップA 楽天市場店 ラクテンイチバ700001"):
    return {
        "計算対象": "1",
        "日付": "2026/09/15",
        "内容": description,
        "金額（円）": "-3000",
        "保有金融機関": "楽天カード",
        "大項目": "食費",
        "中項目": "食料品",
        "メモ": "",
        "振替": transfer,
        "ID": row_id,
    }


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    page = SimpleNamespace(closed=False, screenshots_before_close=[], url=mf.CF_URL)

    def screenshot(*, path):
        if page.closed:
            raise RuntimeError("page is closed")
        page.screenshots_before_close.append(path)

    page.screenshot = Mock(side_effect=screenshot)
    page.goto = Mock()
    context = object()
    events = []
    sessions = []
    login = Mock(side_effect=lambda *args: events.append("login"))
    fetch_rows = Mock()
    set_transfer = Mock()
    match = Mock(return_value=[])
    notify = Mock(side_effect=lambda text: events.append(("notify", text)))

    def op_read(reference):
        events.append("op_read")
        return SECRETS[reference]

    @contextmanager
    def browser_session(minimized):
        sessions.append(minimized)
        try:
            yield page, context
        finally:
            page.closed = True

    monkeypatch.setattr(cli, "_browser_session", browser_session)
    monkeypatch.setattr(cli, "_op_read", Mock(side_effect=op_read))
    monkeypatch.setattr(cli, "_notify", notify)
    monkeypatch.setattr(cli.mf, "login", login)
    monkeypatch.setattr(cli.mf, "fetch_rows", fetch_rows)
    monkeypatch.setattr(cli.mf, "set_transfer", set_transfer)
    monkeypatch.setattr(cli.rakuten, "match", match)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    for name, reference in REFERENCES.items():
        monkeypatch.setenv(name, reference)
    app_dir = tmp_path / "mf_me_dedup"
    return SimpleNamespace(
        page=page,
        context=context,
        events=events,
        sessions=sessions,
        login=login,
        fetch_rows=fetch_rows,
        set_transfer=set_transfer,
        match=match,
        notify=notify,
        op_read=cli._op_read,
        report=app_dir / "last-run.md",
        error_png=str(app_dir / "error.png"),
    )


def invoke(args):
    stdout, stderr = StringIO(), StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = cli.main(args)
    return code, stdout.getvalue(), stderr.getvalue()


def matched(row_data):
    return Result("MATCH", row_data, date(2026, 9, 10))


# --- 引数と参照 ---------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "plain-password", "https://example.test/x"])
def test_invalid_reference_exits_1_before_browser(runtime, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("MF_PASSWORD")
    else:
        monkeypatch.setenv("MF_PASSWORD", value)

    code, _, stderr = invoke([])

    assert code == 1
    assert "MF_PASSWORD" in stderr
    if value:
        assert value not in stderr + runtime.report.read_text(encoding="utf-8")
    assert runtime.sessions == []
    runtime.op_read.assert_not_called()
    runtime.notify.assert_called_once_with(cli.EXIT_TEXTS[1])


@pytest.mark.parametrize("flag", ["--login-only", "--headed"])
def test_removed_flags_are_rejected(flag):
    with redirect_stderr(StringIO()), pytest.raises(SystemExit):
        cli.main([flag])


# --- ログイン状態の確認とログイン -------------------------------------------------


def test_login_flag_logs_in_and_stops(runtime):
    code, _, _ = invoke(["--login"])

    assert code == 0
    assert runtime.sessions == [False]
    runtime.login.assert_called_once_with(
        runtime.page, "user@example.test", "password-secret", "totp-secret"
    )
    runtime.page.goto.assert_not_called()
    runtime.fetch_rows.assert_not_called()
    runtime.notify.assert_not_called()


def test_logged_in_run_checks_cf_first_and_skips_login(runtime):
    runtime.fetch_rows.return_value = []

    code, _, _ = invoke(["--dry-run"])

    assert code == 0
    assert runtime.sessions == [True]
    runtime.page.goto.assert_called_once_with(mf.CF_URL)
    runtime.op_read.assert_not_called()
    runtime.login.assert_not_called()
    runtime.notify.assert_not_called()


def test_sign_in_page_notifies_then_logs_in_and_continues(runtime):
    runtime.page.url = SIGN_IN_URL
    card = row("card-1")
    runtime.fetch_rows.side_effect = [[card], [row("card-1", transfer="1")]]
    runtime.match.return_value = [matched(card)]

    code, _, _ = invoke([])

    assert code == 0
    assert runtime.events == [("notify", cli.LOGIN_NEEDED_TEXT), "op_read", "op_read", "op_read", "login"]
    runtime.set_transfer.assert_called_once()


def test_dismissed_authorization_exits_2_without_changes(runtime):
    runtime.page.url = SIGN_IN_URL
    runtime.op_read.side_effect = cli.LoginCancelled("dismissed")

    code, _, _ = invoke([])

    assert code == 2
    runtime.login.assert_not_called()
    runtime.fetch_rows.assert_not_called()
    runtime.set_transfer.assert_not_called()
    assert runtime.report.read_text(encoding="utf-8").splitlines()[0] == "LOGIN_REQUIRED"
    assert runtime.notify.call_args_list[-1].args == (cli.EXIT_TEXTS[2],)


def test_sign_in_during_processing_exits_2_without_relogin(runtime):
    runtime.fetch_rows.side_effect = mf.LoginRequired("sign-in page")

    code, _, _ = invoke([])

    assert code == 2
    runtime.op_read.assert_not_called()
    runtime.login.assert_not_called()
    runtime.set_transfer.assert_not_called()
    runtime.notify.assert_called_once_with(cli.EXIT_TEXTS[2])


def test_sign_in_during_transfer_exits_2(runtime):
    card = row("card-1")
    runtime.fetch_rows.return_value = [card]
    runtime.match.return_value = [matched(card)]
    runtime.set_transfer.side_effect = mf.LoginRequired("sign-in page")

    code, _, _ = invoke([])

    assert code == 2
    runtime.login.assert_not_called()


# --- 既存の流れ ---------------------------------------------------------------


def test_change_limit_prevents_every_transfer(runtime):
    first, second = row("card-1"), row("card-2", description="ショップB 楽天市場店 ラクテンイチバ700002")
    runtime.fetch_rows.side_effect = [[first, second]]
    runtime.match.return_value = [matched(first), matched(second)]

    code, _, _ = invoke(["--max-changes", "1"])

    assert code == 1
    runtime.set_transfer.assert_not_called()


def test_dry_run_skips_limit_and_transfer(runtime):
    first, second = row("card-1"), row("card-2", description="ショップB 楽天市場店 ラクテンイチバ700002")
    runtime.fetch_rows.side_effect = [[first, second]]
    runtime.match.return_value = [matched(first), matched(second)]

    code, output, _ = invoke(["--dry-run", "--max-changes", "1"])

    assert code == 0
    assert "MATCH=2" in output
    runtime.set_transfer.assert_not_called()


def test_matching_transfer_id_delta_succeeds(runtime):
    card = row("card-1")
    runtime.fetch_rows.side_effect = [[card], [row("card-1", transfer="1")]]
    runtime.match.return_value = [matched(card)]

    code, _, _ = invoke([])

    assert code == 0
    runtime.set_transfer.assert_called_once_with(
        runtime.page, card, "楽天市場(my Rakuten)"
    )


def test_transfer_delta_rejects_another_existing_id(runtime):
    card, other = row("card-1"), row("card-2", description="ショップB 楽天市場店 ラクテンイチバ700002")
    runtime.fetch_rows.side_effect = [
        [card, other],
        [row("card-1", transfer="1"), row("card-2", transfer="1")],
    ]
    runtime.match.return_value = [matched(card)]

    code, _, _ = invoke([])

    assert code == 1
    runtime.page.screenshot.assert_called_once_with(path=runtime.error_png)


def test_transfer_delta_rejects_target_that_remains_untransferred(runtime):
    card = row("card-1")
    runtime.fetch_rows.side_effect = [[card], [row("card-1")]]
    runtime.match.return_value = [matched(card)]

    code, _, _ = invoke([])

    assert code == 1
    runtime.page.screenshot.assert_called_once_with(path=runtime.error_png)


def test_new_rows_are_excluded_from_transfer_delta(runtime):
    card = row("card-1")
    runtime.fetch_rows.side_effect = [
        [card],
        [row("card-1", transfer="1"), row("new-id", transfer="1")],
    ]
    runtime.match.return_value = [matched(card)]

    code, _, _ = invoke([])

    assert code == 0


def test_default_since_and_fetch_range_use_jst_today(runtime, monkeypatch):
    today = date(2026, 9, 29)
    monkeypatch.setattr(cli, "_jst_today", lambda: today)
    runtime.fetch_rows.return_value = []

    code, _, _ = invoke(["--dry-run"])

    assert code == 0
    runtime.fetch_rows.assert_called_once_with(
        runtime.context, today - timedelta(days=120), today
    )
    runtime.match.assert_called_once_with([], today - timedelta(days=30))


def test_today_is_read_in_fixed_jst(runtime, monkeypatch):
    now = Mock(return_value=datetime(2026, 9, 29, 0, 30))
    monkeypatch.setattr(cli, "datetime", SimpleNamespace(now=now))

    cli._jst_today()

    now.assert_called_once_with(timezone(timedelta(hours=9)))


# --- 秘密情報・結果・通知 --------------------------------------------------------


@pytest.mark.parametrize("secret", ["user@example.test", "password-secret", "totp-secret"])
def test_errors_do_not_expose_secrets_read_from_op(runtime, secret):
    runtime.page.url = SIGN_IN_URL
    runtime.login.side_effect = RuntimeError(f"login failed near {secret}")

    code, stdout, stderr = invoke([])

    assert code == 1
    report = runtime.report.read_text(encoding="utf-8")
    assert "login failed near [REDACTED]" in stderr
    for text in (stdout, stderr, report, repr(runtime.notify.call_args_list)):
        assert secret not in text
    assert runtime.page.closed
    assert runtime.page.screenshots_before_close == [runtime.error_png]


def test_last_run_report_and_stdout(runtime):
    card = row("card-1")
    other = row("card-2", description="ショップB 楽天市場店 ラクテンイチバ700002")
    runtime.fetch_rows.return_value = [card, other]
    runtime.match.return_value = [
        matched(card),
        Result("AMOUNT_MISMATCH", other, date(2026, 9, 11)),
    ]

    code, stdout, _ = invoke(["--dry-run"])

    assert code == 1
    assert stdout == "MATCH=1 AMOUNT_MISMATCH=1 changed=0\n"
    lines = runtime.report.read_text(encoding="utf-8").splitlines()
    assert lines[:2] == ["AMOUNT_MISMATCH", "MATCH=1 AMOUNT_MISMATCH=1 changed=0"]
    assert "| 日付 | 内容 | 金額（円） | 判定 | 市場明細日 |" in lines
    assert "| 2026/09/15 | ショップB 楽天市場店 ラクテンイチバ700002 | -3000 | AMOUNT_MISMATCH | 2026-09-11 |" in lines
    runtime.notify.assert_called_once_with(cli.EXIT_TEXTS[1])


def test_report_is_overwritten_with_success(runtime):
    runtime.report.parent.mkdir(parents=True)
    runtime.report.write_text("old\n", encoding="utf-8")
    runtime.fetch_rows.return_value = []

    code, _, _ = invoke(["--dry-run"])

    assert code == 0
    assert runtime.report.read_text(encoding="utf-8").splitlines()[:2] == [
        "SUCCESS",
        "MATCH=0 AMOUNT_MISMATCH=0 changed=0",
    ]


def test_exit_texts_are_fixed_and_distinct():
    texts = [cli.LOGIN_NEEDED_TEXT, cli.ALREADY_RUNNING_TEXT, *cli.EXIT_TEXTS.values()]
    assert set(cli.EXIT_TEXTS) == {1, 2}
    assert len(set(texts)) == len(texts)
    assert all("'" not in text for text in texts)


def test_notify_runs_powershell_with_fixed_text(monkeypatch):
    run = Mock()
    monkeypatch.setattr(cli.subprocess, "run", run)

    cli._notify(cli.EXIT_TEXTS[2])

    command = run.call_args.args[0]
    assert command[:2] == ["powershell", "-NoProfile"]
    assert f"CreateTextNode('{cli.EXIT_TEXTS[2]}')" in command[-1]


def test_notification_failure_keeps_exit_code(runtime, monkeypatch):
    monkeypatch.setattr(cli, "_notify", REAL_NOTIFY)
    monkeypatch.setattr(cli.subprocess, "run", Mock(side_effect=OSError("no powershell")))
    runtime.fetch_rows.side_effect = mf.LoginRequired("sign-in page")

    code, _, _ = invoke([])

    assert code == 2


# --- op read ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stderr", "kind"),
    [
        ('[ERROR] 2026/09/30 12:00:00 authorization timeout\n', "retry"),
        ('[ERROR] 2026/09/30 12:00:00 authorization prompt dismissed, please try again\n', "cancelled"),
        ('[ERROR] 2026/09/30 12:00:00 "vault" isn\'t a vault in this account\n', "error"),
        ("", "error"),
    ],
)
def test_op_error_kind(stderr, kind):
    assert cli._op_error_kind(stderr) == kind


def fake_op(monkeypatch, *outcomes):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        returncode, stdout, stderr = outcomes[len(calls) - 1]
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    monkeypatch.setattr(cli.subprocess, "run", run)
    return calls


def test_op_read_retries_on_timeout_and_strips_newline(monkeypatch):
    calls = fake_op(
        monkeypatch,
        (1, "", "[ERROR] authorization timeout\n"),
        (1, "", "[ERROR] authorization timeout\n"),
        (0, "JBSW Y3DP\n", ""),
    )

    assert cli._op_read("op://vault/item/totp") == "JBSW Y3DP"
    assert [command for command, _ in calls] == [["op", "read", "op://vault/item/totp"]] * 3
    assert calls[0][1]["encoding"] == "utf-8"


def test_op_read_dismissed_raises_login_cancelled(monkeypatch):
    fake_op(monkeypatch, (1, "", "[ERROR] authorization prompt dismissed\n"))

    with pytest.raises(cli.LoginCancelled):
        cli._op_read("op://vault/item/password")


def test_op_read_other_failure_is_an_error(monkeypatch):
    fake_op(monkeypatch, (1, "", "[ERROR] item not found\n"))

    with pytest.raises(RuntimeError) as error:
        cli._op_read("op://vault/item/password")

    assert not isinstance(error.value, cli.LoginCancelled)
    assert "item not found" in str(error.value)


def test_op_read_missing_cli_is_an_error(monkeypatch):
    monkeypatch.setattr(cli.subprocess, "run", Mock(side_effect=FileNotFoundError()))

    with pytest.raises(RuntimeError, match="op"):
        cli._op_read("op://vault/item/password")


# --- ロックとブラウザ ------------------------------------------------------------


def test_lock_rejects_a_second_holder(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    with cli._run_lock():
        with pytest.raises(cli.AlreadyRunning):
            with cli._run_lock():
                pass
    with cli._run_lock():
        pass


def test_running_instance_blocks_main_before_browser(runtime):
    with cli._run_lock():
        code, _, stderr = invoke([])

    assert code == 1
    assert "Another mf_me_dedup run" in stderr
    assert runtime.sessions == []
    runtime.notify.assert_called_once_with(cli.ALREADY_RUNNING_TEXT)


def test_leftover_lock_file_does_not_block(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    (tmp_path / "mf_me_dedup").mkdir()
    (tmp_path / "mf_me_dedup" / "run.lock").write_text("stale", encoding="utf-8")

    with cli._run_lock():
        pass


def test_killed_holder_does_not_block_next_run(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time\nfrom mf_me_dedup import __main__ as cli\n"
            "with cli._run_lock():\n    print('locked', flush=True)\n    time.sleep(60)\n",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "LOCALAPPDATA": str(tmp_path)},
        stdout=subprocess.PIPE,
    )
    try:
        assert holder.stdout.readline().strip() == b"locked"
        with pytest.raises(cli.AlreadyRunning):
            with cli._run_lock():
                pass
    finally:
        holder.kill()
        holder.wait()
        holder.stdout.close()

    # OS がロックを解放するまで少しかかることがある（LockFile の仕様）。
    deadline = time.monotonic() + 10
    while True:
        try:
            with cli._run_lock():
                break
        except cli.AlreadyRunning:
            assert time.monotonic() < deadline
            time.sleep(0.05)


@pytest.mark.parametrize(("minimized", "args"), [(True, ["--start-minimized"]), (False, [])])
def test_browser_session_uses_chrome_with_dedicated_profile(monkeypatch, tmp_path, minimized, args):
    import playwright.sync_api

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    context = Mock(pages=["first-page"])
    chromium = Mock()
    chromium.launch_persistent_context.return_value = context

    @contextmanager
    def sync_playwright():
        yield SimpleNamespace(chromium=chromium)

    monkeypatch.setattr(playwright.sync_api, "sync_playwright", sync_playwright)

    with cli._browser_session(minimized) as (page, yielded):
        assert (page, yielded) == ("first-page", context)

    chromium.launch_persistent_context.assert_called_once_with(
        str(tmp_path / "mf_me_dedup" / "profile"),
        channel="chrome",
        headless=False,
        args=args,
    )
    context.close.assert_called_once()
