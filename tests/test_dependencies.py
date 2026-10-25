"""The database driver's dependencies still fit together.

scramp 1.5.0 removed make_channel_binding, which pg8000 1.31.5 calls on every
connection; for a day every job that touched the database failed. CI installs
requirements.txt, so this catches a bad combination on a branch, before the
live jobs install it.
"""

from __future__ import annotations


def test_pg8000_can_still_build_its_channel_binding():
    import scramp

    assert hasattr(scramp, "make_channel_binding"), (
        "scramp no longer has make_channel_binding: pg8000 cannot connect. Pin scramp in requirements.txt."
    )


def test_pg8000_and_scramp_are_pinned():
    from pathlib import Path

    pins = {line.split("==")[0].strip().lower() for line in Path("requirements.txt").read_text().splitlines()
            if "==" in line and not line.lstrip().startswith("#")}
    assert {"pg8000", "scramp", "asn1crypto"} <= pins
