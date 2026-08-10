from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def test_production_dispatcher_exposes_only_v6_and_diagnostics():
    namespace = {}
    source = (SCRIPTS / "word_to_editable_ppt.py").read_text(encoding="utf-8")
    exec(compile(source, "word_to_editable_ppt.py", "exec"), namespace)
    assert namespace["TOOLS"] == {
        "confirm-ui": "confirm_ui/server.py",
        "doctor": "doctor.py",
        "v6": "workflow_v6_cli.py",
    }


def test_v6_cli_does_not_import_legacy_workflows():
    source = (SCRIPTS / "workflow_v6_cli.py").read_text(encoding="utf-8")
    assert "workflow_v4" not in source
    assert "workflow_v5" not in source
