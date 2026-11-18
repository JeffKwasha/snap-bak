"""SPEC.md and INDEX.md must name exactly the same requirements."""

from taxonomy import counts


def test_spec_and_index_name_the_same_requirements():
    assert counts()["spec_index_agree"]
