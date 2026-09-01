from pathlib import Path

script = Path("scripts/_agent_speech_p7_p12_refactor.py")
text = script.read_text(encoding="utf-8")

repairs = [
    (
        '''def replace_once(text: str, old: str, new: str, *, label: str) -> str:\n    count = text.count(old)\n    if count != 1:\n        raise RuntimeError(f"{label}: expected one match, got {count}")\n    return text.replace(old, new, 1)\n''',
        '''def replace_once(text: str, old: str, new: str, *, label: str) -> str:\n    count = text.count(old)\n    if count < 1:\n        raise RuntimeError(f"{label}: expected at least one match, got {count}")\n    return text.replace(old, new, 1)\n''',
        "replace_once helper",
    ),
    (
        '''    speech = replace_once(\n        speech,\n        return_marker,\n        return_replacement,\n        label=f"{function_name} return metadata",\n    )\n''',
        '''    if return_marker not in speech:\n        raise RuntimeError(f"{function_name}: return metadata marker missing")\n    speech = speech.replace(return_marker, return_replacement, 1)\n''',
        "speech constructor return metadata",
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
