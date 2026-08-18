from app.services.summarization import generate_mock_summary


def test_summary_uses_leading_sentences_and_correct_stats():
    content = "First sentence here. Second sentence follows! Third one too? Fourth is extra."

    summary = generate_mock_summary(content)

    assert summary.summary_text == "First sentence here. Second sentence follows! Third one too?"
    assert summary.key_points == ["First sentence here.", "Second sentence follows!", "Third one too?"]
    assert summary.word_count == len(content.split())
    assert summary.character_count == len(content)


def test_summary_falls_back_when_content_has_no_sentence_punctuation():
    content = "no punctuation at all just words going on and on"

    summary = generate_mock_summary(content)

    assert summary.summary_text == content
    assert summary.key_points == [content]


def test_summary_truncates_extremely_long_unpunctuated_content():
    content = "word " * 200  # no punctuation, well past the fallback char limit

    summary = generate_mock_summary(content)

    assert len(summary.summary_text) <= 280
