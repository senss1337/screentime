from screentime.sources.biome_local import _merge, subtract


def test_subtract_cuts_gaps_out_of_an_interval():
    gaps = _merge([(20.0, 30.0), (50.0, 60.0), (55.0, 70.0)])
    starts = [g[0] for g in gaps]
    assert gaps == [(20.0, 30.0), (50.0, 70.0)]
    assert subtract(10.0, 80.0, gaps, starts) == [(10.0, 20.0), (30.0, 50.0), (70.0, 80.0)]
    assert subtract(22.0, 28.0, gaps, starts) == []
    assert subtract(25.0, 40.0, gaps, starts) == [(30.0, 40.0)]
    assert subtract(0.0, 5.0, gaps, starts) == [(0.0, 5.0)]
    assert subtract(0.0, 5.0, [], []) == [(0.0, 5.0)]
