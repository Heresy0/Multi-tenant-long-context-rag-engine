from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas


DIRECTORY = Path(__file__).resolve().parent
FONT_PATH = Path(r"C:\Windows\Fonts\msyh.ttc")
FONT_NAME = "MicrosoftYaHeiFixture"


def _register_font() -> None:
    if not FONT_PATH.is_file():
        raise FileNotFoundError(
            "Fixture generation requires Microsoft YaHei on Windows"
        )
    pdfmetrics.registerFont(TTFont(FONT_NAME, str(FONT_PATH)))


def _draw_header_footer(
    canvas: Canvas,
    page_number: int,
) -> None:
    width, height = A4
    canvas.setFont(FONT_NAME, 9)
    canvas.drawString(50, height - 35, "星河科技员工审批制度")
    canvas.drawCentredString(width / 2, 25, f"第 {page_number} 页")


def create_structured_policy() -> None:
    output = DIRECTORY / "structured_policy.pdf"
    canvas = Canvas(str(output), pagesize=A4)
    width, height = A4

    _draw_header_footer(canvas, 1)
    canvas.setFont(FONT_NAME, 17)
    canvas.drawString(50, height - 90, "1 总则")
    canvas.setFont(FONT_NAME, 11)
    canvas.drawString(
        50,
        height - 125,
        "本制度用于验证 PDF 页眉页脚清洗、标题上下文和跨页切分。",
    )
    canvas.drawString(
        50,
        55,
        "跨页审批要求由申请人提交完整材料并说明",
    )
    canvas.showPage()

    _draw_header_footer(canvas, 2)
    canvas.setFont(FONT_NAME, 11)
    canvas.drawString(
        50,
        height - 70,
        "业务理由后，部门负责人应在两个工作日内审批。",
    )
    canvas.setFont(FONT_NAME, 15)
    canvas.drawString(50, height - 115, "2 审批标准")
    canvas.setFont(FONT_NAME, 10)
    table_x = 50
    table_y = height - 165
    widths = [150, 170, 150]
    row_height = 28
    rows = [
        ["事项", "审批人", "时限"],
        ["普通采购", "部门负责人", "2个工作日"],
        ["紧急采购", "分管副总", "4小时"],
    ]
    x_positions = [table_x]
    for cell_width in widths:
        x_positions.append(x_positions[-1] + cell_width)
    for row_index in range(len(rows) + 1):
        y = table_y - row_index * row_height
        canvas.line(table_x, y, x_positions[-1], y)
    for x in x_positions:
        canvas.line(
            x,
            table_y,
            x,
            table_y - len(rows) * row_height,
        )
    for row_index, row in enumerate(rows):
        for column_index, value in enumerate(row):
            canvas.drawString(
                x_positions[column_index] + 6,
                table_y - row_index * row_height - 19,
                value,
            )
    canvas.showPage()

    _draw_header_footer(canvas, 3)
    canvas.setFont(FONT_NAME, 15)
    canvas.drawString(50, height - 85, "补充规则")
    canvas.setFont(FONT_NAME, 10)
    left_lines = [
        "左栏规则：提交人必须保留原始凭证。",
        "左栏事实：费用编号必须唯一。",
    ]
    right_lines = [
        "右栏规则：审批人不得代替申请人签字。",
        "右栏事实：归档期限为五年。",
    ]
    for index, line in enumerate(left_lines):
        canvas.drawString(50, height - 125 - index * 24, line)
    for index, line in enumerate(right_lines):
        canvas.drawString(width / 2 + 15, height - 125 - index * 24, line)
    canvas.save()


def create_scanned_notice() -> None:
    image_path = DIRECTORY / "scanned_notice_source.png"
    image = Image.new("RGB", (1400, 1800), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(r"C:\Windows\Fonts\arial.ttf", 54)
    draw.text(
        (120, 220),
        "OCR FALLBACK NOTICE",
        fill="black",
        font=font,
    )
    draw.text(
        (120, 330),
        "Scanned approval code: OCR-2026-17",
        fill="black",
        font=font,
    )
    image.save(image_path)

    output = DIRECTORY / "scanned_notice.pdf"
    canvas = Canvas(str(output), pagesize=A4)
    width, height = A4
    canvas.drawImage(
        str(image_path),
        35,
        45,
        width=width - 70,
        height=height - 90,
        preserveAspectRatio=True,
        anchor="c",
    )
    canvas.save()
    image_path.unlink()


if __name__ == "__main__":
    DIRECTORY.mkdir(parents=True, exist_ok=True)
    _register_font()
    create_structured_policy()
    create_scanned_notice()
