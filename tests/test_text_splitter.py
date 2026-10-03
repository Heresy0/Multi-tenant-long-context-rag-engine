from pathlib import Path

import pytest

from backend.app.knowledge.rag import split_file


def _bodies(chunks) -> list[str]:
    return [chunk.page_content.split("\n\n", 1)[1] for chunk in chunks]


def test_txt_cleans_bom_and_separates_paragraphs(tmp_path: Path) -> None:
    path = tmp_path / "制度.TXT"
    path.write_bytes(
        ("\ufeff1 总则\r\n第一段。\r\n\r\n\r\n第二段。"
         "\r\n1.1 范围\r\n适用于全体员工。\x00").encode("utf-8")
    )
    chunks = split_file(path, document_name="正式制度")

    assert _bodies(chunks) == ["第一段。", "第二段。", "适用于全体员工。"]
    assert [c.metadata["section_path"] for c in chunks] == [
        "1 总则", "1 总则", "1 总则 > 1.1 范围",
    ]
    for chunk in chunks:
        assert chunk.page_content.startswith("文档：正式制度\n")
        assert chunk.metadata["source"] == str(path.resolve())
        assert chunk.metadata["parent_key"]
        assert "\ufeff" not in chunk.page_content
        assert "\x00" not in chunk.page_content


def test_txt_keeps_question_with_answer_across_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "服务FAQ.txt"
    path.write_text(
        "Q1 如何申请？\n\nA：提交审批表。\n\n补充：附上凭证。\n\n"
        "Q2 时限多久？\nA：两个工作日。", encoding="utf-8",
    )
    chunks = split_file(path)
    assert len(chunks) == 2
    assert all(c.metadata["chunk_type"] == "faq" for c in chunks)
    assert "Q1 如何申请？" in chunks[0].page_content
    assert "附上凭证" in chunks[0].page_content
    assert "Q2" not in chunks[0].page_content
    assert "两个工作日" in chunks[1].page_content


def test_long_txt_has_bounded_body_and_keeps_end_fact(tmp_path: Path) -> None:
    path = tmp_path / "long.txt"
    path.write_text("长段落测试。" * 300 + "最终期限为七天。", encoding="utf-8")
    chunks = split_file(path)
    assert len(chunks) > 1
    assert all(len(body) <= 500 for body in _bodies(chunks))
    assert "最终期限为七天。" in chunks[-1].page_content
    assert len({c.metadata["parent_key"] for c in chunks}) == 1


def test_markdown_heading_levels_and_setext(tmp_path: Path) -> None:
    path = tmp_path / "api.MD"
    path.write_text(
        "API 接入\n========\n### 访问令牌\n有效期两小时。\n\n"
        "### 刷新令牌\n只允许使用一次。\n\n"
        "安全要求\n--------\n需要签名。\n# 附录 #\n联系管理员。",
        encoding="utf-8-sig",
    )
    chunks = split_file(path, document_name="正式API手册")
    assert [c.metadata["section_path"] for c in chunks] == [
        "API 接入 > 访问令牌", "API 接入 > 刷新令牌",
        "API 接入 > 安全要求", "附录",
    ]
    assert all(c.metadata["document_name"] == "正式API手册" for c in chunks)


def test_markdown_fences_keep_code_and_ignore_inner_headings(tmp_path: Path) -> None:
    path = tmp_path / "code.md"
    code = "# 代码注释不是标题\ndef invoke():\n    return 'a|b'\n\n"
    path.write_text(
        f"# 接入\n~~~python\n{code}~~~\n\n接口说明。", encoding="utf-8",
    )
    chunks = split_file(path)
    assert [c.metadata["chunk_type"] for c in chunks] == ["code", "paragraph"]
    assert "    return 'a|b'" in _bodies(chunks)[0]
    assert f"~~~python\n{code}~~~" == _bodies(chunks)[0]
    assert _bodies(chunks)[0].startswith("~~~python\n")
    assert _bodies(chunks)[0].endswith("\n~~~")
    assert chunks[0].metadata["code_language"] == "python"
    assert all(c.metadata["section_path"] == "接入" for c in chunks)


def test_long_code_splits_on_lines_with_fences_and_preserves_order(tmp_path: Path) -> None:
    path = tmp_path / "long-code.md"
    lines = [f"    value_{i} = {i}\n" for i in range(120)]
    code = "".join(lines)
    path.write_text(f"# 示例\n```python\n{code}```", encoding="utf-8")
    chunks = split_file(path)
    bodies = _bodies(chunks)
    assert len(chunks) > 1
    assert all(body.startswith("```python\n") and body.endswith("\n```")
               for body in bodies)
    recovered = "".join(body.removeprefix("```python\n").removesuffix("\n```")
                        + "\n" for body in bodies)
    assert recovered == code


def test_markdown_table_repeats_header_and_keeps_all_rows(tmp_path: Path) -> None:
    path = tmp_path / "table.md"
    rows = [f"| 城市{i} | {i}元 |" for i in range(150)]
    path.write_text(
        "# 报销标准\n| 城市 | 金额 |\n| :--- | ---: |\n"
        + "\n".join(rows) + "\n\n办理完毕。", encoding="utf-8",
    )
    chunks = split_file(path)
    table_bodies = _bodies([c for c in chunks if c.metadata["chunk_type"] == "table"])
    assert len(table_bodies) > 1
    assert all(body.startswith("城市|金额\n") for body in table_bodies)
    data_rows = [line for body in table_bodies for line in body.splitlines()[1:]]
    assert data_rows == [f"城市{i}|{i}元" for i in range(150)]
    assert chunks[-1].metadata["chunk_type"] == "paragraph"


def test_markdown_table_preserves_escaped_and_code_pipes(tmp_path: Path) -> None:
    path = tmp_path / "escaped.md"
    path.write_text(
        "| 参数 | 示例 |\n| --- | --- |\n| 模式 | `a|b` |\n"
        "| 转义 | a\\|b |", encoding="utf-8",
    )
    assert _bodies(split_file(path)) == ["参数|示例\n模式|`a|b`\n转义|a\\|b"]


def test_markdown_keeps_list_item_and_continuation_together(tmp_path: Path) -> None:
    path = tmp_path / "list.md"
    path.write_text(
        "# 步骤\n- 提交申请\n  必须附上凭证。\n- 等待审批\n\n结束。",
        encoding="utf-8",
    )
    chunks = split_file(path)
    assert [c.metadata["chunk_type"] for c in chunks] == ["list", "list", "paragraph"]
    assert _bodies(chunks)[0] == "- 提交申请\n  必须附上凭证。"


@pytest.mark.parametrize("suffix", [".txt", ".md"])
def test_empty_text_has_no_chunks(tmp_path: Path, suffix: str) -> None:
    path = tmp_path / f"empty{suffix}"
    path.write_text("\ufeff\n\n  \n", encoding="utf-8")
    assert split_file(path) == []
