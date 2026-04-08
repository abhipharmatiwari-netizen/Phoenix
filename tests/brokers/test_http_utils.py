from app.brokers.http_utils import is_html_block


def test_is_html_block_detects_request_rejected():
    assert is_html_block("<html><body>Request Rejected</body></html>", None) is True
