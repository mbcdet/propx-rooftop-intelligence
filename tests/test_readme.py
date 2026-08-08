"""The README's AI-assistance disclosure is immutable content.

The disclosure below is a commitment about how this submission was produced. It must remain in
README.md — visible, verbatim, under its own heading — through every future edit, shortening or
restructure. It may not be paraphrased, weakened, relocated to another document, collapsed, or
left only in Git history. This test exists so that any such change fails CI instead of slipping
through a documentation cleanup.
"""

from pathlib import Path

README = Path(__file__).resolve().parents[1] / "README.md"

# Verbatim. Do not reflow or edit this string to make the test pass; the README is what must
# carry the text, and this constant is the reference copy.
DISCLOSURE = (
    "I defined the problem framing, architecture, data-source strategy, acceptance criteria and "
    "final\ntrade-offs. I also ran and inspected the pipeline and its submitted outputs, "
    "evaluated review\nfindings and made the final decisions on every correction. Claude and "
    "OpenAI Codex were used as\nsupporting tools for targeted implementation assistance, code "
    "review, test development and\neditorial feedback. I verified and take full responsibility "
    "for the submitted result."
)


def test_the_ai_disclosure_is_present_verbatim() -> None:
    text = README.read_text(encoding="utf-8")
    # Compare with whitespace normalised so Markdown reflowing alone cannot break the check,
    # while any change to the words themselves still fails.
    normalised_readme = " ".join(text.split())
    normalised_disclosure = " ".join(DISCLOSURE.split())
    assert normalised_disclosure in normalised_readme, (
        "The AI-assistance disclosure is missing or altered in README.md. It is immutable "
        "content: restore the exact wording under the 'AI assistance' heading."
    )


def test_the_ai_disclosure_has_its_own_visible_heading() -> None:
    text = README.read_text(encoding="utf-8")
    assert "## AI assistance" in text, (
        "The 'AI assistance' heading must remain a visible top-level section of README.md, "
        "not collapsed, footnoted or moved to another document."
    )
    # The disclosure must live under that heading, not elsewhere in the file.
    section = text.split("## AI assistance", 1)[1]
    assert "I defined the problem framing" in section
