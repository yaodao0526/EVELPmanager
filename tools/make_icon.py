"""生成应用图标 assets/eve-lp.ico（一次性脚本，改图标时才需要跑）。"""
import os
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "assets")
S = 1024

BG = (13, 19, 28, 255)
BORDER = (38, 55, 76, 255)
GOLD = (232, 184, 75, 255)
CYAN = (73, 214, 200, 255)


def font(size):
    try:
        import matplotlib

        p = os.path.join(matplotlib.get_data_path(), "fonts", "ttf", "DejaVuSans-Bold.ttf")
        return ImageFont.truetype(p, size)
    except Exception:
        return ImageFont.load_default(size)


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([8, 8, S - 8, S - 8], radius=190, fill=BG, outline=BORDER, width=14)

    f = font(430)
    text = "LP"
    box = d.textbbox((0, 0), text, font=f)
    w, h = box[2] - box[0], box[3] - box[1]
    d.text(((S - w) / 2 - box[0], (S - h) / 2 - box[1] - 70), text, font=f, fill=GOLD)

    # 底部青色进度条：暗示"收益/前沿"
    d.rounded_rectangle([250, 780, 774, 830], radius=25, fill=(30, 44, 60, 255))
    d.rounded_rectangle([250, 780, 640, 830], radius=25, fill=CYAN)

    img = img.resize((256, 256), Image.LANCZOS)
    ico = os.path.join(OUT, "eve-lp.ico")
    img.save(ico, sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    img.save(os.path.join(OUT, "eve-lp.png"))
    print("wrote", ico, os.path.getsize(ico), "bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
