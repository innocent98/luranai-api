"""A read-only view of the sales config with some fields forced, for prompt building."""

from typing import Any


class SalesConfigView:
    """Delegates every attribute to the stored config, except the overridden ones.

    Used when the plan does not include meeting booking: the AI prompt must read the option as
    off, while the stored setting is left exactly as the tenant saved it.
    """

    def __init__(self, config: Any, **overrides: Any) -> None:
        self._config = config
        self._overrides = overrides

    def __getattr__(self, name: str) -> Any:
        if name in self._overrides:
            return self._overrides[name]
        return getattr(self._config, name)
