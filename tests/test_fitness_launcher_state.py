from supervisor.state import ensure_state_defaults


def test_fitness_and_background_flags_default_to_disabled() -> None:
    st = ensure_state_defaults({})

    assert st["bg_consciousness_enabled"] is False
    assert st["fitness_enabled"] is False
    assert st["fitness_awaiting_reply"] is False
    assert st["fitness_next_message"] is False


def test_fitness_flags_preserve_existing_state() -> None:
    st = ensure_state_defaults({
        "bg_consciousness_enabled": True,
        "fitness_enabled": True,
        "fitness_awaiting_reply": True,
        "fitness_next_message": True,
    })

    assert st["bg_consciousness_enabled"] is True
    assert st["fitness_enabled"] is True
    assert st["fitness_awaiting_reply"] is True
    assert st["fitness_next_message"] is True
