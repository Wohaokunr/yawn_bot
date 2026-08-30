from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_runtime_image_provides_cjk_font_for_htmlkit_cards() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    panel_css = (
        REPO_ROOT / "src" / "plugins" / "yawn_core" / "templates" / "panel.css"
    ).read_text(encoding="utf-8")

    assert "fonts-wqy-microhei" in dockerfile
    assert "fontconfig" in dockerfile
    assert "fc-cache -f" in dockerfile
    assert 'font-family: "WenQuanYi Micro Hei"' in panel_css
