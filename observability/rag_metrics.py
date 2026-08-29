from __future__ import annotations

from typing import Any, Iterable

import numpy as np

from observability.anomaly import mad_detector, zscore_detector


def approximate_token_lengths(texts: Iterable[str]) -> list[int]:
    # Deliberately simple proxy; no tokenizer/model download needed.
    return [len(str(t).split()) for t in texts]


def detect_text_length_shift(
    current_texts: Iterable[str],
    baseline_batch_means: Iterable[float],
    *,
    threshold: float = 3.0,
) -> dict[str, Any]:
    lengths = approximate_token_lengths(current_texts)
    current_mean = float(np.mean(lengths)) if lengths else 0.0
    result = zscore_detector(current_mean, baseline_batch_means, threshold=threshold)
    result["metric"] = "mean_text_length"
    result["current_mean"] = current_mean
    return result


def detect_embedding_norm_shift(
    current_norms: Iterable[float],
    baseline_norms: Iterable[float],
    *,
    threshold: float = 3.5,
) -> dict[str, Any]:
    """Embedding-space drift via the mean L2 norm of a batch of embeddings.

    No embedding model is required for the lab -- the stable interface takes
    precomputed norms directly (`data/history/metrics_history.csv` already
    tracks `embedding_norm_mean` for exactly this). Two independent signals:

    1. A robust median/MAD z-score (`mad_detector`, same approach as
       `detect_text_length_shift`'s zscore, just outlier-resistant) of the
       current batch's mean norm against `baseline_norms` -- catches a
       gradual model/preprocessing drift.
    2. A direct check for near-zero-norm vectors in the *current* batch.
       A properly functioning embedding call essentially never returns a
       zero vector; a batch containing one is a strong, independent sign of
       an encoding pipeline failure (e.g. the embedding API silently
       returned zeros/nulls that got cast to 0.0) rather than organic
       content drift -- worth flagging even on the rare occasion the MAD
       comparison alone wouldn't have crossed threshold (a handful of zeros
       mixed into an otherwise-normal batch can leave the mean looking
       unremarkable).
    """
    norms = np.asarray(list(current_norms), dtype=float)
    if norms.size == 0:
        return {"is_anomaly": False, "score": 0.0, "method": "embedding_norm_mad", "reason": "empty_input"}

    current_mean = float(np.mean(norms))
    result = mad_detector(current_mean, baseline_norms, threshold=threshold)

    degenerate_count = int(np.sum(norms < 1e-6))
    is_degenerate = degenerate_count > 0

    reason = result["reason"]
    if is_degenerate:
        reason += f"; degenerate_embeddings={degenerate_count}/{norms.size} near-zero-norm vectors"

    return {
        "is_anomaly": bool(result["is_anomaly"] or is_degenerate),
        "score": float(result["score"]),
        "method": "embedding_norm_mad",
        "metric": "mean_embedding_norm",
        "current_mean": current_mean,
        "reason": reason,
    }
