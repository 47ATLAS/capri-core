import pytest

from config import Settings


def test_api_key_required():
    with pytest.raises(ValueError, match="CAPRI_API_KEY"):
        Settings(api_key="")


def test_no_other_keys_required():
    settings = Settings(api_key="test-key")
    assert not settings.cloud_api_key and not settings.mem0_api_key
    assert not settings.elevenlabs_api_key and not settings.backup_key
    assert "test-key" not in repr(settings)


def test_env_file_and_process_priority(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("CAPRI_API_KEY=file-key\nCAPRI_ENV=dev\nCAPRI_ENGINE=fake\n", encoding="utf-8")
    monkeypatch.delenv("CAPRI_API_KEY", raising=False)
    monkeypatch.delenv("CAPRI_ENV", raising=False)
    monkeypatch.delenv("CAPRI_ENGINE", raising=False)
    assert Settings.from_env(path).api_key == "file-key"
    monkeypatch.setenv("CAPRI_API_KEY", "env-key")
    assert Settings.from_env(path).api_key == "env-key"


def test_fake_forbidden_in_production():
    with pytest.raises(ValueError):
        Settings(api_key="test", environment="prod", cognitive_provider="fake")
