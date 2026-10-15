from dataclasses import dataclass

# The Live model every direction uses unless the UI picks another. The app is
# built around the dedicated translation model (developer decision 2026-10-15,
# replacing distribution plan decision 8's gemini-3.8-live).
DEFAULT_MODEL = "gemini-3.5-live-translate-preview"


@dataclass(frozen=True)
class LiveModel:
    id: str
    # "translate": a dedicated translation model that obeys translation_config.
    # "general": a voice assistant. It accepts translation_config on connect but
    # ignores it and answers the speaker instead (gemini-3.8-live, 2026-10-15).
    kind: str
    # Passed the translation checks (guide/Live_test_check.md). Release builds
    # offer only verified models; dev lists every model, for comparison.
    verified: bool = False


# Display name -> model, for the model combo in ui/live_interpreter.py. Every id
# here was listed with bidiGenerateContent for the dev key on 2026-10-06 and
# accepted the translation config on connect. Accepting the config doesn't
# prove a model translates: verify a model before marking it verified.
LIVE_MODELS = {
    "Gemini 3.5 Live Translate (preview)": LiveModel(DEFAULT_MODEL, "translate", verified=True),
    "Gemini 3.8 Live (general, doesn't translate)": LiveModel("gemini-3.8-live", "general"),
    "Gemini 3.1 Flash Live (general, preview)": LiveModel("gemini-3.1-flash-live-preview", "general"),
}


def offered_models(release: bool) -> dict[str, str]:
    """Display name -> model id for the model combo: verified models only in release."""
    return {name: model.id for name, model in LIVE_MODELS.items() if model.verified or not release}


@dataclass
class ResilienceConfig:
    """Tunables for engines/gemini_live.run_session's reconnect loop (spec §10.2)."""

    # Ask for resumption handles and context window compression. A model that
    # rejects them on the first connect is retried once without them.
    session_resumption: bool = True
    context_compression: bool = True
    # An unplanned outage longer than this fails the direction (NETWORK_LOST).
    # Spec §13.5 expects a 15 s internet drop to recover.
    reconnect_give_up_s: float = 60.0
    # Wait before reconnect attempt n (the last value repeats).
    reconnect_backoff_s: tuple[float, ...] = (0.0, 1.0, 2.0, 4.0)
    # A single reconnect attempt that takes longer than this is abandoned and retried.
    connect_timeout_s: float = 8.0
    # After a GoAway, the connection is renewed at the next turn end, or this long
    # before the server's deadline if no turn ends first.
    goaway_margin_s: float = 2.0
    # Newest captured audio kept while no connection is sending it, then sent
    # first after a reconnect, so speech that starts mid-reconnect isn't lost.
    # Bounded for freshness: older audio is dropped.
    preroll_ms: float = 3000.0
    # Stall watch: a connection that keeps transcribing speech (at least
    # stall_min_input_chars) but sends no translation back for stall_after_s is
    # stuck, and is replaced by a fresh session. Bug report 2026-10-15: B
    # transcribed for 4 minutes with no output and no turn end. 0 turns it off.
    stall_after_s: float = 20.0
    stall_min_input_chars: int = 40
