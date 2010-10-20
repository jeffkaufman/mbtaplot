from __future__ import with_statement
import sys
import os
import time
import cgi
import logging
import xml.dom.minidom as minidom
from google.appengine.ext.webapp import template
from google.appengine.ext import webapp
from google.appengine.ext.webapp.util import run_wsgi_app
from xml.sax.saxutils import escape
import simplejson as json
import dateutil.tz
import datetime
from google.appengine.api import urlfetch
from google.appengine.api import memcache

BUS_FEED="http://webservices.nextbus.com/service/publicXMLFeed?"
SUBWAY_FEED_DIR="http://developer.mbta.com/Data/"
SUBWAY_KEY="http://developer.mbta.com/RT_Archive/RealTimeHeavyRailKeys.csv"

def is_subway(route):
    return route in('Red', 'Orange', 'Blue')

def get_xml(use_url, refresh=10):
    """ only update every /refresh/ seconds """

    return get_text(use_url,refresh=refresh,isxml=True)

class FailedFetchException(Exception):
    pass

class InvalidRouteException(Exception):
    pass

class InvalidStopException(Exception):
    pass

def get_text(use_url,refresh,isxml=False,cache=memcache.Client()):
    cached_val = cache.get(use_url)
    if cached_val:
        result_age, result_val = cached_val
    else:
        result_age, result_val = 1000, None

    if not result_val or time.time()-result_age > refresh:
        logging.info("fetch %s" % use_url)
        result = urlfetch.fetch(url=use_url,
                                headers={"Cache-Control": "no-cache,max-age=0",
                                         "Pragma": "no-cache"})
        if result.status_code == 200:
            result_val = result.content
            result_age = time.time()
            cached_val = result_age, result_val

            cache.set(use_url, cached_val, time=refresh)
        else:
            logging.warning("fetch failed status=%s %s" % (result.status_code, use_url))

    if result_val:
        if isxml:
            result_val = minidom.parseString(result_val)

        return result_val, time.time()-result_age
    else:
        raise FailedFetchException("Failed to Fetch %s and didn't have it cached" % use_url)

class DebugText(webapp.RequestHandler):
    def get(self):
        self.response.out.write(get_text('http://developer.mbta.com/Data/Red.txt',refresh=2)[0].replace("\n","<br>"))

short_names = {"Line": "SLM",
               "701": "CT1",
               "747": "CT2S",
               "748": "CT2N",
               "708" : "CT3",
               "CT2-South": "CT2S",
               "CT2-North": "CT2N",
               }

def short_name(x):
    x = str(x).split()[-1]
    return short_names.get(x,x)

def get_substop_arrivals(stop):
    substop = get_substop_info(stop)
    trips = []

    for route in request_subpaths():
        for trip, stop_info in request_subways_literal(route).items():
            stop_info.sort()
            for wait, stopn, direction in stop_info:
                if stopn == stop:
                    last_stop_wait, last_stop, last_stop_direction = stop_info[-1]
                    substop_target = get_substop_info(last_stop)
                    stop_desc_target = substop_target.stop_desc
                    stop_desc_target = stop_desc_target.replace(" Station","")
                    if wait < 0:
                        continue
                    trips.append((wait/60, route + " Line", stop_desc_target))
    return trips

def get_substop_info(stop):
    subpaths = request_subpaths()
    for route, substops in subpaths.items():
        for substop in substops:
            if substop.stop == stop:
                return substop
    raise InvalidStopException("unknown stop %s" % stop)

def subway_station_loc(route, stop):
    subpaths = request_subpaths()
    try:
        substops = subpaths[route]
    except ValueError:
        raise InvalidRouteException("%s not found" % route)

    for substop in substops:
        if substop.stop == stop:
            return substop.lat, substop.lon

    raise InvalidStopException("%s.%s not found" % (route, stop))

class Bus(object):
    def __init__(self, xml_vehicle):
        if not xml_vehicle:
            return

        for attribute in ("dirTag", "heading", "id", "lat", "lon", "routeTag", "secsSinceReport"):
            setattr(self, attribute, xml_vehicle.getAttribute(attribute))
        self.t = time.time() - int(self.secsSinceReport)
        del self.secsSinceReport

        self.lat = float(self.lat)
        self.lon = float(self.lon)

        self.pred_t = self.t
        self.pred_lat = self.lat
        self.pred_lon = self.lon

        self.type ="bus"


    @staticmethod
    def make_subway(trip, line,
                    wait_i, stop_i, direction_i,
                    wait_j, stop_j, direction_j):
        s = Bus(None)
        s.lat, s.lon = subway_station_loc(line, stop_i)
        s.pred_lat, s.pred_lon = subway_station_loc(line, stop_j)
        now = time.time()
        s.t = now+wait_i
        s.pred_t = now+wait_j
        s.dirTag = direction_i
        s.heading = 0
        s.id = trip
        s.routeTag = line

        s.type = "subway"

        return s


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
            "age_i": int(self.predAge+.5),
            "age_j": int(self.age+.5),
            "rhead": self.round_heading,
            }


class SubStop(object):
    def __init__(self, strstop):
        self.route, self.stop,_,_,_,_,_,self.branch,_,_,_,self.stop_desc,_,self.lat,self.lon = strstop.strip().split(',')
        self.lat, self.lon = float(self.lat), float(self.lon)


def request_subpaths(routes={}):
    """ like request_paths but for subways, all routes at once"""

    if not routes:
        for x in get_text(SUBWAY_KEY,refresh=60*60*12)[0].split("\n"):
            if x.startswith("Line,"):
                continue
            try:
                substop = SubStop(x)
            except ValueError:
                continue

            if substop.route not in routes:
                routes[substop.route] = []

            routes[substop.route].append(substop)

    return routes





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
            xmldoc, doc_age = get_xml(use_url)
        except FailedFetchException:
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

    def updatePredictions(xmldoc, doc_age):
        for predictions in xmldoc.getElementsByTagName("predictions"):
            stop = stops[predictions.getAttribute("stopTag")]
            for prediction in predictions.getElementsByTagName("prediction"):
                minutes = int(prediction.getAttribute("minutes")) - int(doc_age/60)
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
            xmldoc, doc_age = get_xml(use_url, refresh=200)
        except FailedFetchException:
            logging.warning('request_predictions: failed url: %s' % use_url)
            return

        updatePredictions(xmldoc, doc_age)

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
        xmldoc, doc_age = get_xml(use_url)
    except FailedFetchException:
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
        xmldoc, doc_age = get_xml(use_url)
    except FailedFetchException:
        logging.warning('allRoutes: failed url: %s' % use_url)
        return []


    allr = []
    allr.extend([("Red", "Red"),("Orange","Orng"), ("Blue","Blue")])
    allr.extend([[route.getAttribute("tag"), route.getAttribute("title")]
                 for route in xmldoc.getElementsByTagName("route")])
    return allr

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

def is_ashmont_stop(stop, astops=[]):
    if not astops:
        for substop in request_subpaths()["Red"]:
            if substop.branch == "Ashmont":
                astops.append(substop.stop)
    return stop in astops

class Paths(webapp.RequestHandler):
    cache = {}


    def for_bus(self,route):
        paths, directions, stops = request_paths(route)

        #path_structure = [[{"lat": point.lat, "lon": point.lon} for point in path] for path in paths]

        direction_structure = {}
        for direction in directions:
            direction_structure[direction] = [[{"lat": point.lat, "lon": point.lon} for point in path]
                                              for path in paths
                                              if path.is_for(direction)]

        stop_structure = [{"lat": stop.lat, "lon": stop.lon, "title" : stop.title, "tag": stop.tag}
                          for stop in stops.values()]

        self.cache[route] = json.dumps({
                "directions": direction_structure,
                "stops": stop_structure})


    def for_subway(self,route):
        substops = request_subpaths()[route]

        def branch_in_route(branch, direction):
            if direction == -1:
                return True

            if branch == "Ashmont":
                return direction == 1
            elif branch == "Braintree":
                return direction == 0
            return True

        directions = [-1,0]
        if route == "Red":
            directions.append(1)

        direction_structure = {}
        for direction in directions:
            direction_structure[direction] = [[{"lat": substop.lat, "lon": substop.lon, "title": substop.stop_desc, "tag": substop.stop}
                                               for substop in substops
                                               if branch_in_route(substop.branch, direction)]]

        stop_structure = direction_structure[-1][-0]
        del direction_structure[-1]

        self.cache[route] = json.dumps({
                "directions": direction_structure,
                "stops": stop_structure})

    def get(self):
        route = cgi.escape(self.request.get('route'))
        if route not in self.cache:
            if is_subway(route):
                self.for_subway(route)
            else:
                self.for_bus(route)

        self.response.out.write(self.cache[route])


class Arrivals(webapp.RequestHandler):
    def get(self):
        stop = cgi.escape(self.request.get('stop'))

        if stop.isalpha():
            try:
                p = get_substop_arrivals(stop)
            except (InvalidRouteException, InvalidStopException):
                self.response.out.write(json.dumps(["error", []]))
                return
        else:
            use_url = BUS_FEED + "&".join(("command=predictions",
                                           "a=mbta",
                                           "stopId=%s" % stop))
            try:
                xmldoc, doc_age = get_xml(use_url)
            except FailedFetchException:
                logging.warning('Arrivals: failed url: %s' % use_url)
                self.response.out.write(json.dumps(["error", []]))
                return

            p = []
            for predictions in xmldoc.getElementsByTagName("predictions"):
                route = short_name(predictions.getAttribute("routeTitle"))
                for direction in predictions.getElementsByTagName("direction"):
                    title = direction.getAttribute("title")
                    for prediction in direction.getElementsByTagName("prediction"):
                        minutes = int(prediction.getAttribute("minutes")) - int(doc_age/60)
                        if minutes < 0:
                            continue
                        p.append((minutes,route,title))

        p.sort()
        self.response.out.write(json.dumps(["none" if not p else "ok", p,stop]))


class Routes(webapp.RequestHandler):
    def get(self):
        self.response.out.write(json.dumps(allRoutes()))

def request_subways_literal(line):
    tz_boston = dateutil.tz.tzstr('EST5EDT')

    use_url = SUBWAY_FEED_DIR + line + ".txt"

    try:
        text = get_text(use_url,refresh=20)[0]
    except FailedFetchException:
        logging.warning('request_subways: failed url: %s' % use_url)
        return {}

    trips = {}
    for x in text.split("\n"):
        try:
            _, n, stop, source, date, t, ampm, wait, rev, direction = x.replace(",","").split()
        except ValueError:
            sys.stderr.write(x+"\n")
            continue

        if rev != "Revenue":
            continue

        def to_sec(t,ampm):
            t_hr, t_min, t_sec = t.split(':')
            if t_hr == "12":
                if ampm == "AM":
                    t_hr = "0"
            elif ampm=="PM":
                t_hr = int(t_hr)+12
            t = int(t_hr)*60*60+int(t_min)*60+int(t_sec)
            return t

        t_then = to_sec(t,ampm)
        now_local = datetime.datetime.now(tz_boston).strftime("%H:%M:%S")
        t_now = to_sec(now_local, "NA")

        if n not in trips:
            trips[n] = []

        wait = t_then - t_now

        #logging.info("t_then: %s; t_now: %s; t: %s; wait: %s, now_local: %s" % (t_then, t_now, t, wait, now_local))

        trips[n].append((wait, stop, direction))

    for trip, stop_info in trips.items():
        stop_info.sort()

        while len(stop_info) > 2 and stop_info[1][0] < 0:
            del stop_info[0] # only have one negative wait at a time

    return trips

def visited_ashmont_stop(stop_info):
    for wait, stop, direction in stop_info:
        if is_ashmont_stop(stop):
            return True
    return False

def request_subways(route):
    subways = {}
    for trip, stop_info in request_subways_literal(route).items():
        if not stop_info:
            continue

        wait_j,stop_j,direction_j = stop_info[0]

        if len(stop_info) > 1:
            wait_i,stop_i,direction_i = stop_info[1]
        else:
            wait_i,stop_i,direction_i = stop_info[0]

        # the direction/branch flag is often set wrong on trains
        if route == "Red":
            if visited_ashmont_stop(stop_info):
                direction_i = direction_i = "1" # mark as ashmont line
            else:
                direction_i = direction_i = "0" # mark as braintree

        subways[trip] = Bus.make_subway(trip, route,
                                        wait_i, stop_i, direction_i,
                                        wait_j, stop_j, direction_j)

    return subways


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
        if now - self.timestamp(route) > self.max_refresh or is_subway(route):
            """ refresh buses from server every Nsec; subway caching is done in get_text """

            if is_subway(route):
                subways =request_subways(route)
                self.cache[route] = now, subways
            else:
                buses = request_buses(route)
                self.cache[route] = now, buses
                update_predictions(route, buses)

        self.response.out.write(json.dumps(
                [bus.sendable() for bus in self.buses(route).values()]))

class Subways(webapp.RequestHandler):
    def get(self):
        template_values = {"routes": ["Red", "Orange", "Blue"],
                           "buses": True,
                           "stops": True,
                           "shading": False,
                           "est": True,
                           "snap": False,
                           }
        path = os.path.join(os.path.dirname(__file__), 'index.html')
        self.response.out.write(template.render(path, template_values))

class MainPage(webapp.RequestHandler):

    def get(self):
        buses = self.request.get('buses').lower() != "false"
        stops = self.request.get('stops').lower() != "false"
        shading = self.request.get('shading').lower() == "true"

        snap = self.request.get('snap').lower() != "false"
        est = self.request.get('est').lower() != "false"

        routes = []
        routes_req = cgi.escape(self.request.get('routes'))
        if routes_req:
            routes = routes_req.split(",")

        for route in self.request.get_all("route"):
            routes.append(cgi.escape(route))

        if not routes:
            path = os.path.join(os.path.dirname(__file__), 'chooser.html')

            template_values = {"routes": [{"tag": tag, "title": short_name(title)} for tag, title in allRoutes()]}
        else:
            template_values = {"shading": shading,
                               "snap": snap,
                               "est": est,
                               "stops": stops,
                               "buses": buses,
                               "routes": routes}

            path = os.path.join(os.path.dirname(__file__), 'index.html')

        self.response.out.write(template.render(path, template_values))

application = webapp.WSGIApplication([('/', MainPage),
                                      ('/subways', Subways),
                                      ('/Paths', Paths),
                                      ('/Buses', Buses),
                                      ('/Routes', Routes),
                                      ('/Arrivals', Arrivals),
                                      ('/DebugText', DebugText),
                                     ], debug=True)

def main():
    run_wsgi_app(application)

if __name__ == "__main__":
    main()
