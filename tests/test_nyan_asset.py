import json
from pathlib import Path
from tools.anim_encoder import parse_header
from tools.build_nyan_anim import FRAMES, H, W, render_frame, render_frames

ASSET = Path(__file__).resolve().parents[1] / "assets" / "nyan" / "nyan_72x16.anim"
META  = Path(__file__).resolve().parents[1] / "assets" / "nyan" / "meta.json"

def test_committed_asset_parses():
    h = parse_header(ASSET.read_bytes())
    assert h["magic"] == b"bicycle0"
    assert (h["width"], h["height"]) == (72, 16)
    assert h["color_mode"] == 0
    assert h["n_display"] == FRAMES
    assert h["fps"] == json.loads(META.read_text())["fps"]


def test_renderer_is_periodic_and_pixel_bounded():
    frames = render_frames()
    assert len(frames) == FRAMES == 24
    assert all(len(frame) == W * H for frame in frames)
    assert all(0 <= channel <= 255 for frame in frames for pixel in frame for channel in pixel)
    assert frames == render_frames()
    assert all(render_frame(t) == render_frame(t + FRAMES) for t in range(FRAMES))


def test_generated_frames_have_panel_dimensions():
    from PIL import Image

    frame_paths = sorted((ASSET.parent / "frames").glob("frame_*.png"),
                         key=lambda path: int(path.stem.split("_")[1]))
    assert len(frame_paths) == FRAMES
    for path in frame_paths:
        with Image.open(path) as image:
            assert image.size == (W, H)
