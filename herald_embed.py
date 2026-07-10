"""Herald embeddings + similarity clustering.

Embeds posts via OpenRouter's /embeddings endpoint (text-embedding-3-small,
1536-dim vectors, $0.02/M tokens) and clusters them by cosine similarity.

Used by the herald_digest.py pre-filter to produce a curated candidate set
for the LLM. The point is to NOT dump hundreds of tweets into the LLM — we
group posts by topical similarity first, then send the LLM a representative
slice (~30-50 posts).

Why this exists separately from herald_digest.py: keeps the embedding call
+ clustering logic self-contained and unit-testable; the digest CLI just
calls prepare_candidates(...) and gets a list back.
"""
import json
import math
import os
import time
import urllib.error
import urllib.request

EMBEDDINGS_URL = "https://openrouter.ai/api/v1/embeddings"
DEFAULT_EMBEDDING_MODEL = os.environ.get("HERALD_EMBEDDING_MODEL", "openai/text-embedding-3-small")

# Cosine similarity threshold for "same topic". 0.50 was tuned against a real
# X feed sample (2026-06-28): at 0.65 we got 0 clusters in 41 posts; at 0.50
# we get ~3-5 clusters that match human judgement of "same story." Below 0.40
# the clusters start merging unrelated posts. Override via HERALD_SIM_THRESHOLD.
DEFAULT_SIM_THRESHOLD = float(os.environ.get("HERALD_SIM_THRESHOLD", "0.50"))


def embed_texts(texts, model=None, timeout=120):
    """Embed a list of texts via OpenRouter. Returns (vectors, usage_dict).

    Each vector is a list of floats (1536 for text-embedding-3-small).
    Empty/None inputs are filtered out before the call; the returned list
    is parallel to the *original* input list, with None for skipped inputs.
    """
    model = model or DEFAULT_EMBEDDING_MODEL
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set. Load ~/.config/herald/.env first.")

    # Keep only non-empty texts; remember their original indices.
    pairs = [(i, t) for i, t in enumerate(texts) if t and t.strip()]
    if not pairs:
        return [None] * len(texts), {"prompt_tokens": 0, "cost": 0.0}

    payload = {
        "model": model,
        "input": [t for _, t in pairs],
    }
    req = urllib.request.Request(
        EMBEDDINGS_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://herald.jaaaames.com",
            "X-Title": "Herald",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")[:600]
        raise RuntimeError(f"OpenRouter embeddings HTTP {e.code}: {err_body}") from e

    data = json.loads(body)
    items = data.get("data") or []
    # OpenRouter returns items in the same order as input
    vectors_out = [None] * len(texts)
    for (orig_idx, _), item in zip(pairs, items):
        vectors_out[orig_idx] = item["embedding"]
    return vectors_out, data.get("usage") or {}


def cosine(a, b):
    """Cosine similarity between two equal-length vectors. Returns 0.0 if either is zero."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def cluster_by_similarity(posts, vectors, threshold=DEFAULT_SIM_THRESHOLD):
    """Greedy single-pass clustering by cosine similarity.

    Posts processed in given order (caller's responsibility to sort by
    whatever signal matters most — usually engagement). Each post joins an
    existing cluster if its similarity to that cluster's centroid is >=
    threshold; otherwise it starts a new cluster.

    Returns: list of clusters, each a dict:
        {"id": int, "post_indices": [int, ...], "centroid": [float, ...],
         "centroid_post_index": int, "max_sim_to_centroid": float}

    The centroid is the mean of member vectors. The centroid_post_index is
    the member whose vector is closest to the centroid (i.e. the most
    representative post in the cluster).
    """
    clusters = []
    for i, (post, vec) in enumerate(zip(posts, vectors)):
        if vec is None:
            # Skip posts without embeddings — they get their own singleton cluster
            clusters.append({
                "id": len(clusters),
                "post_indices": [i],
                "centroid": None,
                "centroid_post_index": i,
                "max_sim_to_centroid": 0.0,
                "unembedded": True,
            })
            continue
        best_cluster = None
        best_sim = -1.0
        for c in clusters:
            if c["centroid"] is None:
                continue
            sim = cosine(vec, c["centroid"])
            if sim >= threshold and sim > best_sim:
                best_sim = sim
                best_cluster = c
        if best_cluster is not None:
            best_cluster["post_indices"].append(i)
            # Update centroid
            member_vecs = [vectors[j] for j in best_cluster["post_indices"]]
            n = len(member_vecs)
            dim = len(member_vecs[0])
            new_centroid = [sum(v[d] for v in member_vecs) / n for d in range(dim)]
            best_cluster["centroid"] = new_centroid
            # Re-pick the most representative member
            best_dist = float("inf")
            best_member = best_cluster["post_indices"][0]
            for j in best_cluster["post_indices"]:
                d = 1.0 - cosine(vectors[j], new_centroid)
                if d < best_dist:
                    best_dist = d
                    best_member = j
            best_cluster["centroid_post_index"] = best_member
            best_cluster["max_sim_to_centroid"] = best_sim
        else:
            clusters.append({
                "id": len(clusters),
                "post_indices": [i],
                "centroid": list(vec),
                "centroid_post_index": i,
                "max_sim_to_centroid": 1.0,
                "unembedded": False,
            })
    return clusters


def cluster_score(cluster, posts, follower_counts=None, now_ts=None):
    """Score a cluster for ranking. Higher = more editorial weight.

    Formula: (1 + log2(size)) × mean_age_adjusted_engagement × log10(follower_centroid + 10)

    Size boost is logarithmic, not linear: 1-post = 1x, 5-post = 3.3x, 32-post = 6x.
    Prevents surface-keyword mega-clusters (e.g. a "Bitcoin" bucket where 30+ posts
    share one keyword) from dominating candidate selection. Gives good one-shot
    tweets a fair shot. v1 used `size × ...` which over-weighted large clusters.
    Changed 2026-07-03 (Herald phase 2.5: context expansion step 2a).

    Engagement = likes + 2*retweets + replies. Same as before.
    Age-adjusted: divided by hours since the post landed (min clamp 0.5h to
    avoid div-by-zero on freshly-published tweets). A 2h-old tweet with
    50 likes scores 100/h; a 12h-old tweet with 500 likes scores ~42/h.
    Phase 3.7.3 (2026-07-10): adds age adjustment so the ranker doesn't
    systematically over-rank older tweets that have had more time to
    accumulate engagement. Trade-off: a fast-rising 1h-old tweet with low
    absolute numbers now beats a stale 24h-old tweet that's flatlined.
    The actual engagement curve still matters, just normalised by age.

    Follower counts are looked up from posts.author.followers_count if not passed in.
    now_ts: optional epoch seconds for deterministic test scoring.
    """
    indices = cluster["post_indices"]
    if not indices:
        return 0.0
    size = len(indices)
    aa_engagement_sum = 0.0  # age-adjusted
    follower_sum = 0.0
    now = int(now_ts if now_ts is not None else time.time())
    for i in indices:
        p = posts[i]
        m = p.get("metrics") or {}
        raw_engagement = (
            (m.get("likes") or 0)
            + 2 * (m.get("retweets") or 0)
            + (m.get("replies") or 0)
        )
        # Age in hours since the post landed. Min clamp at 0.5h so a tweet
        # that was collected seconds after publication doesn't divide by
        # near-zero and explode the score.
        created_at = p.get("created_at") or now
        age_h = max(0.5, (now - int(created_at)) / 3600.0)
        aa_engagement_sum += raw_engagement / age_h
        fc = follower_counts.get(p.get("author", {}).get("id")) if follower_counts else None
        if fc is None:
            fc = (p.get("author") or {}).get("followers_count") or 0
        follower_sum += max(fc, 0)
    mean_aa_engagement = aa_engagement_sum / size if size else 0.0
    mean_followers = follower_sum / size if size else 1.0
    # log10(followers + 10) so an account with 1000 followers contributes ~3.0
    follower_weight = math.log10(mean_followers + 10)
    # Logarithmic size boost. Linear `size *` let a 32-post surface-keyword cluster
    # (e.g. "Bitcoin" mentions) outscore real one-shot tweets. 1 + log2(size) is
    # the same as the linear formula for size=1, but compresses the boost for
    # larger clusters: 1=1, 2=2, 3=2.6, 5=3.3, 15=4.9, 32=6.
    size_boost = 1.0 + math.log2(size) if size > 0 else 1.0
    # +1 floor on engagement (same as v1) so a 0-engagement post doesn't
    # collapse the score to 0. With age adjustment, an old 0-engagement
    # post now scores ~2/h; a fresh 0-engagement tweet scores 2/0.5 = 4/h.
    return size_boost * (mean_aa_engagement + 1) * follower_weight


def select_candidates(posts, vectors, max_candidates=40, max_clusters=None,
                      sim_threshold=DEFAULT_SIM_THRESHOLD):
    """Cluster posts, rank clusters, pick representatives.

    Returns (candidate_post_indices, clusters, embed_usage_dict).
    candidate_post_indices is a flat list of indices into `posts`.

    Strategy:
    1. Cluster by similarity.
    2. Rank clusters by cluster_score (size × engagement × followers).
    3. From top clusters (up to max_clusters), pick the centroid_post_index
       as representative. For clusters with >2 posts, also pick 1-2
       secondary members (most-engaged non-centroid post).
    4. Stop when we have max_candidates posts.
    """
    if not posts:
        return [], [], {}

    # Need to embed before clustering if vectors not provided
    if vectors is None:
        texts = [(p.get("text") or "")[:1500] for p in posts]
        vectors, usage = embed_texts(texts)
    else:
        usage = {}

    clusters = cluster_by_similarity(posts, vectors, threshold=sim_threshold)
    # Rank
    ranked = sorted(
        clusters,
        key=lambda c: cluster_score(c, posts),
        reverse=True,
    )
    if max_clusters:
        ranked = ranked[:max_clusters]

    selected = []
    seen = set()
    for c in ranked:
        centroid_idx = c["centroid_post_index"]
        if centroid_idx not in seen:
            selected.append(centroid_idx)
            seen.add(centroid_idx)
        # For clusters of 3+, also pick the most-engaged non-centroid post
        if len(c["post_indices"]) >= 3 and len(selected) < max_candidates:
            others = [i for i in c["post_indices"] if i != centroid_idx]
            if others:
                def eng(i):
                    m = posts[i].get("metrics") or {}
                    return (m.get("likes") or 0) + 2*(m.get("retweets") or 0) + (m.get("replies") or 0)
                best = max(others, key=eng)
                if best not in seen:
                    selected.append(best)
                    seen.add(best)
        if len(selected) >= max_candidates:
            break

    # If we still have budget, fill from remaining top-engagement singletons
    if len(selected) < max_candidates:
        remaining = sorted(
            [i for i in range(len(posts)) if i not in seen],
            key=lambda i: (
                (posts[i].get("metrics") or {}).get("likes") or 0
            ) + 2 * ((posts[i].get("metrics") or {}).get("retweets") or 0),
            reverse=True,
        )
        for i in remaining:
            if len(selected) >= max_candidates:
                break
            selected.append(i)
            seen.add(i)

    return selected, ranked, usage
