"""Multi-source email pool with failover."""
from __future__ import annotations

import logging

from .base import EmailProvider, Inbox, ProviderError

log = logging.getLogger(__name__)


class MailPool:
    """Tries providers in order; fails over on ProviderError."""

    def __init__(self, providers: list[EmailProvider]):
        if not providers:
            raise ValueError("at least one provider required")
        self.providers = providers
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        for p in self.providers:
            try:
                await p.start()
            except Exception as e:  # noqa: BLE001
                log.warning("provider %s start failed: %s", getattr(p, "name", p), e)
        self._started = True

    async def create_inbox(self, prefix: str | None = None) -> tuple[EmailProvider, Inbox]:
        last_err: Exception | None = None
        for p in self.providers:
            try:
                inbox = await p.create_inbox(prefix)
                log.info("inbox created via %s: %s", p.name, inbox.address)
                return p, inbox
            except Exception as e:  # noqa: BLE001
                log.warning("provider %s create_inbox failed: %s", p.name, e)
                last_err = e
        raise ProviderError(f"all providers failed: {last_err}")

    async def wait_for_otp(
        self, provider: EmailProvider, inbox: Inbox, *, timeout: float = 180.0
    ) -> str:
        return await provider.wait_for_otp(inbox, timeout=timeout)

    async def close(self) -> None:
        for p in self.providers:
            try:
                await p.close()
            except Exception:  # noqa: BLE001
                pass
        self._started = False


def default_pool() -> MailPool:
    from .emailnator import EmailNatorProvider
    from .tempmailplus import TempMailPlusProvider
    from .mailtm import MailTmProvider
    from .tempmail import TempMailLolProvider

    # EmailNator is FIRST and the only provider that yields @gmail.com
    # addresses (via Gmail dot-trick). @gmail.com is NOT on Trae's domain
    # blocklist — verified 2026-07-18 (Trae Login returns FirstLogin=true).
    # All other providers' domains are blocked at Trae Login (error_code
    # 20116) but kept here as OTP-receiving fallbacks for diagnostics.
    providers: list[EmailProvider] = [
        EmailNatorProvider(),
        TempMailPlusProvider(),
        MailTmProvider(),
        TempMailLolProvider(),
    ]

    # DeepMails requires the optional 'browser' extra (Playwright). Only
    # include it if playwright is actually installed — otherwise skip
    # silently so the default emailnator flow keeps working.
    try:
        from .deepmails import HAS_PLAYWRIGHT, DeepMailsProvider
        if HAS_PLAYWRIGHT:
            providers.insert(2, DeepMailsProvider())
        else:
            log.info(
                "playwright not installed — skipping DeepMails provider. "
                "Install with `pip install -e .[browser]` to enable it."
            )
    except ImportError as e:  # noqa: BLE001
        log.debug("DeepMails provider not available: %s", e)

    return MailPool(providers)
