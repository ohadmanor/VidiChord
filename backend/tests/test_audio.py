"""Stage 1 - where the audio comes from, and how requests are identified.

YouTube refuses anonymous requests for audio, so these cover the three places
a cookie jar may come from and the guidance the app gives when there is none.
"""

import pytest

from vidichord.config import Settings
from vidichord.pipeline import stage1_audio


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """No cookies anywhere: no environment, no jar beside the app."""
    monkeypatch.delenv("VIDICHORD_COOKIES", raising=False)
    monkeypatch.delenv("VIDICHORD_COOKIES_BROWSER", raising=False)
    monkeypatch.setattr(stage1_audio, "DATA_DIR", tmp_path)
    return tmp_path


def test_no_cookies_configured_means_no_options(isolated):
    assert stage1_audio.cookie_options(Settings(library_dir=isolated)) == {}


def test_a_jar_beside_the_app_is_found_without_configuration(isolated):
    jar = isolated / stage1_audio.COOKIE_FILENAME
    jar.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")

    assert stage1_audio.cookie_options(None) == {"cookiefile": str(jar)}


def test_the_configured_file_wins_over_the_one_beside_the_app(isolated):
    (isolated / stage1_audio.COOKIE_FILENAME).write_text("# beside\n", encoding="utf-8")
    chosen = isolated / "chosen.txt"
    chosen.write_text("# chosen\n", encoding="utf-8")

    settings = Settings(library_dir=isolated, cookies_file=chosen)
    assert stage1_audio.cookie_options(settings) == {"cookiefile": str(chosen)}


def test_the_environment_is_honoured(isolated, monkeypatch):
    jar = isolated / "from_env.txt"
    jar.write_text("# env\n", encoding="utf-8")
    monkeypatch.setenv("VIDICHORD_COOKIES", str(jar))

    assert stage1_audio.cookie_options(None) == {"cookiefile": str(jar)}


def test_a_configured_file_that_does_not_exist_is_ignored(isolated):
    settings = Settings(library_dir=isolated, cookies_file=isolated / "absent.txt")
    assert stage1_audio.cookie_options(settings) == {}


def test_a_browser_can_be_named_instead(isolated):
    settings = Settings(library_dir=isolated, cookies_browser="firefox")
    assert stage1_audio.cookie_options(settings) == {
        "cookiesfrombrowser": ("firefox", None, None, None)
    }


def test_a_browser_profile_can_be_named_too(isolated):
    settings = Settings(library_dir=isolated, cookies_browser="Chrome:Profile 1")
    assert stage1_audio.cookie_options(settings) == {
        "cookiesfrombrowser": ("chrome", "Profile 1", None, None)
    }


def test_a_jar_beats_a_named_browser(isolated):
    jar = isolated / stage1_audio.COOKIE_FILENAME
    jar.write_text("# beside\n", encoding="utf-8")

    settings = Settings(library_dir=isolated, cookies_browser="firefox")
    assert stage1_audio.cookie_options(settings) == {"cookiefile": str(jar)}


@pytest.mark.parametrize(
    "refusal",
    [
        "ERROR: [youtube] abc: Sign in to confirm you’re not a bot. Use --cookies",
        "ERROR: [youtube] abc: HTTP Error 429: Too Many Requests",
        "WARNING: abc: mweb client https formats require a GVS PO Token",
        "ERROR: [youtube] abc: Requested format is not available.",
    ],
)
def test_a_refusal_is_explained_with_the_cookie_remedy(isolated, refusal):
    message = stage1_audio.explain_failure(RuntimeError(refusal))

    assert message.startswith(stage1_audio.HEADLINE)
    assert stage1_audio.COOKIE_FILENAME in message
    assert "cookies_browser" in message


def test_the_explanation_changes_once_cookies_are_in_use(isolated):
    (isolated / stage1_audio.COOKIE_FILENAME).write_text("# beside\n", encoding="utf-8")

    message = stage1_audio.explain_failure(RuntimeError("Sign in to confirm you're not a bot"))

    assert message.startswith(stage1_audio.HEADLINE)
    assert "expired" in message


def test_a_browser_that_cannot_be_read_is_explained_too(isolated):
    message = stage1_audio.explain_failure(
        RuntimeError("ERROR: Failed to decrypt with DPAPI")
    )

    assert message.startswith(stage1_audio.HEADLINE)
    assert "firefox" in message


def test_yt_dlp_output_that_was_captured_rather_than_printed_still_counts(isolated):
    # yt-dlp's own warnings explain why no audio format came back; the refusal
    # has to be recognised from them even when the exception is vague.
    message = stage1_audio.explain_failure(
        RuntimeError("ERROR: unable to download video data"),
        None,
        "WARNING: mweb client https formats require a GVS PO Token",
    )

    assert message.startswith(stage1_audio.HEADLINE)


def test_an_unrelated_failure_is_passed_through_untouched(isolated):
    assert stage1_audio.explain_failure(RuntimeError("No space left on device")) == (
        "No space left on device"
    )


def test_cookie_configuration_survives_a_save(tmp_path):
    settings = Settings(
        library_dir=tmp_path,
        cookies_browser="firefox",
        cookies_file=tmp_path / "jar.txt",
        path=tmp_path / "config.json",
    )
    settings.save()

    restored = Settings.load(tmp_path / "config.json")
    assert restored.cookies_browser == "firefox"
    assert restored.cookies_file == tmp_path / "jar.txt"


# -- JavaScript runtimes ----------------------------------------------------
#
# Streaming URLs are signed, and answering the challenge means running the
# player's own JavaScript in a real engine.


def test_every_supported_javascript_engine_is_offered():
    """yt-dlp enables only Deno by default, and Node is what machines have."""
    runtimes = stage1_audio.js_runtime_options()["js_runtimes"]

    assert "node" in runtimes
    assert "deno" in runtimes
    # yt-dlp wants a config mapping per engine, empty meaning "defaults".
    assert all(config == {} for config in runtimes.values())


def test_the_engines_named_are_ones_yt_dlp_knows():
    """A name yt-dlp does not recognise makes it raise rather than shrug."""
    from yt_dlp.globals import supported_js_runtimes

    offered = set(stage1_audio.js_runtime_options()["js_runtimes"])
    assert offered <= set(supported_js_runtimes.value)


# -- retrying a refused download --------------------------------------------


@pytest.mark.parametrize(
    "refusal",
    [
        "ERROR: unable to download video data: HTTP Error 403: Forbidden",
        "HTTP Error 403: Forbidden",
    ],
)
def test_a_bare_403_is_worth_retrying(refusal):
    """Google's media hosts refuse a large share of requests for no reason."""
    assert stage1_audio._is_transient(RuntimeError(refusal))


@pytest.mark.parametrize(
    "refusal",
    [
        "Sign in to confirm you're not a bot",
        "HTTP Error 429: Too Many Requests",
        "Requested format is not available",
    ],
)
def test_a_refusal_that_will_not_pass_is_not_retried(refusal):
    """Retrying these only delays advice the user has to act on."""
    assert not stage1_audio._is_transient(RuntimeError(refusal))


def test_an_unrelated_failure_is_not_retried():
    assert not stage1_audio._is_transient(RuntimeError("No space left on device"))


# -- what the user is told about a missing JavaScript engine -----------------


def test_yt_dlps_javascript_advice_is_replaced_with_our_own():
    """Its warning names a command-line flag for a tool the user never ran."""
    seen = []
    logger = stage1_audio._ProgressLogger(lambda message, percent: seen.append(message))

    logger.warning(
        "No supported JavaScript runtime could be found. Only deno is enabled "
        "by default; to use another runtime add --js-runtimes RUNTIME[:PATH] "
        "to your command/config."
    )

    assert seen == [stage1_audio.NO_JS_ENGINE]
    assert "--js-runtimes" not in seen[0]


def test_a_failed_signature_gets_the_same_answer():
    seen = []
    logger = stage1_audio._ProgressLogger(lambda message, percent: seen.append(message))

    logger.warning("Signature solving failed: Some formats may be missing.")

    assert seen == [stage1_audio.NO_JS_ENGINE]


def test_an_ordinary_warning_still_comes_through():
    seen = []
    logger = stage1_audio._ProgressLogger(lambda message, percent: seen.append(message))

    logger.warning("Falling back to generic extractor")

    assert seen == ["Warning: Falling back to generic extractor"]
