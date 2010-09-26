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

        self.pred_t = self.t
        self.pred_lat = self.lat
        self.pred_lon = self.lon

    @property
    def age(self):
        return time.time() - self.t

    @property
    def predAge(self):
        return time.time() - self.pred_t

    @property
    def round_heading(self):
        return (int(int(self.heading)/3)*3)%120

    def sendable(self):
        return {
            "lat_i": self.pred_lat,
            "lon_i": self.pred_lon,
            "lat_j": self.lat,
            "lon_j": self.lon,
            "id": self.id,
            "age_i": int(self.predAge),
            "age_j": int(self.age),
            "rhead": self.round_heading,
            }


def request_paths(route_num, path_cache={}):
    # path cache shared between calls
    # never updates path cache
    # returns: paths, directions, stops

    if route_num not in path_cache:
        use_url = BUS_FEED + "&".join(("command=routeConfig",
                                       "a=mbta",
                                       "r=%s" % route_num
                                       ))
        usock = urllib2.urlopen(use_url)
        xmldoc = minidom.parse(usock)
        usock.close()

        stops = {}
        for s in xmldoc.getElementsByTagName("route")[0].getElementsByTagName("stop"):
            stop = Stop(s)
            if stop.lat and stop.tag not in stops:
                stops[stop.tag] = stop

        directions = {}
        for d in xmldoc.getElementsByTagName("direction"):
            direction = Direction(d, stops)
            directions[direction.tag] = direction

        paths = [Path(p) for p in xmldoc.getElementsByTagName("path")]

        path_cache[route_num] = paths, directions, stops

    return path_cache[route_num]

def distance(x1,y1,x2,y2):
    return (x1-x2)*(x1-x2) + (y1-y2)*(y1-y2)

def request_predictions(route_num, bus_hash):
    paths, directions, stops = request_paths(route_num)
    use_url = BUS_FEED + "&".join(("command=predictionsForMultiStops", "a=mbta", ))

    for stop in stops.values():
        use_url += "&stops=%s|%s" % (route_num, stop.tag)

    #sys.stderr.write(use_url+"\n\n")

    usock = urllib2.urlopen(use_url)
    xmldoc = minidom.parse(usock)
    usock.close()

    vehicle_predictions = {}

    for predictions in xmldoc.getElementsByTagName("predictions"):
        stop = stops[predictions.getAttribute("stopTag")]
        for prediction in predictions.getElementsByTagName("prediction"):
            minutes = int(prediction.getAttribute("minutes"))
            vehicle = prediction.getAttribute("vehicle")

            if minutes < 2:
                continue

            if vehicle not in vehicle_predictions or minutes < vehicle_predictions[vehicle][0]:
                vehicle_predictions[vehicle] = minutes, stop.lat, stop.lon
            elif minutes == vehicle_predictions[vehicle][0]:
                c_lat = bus_hash[vehicle].lat
                c_lon = bus_hash[vehicle].lon

                o_minutes, o_lat, o_lon = vehicle_predictions[vehicle]

                if distance(c_lat, c_lon, stop.lat, stop.lon) < distance(c_lat, c_lon, o_lat, o_lon):
                    vehicle_predictions[vehicle] = minutes, stop.lat, stop.lon

    return vehicle_predictions
    

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

    for bus_id, prediction in request_predictions(route_num, bus_hash).items():
        minutes, stop_lat, stop_lon = prediction
        bus_hash[bus_id].pred_t = time.time()+minutes*60+30
        bus_hash[bus_id].pred_lat = stop_lat
        bus_hash[bus_id].pred_lon = stop_lon

    return bus_hash


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

class Stop(object):
    def __init__(self, xml_stop):
        for attribute in ("tag", "title", "dirTag", "lat", "lon"):
            setattr(self, attribute, xml_stop.getAttribute(attribute))
        if self.lat:
            self.lat = float(self.lat)
        if self.lon:
            self.lon = float(self.lon)


class Direction(object):
    def __init__(self, xml_direction, stops):
        for attribute in ("tag", "title", "name"):
            setattr(self, attribute, xml_direction.getAttribute(attribute))
            
        self.stops = [stops[s.getAttribute("tag")] 
                      for s in xml_direction.getElementsByTagName("stop")]

class Paths(webapp.RequestHandler):
    cache = {}

    def get(self):
        route = cgi.escape(self.request.get('route'))
        if route not in self.cache:
            paths, directions, stops = request_paths(route)
            self.cache[route] = json.dumps({"paths" : [[{"lat": point.lat, "lon": point.lon}
                                                        for point in path]
                                                       for path in paths],

                                            "stops" : [{"lat": stop.lat, "lon": stop.lon, "title" : stop.title}
                                                       for stop in stops.values()]})

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

        now = time.time()
        if now - self.timestamp(route) > 12:
            """ refresh buses from server every Nsec """

            self.cache[route] = now, request_buses(route)

        self.response.out.write(json.dumps(
                [bus.sendable() for bus in self.buses(route).values()]))


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
