import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
caption = importlib.import_module('utils.caption')


def test_channel_post_style_full_parse():
    """Typical channel forward: code + intro + bare tags + labelled lines."""
    text = (
        "GVH-690 无码流出 中文字幕\n"
        "今天推荐这部 #女朋友 #巨乳\n"
        "演员： 小美、小林\n"
        "类别： #高潮"
    )
    out = caption.restructure_caption(text)
    assert out is not None
    assert out.startswith("GVH-690\n\n")
    assert "#女朋友 #巨乳" in out
    assert "演员：#小美 #小林" in out
    assert "类别：#高潮" in out
    # the bare tags were hoisted out of the intro line
    assert "#女朋友 #巨乳" not in out.split("\n\n")[1]


def test_plain_text_untouched():
    assert caption.restructure_caption("今天天气不错，出去走走") is None
    assert caption.restructure_caption("") is None


def test_single_hashtag_not_enough():
    assert caption.restructure_caption("分享一下 #好物") is None


def test_two_bare_hashtags_reformat():
    out = caption.restructure_caption("周末 #摄影 #街拍")
    assert out is not None
    assert "标签：#摄影 #街拍" in out
    assert out.split("\n\n")[0] == "周末"


def test_fc2_code():
    out = caption.restructure_caption("FC2-PPV-4649113 素人作品 #首发")
    assert out.startswith("FC2-PPV-4649113")


def test_blacklisted_code_shape():
    # COVID-19 is shaped like a code but must not become a 番号
    assert caption.restructure_caption("关于 COVID-19 的新闻") is None


def test_lowercase_code_normalized():
    out = caption.restructure_caption("gvh-690 精品 #无码")
    assert out.startswith("GVH-690\n\n")
    assert "GVH-690" not in out.split("\n\n")[1]  # token dropped from intro


def test_already_formatted_is_stable():
    """Re-forwarding a bot-generated caption must not mangle it."""
    text = "GVH-690\n\n标题简介\n\n演员：#小美\n标签：#巨乳\n类别：#中文字幕"
    out = caption.restructure_caption(text)
    assert out == text


def test_labelled_tag_line_with_hashtags():
    out = caption.restructure_caption("标签：#痴女 #单人\n演员:深田えいみ")
    assert "标签：#痴女 #单人" in out
    assert "演员：#深田えいみ" in out


def test_long_intro_truncated():
    out = caption.restructure_caption("ABP-123 " + "很长的简介" * 300 + "\n标签：#测试")
    assert out is not None
    assert len(out) <= 1024
    # five-line skeleton: intro truncated, then the fixed three label lines
    assert out.count("\n\n") == 2 and "…" in out
    assert out.split("\n")[-3:] == ["演员：", "标签：#测试", "类别："]


def test_code_inside_filename_detected():
    out = caption.restructure_caption("分享文件 SONE-001_4K.mp4 #收藏")
    assert out.startswith("SONE-001")


def test_skeleton_keeps_empty_slots():
    """All five lines + two blank separators, missing items as bare labels
    or empty lines so the user can fill them in by hand."""
    out = caption.restructure_caption("GVH-690 只有番号 #无码")
    lines = out.split("\n")
    assert lines[0] == "GVH-690"
    assert lines[2].startswith("只有番号")
    assert lines[4:] == ["演员：", "标签：#无码", "类别："]
    assert out.count("\n\n") == 2


def test_skeleton_no_code_no_leading_blanks():
    out = caption.restructure_caption("标签：#痴女\n类别：#中文字幕")
    assert not out.startswith("\n")
    lines = out.split("\n")
    assert lines[0] == "演员："
    assert lines == ["演员：", "标签：#痴女", "类别：#中文字幕"]
