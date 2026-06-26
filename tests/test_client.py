"""Status classification + the httpx client path. No network (MockTransport)."""

import json

import httpx
import pytest

from aoe_github_plugin import errors
from aoe_github_plugin.client import GitHubClient
from aoe_github_plugin.client import classify_status


def test_401_unauthorized():
    assert isinstance(classify_status(401, {}, ""), errors.UnauthorizedError)


def test_403_ratelimit_header():
    assert isinstance(classify_status(403, {"X-RateLimit-Remaining": "0"}, ""), errors.RateLimitedError)


def test_403_ratelimit_body():
    err = classify_status(403, {}, "API rate limit exceeded for ...")
    assert isinstance(err, errors.RateLimitedError)


def test_403_missing_scope_names_header():
    err = classify_status(403, {"X-Accepted-OAuth-Scopes": "repo, workflow"}, "")
    assert isinstance(err, errors.InsufficientScopeError)
    assert err.scopes == "repo, workflow"


def test_403_missing_scope_defaults_to_repo():
    err = classify_status(403, {}, "")
    assert isinstance(err, errors.InsufficientScopeError)
    assert err.scopes == "repo"


def test_429_rate_limited():
    assert isinstance(classify_status(429, {}, ""), errors.RateLimitedError)


def test_404_not_found_uses_body_message():
    err = classify_status(404, {}, json.dumps({"message": "Not Found"}))
    assert isinstance(err, errors.NotFoundError)
    assert err.resource == "Not Found"


def test_500_api_error():
    err = classify_status(500, {}, json.dumps({"message": "boom"}))
    assert isinstance(err, errors.ApiError)
    assert err.status == 500


def test_insufficient_scope_names_the_scope():
    assert "repo" in str(errors.InsufficientScopeError("repo"))


def test_unauthorized_mentions_reauthenticate():
    assert "re-authenticate" in str(errors.UnauthorizedError()).lower()


def test_network_does_not_suggest_reauthenticating():
    assert "re-authenticate" not in str(errors.NetworkError("refused")).lower()


def _client(handler):
    return GitHubClient(token="t", transport=httpx.MockTransport(handler))


def test_get_json_success_sends_bearer_token():
    def handler(request):
        assert request.headers["Authorization"] == "Bearer t"
        return httpx.Response(200, json=[{"number": 7}])

    with _client(handler) as client:
        assert client.get_json("/repos/o/r/pulls") == [{"number": 7}]


def test_get_json_403_missing_scope_maps_to_insufficient_scope():
    def handler(request):
        return httpx.Response(403, headers={"X-Accepted-OAuth-Scopes": "repo"}, json={"message": "x"})

    with _client(handler) as client, pytest.raises(errors.InsufficientScopeError):
        client.get_json("/x")


def test_get_json_403_rate_limit_maps_to_rate_limited():
    def handler(request):
        return httpx.Response(403, headers={"X-RateLimit-Remaining": "0"}, text="rate limit")

    with _client(handler) as client, pytest.raises(errors.RateLimitedError):
        client.get_json("/x")


def test_get_json_transport_error_maps_to_network():
    def handler(request):
        raise httpx.ConnectError("refused")

    with _client(handler) as client, pytest.raises(errors.NetworkError):
        client.get_json("/x")


def test_conditional_200_returns_etag_and_body():
    def handler(request):
        assert "If-None-Match" not in request.headers
        # params are encoded by httpx, not interpolated.
        assert request.url.params["head"] == "o:feat/x"
        return httpx.Response(200, headers={"ETag": 'W/"abc"'}, json=[{"number": 7}])

    with _client(handler) as client:
        status, etag, body = client.get_json_conditional("/repos/o/r/pulls", {"head": "o:feat/x"})
    assert status == 200
    assert etag == 'W/"abc"'
    assert body == [{"number": 7}]


def test_conditional_304_sends_if_none_match_and_returns_no_body():
    def handler(request):
        assert request.headers["If-None-Match"] == 'W/"abc"'
        return httpx.Response(304)

    with _client(handler) as client:
        status, etag, body = client.get_json_conditional("/x", etag='W/"abc"')
    assert status == 304
    assert etag == 'W/"abc"'
    assert body is None


def test_conditional_error_still_classifies():
    def handler(request):
        return httpx.Response(403, headers={"X-RateLimit-Remaining": "0"}, text="rate limit")

    with _client(handler) as client, pytest.raises(errors.RateLimitedError):
        client.get_json_conditional("/x")
