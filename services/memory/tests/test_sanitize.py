from leo_memory.sanitize import MAX_MESSAGE_CHARS, clean_text, sanitize_messages, select_new


def test_drops_system_and_tool_messages():
    out = sanitize_messages(
        [
            {"role": "system", "content": "You are Leo"},
            {"role": "tool", "content": '{"results": []}'},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
    )
    assert [m["role"] for m in out] == ["user", "assistant"]


def test_strips_reasoning_sources_tools_and_memory_blocks():
    text = (
        "<think>secret chain of thought</think>Answer A. "
        '<details type="reasoning" done="true"><summary>Thought</summary>more thoughts</details>'
        '<details type="tool_calls" name="search_web">{"q": "x"}</details>'
        '<source id="1">cited page text</source> Done [1].'
        "<leo_memory>old memory</leo_memory>"
    )
    out = clean_text(text)
    for leaked in ("secret", "more thoughts", "search_web", "cited page", "old memory"):
        assert leaked not in out
    assert out.startswith("Answer A.") and "Done [1]." in out


def test_unclosed_think_is_removed():
    assert clean_text("Final answer.<think>truncated reasoning with no end") == "Final answer."


def test_base64_images_removed_from_text_and_parts():
    b64 = "A" * 400
    out = sanitize_messages(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"see data:image/png;base64,{b64} here"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ],
            },
        ]
    )
    assert out[0]["content"] == "see [image] here"


def test_message_cap():
    out = sanitize_messages([{"role": "user", "content": "x" * 5000}])
    assert len(out[0]["content"]) == MAX_MESSAGE_CHARS
    assert out[0]["content"].endswith("…")


def test_empty_after_cleaning_is_dropped():
    assert sanitize_messages([{"role": "assistant", "content": "<think>only thoughts</think>"}]) == []


def _msgs(n: int, size: int = 10):
    return [{"id": f"m{i}", "role": "user" if i % 2 == 0 else "assistant", "content": "y" * size} for i in range(n)]


def test_select_after_last_message_id():
    assert [m["id"] for m in select_new(_msgs(6), "m3", 10_000)] == ["m4", "m5"]


def test_select_unknown_id_falls_back_to_recent_within_budget():
    out = select_new(_msgs(10, size=100), "nope", 350)
    assert [m["id"] for m in out] == ["m7", "m8", "m9"]


def test_select_without_ids_uses_budget():
    msgs = [{"id": None, "role": "user", "content": "z" * 100} for _ in range(5)]
    assert len(select_new(msgs, None, 250)) == 2


def test_select_keeps_at_least_one_message():
    assert len(select_new(_msgs(1, size=500), None, 10)) == 1


def test_raw_base64_blob_replaced_but_plain_text_kept():
    import base64

    blob = base64.b64encode(bytes(range(256)) * 2).decode()
    assert clean_text(f"payload {blob} end") == "payload [binary] end"
    assert clean_text("a" * 300) == "a" * 300
