from dataclasses import dataclass, field

from config.audio_config import AudioDeviceConfig, AudioPipelineConfig

# Direction labels. A = candidate -> staff (target English), B = staff -> candidate.
# The echo detector uses them to tell local echo (A's output on B's input) from
# remote echo (B's output coming back on A's input).
CANDIDATE_TO_STAFF = "A"
STAFF_TO_CANDIDATE = "B"
# User-facing direction names (spec §8.2 wording), e.g. for the degraded-mode message.
DIRECTION_NAMES = {CANDIDATE_TO_STAFF: "Candidate-to-staff", STAFF_TO_CANDIDATE: "Staff-to-candidate"}

# Spec: "Staff hears" is locked to English in V1, but kept as config rather than hard-coded.
STAFF_OUTPUT_LANGUAGE = "en"


@dataclass
class DirectionConfig:
    """One translation direction: capture device -> Gemini -> playback device.
    Loop prevention (audio/routing_check.py, audio/health.py) works on a list of
    these, so the one-way app passes one and two-way passes two."""

    label: str
    capture: AudioDeviceConfig
    playback: AudioDeviceConfig
    target_language_code: str
    # Voice audio that's already in the target language too ("parrot")? A must be
    # True: the candidate may mix English in and staff must hear all of it. B can be
    # False: staff always speak English, and False makes B's own echo die after one pass.
    echo_target_language: bool = True
    pipeline: AudioPipelineConfig = field(default_factory=AudioPipelineConfig)


@dataclass
class TwoWayConfig:
    """Both directions of an interview call.
    A: WhatsApp incoming cable -> headset playback, translated into STAFF_OUTPUT_LANGUAGE.
    B: headset microphone -> WhatsApp outgoing cable, translated into the candidate's language."""

    candidate_to_staff: DirectionConfig   # direction A
    staff_to_candidate: DirectionConfig   # direction B

    def directions(self) -> list[DirectionConfig]:
        return [self.candidate_to_staff, self.staff_to_candidate]
