from arbengine.normalizer import canonical_name


def test_canonical_name():
    assert canonical_name("  Internazionale FC  ") == "internazionale fc"
    assert canonical_name("Paris-Saint Germain") == "paris saint germain"
