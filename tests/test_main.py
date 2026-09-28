from contextlib import nullcontext, redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta, timezone
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mf_me_dedup import __main__ as cli
from mf_me_dedup.rakuten import Result


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
def runtime(monkeypatch):
    page = SimpleNamespace(screenshot=Mock())
    context = object()
    login = Mock()
    fetch_rows = Mock()
    set_transfer = Mock()
    match = Mock(return_value=[])
    monkeypatch.setattr(cli, "_browser_session", lambda headed: nullcontext((page, context)))
    monkeypatch.setattr(cli.mf, "login", login)
    monkeypatch.setattr(cli.mf, "fetch_rows", fetch_rows)
    monkeypatch.setattr(cli.mf, "set_transfer", set_transfer)
    monkeypatch.setattr(cli.rakuten, "match", match)
    monkeypatch.setenv("MF_EMAIL", "user@example.test")
    monkeypatch.setenv("MF_PASSWORD", "password-secret")
    monkeypatch.setenv("MF_TOTP_SECRET", "totp-secret")
    return SimpleNamespace(
        page=page,
        context=context,
        login=login,
        fetch_rows=fetch_rows,
        set_transfer=set_transfer,
        match=match,
    )


def invoke(args):
    stdout, stderr = StringIO(), StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = cli.main(args)
    return code, stdout.getvalue(), stderr.getvalue()


def matched(row_data):
    return Result("MATCH", row_data, date(2026, 9, 10))


def test_missing_credentials_exits_before_login(runtime, monkeypatch):
    monkeypatch.delenv("MF_PASSWORD")

    code, _, _ = invoke([])

    assert code != 0
    runtime.login.assert_not_called()


def test_login_only_stops_after_login(runtime):
    code, _, _ = invoke(["--login-only"])

    assert code == 0
    runtime.login.assert_called_once()
    runtime.fetch_rows.assert_not_called()
    runtime.set_transfer.assert_not_called()


def test_change_limit_prevents_every_transfer(runtime):
    first, second = row("card-1"), row("card-2", description="ショップB 楽天市場店 ラクテンイチバ700002")
    runtime.fetch_rows.side_effect = [[first, second]]
    runtime.match.return_value = [matched(first), matched(second)]

    code, _, _ = invoke(["--max-changes", "1"])

    assert code != 0
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

    with pytest.raises(RuntimeError):
        invoke([])

    runtime.page.screenshot.assert_called_once_with(path="artifacts/error.png")


def test_transfer_delta_rejects_target_that_remains_untransferred(runtime):
    card = row("card-1")
    runtime.fetch_rows.side_effect = [[card], [row("card-1")]]
    runtime.match.return_value = [matched(card)]

    with pytest.raises(RuntimeError):
        invoke([])

    runtime.page.screenshot.assert_called_once_with(path="artifacts/error.png")


def test_new_rows_are_excluded_from_transfer_delta(runtime):
    card = row("card-1")
    runtime.fetch_rows.side_effect = [
        [card],
        [row("card-1", transfer="1"), row("new-id", transfer="1")],
    ]
    runtime.match.return_value = [matched(card)]

    code, _, _ = invoke([])

    assert code == 0


@pytest.mark.parametrize("secret", ["password-secret", "totp-secret"])
def test_exception_text_does_not_expose_credentials(runtime, secret):
    runtime.login.side_effect = RuntimeError(secret)
    output = StringIO()

    with redirect_stdout(output), pytest.raises(RuntimeError) as error:
        cli.main([])

    assert secret not in str(error.value)
    assert secret not in output.getvalue()
    runtime.page.screenshot.assert_called_once_with(path="artifacts/error.png")


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


def test_job_summary_receives_result_table(runtime, monkeypatch, tmp_path):
    card = row("card-1")
    runtime.fetch_rows.return_value = [card]
    runtime.match.return_value = [matched(card)]
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    code, output, _ = invoke(["--dry-run"])

    assert code == 0
    assert "MATCH=1" in output
    content = summary.read_text(encoding="utf-8")
    assert "| 日付 | 内容 | 金額（円） | 判定 | 市場明細日 |" in content
    assert "password-secret" not in output + content
    assert "totp-secret" not in output + content
