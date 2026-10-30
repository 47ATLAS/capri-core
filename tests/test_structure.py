from pathlib import Path


def test_exact_sdd_source_tree():
    root = Path(__file__).resolve().parents[1] / "capri_core"
    expected = {
        "main.py", "memory/mem0_client.py", "memory/backup_cron.py",
        "psyche/core_directives.json", "psyche/psyche_state.json", "psyche/prompt_builder.py",
        "mcp_tools/system_actions.py", "mcp_tools/recursive_edit.py", "mcp_tools/finance_manager.py",
        "voice/voice_engine.py", "telemetry/webhook_receiver.py", "telemetry/fuzzy_logic.py",
    }
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*")
              if p.is_file() and "__pycache__" not in p.parts}
    assert actual == expected
