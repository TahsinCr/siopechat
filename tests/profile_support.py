"""Synthetic cookies used by profile boundary tests."""

from http.cookiejar import Cookie


def cookie(name, value="secret", domain=".google.com", expires=None):
    return Cookie(
        0,
        name,
        value,
        None,
        False,
        domain,
        True,
        domain.startswith("."),
        "/",
        True,
        True,
        expires,
        False,
        None,
        None,
        {},
        False,
    )
