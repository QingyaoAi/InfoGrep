from infogrep.config import ChunkConfig
from infogrep.ingest.chunker import chunk_pages
from infogrep.ingest.types import ExtractedPage


def test_single_short_page_one_passage():
    pages = [ExtractedPage(page=None, text="hello world foo bar")]
    out = chunk_pages("doc.txt", pages, ChunkConfig(size=10, overlap=2))
    assert len(out) == 1
    assert out[0].passage_id == "doc.txt#0"
    assert out[0].text == "hello world foo bar"
    assert out[0].offset == 0
    assert out[0].page is None


def test_windows_overlap_and_offsets():
    words = " ".join(f"w{i}" for i in range(10))  # w0 .. w9
    pages = [ExtractedPage(page=3, text=words)]
    out = chunk_pages("doc.pdf", pages, ChunkConfig(size=4, overlap=1))
    # step = 3 -> windows start at 0,3,6; the start=9 tail (only w9) is redundant
    # with window 2 (w6..w9) and is suppressed.
    assert [p.ordinal for p in out] == [0, 1, 2]
    assert out[0].text == "w0 w1 w2 w3"
    assert out[1].text.startswith("w3 ")  # overlap of 1 word
    assert all(p.page == 3 for p in out)
    # offsets are increasing character positions within the page
    assert out[0].offset == 0
    assert out[1].offset == words.index("w3")


def test_per_page_passages_keep_page_numbers():
    pages = [
        ExtractedPage(page=1, text="alpha beta"),
        ExtractedPage(page=2, text="gamma delta"),
    ]
    out = chunk_pages("d", pages, ChunkConfig(size=50, overlap=5))
    assert [p.page for p in out] == [1, 2]
    assert [p.ordinal for p in out] == [0, 1]
