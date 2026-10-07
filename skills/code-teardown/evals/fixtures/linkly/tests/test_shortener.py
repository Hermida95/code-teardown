from linkly.shortener import is_valid_url, make_code


def test_valid_urls():
    assert is_valid_url("https://example.com/a")
    assert not is_valid_url("javascript:alert(1)")


def test_code_is_stable_and_sized():
    assert make_code("https://example.com") == make_code("https://example.com")
    assert len(make_code("https://example.com", 8)) == 8
