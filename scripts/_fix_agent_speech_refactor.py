from pathlib import Path

script = Path("scripts/_agent_speech_p7_p12_refactor.py")
text = script.read_text(encoding="utf-8")

constructor_old = '''for function_name in ("speech_plan_from_text", "speech_plan_from_segments"):\n    marker = '    reason: str = "",\\n    confidence: float = 1.0,\\n) -> SpeechPlan:\\n'\n    replacement = (\n        '    reason: str = "",\\n'\n        '    confidence: float = 1.0,\\n'\n        '    action: str = "speak",\\n'\n        '    act: str = "continue",\\n'\n        '    turn_pressure: str = "low",\\n'\n        '    topic: str | None = None,\\n'\n        '    topic_action: str = "continue",\\n'\n        ') -> SpeechPlan:\\n'\n    )\n    before_count = speech.count(marker)\n    if before_count < 1:\n        raise RuntimeError(f"{function_name}: signature marker missing")\n    speech = speech.replace(marker, replacement, 1)\n    return_marker = (\n        '        reason=str(reason or "")[:240],\\n'\n        '        confidence=max(0.0, min(float(confidence), 1.0)),\\n'\n    )\n    return_replacement = (\n        '        reason=str(reason or "")[:240],\\n'\n        '        confidence=max(0.0, min(float(confidence), 1.0)),\\n'\n        '        action=str(action or "speak").strip().lower() or "speak",\\n'\n        '        act=str(act or "continue").strip().lower() or "continue",\\n'\n        '        turn_pressure=str(turn_pressure or "low").strip().lower() or "low",\\n'\n        '        topic=str(topic or "").strip()[:240] or None,\\n'\n        '        topic_action=str(topic_action or "continue").strip().lower() or "continue",\\n'\n    )\n    speech = replace_once(\n        speech,\n        return_marker,\n        return_replacement,\n        label=f"{function_name} return metadata",\n    )\nwrite(speech_path, speech)\n'''
constructor_new = '''_speech_signature_marker = '    reason: str = "",\\n    confidence: float = 1.0,\\n) -> SpeechPlan:\\n'\n_speech_signature_replacement = (\n    '    reason: str = "",\\n'\n    '    confidence: float = 1.0,\\n'\n    '    action: str = "speak",\\n'\n    '    act: str = "continue",\\n'\n    '    turn_pressure: str = "low",\\n'\n    '    topic: str | None = None,\\n'\n    '    topic_action: str = "continue",\\n'\n    ') -> SpeechPlan:\\n'\n)\nif speech.count(_speech_signature_marker) != 2:\n    raise RuntimeError("speech constructors: expected two signature markers")\nspeech = speech.replace(_speech_signature_marker, _speech_signature_replacement, 2)\n_return_marker = (\n    '        reason=str(reason or "")[:240],\\n'\n    '        confidence=max(0.0, min(float(confidence), 1.0)),\\n'\n)\n_return_replacement = (\n    '        reason=str(reason or "")[:240],\\n'\n    '        confidence=max(0.0, min(float(confidence), 1.0)),\\n'\n    '        action=str(action or "speak").strip().lower() or "speak",\\n'\n    '        act=str(act or "continue").strip().lower() or "continue",\\n'\n    '        turn_pressure=str(turn_pressure or "low").strip().lower() or "low",\\n'\n    '        topic=str(topic or "").strip()[:240] or None,\\n'\n    '        topic_action=str(topic_action or "continue").strip().lower() or "continue",\\n'\n)\nif speech.count(_return_marker) != 2:\n    raise RuntimeError("speech constructors: expected two return metadata markers")\nspeech = speech.replace(_return_marker, _return_replacement, 2)\nwrite(speech_path, speech)\n'''
if constructor_old not in text:
    raise RuntimeError("speech constructor refactor block not found")
text = text.replace(constructor_old, constructor_new, 1)

repairs = [
    (
        '''def replace_once(text: str, old: str, new: str, *, label: str) -> str:\n    count = text.count(old)\n    if count != 1:\n        raise RuntimeError(f"{label}: expected one match, got {count}")\n    return text.replace(old, new, 1)\n''',
        '''def replace_once(text: str, old: str, new: str, *, label: str) -> str:\n    count = text.count(old)\n    if count < 1:\n        raise RuntimeError(f"{label}: expected at least one match, got {count}")\n    return text.replace(old, new, 1)\n''',
        "replace_once helper",
    ),
    (
        '''outbound = replace_once(\n    outbound,\n    "    speech_autofix: bool = True,\\n) -> PreparedOutboundMessage:\\n",\n    "    speech_autofix: bool = True,\\n    trace_speech: bool = True,\\n) -> PreparedOutboundMessage:\\n",\n    label="prepare_text trace flag",\n)\n''',
        '''_prepare_text_signature = "    speech_autofix: bool = True,\\n) -> PreparedOutboundMessage:\\n"\nif _prepare_text_signature not in outbound:\n    raise RuntimeError("prepare_text trace flag marker missing")\noutbound = outbound.replace(\n    _prepare_text_signature,\n    "    speech_autofix: bool = True,\\n    trace_speech: bool = True,\\n) -> PreparedOutboundMessage:\\n",\n    1,\n)\n''',
        "prepare_text trace flag",
    ),
    (
        '''outbound = replace_once(\n    outbound,\n    "    speech_autofix: bool = False,\\n) -> PreparedOutboundMessage:\\n",\n    "    speech_autofix: bool = False,\\n    trace_speech: bool = True,\\n) -> PreparedOutboundMessage:\\n",\n    label="prepare_segments trace flag",\n)\n''',
        '''_prepare_segments_signature = "    speech_autofix: bool = False,\\n) -> PreparedOutboundMessage:\\n"\nif _prepare_segments_signature not in outbound:\n    raise RuntimeError("prepare_segments trace flag marker missing")\noutbound = outbound.replace(\n    _prepare_segments_signature,\n    "    speech_autofix: bool = False,\\n    trace_speech: bool = True,\\n) -> PreparedOutboundMessage:\\n",\n    1,\n)\n''',
        "prepare_segments trace flag",
    ),
    (
        '''proactive = replace_once(\n    proactive,\n    "            history_text = decision.history_text\\n",\n    "            history_text = speech_plan.visible_text or decision.history_text\\n",\n    label="proactive history from plan",\n)\n''',
        '''_history_marker = "            history_text = decision.history_text\\n"\nif proactive.count(_history_marker) < 2:\n    raise RuntimeError("proactive/followup history markers missing")\nproactive = proactive.replace(\n    _history_marker,\n    "            history_text = speech_plan.visible_text or decision.history_text\\n",\n    2,\n)\n''',
        "proactive/followup history from plan",
    ),
]

for old, new, label in repairs:
    if old not in text:
        raise RuntimeError(f"target refactor-script block not found: {label}")
    text = text.replace(old, new, 1)

script.write_text(text, encoding="utf-8")
Path(__file__).unlink(missing_ok=True)
