from claude_tg import catalog


def test_every_effort_is_valid():
    for choice in catalog.EFFORTS:
        assert catalog.is_valid("effort", choice.key)


def test_unknown_effort_rejected():
    assert not catalog.is_valid("effort", "ultra")


def test_any_model_accepted():
    # Новые модели выходят чаще, чем обновляется каталог — не блокируем их.
    assert catalog.is_valid("model", "claude-opus-9")
    assert not catalog.is_valid("model", "")


def test_thinking_disabled_downgrades_on_high_effort_levels():
    # API возвращает 400 на disabled + xhigh/max, поэтому мягко откатываемся.
    assert catalog.thinking_config("disabled", "xhigh") == {
        "type": "adaptive",
        "display": "omitted",
    }
    assert catalog.thinking_config("disabled", "max")["type"] == "adaptive"
    assert catalog.thinking_config("disabled", "high") == {"type": "disabled"}


def test_summarized_thinking():
    assert catalog.thinking_config("summarized", "high") == {
        "type": "adaptive",
        "display": "summarized",
    }


def test_model_emoji():
    assert catalog.model_emoji("claude-fable-5") == "🍓"
    assert catalog.model_emoji("sonnet") == "🍏"
    assert catalog.model_emoji("opus") == "🍎"
    assert catalog.model_emoji("claude-haiku-4-5") == "🍡"


def test_labels_fall_back_to_key():
    assert catalog.label("mode", "default").startswith("🛡")
    assert catalog.label("mode", "неизвестно") == "неизвестно"
