"""
This script made the slightly non equilateral icons that are in static
"""

import sys, png
import math
def get_img(i):
    def to_rad(deg):
        return deg*math.pi/180

    scale=3

    max_x, max_y = 24*scale, 24*scale
    x_off = y_off = 12*scale
    hyp = float(x_off)-1.0

    def get_point(theta):
        return x_off+math.sin(theta)*hyp, y_off-math.cos(theta)*hyp

    nose = get_point(to_rad(i))
    legA = get_point(to_rad(i+128))
    legB = get_point(to_rad(i-128+360))

    def dist(ptA,ptB):
        x1,y1 = ptA
        x2,y2 = ptB
        return math.sqrt( (x1-x2)*(x1-x2) + (y1-y2)*(y1-y2) )

    def cross(ptA, ptB, ptV):
        x1,y1 = ptA
        x2,y2 = ptB
        xV,yV = ptV

        xA, yA = x1-xV, y1-yV
        xB, yB = x2-xV, y2-yV

        return xA*yB-xB*yA

    def is_left_of(ptQ, ptA, ptB):
        return cross(ptQ, ptA, ptB) > 0        

    def avg_dist(x,y):
        return (dist((x,y), nose) + dist((x,y), legA) + dist((x,y), legB)) / 3

    center = x_off, y_off

    def inside(x,y):
        pt = x,y
        return (is_left_of(pt, nose, legA) and
                is_left_of(pt, legA, legB) and
                is_left_of(pt, legB, nose))

        #return avg_dist(x,y) < avg_dist(*center)*1.14

    def get_val(x,y):
        if inside(x,y):
            return 1.0
        else:
            return 0.0

    def avg_val(x,y):
        s = 0
        for x_move in [-1,0,1]:
            for y_move in [-1,0,1]:
                s += get_val(x*3+x_move,y*3+y_move)
        s = s/9

        assert 0 <= s <= 1, s

        return 0,0,255,int(128*s)

    for y in range(24):
        row = []
        for x in range(24):
            r,g,b,a = avg_val(x,y)
            row.append(r)
            row.append(g)
            row.append(b)
            row.append(a)
        yield row

def mkicons():
    p = png.Writer(width=24, height=24, alpha=True)
    for i in range(0,360,3):
        f = open("dir_%s.png" % i, "wb")
        p.write(f, get_img(i))
        f.close()

if __name__ == "__main__":
    mkicons(*sys.argv[1:])
