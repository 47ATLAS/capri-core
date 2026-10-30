"""Pruebas sin red del editor recursivo de la psique."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from capri_core.mcp_tools import recursive_edit
from capri_core.mcp_tools.recursive_edit import RecursiveEditor
from capri_core.psyche.prompt_builder import WPP_BLOCK


INITIAL_STATE = {
    "mood": "neutral",
    "trust": 0.5,
    "sarcasm_level": 0.8,
    "vad": {"valence": 0.0, "arousal": 0.0, "dominance": 0.0},
}


def _git(repository: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=check,
        capture_output=True,
        text=True,
    )


def _make_repository(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    repository = tmp_path / "repo"
    psyche = repository / "capri_core" / "psyche"
    psyche.mkdir(parents=True)
    (psyche / "core_directives.json").write_text(
        json.dumps({"wpp": WPP_BLOCK}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (psyche / "psyche_state.json").write_text(
        json.dumps(INITIAL_STATE, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "Capri Test")
    _git(repository, "config", "user.email", "capri-test@example.invalid")
    _git(repository, "add", ".")
    _git(repository, "commit", "-q", "-m", "estado inicial")
    audit = repository / "audit" / "psyche.jsonl"
    return repository, psyche, psyche / "psyche_state.json", audit


def _read_state(state_path: Path) -> dict:
    return json.loads(state_path.read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_edicion_allowlist_schema_y_trailer_git(tmp_path: Path) -> None:
    """Una edición válida conserva el esquema y el commit previo lleva el trailer."""
    repository, psyche, state_path, audit = _make_repository(tmp_path)
    state = _read_state(state_path)
    state["trust"] = 0.6
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    editor = RecursiveEditor(psyche, repository, audit)
    result = await editor.overwrite_psyche_trait("mood", "alerta", "prueba local")

    assert result["ok"] is True
    assert _read_state(state_path)["mood"] == "alerta"
    assert _read_state(state_path)["trust"] == 0.6
    message = _git(repository, "log", "-1", "--format=%B").stdout
    assert "Co-authored-by: Manus <dev-agent@manus.ai>" in message
    assert json.loads(audit.read_text(encoding="utf-8").splitlines()[-1])["reason"] == "prueba local"

    with pytest.raises(ValueError):
        await editor.overwrite_psyche_trait("../core_directives.json", "x", "no")
    with pytest.raises(ValueError):
        await editor.overwrite_psyche_trait("trust", "nan", "no")
    with pytest.raises(ValueError):
        await editor.overwrite_psyche_trait("trust", "1.1", "no")
    with pytest.raises(ValueError):
        await editor.overwrite_psyche_trait("core_directives", "x", "no")


@pytest.mark.asyncio
async def test_fallo_git_aborta_sin_escribir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Un fallo del commit previo deja intacto el archivo y no crea auditoría."""
    repository, psyche, state_path, audit = _make_repository(tmp_path)
    original = state_path.read_text(encoding="utf-8")
    editor = RecursiveEditor(psyche, repository, audit)

    def fail_commit() -> str:
        raise RuntimeError("fallo simulado")

    monkeypatch.setattr(editor, "_commit_state_snapshot_locked", fail_commit)
    with pytest.raises(RuntimeError, match="fallo simulado"):
        await editor.overwrite_psyche_trait("mood", "cansada", "no persistir")
    assert state_path.read_text(encoding="utf-8") == original
    assert not audit.exists()


@pytest.mark.asyncio
async def test_staging_ajeno_preservado(tmp_path: Path) -> None:
    """El índice temporal no incorpora ni altera un archivo staged ajeno."""
    repository, psyche, state_path, audit = _make_repository(tmp_path)
    unrelated = repository / "unrelated.txt"
    unrelated.write_text("cambio staged", encoding="utf-8")
    _git(repository, "add", "unrelated.txt")

    editor = RecursiveEditor(psyche, repository, audit)
    await editor.overwrite_psyche_trait("trust", "0.7", "preservar staging")

    staged = _git(repository, "diff", "--cached", "--name-only").stdout.splitlines()
    assert staged == ["unrelated.txt"]
    assert _read_state(state_path)["trust"] == 0.7


@pytest.mark.asyncio
async def test_recuperacion_desde_ultimo_commit_valido_sin_reset(tmp_path: Path) -> None:
    """La corrupción se recupera sólo escribiendo psyche_state desde Git."""
    repository, psyche, state_path, audit = _make_repository(tmp_path)
    unrelated = repository / "keep.txt"
    unrelated.write_text("no tocar", encoding="utf-8")
    editor = RecursiveEditor(psyche, repository, audit)

    state_path.write_text('{"mood": ', encoding="utf-8")
    assert await editor.validate_and_recover() is True
    assert _read_state(state_path) == INITIAL_STATE
    assert unrelated.read_text(encoding="utf-8") == "no tocar"


@pytest.mark.asyncio
async def test_watchdog_repara_corrupcion_y_acepta_replace_atomico(tmp_path: Path) -> None:
    """Los eventos de archivo no compiten con la escritura atómica validada."""
    repository, psyche, state_path, audit = _make_repository(tmp_path)
    editor = RecursiveEditor(psyche, repository, audit)
    editor.start_watchdog()
    try:
        state_path.write_text("{", encoding="utf-8")
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            try:
                if _read_state(state_path) == INITIAL_STATE:
                    break
            except json.JSONDecodeError:
                pass
            await asyncio.sleep(0.05)
        assert _read_state(state_path) == INITIAL_STATE

        replacement = psyche / "replacement.tmp"
        replacement.write_text(json.dumps({**INITIAL_STATE, "mood": "atenta"}), encoding="utf-8")
        os.replace(replacement, state_path)
        await asyncio.sleep(0.2)
        assert _read_state(state_path)["mood"] == "atenta"
    finally:
        editor.stop_watchdog()


@pytest.mark.asyncio
async def test_funcion_de_modulo_requiere_configuracion_y_delega(tmp_path: Path) -> None:
    """La función pública conserva la firma y delega en configure_editor."""
    repository, psyche, state_path, audit = _make_repository(tmp_path)
    editor = RecursiveEditor(psyche, repository, audit)
    recursive_edit.configure_editor(editor)
    result = await recursive_edit.overwrite_psyche_trait("sarcasm_level", "0.4", "delegación")
    assert result["target_key"] == "sarcasm_level"
    assert _read_state(state_path)["sarcasm_level"] == 0.4
