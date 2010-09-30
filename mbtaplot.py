from __future__ import with_statement
import sys
import os
import urllib2
import time
import cgi
import logging
import xml.dom.minidom as minidom
from google.appengine.ext.webapp import template
from google.appengine.ext import webapp
from google.appengine.ext.webapp.util import run_wsgi_app
from xml.sax.saxutils import escape
import simplejson as json

BUS_FEED="http://webservices.nextbus.com/service/publicXMLFeed?"

def get_xml(use_url):
    usock = urllib2.urlopen(use_url)
    xmldoc = minidom.parse(usock)
    usock.close()
    return xmldoc


short_names = {"Line": "SLM",
               "701": "CT1",
               "747": "CT2S",
               "748": "CT2N",
               "708" : "CT3"
               }

def short_name(x):
    x = str(x).split()[-1]
    return short_names.get(x,x)

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
            "dir": self.dirTag,
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

        try:
            xmldoc = get_xml(use_url)
        except Exception:
            logging.warning('request_paths: failed url: %s' % use_url)
            return [], {}, {}

        stops = {}

        xml_routes = xmldoc.getElementsByTagName("route")
        if not xml_routes:
            logging.warning('request_paths: system returned no route for %s\n' % route_num)
            return [], {}, {}

        for s in xml_routes[0].getElementsByTagName("stop"):
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

    vehicle_predictions = {}

    def updatePredictions(xmldoc):
        for predictions in xmldoc.getElementsByTagName("predictions"):
            stop = stops[predictions.getAttribute("stopTag")]
            for prediction in predictions.getElementsByTagName("prediction"):
                minutes = int(prediction.getAttribute("minutes"))
                vehicle = prediction.getAttribute("vehicle")
                if vehicle not in bus_hash:
                    continue

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

    def predict_some_stops(stop_list):
        use_url = BUS_FEED + "&".join(("command=predictionsForMultiStops", "a=mbta", ))
        for stop in stop_list:
            use_url += "&stops=%s|%s" % (route_num, stop.tag)

        try:
            xmldoc = get_xml(use_url)
        except Exception:
            logging.warning('request_predictions: failed url: %s' % use_url)
            return

        updatePredictions(xmldoc)

    # submit stops only N at a time
    # prevents urls from getting too long
    cur_stops = []
    for stop in stops.values():
        if len(cur_stops) > 50:
            predict_some_stops(cur_stops)
            cur_stops = []
        cur_stops.append(stop)
    if cur_stops:
        predict_some_stops(cur_stops)

    return vehicle_predictions


def request_buses(route_num):
    use_url = BUS_FEED + "&".join(("command=vehicleLocations",
                                   "a=mbta",
                                   "r=%s" % route_num,
                                   #"t=%s" % int(time.time())
                                   "t=0"
                                   ))

    bus_hash = {}

    try:
        xmldoc = get_xml(use_url)
    except Exception:
        logging.warning('request_buses: failed url: %s' % use_url)
        return bus_hash


    for vehicle in xmldoc.getElementsByTagName("vehicle"):
        bus = Bus(vehicle)
        bus_hash[bus.id] = bus

    return bus_hash


def update_predictions(route_num, bus_hash):
    for bus_id, prediction in request_predictions(route_num, bus_hash).items():
        minutes, stop_lat, stop_lon = prediction
        bus_hash[bus_id].pred_t = time.time()+minutes*60+30
        bus_hash[bus_id].pred_lat = stop_lat
        bus_hash[bus_id].pred_lon = stop_lon


def allRoutes():
    use_url = BUS_FEED + "&".join(("command=routeList",
                                       "a=mbta"))

    try:
        xmldoc = get_xml(use_url)
    except Exception:
        logging.warning('allRoutes: failed url: %s' % use_url)
        return []

    return [[route.getAttribute("tag"), route.getAttribute("title")]
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
        self.tags = [tag.getAttribute("id") for tag in xml_path.getElementsByTagName("tag")]

    def is_for(self, direction):
        """ is this path one of the ones for this direction? """
        # The format for path tags appears to be that the path tag
        # will start with the direction tag it belongs to:
        #
        #    dir = 78_780004v0_1
        #   path = 78_780004v0_130_2156_2159, 78_780004v0_110_2330_2332, ...
        #
        #    dir = 77_770009v0_1
        #   path = 77_770009v0_18_2303_2305, ...
        #
        # I can't find this documented anywhere, so it might change.
        # If it does, we can just return "true", and the downside will
        # be more network traffic and busses appearing on the wrong
        # side of divided streets like mass av
        #
        return any(tag.startswith(direction) for tag in self.tags)

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

            path_structure = [[{"lat": point.lat, "lon": point.lon} for point in path] for path in paths]

            direction_structure = {}
            for direction in directions:
                direction_structure[direction] = [[{"lat": point.lat, "lon": point.lon} for point in path] 
                                                  for path in paths
                                                  if path.is_for(direction)]

            stop_structure = [{"lat": stop.lat, "lon": stop.lon, "title" : stop.title, "tag": stop.tag}
                              for stop in stops.values()]

            self.cache[route] = json.dumps({"paths" : path_structure,
                                            "directions": direction_structure,
                                            "stops": stop_structure})

        self.response.out.write(self.cache[route])

class Arrivals(object):
    def __init__(route, title, direction):
        self.route = route
        self.title = title


class Arrivals(webapp.RequestHandler):
    def get(self):
        stop = cgi.escape(self.request.get('stop'))
        use_url = BUS_FEED + "&".join(("command=predictions",
                                       "a=mbta",
                                       "stopId=%s" % stop))
        try:
            xmldoc = get_xml(use_url)
        except Exception:
            logging.warning('Arrivals: failed url: %s' % use_url)
            self.response.out.write(json.dumps([]))
            return

        p = []
        for predictions in xmldoc.getElementsByTagName("predictions"):
            route = short_name(predictions.getAttribute("routeTitle"))
            for direction in predictions.getElementsByTagName("direction"):
                title = direction.getAttribute("title")
                for prediction in direction.getElementsByTagName("prediction"):
                    minutes = int(prediction.getAttribute("minutes"))
                    p.append((minutes,route,title))
        p.sort()
        self.response.out.write(json.dumps(p))


class Routes(webapp.RequestHandler):
    def get(self):
        self.response.out.write(json.dumps(allRoutes()))


class Buses(webapp.RequestHandler):
    cache = {}
    max_refresh = 12

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
        if now - self.timestamp(route) > self.max_refresh:
            """ refresh buses from server every Nsec """

            buses = request_buses(route)
            self.cache[route] = now, buses
            update_predictions(route, buses)

        self.response.out.write(json.dumps(
                [bus.sendable() for bus in self.buses(route).values()]))

class MainPage(webapp.RequestHandler):

    def get(self):
        buses = self.request.get('buses').lower() != "false"
        stops = self.request.get('stops').lower() != "false"
        shading = self.request.get('shading').lower() == "true"

        snap = self.request.get('snap').lower() != "false"
        est = self.request.get('est').lower() != "false"

        all_routes = False

        routes = []
        routes_req = cgi.escape(self.request.get('routes'))
        if routes_req == "all":
            all_routes = True
        elif routes_req:
            routes = routes_req.split(",")

        for route in self.request.get_all("route"):
            routes.append(cgi.escape(route))

        if not routes and not all_routes:
            path = os.path.join(os.path.dirname(__file__), 'chooser.html')

            template_values = {"routes": [{"tag": tag, "title": short_name(title)} for tag, title in allRoutes()]}
        else:
            template_values = {"shading": shading,
                               "all_routes": all_routes,
                               "snap": snap,
                               "est": est,
                               "stops": stops,
                               "buses": buses,
                               "routes": routes}

            path = os.path.join(os.path.dirname(__file__), 'index.html')

        self.response.out.write(template.render(path, template_values))

application = webapp.WSGIApplication([('/', MainPage),
                                      ('/Paths', Paths),
                                      ('/Buses', Buses),
                                      ('/Routes', Routes),
                                      ('/Arrivals', Arrivals),
                                     ], debug=True)

def main():
    run_wsgi_app(application)

if __name__ == "__main__":
    main()
