from infogrep.retrieval.base import Result
from infogrep.retrieval.fusion import reciprocal_rank_fusion


def _r(doc: str, score: float, retriever: str) -> Result:
    return Result(
        doc_id=doc,
        passage_id="p0",
        path=f"/x/{doc}.txt",
        snippet=doc,
        score=score,
        retriever=retriever,
    )


def test_rrf_rewards_agreement_across_lists():
    sparse = [_r("a", 5.0, "sparse"), _r("b", 4.0, "sparse")]
    dense = [_r("b", 0.9, "dense"), _r("c", 0.8, "dense")]
    fused = reciprocal_rank_fusion([sparse, dense], top_n=3)

    # "b" appears in both lists, so it should win despite not being rank 1 anywhere.
    assert fused[0].doc_id == "b"
    assert fused[0].retriever == "hybrid"
    assert {r.doc_id for r in fused} == {"a", "b", "c"}


def test_rrf_dedups_by_passage():
    sparse = [_r("a", 5.0, "sparse")]
    dense = [_r("a", 0.9, "dense")]
    fused = reciprocal_rank_fusion([sparse, dense], top_n=10)
    assert len(fused) == 1
