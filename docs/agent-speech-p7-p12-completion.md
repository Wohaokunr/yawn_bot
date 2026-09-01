# Agent Speech P7-P12 completion

This pass audits the original Speech Pipeline roadmap rather than later experimental numbering.

- **P7 topic judgement**: `topic_state.py` now owns `continue / shift / close`. A fresh active cluster keeps its semantic label instead of replacing `active_topic` with every raw user sentence. Existing proactive model topic output is reused when available; no extra model call is introduced.
- **P8 tool results**: each executed tool contributes bounded `SpeechEvidence`, and the final reply after any tool round is compiled as `scene=tool_result` before outbound delivery.
- **P9 proactive merge**: proactive/warmup/followup keep `SpeakDecision` / compatibility parsing, but user-visible content is converted to the same `SpeechPlan` and `prepare_speech_plan()` path as normal dialogue.
- **P10 trace**: `speech` is now a first-class execution phase showing action, scene, target, speech act, turn pressure, topic transition, Persona style, Emotion and quality checks.
- **P11 WebUI simulator**: the existing no-side-effect debug runner now exposes a dedicated `speechSimulation` result and a visible **发言模拟器** card. Model-off mode previews policy only; model-on mode shows final text/segments/quality without executing tools or sending QQ messages.
- **P12 cleanup**: reusable activity aggregation, context loading, send/persistence helpers and speech finalization leave `dialogue.py`. Thin compatibility names remain where existing tests/internal callers need them; new code imports the new modules directly.

No database migration or additional LLM request is added by this pass. OneBot validation and permission boundaries remain in `outbound.py` / tools.
