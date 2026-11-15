from __future__ import annotations

from seo_service.parse import parse_html


def test_images_inside_noscript_do_not_crash():
    html = (
        b"<html><head><title>t</title></head><body>"
        b'<img src="/a.jpg" alt="ok"><img src="/b.jpg">'
        b'<noscript><img src="/pixel.gif"></noscript>'
        b"</body></html>"
    )
    page, _ = parse_html(html, "utf-8", "https://example.ru/")
    assert page["images"] == 3
    assert page["images_no_alt"] == 2
