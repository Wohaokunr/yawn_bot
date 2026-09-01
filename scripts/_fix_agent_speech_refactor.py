from pathlib import Path

script = Path("scripts/_agent_speech_p7_p12_refactor.py")
text = script.read_text(encoding="utf-8")
old = '''    speech = replace_once(\n        speech,\n        return_marker,\n        return_replacement,\n        label=f"{function_name} return metadata",\n    )\n'''
new = '''    if return_marker not in speech:\n        raise RuntimeError(f"{function_name}: return metadata marker missing")\n    speech = speech.replace(return_marker, return_replacement, 1)\n'''
if old not in text:
    raise RuntimeError("target refactor-script block not found")
script.write_text(text.replace(old, new, 1), encoding="utf-8")
Path(__file__).unlink(missing_ok=True)
