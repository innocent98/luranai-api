"""Privacy masking for emails shown to marketers."""

_FULLY_MASKED = "***"


def mask_email(email: str) -> str:
    """Mask an email as ``a***@d***.com``.

    Keeps the first character of the local part and of the first domain label, each followed
    by ``***``, and everything after the first dot of the domain. Total: anything that is not
    a single well-formed ``local@domain`` returns ``***`` rather than raising or leaking.
    """
    if not isinstance(email, str):
        return _FULLY_MASKED
    value = email.strip()
    if value.count("@") != 1:
        return _FULLY_MASKED
    local, domain = value.split("@")
    label, dot, rest = domain.partition(".")
    if not local or not label:
        return _FULLY_MASKED
    return f"{local[0]}***@{label[0]}***{dot}{rest}"
