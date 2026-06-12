"""Regression tests for the auth-hardening fixes (tracker #11, #12, #13):
proxy-aware rate-limit bucketing, the anonymous settings whitelist, and
restrictive permissions on the API-key encryption material."""

import asyncio
import stat
import sys
from types import SimpleNamespace

import pytest

from src.rate_limiter import client_key


def _req(peer, headers=None):
    return SimpleNamespace(client=SimpleNamespace(host=peer), headers=headers or {})


class TestClientKey:
    """#11: behind the recommended proxy every request arrives from one IP —
    the bucket key must be the real client, but only when the direct peer is
    a trusted proxy (honoring untrusted forwarded headers would hand an
    attacker unlimited fresh buckets)."""

    def test_direct_connection_uses_peer(self):
        assert client_key(_req("203.0.113.7")) == "203.0.113.7"

    def test_trusted_proxy_uses_last_xff_hop(self):
        # The LAST entry is the hop the trusted proxy itself appended;
        # earlier entries are caller-supplied and spoofable.
        req = _req("172.18.0.1", {"x-forwarded-for": "1.2.3.4, 203.0.113.7"})
        assert client_key(req) == "203.0.113.7"

    def test_trusted_proxy_prefers_cf_connecting_ip(self):
        req = _req("127.0.0.1", {
            "cf-connecting-ip": "198.51.100.9",
            "x-forwarded-for": "1.2.3.4",
        })
        assert client_key(req) == "198.51.100.9"

    def test_untrusted_peer_ignores_forwarded_headers(self):
        # A direct external client must not mint fresh buckets via XFF.
        req = _req("203.0.113.7", {"x-forwarded-for": "9.9.9.9"})
        assert client_key(req) == "203.0.113.7"

    def test_trusted_proxy_without_headers_uses_peer(self):
        assert client_key(_req("172.18.0.1")) == "172.18.0.1"

    def test_non_ip_peer_passes_through(self):
        # Starlette's TestClient reports "testclient"; never crash on it.
        assert client_key(_req("testclient")) == "testclient"

    def test_missing_client_is_stable(self):
        assert client_key(SimpleNamespace(client=None, headers={})) == "unknown"


class TestAnonymousSettingsWhitelist:
    """#12: /api/auth/settings is auth-exempt; anonymous callers must get
    only the display-pref whitelist, never owner PII or topology."""

    def _get_settings_endpoint(self, auth_manager):
        from routes.auth_routes import setup_auth_routes
        router = setup_auth_routes(auth_manager)
        return next(
            r.endpoint for r in router.routes
            if getattr(r, "path", "") == "/api/auth/settings"
            and "GET" in getattr(r, "methods", set())
        )

    def _settings_fixture(self):
        return {
            "tts_enabled": True,
            "tts_provider": "endpoint:chatterbox",
            "keybinds": {"send": "Enter"},
            "search_provider": "searxng",
            # Sensitive values that leaked through name-based scrubbing:
            "reminder_email_to": "owner@example.com",
            "reminder_ntfy_topic": "secret-topic",
            "app_public_url": "https://jarvis.example.com",
            "tool_path_extra_roots": "/mnt/private",
        }

    def test_anonymous_gets_only_whitelist(self, monkeypatch):
        import routes.auth_routes as ar
        monkeypatch.setattr(ar, "_load_settings", self._settings_fixture)
        auth_manager = SimpleNamespace(
            get_username_for_token=lambda tok: None,
            is_admin=lambda u: False,
        )
        endpoint = self._get_settings_endpoint(auth_manager)
        req = SimpleNamespace(cookies={})
        out = asyncio.run(endpoint(req))
        assert out == {
            "tts_enabled": True,
            "tts_provider": "endpoint:chatterbox",
            "keybinds": {"send": "Enter"},
            "search_provider": "searxng",
        }
        assert "reminder_email_to" not in out
        assert "reminder_ntfy_topic" not in out
        assert "app_public_url" not in out
        assert "tool_path_extra_roots" not in out

    def test_admin_still_gets_everything(self, monkeypatch):
        import routes.auth_routes as ar
        monkeypatch.setattr(ar, "_load_settings", self._settings_fixture)
        auth_manager = SimpleNamespace(
            get_username_for_token=lambda tok: "admin",
            is_admin=lambda u: True,
        )
        endpoint = self._get_settings_endpoint(auth_manager)
        req = SimpleNamespace(cookies={"jarvis_session": "x"})
        out = asyncio.run(endpoint(req))
        assert out["reminder_email_to"] == "owner@example.com"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX mode bits (0o600) don't exist on Windows; the files are "
    "protected by the user-profile NTFS ACL instead, and safe_chmod no-ops there.",
)
class TestApiKeyFilePermissions:
    """#13: the Fernet key decrypts every stored provider key — it (and the
    encrypted store) must not be world-readable on the bind-mounted data dir."""

    def _mode(self, path):
        return stat.S_IMODE(path.stat().st_mode)

    def test_key_file_created_0600(self, tmp_path):
        from src.api_key_manager import APIKeyManager
        mgr = APIKeyManager(str(tmp_path))
        mgr.get_or_create_key()
        assert self._mode(tmp_path / ".key") == 0o600

    def test_existing_loose_key_file_is_tightened(self, tmp_path):
        from src.api_key_manager import APIKeyManager
        from cryptography.fernet import Fernet
        key_file = tmp_path / ".key"
        key_file.write_bytes(Fernet.generate_key())
        key_file.chmod(0o644)  # legacy deployment
        mgr = APIKeyManager(str(tmp_path))
        mgr.get_or_create_key()
        assert self._mode(key_file) == 0o600

    def test_api_keys_json_saved_0600(self, tmp_path):
        from src.api_key_manager import APIKeyManager
        mgr = APIKeyManager(str(tmp_path))
        mgr.save("openai", "sk-test")
        assert self._mode(tmp_path / "api_keys.json") == 0o600
        # Round-trip still works with the tightened mode.
        assert mgr.load() == {"openai": "sk-test"}
