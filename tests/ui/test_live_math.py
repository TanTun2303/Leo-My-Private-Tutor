"""Live math through the real UI (§12 smoke test 3): stream a Leo answer and check the rendering."""

import re

import pytest

PROMPTS = [
    "Derive the closed form of the geometric series and prove it by induction.",
    "Write the recurrence for merge sort and solve it with the master theorem.",
    "기하급수의 합 공식을 유도하고 수학적 귀납법으로 증명해 줘.",
]

PROBE = r"""(el) => {
  const clone = el.cloneNode(true);
  clone.querySelectorAll('.katex, .katex-display, pre, code, .cm-editor, button, svg, details').forEach(n => n.remove());
  return {
    display: el.querySelectorAll('.katex-display').length,
    katex: el.querySelectorAll('.katex').length,
    errors: el.querySelectorAll('.katex-error').length,
    text: clone.textContent,
  };
}"""
RAW = re.compile(r"\$\$|\\\[|\\\(|\\begin")


def send(ui, prompt: str) -> None:
    box = ui.locator("#chat-input")
    box.click()
    box.fill(prompt)
    ui.keyboard.press("Enter")


@pytest.mark.live
@pytest.mark.parametrize("prompt", PROMPTS, ids=["geometric", "master-theorem", "korean"])
def test_live_math(ui, api, prompt):
    send(ui, prompt)
    ui.wait_for_url(re.compile(r".*/c/[\w-]+"), timeout=60_000)
    api.created.append(ui.url.rsplit("/", 1)[-1])
    last = ui.locator("[id^=message-]").filter(has=ui.locator("#response-content-container")).last
    # Done when the action row (copy button etc.) appears under the answer.
    last.locator("button[aria-label='Copy']").wait_for(timeout=300_000)
    ui.wait_for_timeout(1500)  # outlet normalization + re-render
    probe = last.locator("#response-content-container").evaluate(PROBE)
    assert probe["errors"] == 0, "KaTeX errors in the answer"
    assert probe["display"] >= 1, "no display equation"
    leftovers = RAW.findall(probe["text"])
    assert not leftovers, f"raw math delimiters visible: {set(leftovers)}"
