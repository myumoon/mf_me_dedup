from datetime import date


def login(page, email, password, totp_secret) -> None:
    raise NotImplementedError


def fetch_rows(context, start: date, end: date) -> list[dict]:
    raise NotImplementedError


def set_transfer(page, row: dict, counterpart: str) -> None:
    raise NotImplementedError
