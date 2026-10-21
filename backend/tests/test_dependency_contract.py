from pathlib import Path
import tomllib


def test_dependencies_and_lock_are_declared():
    import openai
    import jsonschema

    root = Path(__file__).parents[1]
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = config["project"]["dependencies"]
    assert any(x.startswith("openai") for x in dependencies)
    assert any(x.startswith("jsonschema") for x in dependencies)
    assert callable(openai.OpenAI) and callable(jsonschema.validate)
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    names = {p["name"] for p in lock["package"]}
    assert {"openai", "jsonschema", "ruff", "mypy", "pytest-cov"} <= names
