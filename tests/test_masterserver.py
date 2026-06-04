"""Tests for master-server resolution (aotbot.masterserver)."""

import io

import pytest

from aotbot import masterserver
from aotbot.masterserver import (
    MasterServerError,
    parse_server_txt,
    fetch_server_host,
)

# The exact body the live master server returns.
SAMPLE = "IP 45.148.165.55\nNAME AoT Server\nPLAYERS ?/30\n"


def test_parse_server_txt_fields():
    fields = parse_server_txt(SAMPLE)
    assert fields["IP"] == "45.148.165.55"
    assert fields["NAME"] == "AoT Server"   # value keeps its internal space
    assert fields["PLAYERS"] == "?/30"


def test_parse_tolerates_blanks_and_case():
    fields = parse_server_txt("\n  ip 1.2.3.4  \n\nFOO\n")
    assert fields["IP"] == "1.2.3.4"        # key upper-cased, value trimmed
    assert fields["FOO"] == ""              # valueless line tolerated


def test_fetch_server_host_parses_ip(monkeypatch):
    def fake_urlopen(req, timeout=0):
        # fetch passes a Request with a non-default User-Agent (avoids 403).
        assert req.full_url == "https://master.ageoftime.com/server.txt"
        assert "aot-headless-client" in req.get_header("User-agent", "")
        return io.BytesIO(SAMPLE.encode())

    monkeypatch.setattr(masterserver.urllib.request, "urlopen", fake_urlopen)
    assert fetch_server_host() == "45.148.165.55"


def test_fetch_rejects_non_http_scheme():
    with pytest.raises(MasterServerError):
        fetch_server_host("file:///etc/passwd")


def test_fetch_raises_when_no_ip(monkeypatch):
    monkeypatch.setattr(
        masterserver.urllib.request, "urlopen",
        lambda url, timeout=0: io.BytesIO(b"NAME AoT Server\nPLAYERS 0/30\n"),
    )
    with pytest.raises(MasterServerError):
        fetch_server_host("https://example.com/s.txt")


def test_fetch_wraps_network_error(monkeypatch):
    def boom(url, timeout=0):
        raise OSError("connection refused")

    monkeypatch.setattr(masterserver.urllib.request, "urlopen", boom)
    with pytest.raises(MasterServerError):
        fetch_server_host("https://example.com/s.txt")
