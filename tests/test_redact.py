import pytest

from flagbox.data.redact import (
    display_name,
    drop_contact_points,
    has_incorporation_marker,
    normalise_name,
    supplier_key,
)


@pytest.mark.parametrize(
    "name",
    [
        "Acme Furniture Ltd",
        "ACME FURNITURE LTD.",
        "Acme (K) Limited",
        "Acme Company Limited",
        "Acme Co. Ltd",
        "Acme PLC",
        "Acme Partners LLP",
        "Acme Inc.",
        "Acme Corporation",
    ],
)
def test_incorporation_markers_are_recognised(name):
    assert has_incorporation_marker(name)


@pytest.mark.parametrize(
    "name", ["JOHN FAKESON", "Mama Mboga Traders", "Unlimited Ventures", "Princeton", "Linc"]
)
def test_names_without_a_marker_are_not_recognised(name):
    assert not has_incorporation_marker(name)


def test_display_name_keeps_incorporated_names_and_tidies_spaces():
    assert display_name("Acme   Furniture Ltd ", "900") == ("Acme Furniture Ltd", False)


def test_display_name_withholds_everything_else():
    shown, withheld = display_name("JOHN FAKESON", "901")
    assert (shown, withheld) == ("Supplier 901 (name withheld)", True)
    assert "FAKESON" not in shown


@pytest.mark.parametrize(
    "name",
    ["G5 HAULER LIMITED TEL 0721160612", "Smart Ltd P.O BOX 0722745161-00100", "Ann@home Ltd"],
)
def test_display_name_withholds_names_that_contain_contact_details(name):
    shown, withheld = display_name(name, "7")
    assert withheld and "0721" not in shown and "@" not in shown


def test_display_name_keeps_short_numbers_in_company_names():
    assert display_name("Block 7 Holdings Ltd 2020", "8") == ("Block 7 Holdings Ltd 2020", False)


def test_display_name_withholds_a_missing_name():
    assert display_name(None, "902") == ("Supplier 902 (name withheld)", True)


def test_supplier_key_prefers_the_raw_id():
    assert supplier_key("900", "Acme Ltd") == "900"


def test_supplier_key_from_name_is_hashed_and_stable():
    key = supplier_key(None, "Mary  Nobody")
    assert key == supplier_key(None, "MARY NOBODY")
    assert key is not None and key.startswith("name:") and "mary" not in key.lower()
    assert len(key) == len("name:") + 12


def test_supplier_key_is_none_without_id_or_name():
    assert supplier_key(None, None) is None
    assert supplier_key("", "  ") is None


def test_normalise_name():
    assert normalise_name("  Acme,  Ltd. ") == "acme ltd"


def test_drop_contact_points_removes_them_at_any_depth_without_mutating_input():
    release = {
        "parties": [
            {"id": "1", "contactPoint": {"email": "a@example.test"}},
            {"id": "2", "extra": {"deep": [{"contactPoint": {"name": "X"}, "keep": 1}]}},
        ],
        "contactPoint": {"telephone": "1"},
    }
    cleaned = drop_contact_points(release)
    assert "contactPoint" not in str(cleaned)
    assert cleaned["parties"][1]["extra"]["deep"][0]["keep"] == 1
    assert "contactPoint" in release["parties"][0]  # original untouched
