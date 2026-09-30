"""Unit tests for the Leo LaTeX normalizer (§9.3). Imports the function file directly."""

import importlib.util
import random
from pathlib import Path

import pytest

_path = Path(__file__).resolve().parents[1] / "functions" / "leo_latex_normalizer.py"
_spec = importlib.util.spec_from_file_location("leo_latex_normalizer", _path)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)
normalize_math, StreamNormalizer, Filter = mod.normalize_math, mod.StreamNormalizer, mod.Filter

D = "\n\n$$\n{}\n$$\n\n"


def n(text, style="dollar"):
    return normalize_math(text, style)


# Rule 1: \[ … \] → canonical display block
def test_bracket_display_single_line():
    assert n("Thus \\[x^2 + 1\\] holds.") == "Thus " + D.format("x^2 + 1") + " holds."


def test_bracket_display_multiline():
    assert n("We get\n\\[\na + b\n= c\n\\]\ndone") == "We get\n\n$$\na + b\n= c\n$$\n\ndone"


# Rule 2: existing $$ … $$ display blocks get own lines + blank lines
def test_dollar_display_alone_on_line():
    assert n("Result:\n$$x = 1$$\nNext") == "Result:\n\n$$\nx = 1\n$$\n\nNext"


def test_dollar_display_multiline_with_text_around():
    assert n("so $$\n\\sum_{i=1}^n i\n$$ as shown") == "so " + D.format("\\sum_{i=1}^n i") + " as shown"


def test_display_block_blank_lines_inside_removed():
    assert n("$$\na\n\\\\\nb\n$$") == "$$\na\n\\\\\nb\n$$\n\n".lstrip("\n")


# Rule 3: inline styles
@pytest.mark.parametrize(
    "style,expected",
    [("dollar", "Let $x+1$ be"), ("paren", "Let \\(x+1\\) be"), ("double", "Let $$x+1$$ be")],
)
def test_inline_conversion(style, expected):
    for src in ("Let \\(x+1\\) be", "Let $x+1$ be", "Let $$x+1$$ be"):
        assert n(src, style) == expected


def test_inline_next_to_korean_and_punctuation():
    assert n("함수 \\(f(x)=x^2\\)의 도함수는") == "함수 $f(x)=x^2$의 도함수는"
    assert n("값은 $x$, 그리고 $y$.") == "값은 $x$, 그리고 $y$."


def test_inline_in_list_and_table():
    src = "- \\(a_n\\) converges\n| col | \\(x^2\\) |"
    assert n(src) == "- $a_n$ converges\n| col | $x^2$ |"


# Rule 4: top-level environments
def test_align_becomes_aligned_and_labels_dropped():
    src = "\\begin{align}\na &= b \\label{eq:1} \\\\\nc &= d\n\\end{align}"
    assert n(src) == "$$\n\\begin{aligned}\na &= b \\\\\nc &= d\n\\end{aligned}\n$$\n\n"


def test_equation_and_gather():
    assert n("\\begin{equation} E = mc^2 \\end{equation}") == "$$\nE = mc^2\n$$\n\n"
    assert "\\begin{gathered}" in n("\\begin{gather*}\na\\\\b\n\\end{gather*}")


# Rule 5: currency guard
def test_currency_escaped():
    assert n("It costs $5 and $10.") == "It costs \\$5 and \\$10."
    assert n("Pay $5, then compute $x$.") == "Pay \\$5, then compute $x$."
    assert n("Already escaped \\$5.") == "Already escaped \\$5."


def test_digit_math_still_math():
    assert n("We have $2^n$ subsets") == "We have $2^n$ subsets"


# Protected regions
def test_fenced_code_untouched():
    src = "Code:\n```python\nprice = '$5'  # \\(not math\\)\n```\nand \\(x\\)"
    assert n(src) == "Code:\n```python\nprice = '$5'  # \\(not math\\)\n```\nand $x$"


def test_tilde_fence_and_inline_code_untouched():
    src = "~~~\n$x$\n~~~\nUse `$x$` or ``\\(y\\)``."
    assert n(src) == src


def test_details_blocks_untouched():
    src = '<details type="tool_calls" arguments="{&quot;q&quot;: &quot;$5&quot;}">\nresult $5\n</details>\nok $x$'
    assert n(src) == src


def test_boxed_and_constructs_pass_through():
    src = "Final: $\\boxed{42}$ with $\\operatorname{lcm}(a,b)$ and $\\text{if } n>0$."
    assert n(src) == src


# Idempotence
FIXTURE = next(
    (
        p / "tests" / "ui" / "fixtures" / "latex_matrix.md"
        for p in Path(__file__).resolve().parents
        if (p / "tests" / "ui" / "fixtures").is_dir()
    ),
    Path("/nonexistent"),
)
SAMPLES = [
    "Thus \\[x^2\\] and \\(y\\) cost $5.",
    "We get\n\\[\na + b\n\\]\n\n```\n$x$\n```\n$$z$$",
    "\\begin{align*}\na&=b\\\\\nc&=d\n\\end{align*}\ntext $$q$$ more",
    "1. First $a$\n2. \\(b\\)\n\n| a | $x$ |\n|---|---|",
]


@pytest.mark.parametrize("style", ["dollar", "paren", "double"])
def test_idempotent(style):
    samples = SAMPLES + ([FIXTURE.read_text()] if FIXTURE.exists() else [])
    for s in samples:
        once = n(s, style)
        assert n(once, style) == once


# Streaming ≡ whole-text
def _stream(text, style, rng):
    sn = StreamNormalizer(style)
    out, i = [], 0
    while i < len(text):
        k = rng.randint(1, 12)
        out.append(sn.feed(text[i : i + k]))
        i += k
    out.append(sn.flush())
    return "".join(out)


@pytest.mark.parametrize("seed", range(40))
def test_stream_random_chunks_equal_whole(seed):
    rng = random.Random(seed)
    samples = SAMPLES + ([FIXTURE.read_text()] if FIXTURE.exists() else [])
    for s in samples:
        for style in ("dollar", "paren"):
            assert _stream(s, style, rng) == n(s, style)


def test_stream_emits_plain_text_early():
    sn = StreamNormalizer()
    assert sn.feed("Hello wor") == "Hello wor"
    assert sn.feed("ld, see \\(x") == "ld, see "  # held from the delimiter on
    assert sn.feed("\\) now") == "$x$ now"


# Filter hooks
def _chunk(content, finish=None, cid="c1"):
    return {"id": cid, "choices": [{"delta": {"content": content}, "finish_reason": finish}]}


def test_filter_stream_and_outlet():
    f = Filter()
    parts = ["Let \\(", "x\\) be ", "$5", ""]
    got = "".join(f.stream(_chunk(p))["choices"][0]["delta"]["content"] for p in parts[:-1])
    got += f.stream(_chunk(None, finish="stop"))["choices"][0]["delta"]["content"]
    assert got == "Let $x$ be \\$5"
    body = {"messages": [{"role": "user", "content": "\\(u\\)"}, {"role": "assistant", "content": "\\(a\\)"}]}
    out = f.outlet(body)
    assert out["messages"][1]["content"] == "$a$" and out["messages"][0]["content"] == "\\(u\\)"


def test_filter_never_raises():
    f = Filter()
    assert f.stream({"choices": None}) == {"choices": None}
    assert f.outlet({"messages": "garbage"}) == {"messages": "garbage"}


_TOKENS = [
    "text ",
    "word",
    " ",
    "\n",
    "\n\n",
    "$",
    "$$",
    "\\(",
    "\\)",
    "\\[",
    "\\]",
    "x^2",
    "5",
    "$5",
    "`",
    "```",
    "~~~",
    "\\begin{align}",
    "\\end{align}",
    "&=",
    "\\\\",
    "\\label{a}",
    "한글",
    "<details>",
    "</details>",
    "- ",
    "| ",
    "\\boxed{1}",
    "    ",
]


@pytest.mark.parametrize("seed", range(300))
def test_fuzz_stream_equals_whole_and_idempotent(seed):
    rng = random.Random(1000 + seed)
    text = "".join(rng.choice(_TOKENS) for _ in range(rng.randint(5, 60)))
    for style in ("dollar", "paren", "double"):
        whole = n(text, style)
        assert _stream(text, style, rng) == whole, text


_PIECES = [
    "The value ",
    "we get ",
    "함수 ",
    "의 값은 ",
    ". ",
    ", ",
    "costs $5 and $12. ",
    "\n",
    "\n\n",
    "$x^2$",
    "\\(a+b\\)",
    "$$\\sqrt{2}$$",
    "$\\boxed{7}$",
    "$\\text{if } n>0$",
    "\n\\[\nz = 1\n\\]\n",
    "\n$$\nw_i\n$$\n",
    "\\[ q \\]",
    "\n\\begin{align}\na &= b \\label{e} \\\\\nc &= d\n\\end{align}\n",
    "\n```python\nprint('$x$')\n```\n",
    "`$y$`",
    "\n- item $a_n$\n",
    "\n| a | \\(b\\) |\n|---|---|\n",
]


@pytest.mark.parametrize("seed", range(200))
def test_fuzz_realistic_idempotent_and_streamable(seed):
    rng = random.Random(5000 + seed)
    text = "".join(rng.choice(_PIECES) for _ in range(rng.randint(3, 25)))
    for style in ("dollar", "paren", "double"):
        once = n(text, style)
        if style != "double":  # adjacent $$a$$$$b$$ is inherently ambiguous; double is best-effort
            assert n(once, style) == once, text
        assert _stream(text, style, rng) == once, text
