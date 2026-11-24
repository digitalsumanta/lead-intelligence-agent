"""Provider-contract, factory, and SMTP transport tests for Phase 11A."""

from email import policy
from email.parser import BytesParser
from types import SimpleNamespace
import pytest

from app.providers.email import (
    EmailProviderResult,
    EmailSendRequest,
    SMTPEmailProvider,
    resolve_email_provider,
)
from app.database import SCHEMA_REVISION
from app.models import Base
from pathlib import Path
import importlib.util


def smtp_config(**overrides):
    values = {
        "outreach_email_enabled": True,
        "email_provider": "smtp",
        "smtp_host": "smtp.example.test",
        "smtp_port": 587,
        "smtp_username": "mailer-user",
        "smtp_password": "DO_NOT_LEAK_THIS_SECRET",
        "smtp_from_email": "sales@example.test",
        "smtp_use_tls": True,
        "smtp_use_ssl": False,
        "smtp_timeout_seconds": 5,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeSMTP:
    instances = []

    def __init__(self, *args, **kwargs):
        self.args, self.kwargs, self.credentials, self.raw_message = args, kwargs, None, None
        self.__class__.instances.append(self)

    def ehlo(self):
        return 250, b"ok"

    def starttls(self, **kwargs):
        return 220, b"ready"

    def login(self, username, password):
        self.credentials = username, password

    def send_message(self, message, **kwargs):
        self.raw_message = message.as_bytes()
        self.envelope = kwargs
        return {}

    def quit(self):
        return 221, b"bye"


def test_smtp_configuration_is_validated_and_factory_is_explicit():
    provider = resolve_email_provider(smtp_config())
    assert isinstance(provider, SMTPEmailProvider)
    with pytest.raises(ValueError, match="EMAIL_PROVIDER is required"):
        resolve_email_provider(smtp_config(email_provider=""))
    with pytest.raises(ValueError, match="unsupported"):
        resolve_email_provider(smtp_config(email_provider="unknown"))
    with pytest.raises(ValueError, match="disabled"):
        resolve_email_provider(smtp_config(outreach_email_enabled=False))
    with pytest.raises(ValueError, match="cannot both"):
        SMTPEmailProvider(smtp_config(smtp_use_ssl=True))
    with pytest.raises(ValueError, match="username and password"):
        SMTPEmailProvider(smtp_config(smtp_password=""))


def test_provider_status_migration_matches_model_and_is_reversible():
    path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0025_email_provider_status.py"
    spec = importlib.util.spec_from_file_location("phase11a_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert (migration.revision, migration.down_revision) == (
        "0025_email_provider_status", "0024_ai_outreach_executions",
    )
    assert SCHEMA_REVISION == "0045_encrypt_api_keys"
    assert Base.metadata.tables["ai_outreach_executions"].c.provider_status.nullable


@pytest.mark.asyncio
async def test_smtp_send_preserves_approved_content_and_redacts_credentials(monkeypatch):
    FakeSMTP.instances.clear()
    monkeypatch.setattr("app.providers.email.smtplib.SMTP", FakeSMTP)
    provider = SMTPEmailProvider(smtp_config())
    request = EmailSendRequest(
        recipient="approved@example.test", subject="Exact approved subject",
        body="Exact approved body\nSecond line", idempotency_key="stable-key",
        metadata={"execution_id": "execution-1"},
    )
    result = await provider.send(request)
    transport = FakeSMTP.instances[0]
    parsed = BytesParser(policy=policy.default).parsebytes(transport.raw_message)
    assert result == EmailProviderResult(outcome="SENT", provider="smtp", provider_status="accepted")
    assert parsed["To"] == request.recipient
    assert parsed["Subject"] == request.subject
    assert parsed.get_content().strip() == request.body
    assert transport.envelope == {"from_addr": "sales@example.test", "to_addrs": [request.recipient]}
    assert transport.credentials == ("mailer-user", "DO_NOT_LEAK_THIS_SECRET")
    assert "DO_NOT_LEAK_THIS_SECRET" not in repr(result)


@pytest.mark.asyncio
async def test_smtp_rejection_is_failed_and_timeout_is_uncertain(monkeypatch):
    class RejectingSMTP(FakeSMTP):
        def send_message(self, message, **kwargs):
            import smtplib
            raise smtplib.SMTPDataError(550, b"private server details")

    monkeypatch.setattr("app.providers.email.smtplib.SMTP", RejectingSMTP)
    request = EmailSendRequest("approved@example.test", "subject", "body", "key")
    rejected = await SMTPEmailProvider(smtp_config()).send(request)
    assert rejected.outcome == "FAILED"
    assert rejected.error_code == "PROVIDER_REJECTED"
    assert "private server details" not in (rejected.error_message or "")

    class TimingOutSMTP(FakeSMTP):
        def send_message(self, message, **kwargs):
            import socket
            raise socket.timeout("sensitive host diagnostic")

    monkeypatch.setattr("app.providers.email.smtplib.SMTP", TimingOutSMTP)
    uncertain = await SMTPEmailProvider(smtp_config()).send(request)
    assert uncertain.outcome == "UNCERTAIN"
    assert uncertain.error_code == "PROVIDER_OUTCOME_UNKNOWN"
    assert "sensitive host diagnostic" not in (uncertain.error_message or "")
