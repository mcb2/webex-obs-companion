from unittest.mock import Mock

import pytest
import requests

from webex_obs.webex_client import WebexClient


@pytest.mark.parametrize("outcome", [201, 500, requests.Timeout("synthetic timeout")])
def test_attachment_is_open_during_request_and_closed_after_all_outcomes(tmp_path, monkeypatch, outcome):
    transcript = tmp_path / "transcript.txt"
    transcript.write_text("Synthetic transcript. " * 400)
    handles = []

    def post(url, **kwargs):
        attachment = kwargs["files"]["files"][1]
        handles.append(attachment)
        assert not attachment.closed
        assert attachment.read() == transcript.read_bytes()
        assert kwargs["data"]["roomId"] == "synthetic-room"
        assert "toPersonEmail" not in kwargs["data"]
        if isinstance(outcome, Exception):
            raise outcome
        return Mock(status_code=outcome, text="synthetic response")

    monkeypatch.setattr("webex_obs.webex_client.requests.post", post)
    client = WebexClient("synthetic-token", recipient_email="person@example.com", room_id="synthetic-room")
    assert client.send_transcript(transcript) == (outcome == 201)
    assert len(handles) == 1
    assert handles[0].closed
    assert transcript.exists()


def test_short_transcript_uses_json_and_no_attachment(tmp_path, monkeypatch):
    transcript = tmp_path / "transcript.txt"
    transcript.write_text("Synthetic short transcript.")
    post = Mock(return_value=Mock(status_code=200))
    monkeypatch.setattr("webex_obs.webex_client.requests.post", post)
    assert WebexClient("synthetic-token", recipient_email="person@example.com").send_transcript(transcript)
    payload = post.call_args.kwargs
    assert payload["json"]["toPersonEmail"] == "person@example.com"
    assert "Synthetic short transcript." in payload["json"]["markdown"]
    assert "files" not in payload
