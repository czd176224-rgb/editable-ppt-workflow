from pathlib import Path
import sys


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import doctor


def test_auth_prefers_explicit_file(monkeypatch, tmp_path):
    selected = tmp_path / "explicit.json"
    monkeypatch.setenv("CODEX_AUTH_FILE", str(selected))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    assert doctor.resolve_codex_auth_file() == selected


def test_auth_uses_codex_home_before_user_profile(monkeypatch, tmp_path):
    root = tmp_path / "codex-home"
    root.mkdir()
    selected = root / "auth.json"
    selected.write_text("{}", encoding="utf-8")
    monkeypatch.delenv("CODEX_AUTH_FILE", raising=False)
    monkeypatch.setenv("CODEX_HOME", str(root))
    assert doctor.resolve_codex_auth_file() == selected


def test_fake_ip_dns_is_reported_as_unavailable(monkeypatch):
    monkeypatch.setattr(doctor.socket, "getaddrinfo", lambda *_: [(2, 1, 6, "", ("198.18.0.1", 443))])
    result = doctor.codex_dns_status()
    assert result["fake_ip"] is True
    assert result["available"] is False
