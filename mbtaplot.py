from __future__ import with_statement
import sys
import os
import urllib2
import time
import cgi
import xml.dom.minidom as minidom
from google.appengine.ext.webapp import template
from google.appengine.ext import webapp
from google.appengine.ext.webapp.util import run_wsgi_app
from xml.sax.saxutils import escape
import simplejson as json

BUS_FEED="http://webservices.nextbus.com/service/publicXMLFeed?"

class Bus(object):
    def __init__(self, xml_vehicle):
        for attribute in ("dirTag", "heading", "id", "lat", "lon", "routeTag", "secsSinceReport"):
            setattr(self, attribute, xml_vehicle.getAttribute(attribute))
        self.t = time.time() - int(self.secsSinceReport)
        del self.secsSinceReport

        self.lat = float(self.lat)
        self.lon = float(self.lon)

        self.oldT = self.t
        self.oldLat = self.lat
        self.oldLon = self.lon

    @property
    def age(self):
        return time.time() - self.t

    @property
    def oldAge(self):
        return time.time() - self.oldT

    @property
    def round_heading(self):
        return (int(int(self.heading)/3)*3)%120

    def estimate_pos(self, l_i, l_j):
        t_i = self.oldT
        t_j = self.t
        t_k = time.time()

        if abs(l_i - l_j) < .00001 or int(t_i) == int(t_j):
            return l_j

        delta_t = t_j - t_i
        delta_l = l_j - l_i

        l_k = l_j + (t_k-t_j)*(l_j-l_i)/(t_j-t_i)

        return l_k

    @property
    def elat(self):
        return self.estimate_pos(self.oldLat, self.lat)

    @property
    def elon(self):
        return self.estimate_pos(self.oldLon, self.lon)

    def sendable(self, snap, estimate):
        if estimate:
            lat, lon = self.elat, self.elon
        else:
            lat, lon = self.lat, self.lon

        if snap:
            lat, lon = snap_to_path(self.routeTag, lat, lon)

        return {
            "elat": lat,
            "elon": lon,
            "id": self.id,
            "age": int(self.age),
            "rhead": self.round_heading,
            }

def distance(x1,y1,x2,y2):
    return (x1-x2)*(x1-x2) + (y1-y2)*(y1-y2)

def closest_point_on_line(line, x3, y3):
    """ from http://local.wasp.uwa.edu.au/~pbourke/geometry/pointline/ """
    x1, y1, x2, y2 = line

    u = ((x3-x1)*(x2-x1) + (y3-y1)*(y2-y1)) / distance(*line)

    if u < 0 or u > 1:
        """ use one of the ends """
        if distance(x1,y1,x3,y3) > distance(x2,y2,x3,y3):
            return x2,y2
        else:
            return x1,y1
    else:
        """ use a point on the line """

        x4 = x1 + u*(x2 - x1)
        y4 = y1 + u*(y2 - y1)
        
        return x4, y4

def distance_to_line(line, x3, y3):
    """ from http://local.wasp.uwa.edu.au/~pbourke/geometry/pointline/ """
    x1, y1, x2, y2 = line

    x4, y4 = closest_point_on_line(line, x3, y3)
    return distance(x4, y4, x3, y3)


def snap_to_path(route, lat, lon):
    best_line = None
    best_dist = None

    for path in request_paths(route):

        for p_i, p_j in zip(path[:-1], path[1:]):
            line = p_i.lat, p_i.lon, p_j.lat, p_j.lon

            dist = distance_to_line(line, lat, lon)
            if best_line == None or dist < best_dist:
                best_line = line
                best_dist = dist

    if best_line is None or best_dist > .00001:
        """ if we failed to find a good line, or if we'd be adjusting too far, do nothing """
        return lat, lon

    return closest_point_on_line(best_line, lat, lon)


def request_paths(route_num, path_cache={}):
    # path cache shared between calls
    # never updates path cache

    if route_num not in path_cache:
        use_url = BUS_FEED + "&".join(("command=routeConfig",
                                       "a=mbta",
                                       "r=%s" % route_num
                                       ))
        usock = urllib2.urlopen(use_url)
        xmldoc = minidom.parse(usock)
        usock.close()

        path_cache[route_num] = [Path(p) for p in xmldoc.getElementsByTagName("path")]
    return path_cache[route_num]

def request_buses(route_num):
    use_url = BUS_FEED + "&".join(("command=vehicleLocations",
                                   "a=mbta",
                                   "r=%s" % route_num,
                                   #"t=%s" % int(time.time())
                                   "t=0"
                                   ))

    usock = urllib2.urlopen(use_url)
    xmldoc = minidom.parse(usock)
    usock.close()

    bus_hash = {}
    for vehicle in xmldoc.getElementsByTagName("vehicle"):
        bus = Bus(vehicle)
        bus_hash[bus.id] = bus

    return bus_hash


def update_buses(old_buses, new_buses):
    for bus_id, new_bus in new_buses.items():
        try:
            old_bus = old_buses[new_bus.id]
        except KeyError:
            continue

        if old_bus.age - new_bus.age < 5:
            """ if the new bus is not at least 5sec more recently
            updated than the old bus, don't update previous
            position and report time """
            new_bus.oldT = old_bus.oldT
            new_bus.oldLat = old_bus.oldLat
            new_bus.oldLon = old_bus.oldLon
        else:
            new_bus.oldT = old_bus.t
            new_bus.oldLat = old_bus.lat
            new_bus.oldLon = old_bus.lon

    return new_buses


def allRoutes():
    use_url = BUS_FEED + "&".join(("command=routeList",
                                       "a=mbta"))
    usock = urllib2.urlopen(use_url)
    xmldoc = minidom.parse(usock)
    usock.close()

    return [BusRoute(route.getAttribute("title"))
            for route in xmldoc.getElementsByTagName("route")]

class Point(object):
    def __init__(self, xml_point):
        self.lat = float(xml_point.getAttribute("lat"))
        self.lon = float(xml_point.getAttribute("lon"))
    def __repr__(self):
        return "(%s, %s)" % (self.lat, self.lon)

class Path(object):
    def __init__(self, xml_path):
        self.points = [Point(p) for p in xml_path.getElementsByTagName("point")]
    def __getitem__(self, x):
        return self.points[x]

class Paths(webapp.RequestHandler):
    cache = {}

    def get(self):
        route = cgi.escape(self.request.get('route'))
        if route not in self.cache:
            self.cache[route] = json.dumps([[{"lat": point.lat,
                                              "lng": point.lon}
                                             for point in path]
                                            for path in request_paths(route)])
        self.response.out.write(self.cache[route])


class Routes(webapp.RequestHandler):
    def get(self):
        routes = allRoutes()
        self.response.out.write(json.dumps([route.route_num for route in routes]))


class Buses(webapp.RequestHandler):
    cache = {}
    def buses(self, route):
        try:
            timestamp, buses = self.cache[route]
            return buses
        except KeyError:
            return {}

    def timestamp(self, route):
        try:
            timestamp, buses = self.cache[route]
            return timestamp
        except KeyError:
            return 0

    def get(self):
        route = cgi.escape(self.request.get('route'))
        
        snap = cgi.escape(self.request.get('snap')).lower() != "false"
        est = cgi.escape(self.request.get('est')).lower() != "false"

        now = time.time()
        if now - self.timestamp(route) > 12:
            """ refresh buses from server every 10sec """

            new_buses = update_buses(self.buses(route),
                                     request_buses(route))
            self.cache[route] = now, new_buses

        self.response.out.write(json.dumps(
                [bus.sendable(snap, est) for bus in self.buses(route).values()]))


class MainPage(webapp.RequestHandler):
    def get(self):

        buses = cgi.escape(self.request.get('buses')).lower() == "true"
        shading = cgi.escape(self.request.get('shading')).lower() == "true"

        snap = cgi.escape(self.request.get('snap')).lower() != "false"
        est = cgi.escape(self.request.get('est')).lower() != "false"

        all_routes = False

        routes = []
        routes_req = cgi.escape(self.request.get('routes'))
        if routes_req == "all":
            all_routes = True
        elif routes_req:
            routes = routes_req.split(",")

        if not routes:
            routes = ["77", "78", "74", "75", "72", "94", "96"]

        template_values = {"shading": shading,
                           "all_routes": all_routes,
                           "snap": snap,
                           "est": est,
                           "buses": buses,
                           "routes": routes}

        path = os.path.join(os.path.dirname(__file__), 'index.html')
        self.response.out.write(template.render(path, template_values))

application = webapp.WSGIApplication([('/', MainPage),
                                      ('/Paths', Paths),
                                      ('/Buses', Buses),
                                      ('/Routes', Routes),
                                     ], debug=True)

def main():
    run_wsgi_app(application)

if __name__ == "__main__":
    main()
