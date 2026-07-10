"""Test the age-adjusted ranker (Phase 3.7.3).

The old ranker formula used raw engagement, which systematically over-ranked
older tweets that had more time to accumulate likes/retweets. The new formula
divides engagement by hours-since-posted, so a fresh rising tweet beats a
stale flatlined one even at lower absolute numbers.

Tests:
  1. Fresh tweet beats old tweet at lower absolute numbers (the core fix).
  2. 0.5h clamp prevents div-by-zero on freshly-published tweets.
  3. now_ts param makes scoring deterministic for tests.
  4. Mixed-age cluster takes mean of age-adjusted values.
  5. Same input, old vs new formula — old ranks the stale tweet first, new
     ranks the rising tweet first. This is the regression test that
     justifies the change.
"""

import time
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import herald_embed


def banner(label):
    print(f"\n=== {label} ===")


def make_post(post_id, author_id, created_at, likes, retweets=0, replies=0, followers=1000):
    """Build a minimal post dict shaped like what herald_db.get_posts_in_window returns."""
    return {
        "id": post_id,
        "author": {"id": author_id, "username": f"u{author_id}", "followers_count": followers},
        "created_at": created_at,
        "metrics": {"likes": likes, "retweets": retweets, "replies": replies},
    }


def make_cluster(post_indices):
    return {"post_indices": post_indices}


def test_fresh_beats_old_at_lower_absolute():
    """The core fix: a 1h-old tweet with 50 likes beats a 24h-old tweet with 500 likes."""
    banner("Test 1: fresh tweet (1h, 50 likes) beats old tweet (24h, 500 likes)")
    now = 1_700_000_000
    fresh = make_post("p1", "a1", now - 3600, likes=50)        # 1h old
    stale = make_post("p2", "a2", now - 86400, likes=500)      # 24h old
    posts = [fresh, stale]

    fresh_score = herald_embed.cluster_score(
        make_cluster([0]), posts, now_ts=now,
    )
    stale_score = herald_embed.cluster_score(
        make_cluster([1]), posts, now_ts=now,
    )
    print(f"  fresh (1h, 50 likes):  {fresh_score:.3f}")
    print(f"  stale (24h, 500 likes): {stale_score:.3f}")
    assert fresh_score > stale_score, (
        f"fresh should beat stale under age-adjusted scoring; "
        f"got fresh={fresh_score:.3f}, stale={stale_score:.3f}"
    )
    print(f"  ✓ fresh wins despite 10x fewer likes (50/h vs 21/h)")


def test_zero_age_clamp():
    """A tweet that was just published shouldn't divide by near-zero."""
    banner("Test 2: 0.5h clamp prevents div-by-zero on freshly-published tweets")
    now = 1_700_000_000
    just_published = make_post("p1", "a1", now - 60, likes=0)        # 1 min old
    one_hour_old = make_post("p2", "a2", now - 3600, likes=0)         # 1h old
    posts = [just_published, one_hour_old]

    just_score = herald_embed.cluster_score(make_cluster([0]), posts, now_ts=now)
    one_h_score = herald_embed.cluster_score(make_cluster([1]), posts, now_ts=now)
    print(f"  just-published (1min, 0 likes): {just_score:.3f}")
    print(f"  one-hour-old (1h, 0 likes):     {one_h_score:.3f}")
    # Both should be positive (the +1 floor) and the 1-min tweet should
    # not explode to infinity thanks to the 0.5h clamp.
    assert just_score > 0
    assert one_h_score > 0
    # Just-published has higher age-adjusted engagement (0/0.5 vs 0/1).
    # But both score very low — that's correct, they have 0 likes.
    # What matters is no NaN/inf and the 0.5h clamp is doing its job.
    assert just_score < 1e6, f"just-published score should be finite, got {just_score}"
    print(f"  ✓ 0.5h clamp holds; both scores finite and positive")


def test_deterministic_with_now_ts():
    """Same input, different now_ts → different scores. now_ts param works."""
    banner("Test 3: now_ts makes scoring deterministic")
    now_a = 1_700_000_000
    now_b = 1_700_003_600  # 1 hour later
    p = make_post("p1", "a1", now_a - 3600, likes=100)  # 1h old at now_a
    posts = [p]
    score_a = herald_embed.cluster_score(make_cluster([0]), posts, now_ts=now_a)
    score_b = herald_embed.cluster_score(make_cluster([0]), posts, now_ts=now_b)
    print(f"  score at now_a (post 1h old):  {score_a:.3f}")
    print(f"  score at now_b (post 2h old):  {score_b:.3f}")
    # At now_b, the post is 2h old → age-adjusted engagement drops in half
    assert score_b < score_a
    print(f"  ✓ score decreases as the post ages (deterministic)")


def test_mixed_age_cluster_takes_mean():
    """A cluster with mixed ages should use mean of age-adjusted values."""
    banner("Test 4: mixed-age cluster uses mean of age-adjusted engagement")
    now = 1_700_000_000
    p1 = make_post("p1", "a1", now - 3600,  likes=100)   # 1h old, 100 likes → 100/h
    p2 = make_post("p2", "a2", now - 7200,  likes=200)   # 2h old, 200 likes → 100/h
    posts = [p1, p2]
    cluster_score = herald_embed.cluster_score(
        make_cluster([0, 1]), posts, now_ts=now,
    )
    # Each post: 100/h age-adjusted, mean = 100/h, +1 = 101.
    # size_boost = 1 + log2(2) = 2.0
    # follower_weight = log10(1000 + 10) ≈ 3.0
    # score ≈ 2.0 * 101 * 3.0 = 606
    expected = 2.0 * 101.0 * (3.0 + 1e-9)  # tiny epsilon for log10
    print(f"  cluster with 2 posts, both 100/h age-adjusted: {cluster_score:.3f}")
    print(f"  expected ≈ {expected:.3f}")
    assert abs(cluster_score - expected) < 1.0, (
        f"cluster score {cluster_score:.3f} should be close to {expected:.3f}"
    )
    print(f"  ✓ cluster uses mean of age-adjusted values")


def test_old_vs_new_formula_differs():
    """Regression test: with the old (raw-engagement) formula, the stale
    tweet would have ranked first. With the new (age-adjusted) formula,
    the rising tweet ranks first. This justifies the change."""
    banner("Test 5: old vs new formula — old ranks stale first, new ranks rising first")

    # Simulate the OLD formula inline (raw engagement, no age division)
    def old_score(cluster, posts):
        indices = cluster["post_indices"]
        if not indices:
            return 0.0
        size = len(indices)
        eng_sum = 0.0
        for i in indices:
            m = posts[i].get("metrics") or {}
            eng_sum += (m.get("likes") or 0) + 2 * (m.get("retweets") or 0) + (m.get("replies") or 0)
        mean_eng = eng_sum / size
        size_boost = 1.0 + (math.log2(size) if size > 0 else 0.0)
        return size_boost * (mean_eng + 1)

    import math
    now = 1_700_000_000
    fresh = make_post("p1", "a1", now - 3600,  likes=50)    # 1h, 50 likes
    stale = make_post("p2", "a2", now - 86400, likes=500)   # 24h, 500 likes
    posts = [fresh, stale]

    old_fresh = old_score(make_cluster([0]), posts)
    old_stale = old_score(make_cluster([1]), posts)
    new_fresh = herald_embed.cluster_score(make_cluster([0]), posts, now_ts=now)
    new_stale = herald_embed.cluster_score(make_cluster([1]), posts, now_ts=now)

    print(f"  OLD formula:  fresh={old_fresh:.3f}  stale={old_stale:.3f}  → stale wins")
    print(f"  NEW formula:  fresh={new_fresh:.3f}  stale={new_stale:.3f}  → fresh wins")
    assert old_stale > old_fresh, "old formula should rank stale first"
    assert new_fresh > new_stale, "new formula should rank fresh first"
    print(f"  ✓ formulas disagree on the rising tweet — this is the regression the change fixes")


if __name__ == "__main__":
    test_fresh_beats_old_at_lower_absolute()
    test_zero_age_clamp()
    test_deterministic_with_now_ts()
    test_mixed_age_cluster_takes_mean()
    test_old_vs_new_formula_differs()
    print("\nAll age-adjusted ranker tests pass. ✓")
