from pathlib import Path


def test_fitness_prompt_exists_and_sets_scope() -> None:
    prompt = (Path(__file__).resolve().parents[1] / "prompts" / "FITNESS.md").read_text(encoding="utf-8")

    assert "84 кг" in prompt
    assert "173 см" in prompt
    assert "recomposition" in prompt
    assert "калистеника" in prompt
    assert "FatSecret" in prompt
    assert "Все сообщения владельцу — только на русском." in prompt
