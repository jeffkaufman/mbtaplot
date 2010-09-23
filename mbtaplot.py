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
        self.attrs = {}
        for attribute in ("dirTag", "heading", "id", "lat", "lon", "routeTag", "secsSinceReport"):
            self.attrs[attribute] = xml_vehicle.getAttribute(attribute)
        self.attrs["roundHeading"] = self.round_heading
        
    @property
    def round_heading(self):
        return (int(int(self.attrs["heading"])/3)*3)%120

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
        
        return [Bus(vehicle) for vehicle in xmldoc.getElementsByTagName("vehicle")]

    @staticmethod
    def age_buses(buses, time_diff):
        for bus in buses:
            bus.attrs["secsSinceReport"] = int(bus.attrs["secsSinceReport"]) + time_diff

    @staticmethod
    def allRoutes():
        use_url = BUS_FEED + "&".join(("command=routeList",
                                       "a=mbta"))
        usock = urllib2.urlopen(use_url)
        xmldoc = minidom.parse(usock)
        usock.close()

        return [BusRoute(route.getAttribute("title")) for route in xmldoc.getElementsByTagName("route")]

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
    def buses_for_route(self, route):
        now = time.time()
        if route in self.cache:
            timestamp, buses = self.cache[route]
            time_diff = int(now - timestamp)
            if time_diff < 45:
                BusRoute.age_buses(buses, time_diff)
                self.cache[route] = now, buses
                return buses

        buses = BusRoute(route).buses()
        self.cache[route] = (now, buses)
        return buses

    def get(self):
        route = cgi.escape(self.request.get('route'))
        buses = self.buses_for_route(route)
        self.response.out.write(json.dumps([bus.attrs for bus in buses]))


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
