from dataclasses import dataclass

# The Live model every direction uses unless the UI picks another
# (guide/two_way_plan.md Phase 6b: models are compared without code edits).
DEFAULT_MODEL = "gemini-3.5-live-translate-preview"

# Display name -> model id, for the model combo in ui/live_interpreter.py.
# Every id here was listed with bidiGenerateContent for the dev key on 2026-10-06
# and accepted the translation config on connect. Accepting the config doesn't
# prove a model translates well: run the §7 checks and the §7.6 sheet per model.
LIVE_MODELS = {
    "Gemini 3.5 Live Translate (preview)": DEFAULT_MODEL,
    "Gemini 3.8 Live": "gemini-3.8-live",
    "Gemini 3.1 Flash Live (preview)": "gemini-3.1-flash-live-preview",
}


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
