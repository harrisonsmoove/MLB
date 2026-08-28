"""Plate-discipline proxies as a strikeout prior.

Stuff+ is proprietary, so the substitute is what it summarises: swings and
misses, and strikes taken. The value is timing -- every pitch informs a whiff
rate while only the last pitch of a plate appearance informs a strikeout rate,
so the proxy is meaningful months before the outcome it predicts.

What has to be true: it recovers a real relationship when one exists, and it
declines to be used when one does not. The second is the more important of the
two, because a noisy regression fit substituted for a well-estimated league
cell would make the projection worse while looking more sophisticated.
"""

from __future__ import annotations

import numpy as np

from mlb_edge.features.pitcher_proxies import fit_proxy_model


def _pitchers(n, *, seed, signal=True, trials=(250, 700)):
    """Pitchers whose strikeout rate does or does not follow their whiff rate."""
    rng = np.random.default_rng(seed)
    whiff = rng.normal(0.24, 0.05, n).clip(0.08, 0.45)
    csw = 0.5 * whiff + rng.normal(0.18, 0.02, n)
    counts = rng.integers(*trials, n).astype(float)
    if signal:
        k_rate = (0.02 + 0.85 * whiff + rng.normal(0, 0.012, n)).clip(0.05, 0.45)
    else:
        k_rate = rng.normal(0.22, 0.03, n).clip(0.05, 0.45)
    return list(zip(whiff, csw, k_rate, counts, strict=True))


def test_recovers_a_real_relationship():
    model = fit_proxy_model(_pitchers(300, seed=1, signal=True))
    assert model.usable
    assert model.r_squared > 0.7, f"r2={model.r_squared:.3f} on a strong built-in signal"

    # A high-whiff pitcher must project a higher strikeout rate than a low-whiff one.
    high = model.predict(0.35, 0.36)
    low = model.predict(0.12, 0.24)
    assert high is not None and low is not None
    assert high > low + 0.10, f"high={high:.3f} low={low:.3f}"


def test_declines_to_be_used_when_there_is_no_signal():
    """A map that explains nothing is worse than no map.

    It would replace a well-estimated league cell with a noisy regression fit.
    """
    model = fit_proxy_model(_pitchers(300, seed=2, signal=False))
    assert not model.usable
    assert model.predict(0.35, 0.36) is None


def test_too_few_pitchers_falls_back_to_the_league_rate():
    model = fit_proxy_model(_pitchers(12, seed=3))
    assert not model.usable
    assert model.whiff_coefficient == 0.0
    assert 0.0 < model.league_k_rate < 1.0


def test_predictions_are_clamped_to_a_plausible_range():
    """A linear map extrapolates nonsense at the extremes.

    An unclamped negative prior would corrupt the entire multinomial, since the
    other buckets are rescaled around it.
    """
    model = fit_proxy_model(_pitchers(300, seed=4, signal=True))
    assert model.usable
    for whiff, csw in ((0.0, 0.0), (1.0, 1.0), (-5.0, -5.0), (10.0, 10.0)):
        prediction = model.predict(whiff, csw)
        assert prediction is not None
        assert 0.02 <= prediction <= 0.60, f"({whiff}, {csw}) -> {prediction}"


def test_missing_proxy_inputs_return_nothing():
    model = fit_proxy_model(_pitchers(300, seed=5, signal=True))
    assert model.predict(None, 0.3) is None
    assert model.predict(0.3, None) is None


def test_low_workload_pitchers_do_not_set_the_relationship():
    """A pitcher with 30 batters faced should not shape the map."""
    real = _pitchers(200, seed=6, signal=True, trials=(300, 700))
    noise = [(0.4, 0.4, 0.05, 25.0)] * 400  # many, tiny, and contradictory
    model = fit_proxy_model(real + noise, min_trials=200.0)
    assert model.n_pitchers == 200
    assert model.usable


def test_projector_leaves_the_prior_alone_when_the_proxy_is_useless(request):
    """End to end: no discipline signal means no '+proxy' prior label."""
    import sys
    from datetime import date, timedelta

    sys.path.insert(0, str(request.config.rootpath / "tests"))
    from synthetic import make_players, simulate_pa_outcomes

    from mlb_edge.config import load_settings
    from mlb_edge.features.ratings import Projector
    from mlb_edge.storage.warehouse import Warehouse

    rng = np.random.default_rng(31)
    players = make_players(120, rng=rng)
    frame = simulate_pa_outcomes(players, rng=rng, season_start=date(2024, 4, 1))

    warehouse = Warehouse.in_memory()
    warehouse.load("pa_outcomes", frame)
    rates, report = Projector(load_settings(request.config.rootpath), warehouse).build(
        date(2024, 4, 1) + timedelta(days=200), player_type="pitcher"
    )

    # The generator gives every PA identical pitch counts, so there is nothing
    # to learn and the model must say so rather than fitting noise.
    assert report.proxy is not None
    assert not report.proxy.usable
    assert not any("+proxy" in cell for cell in rates["prior_cell"].to_list())
    warehouse.close()
