import analyze_image as ai
import builders as b


def run(tmp_path, data: bytes, name: str = "photo.bin") -> dict:
    path = tmp_path / name
    path.write_bytes(data)
    return ai.analyze(path)


def by_id(result: dict) -> dict:
    return {e["id"]: e for e in result["evidence"]}


def test_a1111_parameters_in_png_text_chunk_is_conclusive(tmp_path):
    params = "a cat\nNegative prompt: blurry\nSteps: 20, Sampler: Euler a, CFG scale: 7, Seed: 1"
    found = by_id(run(tmp_path, b.png(text={"parameters": params}), "x.png"))
    assert found["sd-parameters"]["score"] == 10
    assert found["sd-parameters"]["weight"] >= ai.CONCLUSIVE


def test_parameters_inside_compressed_chunk_are_found(tmp_path):
    params = "Steps: 30, Sampler: DPM++, CFG scale: 5"
    assert "sd-parameters" in by_id(run(tmp_path, b.png(ztxt={"parameters": params}), "x.png"))


def test_comfyui_graph_midjourney_job_and_swarm(tmp_path):
    comfy = '{"3": {"class_type": "KSampler", "inputs": {"seed": 1}}}'
    assert "comfyui-graph" in by_id(run(tmp_path, b.png(text={"prompt": comfy}), "a.png"))
    mj = "a fox --ar 16:9 --v 6\nJob ID: 0b1f2c3d-aaaa-bbbb-cccc-1234567890ab"
    assert "midjourney-job" in by_id(run(tmp_path, b.png(itxt={"Description": mj}), "b.png"))
    assert "swarmui-params" in by_id(run(tmp_path, b.png(text={"parameters": '{"sui_image_params": {}}'}), "c.png"))


def test_generator_named_in_free_text_is_strong_but_not_conclusive(tmp_path):
    found = by_id(run(tmp_path, b.png(text={"Description": "made with Midjourney"}), "x.png"))
    item = found["generator-mention"]
    assert 0.5 <= item["weight"] < ai.CONCLUSIVE


def test_ordinary_words_in_a_caption_are_not_read_as_generators(tmp_path):
    caption = "Runway at sunset, Gemini constellation, Flux capacitor, my friend Sora"
    assert "generator-mention" not in by_id(run(tmp_path, b.png(text={"Description": caption}), "x.png"))
    # ...but the same words in a field the tool writes itself do count
    exif = b.tiff({0x0131: "Google Gemini"})
    assert "exif-software-generator" in by_id(run(tmp_path, b.jpeg(exif=exif), "y.jpg"))


def test_c2pa_trained_media_in_jpeg_app11(tmp_path):
    data = b.jpeg(app11=b.jumbf("claim_generator", "OpenAI ChatGPT", b.IPTC + "trainedAlgorithmicMedia"))
    found = by_id(run(tmp_path, data, "x.jpg"))
    assert found["c2pa-trained-media"]["weight"] >= ai.CONCLUSIVE
    assert "OpenAI" in found["c2pa-trained-media"]["title"]["en"]
    assert found["c2pa-present"]["weight"] == 0


def test_c2pa_composite_is_ai_edited_not_generated(tmp_path):
    data = b.jpeg(app11=b.jumbf(b.IPTC + "compositeWithTrainedAlgorithmicMedia"))
    found = by_id(run(tmp_path, data, "x.jpg"))
    assert found["c2pa-composite"]["claim"] == "ai_edited"
    assert "c2pa-trained-media" not in found


def test_c2pa_capture_points_to_not_ai(tmp_path):
    found = by_id(run(tmp_path, b.png(c2pa=b.jumbf(b.IPTC + "digitalCapture")), "x.png"))
    assert found["c2pa-capture"]["score"] <= 2


def test_c2pa_from_a_camera_vendor_naming_google_is_not_read_as_ai(tmp_path):
    data = b.jpeg(app11=b.jumbf("claim_generator", "Google C2PA Core", b.IPTC + "computationalCapture"))
    found = by_id(run(tmp_path, data, "x.jpg"))
    assert "c2pa-capture" in found and "c2pa-generator-named" not in found


def test_xmp_digital_source_type_in_png_and_webp(tmp_path):
    xmp = f'<x:xmpmeta><rdf:Description Iptc4xmpExt:DigitalSourceType="{b.IPTC}trainedAlgorithmicMedia"/></x:xmpmeta>'
    assert "xmp-trained-media" in by_id(run(tmp_path, b.png(xmp=xmp), "x.png"))
    assert "xmp-trained-media" in by_id(run(tmp_path, b.webp(xmp=xmp), "x.webp"))


def test_xmp_found_by_generic_scan_when_container_is_unknown(tmp_path):
    xmp = f'<x:xmpmeta DigitalSourceType="{b.IPTC}trainedAlgorithmicMedia"></x:xmpmeta>'
    result = run(tmp_path, b"\x00\x00\x00\x18ftypavif" + b"\x00" * 8 + xmp.encode(), "x.avif")
    assert "xmp-trained-media" in by_id(result)
    assert result["file"]["format"] == "avif"


def test_exif_complete_camera_record_lowers_the_score(tmp_path):
    exif = b.tiff({0x010F: "Canon", 0x0110: "EOS R5", 0x0132: "2024:05:01 10:00:00"},
                  {0x829A: (1, 250), 0x829D: (28, 10), 0x8827: 200, 0x9003: "2024:05:01 10:00:00", 0xA434: "RF 24-70mm"})
    result = run(tmp_path, b.jpeg(exif=exif), "x.jpg")
    camera = by_id(result)["exif-camera"]
    assert camera["score"] <= 2 and camera["weight"] == 0.5
    assert result["metadata_summary"]["exif"]["make"] == "Canon"
    assert "no-metadata" not in by_id(result)


def test_exif_big_endian_is_read(tmp_path):
    exif = b.tiff({0x010F: "NIKON", 0x0110: "Z6"}, {0x829D: (4, 1), 0x8827: 100, 0x9003: "2023:01:01 00:00:00"}, big_endian=True)
    assert "exif-camera" in by_id(run(tmp_path, b.jpeg(exif=exif), "x.jpg"))


def test_exif_software_naming_a_generator_vs_an_editor(tmp_path):
    gen = by_id(run(tmp_path, b.jpeg(exif=b.tiff({0x0131: "Stable Diffusion 1.5"})), "g.jpg"))
    assert gen["exif-software-generator"]["weight"] >= 0.8
    edit = by_id(run(tmp_path, b.jpeg(exif=b.tiff({0x0131: "Adobe Photoshop 25.0"})), "e.jpg"))
    assert edit["exif-software-editor"]["weight"] == 0 and "exif-software-generator" not in edit


def test_user_comment_with_parameters_in_jpeg(tmp_path):
    comment = b"UNICODE\x00" + "Steps: 20, Sampler: Euler, CFG scale: 7".encode("utf-16-be")
    exif = b.tiff({}, {0x9286: comment})
    assert "sd-parameters" in by_id(run(tmp_path, b.jpeg(exif=exif), "x.jpg"))


def test_modified_long_after_capture_is_informative_only(tmp_path):
    exif = b.tiff({0x0132: "2024:09:01 10:00:00"}, {0x9003: "2024:01:01 10:00:00"})
    item = by_id(run(tmp_path, b.jpeg(exif=exif), "x.jpg"))["exif-modified-later"]
    assert item["weight"] == 0


def test_generator_dimensions_only_count_without_camera_data(tmp_path):
    assert "generator-dimensions" in by_id(run(tmp_path, b.png(1024, 1024, text={"x": "y"}), "x.png"))
    exif = b.tiff({0x010F: "Sony", 0x0110: "A7"}, {0x829A: (1, 60), 0x829D: (4, 1), 0x8827: 400})
    assert "generator-dimensions" not in by_id(run(tmp_path, b.jpeg(1024, 1024, exif=exif), "x.jpg"))
    assert "generator-dimensions" not in by_id(run(tmp_path, b.png(4000, 3000, text={"x": "y"}), "y.png"))


def test_no_metadata_is_weak_evidence(tmp_path):
    item = by_id(run(tmp_path, b.png(), "x.png"))["no-metadata"]
    assert item["weight"] <= 0.1


def test_filename_hint(tmp_path):
    found = by_id(run(tmp_path, b.png(text={"a": "b"}), "ChatGPT Image Jul 3, 2025.png"))
    assert found["filename-hint"]["source"] == "filename"
    assert "filename-hint" not in by_id(run(tmp_path, b.png(text={"a": "b"}), "holiday.png"))


def test_dimensions_are_read_for_every_container(tmp_path):
    assert run(tmp_path, b.png(300, 200), "a.png")["file"]["width"] == 300
    assert run(tmp_path, b.jpeg(300, 200), "b.jpg")["file"]["height"] == 200
    webp = run(tmp_path, b.webp(300, 200), "c.webp")["file"]
    assert (webp["width"], webp["height"]) == (300, 200)
    assert run(tmp_path, b"GIF89a" + (300).to_bytes(2, "little") + (200).to_bytes(2, "little") + b"\x00" * 8, "d.gif")["file"]["width"] == 300


def test_quotes_are_cleaned_and_bounded(tmp_path):
    params = "Steps: 20, Sampler: x, CFG scale: 7 \x00\x07" + "A" * 500
    quote = by_id(run(tmp_path, b.png(text={"parameters": params}), "x.png"))["sd-parameters"]["quote"]
    assert len(quote) <= 160 and "\x00" not in quote and "\x07" not in quote


# --- hostile input: nothing here may raise or hang ---------------------------------------

def test_hostile_files_do_not_crash(tmp_path):
    cases = [
        b"", b"\x89PNG\r\n\x1a\n", b"\x89PNG\r\n\x1a\n" + b"\xff\xff\xff\xffIDAT",
        b"\xff\xd8\xff", b"\xff\xd8\xff\xe1\x00\x00" + b"\x00" * 20, b"\xff\xd8\xff\xe1\xff\xff" + b"Exif\x00\x00II*\x00",
        b"RIFF\x00\x00\x00\x00WEBP" + b"VP8X\xff\xff\xff\xff", b"GIF89a", b"\x00" * 1000,
        b"\xff\xd8" + b"\xff\xe1\x00\x10Exif\x00\x00II*\x00\x08\x00\x00\x00" + b"\xff" * 64,
    ]
    for index, data in enumerate(cases):
        result = run(tmp_path, data, f"h{index}.bin")
        assert isinstance(result["evidence"], list)


def test_exif_ifd_loop_and_oversized_entry_count_terminate():
    loop = b"II*\x00\x08\x00\x00\x00" + (2).to_bytes(2, "little") + b"\x69\x87\x04\x00\x01\x00\x00\x00\x08\x00\x00\x00" * 2 + b"\x00" * 4
    assert isinstance(ai.parse_tiff(loop), dict)
    huge = b"II*\x00\x08\x00\x00\x00" + b"\xff\xff" + b"\x00" * 40
    assert isinstance(ai.parse_tiff(huge), dict)


def test_decompression_bomb_in_ztxt_is_capped(tmp_path):
    import zlib
    bomb = zlib.compress(b"A" * 50_000_000)
    body = b"parameters\x00\x00" + bomb
    data = b.png() [:-12] + b.png_chunk(b"zTXt", body) + b.png_chunk(b"IEND", b"")
    result = run(tmp_path, data, "bomb.png")
    assert result["file"]["format"] == "png"


def test_png_text_chunk_count_is_bounded(tmp_path):
    many = b"".join(b.png_chunk(b"tEXt", b"k\x00v") for _ in range(ai.MAX_CHUNKS + 500))
    data = b"\x89PNG\r\n\x1a\n" + many
    assert run(tmp_path, data, "many.png")["file"]["format"] == "png"
