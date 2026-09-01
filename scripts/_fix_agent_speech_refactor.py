from pathlib import Path

script = Path("scripts/_agent_speech_p7_p12_refactor.py")
text = script.read_text(encoding="utf-8")

repairs = [
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
]

for old, new, label in repairs:
    if old not in text:
        raise RuntimeError(f"target refactor-script block not found: {label}")
    text = text.replace(old, new, 1)

script.write_text(text, encoding="utf-8")
Path(__file__).unlink(missing_ok=True)
