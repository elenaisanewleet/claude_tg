from claude_tg.telegram_md import md_to_html, quote_block, split_markdown, truncate


def test_short_text_is_one_chunk():
    assert split_markdown("привет", 100) == ["привет"]


def test_empty_text_gives_no_chunks():
    assert split_markdown("", 100) == []


def test_split_keeps_code_fences_balanced():
    body = "\n".join(f"line {i}" for i in range(200))
    text = f"вступление\n\n```python\n{body}\n```\n\nконец"
    chunks = split_markdown(text, 400)

    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0, chunk


def test_split_reopens_fence_with_language():
    body = "\n".join(f"x = {i}" for i in range(100))
    chunks = split_markdown(f"```python\n{body}\n```", 300)
    assert len(chunks) > 1
    assert all(c.startswith("```python") for c in chunks)


def test_hard_wrap_of_giant_line():
    chunks = split_markdown("a" * 5000, 500)
    assert chunks
    assert all(len(c) <= 500 for c in chunks)


def test_html_is_escaped():
    html = md_to_html("<script>alert(1)</script>")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_bold_and_code_render():
    html = md_to_html("**жирный** и `код`")
    assert "<b>жирный</b>" in html
    assert "<code>код</code>" in html


def test_code_block_keeps_language():
    html = md_to_html("```python\nprint('привет')\n```")
    assert '<pre><code class="language-python">' in html
    assert "print(&#x27;привет&#x27;)" in html or "print('привет')" in html


def test_link_rendering():
    html = md_to_html("[дока](https://example.com/a?b=1)")
    assert '<a href="https://example.com/a?b=1">дока</a>' in html


def test_heading_becomes_bold():
    assert md_to_html("## Заголовок") == "<b>Заголовок</b>"


def test_snake_case_is_not_italic():
    html = md_to_html("some_variable_name")
    assert "<i>" not in html


def test_bullet_normalised():
    assert "• пункт" in md_to_html("- пункт")


def test_quote_block_escapes():
    assert "&lt;b&gt;" in quote_block("<b>")


def test_truncate():
    assert truncate("абвгде", 4) == "абв…"
    assert truncate("абв", 10) == "абв"
