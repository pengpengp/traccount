"""Email providers for Trae account registration."""
from .base import EmailProvider, Inbox, ProviderError
from .deepmails import DeepMailsProvider
from .mailtm import MailTmProvider
from .tempmail import TempMailLolProvider
from .pool import MailPool

__all__ = [
    "EmailProvider",
    "Inbox",
    "ProviderError",
    "DeepMailsProvider",
    "MailTmProvider",
    "TempMailLolProvider",
    "MailPool",
]
