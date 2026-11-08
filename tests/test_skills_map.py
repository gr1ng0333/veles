import pathlib

SKILLS_DIR = pathlib.Path(__file__).resolve().parents[1] / "prompts/skills"
MAP_FILE = SKILLS_DIR / "_map.md"


def test_map_file_exists():
    """_map.md must exist in prompts/skills/"""
    assert MAP_FILE.exists(), "prompts/skills/_map.md is missing"


def test_map_not_empty():
    content = MAP_FILE.read_text(encoding="utf-8")
    assert content.strip(), "_map.md is empty"


def test_map_has_header_row():
    """Map must contain a markdown table with Name | File | When to load."""
    content = MAP_FILE.read_text(encoding="utf-8")
    assert "Name" in content and "File" in content and "When to load" in content


def test_map_lists_3xui():
    content = MAP_FILE.read_text(encoding="utf-8")
    assert "3xui" in content


def test_map_lists_ssh_servers():
    content = MAP_FILE.read_text(encoding="utf-8")
    assert "ssh-servers" in content


def test_map_contains_skill_load_hint():
    """Map must tell agent to use skill_load."""
    content = MAP_FILE.read_text(encoding="utf-8")
    assert "skill_load" in content


def test_context_loads_map_into_static_block(tmp_path):
    """context.py must inject skills map into Block 0 static_text."""
    from ouroboros.context import build_llm_messages
    from ouroboros.memory import Memory

    # Minimal env mock
    class FakeEnv:
        repo_dir = SKILLS_DIR.parents[1]
        drive_root = tmp_path

        def repo_path(self, rel):
            return self.repo_dir / rel

        def drive_path(self, rel):
            return self.drive_root / rel

    env = FakeEnv()
    memory = Memory(drive_root=tmp_path)

    task = {"id": "test-map-01", "type": "task", "text": "hello"}
    messages, _ = build_llm_messages(env, memory, task)

    # Block 0 is messages[0]["content"][0]["text"]
    static_block = messages[0]["content"][0]["text"]
    assert "Skills Map" in static_block or "skill_load" in static_block, (
        "Skills map not found in Block 0 static_text"
    )


def test_map_matches_local_skills():
    content = MAP_FILE.read_text(encoding="utf-8")
    for skill in SKILLS_DIR.glob("*.md"):
        if not skill.name.startswith("_"):
            assert f"prompts/skills/{skill.name}" in content
