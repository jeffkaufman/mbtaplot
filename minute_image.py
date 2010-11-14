"""
Little script to generate files that say 'n min' for each minute
"""


import sys
import Image, ImageDraw, ImageFont

def start():
    f = ImageFont.truetype("/usr/share/fonts/truetype/freefont/FreeSans.ttf", 12)
    for n in range(200):
        i = Image.new("RGB", (48,15), (0,0,0))

        a = Image.new("L", i.size, 0)
        d = ImageDraw.Draw(a)
        d.text((0,0), "%s min" % n, font=f, fill="#FFFFFF")

        i.putalpha(a)
        i.save(open("min%s.png" % n, "wb"), "PNG")

if __name__ == "__main__":
  start(*sys.argv[1:])
