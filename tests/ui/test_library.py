"""Phase 6: a question about the ingested book is answered from the library, with a chapter citation."""

import re

import pytest
from test_live_math import PROBE, RAW, send

QUESTION = (
    "Using my linear algebra book, what are the four fundamental subspaces of an m by n matrix A of rank r, "
    "and what are their dimensions? Cite the book chapter."
)


@pytest.mark.live
def test_book_question_cites_chapter(ui, api):
    send(ui, QUESTION)
    ui.wait_for_url(re.compile(r".*/c/[\w-]+"), timeout=60_000)
    api.created.append(ui.url.rsplit("/", 1)[-1])
    last = ui.locator("[id^=message-]").filter(has=ui.locator("#response-content-container")).last
    last.locator("button[aria-label='Copy']").wait_for(timeout=420_000)
    ui.wait_for_timeout(1500)
    body = last.locator("#response-content-container")
    probe = body.evaluate(PROBE)
    text = last.inner_text()
    assert probe["errors"] == 0, "KaTeX errors"
    assert probe["katex"] >= 1, "no formulas rendered"
    assert not RAW.findall(probe["text"]), "raw delimiters visible"
    # Retrieved from the library: the answer (or its citation list) names the book chapter.
    assert re.search(r"Chapter\s*2|Vector Spaces|linear-algebra-and-its-applications", text, re.I), text[-1500:]
    ui.screenshot(path="/docs/library-answer.png", full_page=True)
