from __future__ import with_statement
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
        l_i, l_j = float(l_i), float(l_j)

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

    def sendable(self):
        return {
            "elat": self.elat,
            "elon": self.elon,
            "id": self.id,
            "age": int(self.age),
            "rhead": self.round_heading,
            
            "lat": self.lat,
            "lon": self.lon,
            
            "oldLat": self.oldLat,
            "oldLon": self.oldLon,

            "t": self.t,
            "oldT": self.oldT,
            }

class BusRoute(object):
    def __init__(self, route_num):
        self.route_num = route_num

    def paths(self):
        use_url = BUS_FEED + "&".join(("command=routeConfig",
                                       "a=mbta",
                                       "r=%s" % self.route_num
                                       ))
        usock = urllib2.urlopen(use_url)
        xmldoc = minidom.parse(usock)
        usock.close()

        return [Path(p) for p in xmldoc.getElementsByTagName("path")]

    def buses(self):
        use_url = BUS_FEED + "&".join(("command=vehicleLocations",
                                       "a=mbta",
                                       "r=%s" % self.route_num,
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

    @staticmethod
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

    @staticmethod
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
        self.lat = xml_point.getAttribute("lat")
        self.lon = xml_point.getAttribute("lon")

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
                                            for path in BusRoute(route).paths()])
        self.response.out.write(self.cache[route])


class Routes(webapp.RequestHandler):
    def get(self):
        routes = BusRoute.allRoutes()
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
        now = time.time()
        if now - self.timestamp(route) > 12:
            new_buses = BusRoute.update_buses(self.buses(route), 
                                              BusRoute(route).buses())
            self.cache[route] = now, new_buses

        self.response.out.write(json.dumps(
                [bus.sendable() for bus in self.buses(route).values()]))


class MainPage(webapp.RequestHandler):
    def get(self):

        buses = cgi.escape(self.request.get('buses')).lower() == "true"
        shading = cgi.escape(self.request.get('shading')).lower() == "true"
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
