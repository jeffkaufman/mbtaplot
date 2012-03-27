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
import route_table

BUS_FEED="http://webservices.nextbus.com/service/publicXMLFeed?"
SUBWAY_FEED_DIR="http://developer.mbta.com/Data/"
SUBWAY_KEY="http://developer.mbta.com/RT_Archive/RealTimeHeavyRailKeys.csv"

def is_subway(route):
    return route in('Red', 'Orange', 'Blue')

class FailedFetchException(Exception):
    pass

class InvalidRouteException(Exception):
    pass

class InvalidStopException(Exception):
    pass

def get_xml(use_url, refresh=10):
    """ get xml from url, only updating every /refresh/ seconds """

    return get_text(use_url,refresh=refresh,isxml=True)

def get_text(use_url, refresh, isxml=False,
             headers={"Cache-Control": "no-cache,max-age=0",
                      "Pragma": "no-cache"},
             cache=memcache.Client()):
    """
    Request data from a url with caching and possibly with xml parsing
    
    Options:

       refresh: how many seconds to go between cach refreshes
        - use 0 to disable caching
        
       isxml: set to true if we should parse the result

       headers: what headers to use for the request.
        - by default we just disable caching by intermediate services

       cache: python mutable default args trickery / don't set this

    If the fetch fails we return the cached value even if it's too old.
    If we don't have a cached value we raise a FailedFetchException

    Cachine uses memcache.
    """


    cached_val = cache.get(use_url)
    if cached_val:
        result_age, result_val = cached_val
    else:
        result_age, result_val = 1000, None

    if not result_val or time.time()-result_age > refresh:
        logging.info("fetch %s" % use_url)
        
        try:
            result = urlfetch.fetch(url=use_url,
                                    headers=headers)
        except Exception:
            result = None

        if result is not None and result.status_code == 200:
            result_val = result.content
            result_age = time.time()
            cached_val = result_age, result_val

            cache.set(use_url, cached_val, time=refresh)
        else:
            logging.warning("fetch failed status=%s %s" % (
                    result.status_code if result else "result none", use_url))

    if result_val:
        if isxml:
            result_val = minidom.parseString(result_val)

        return result_val, time.time()-result_age
    else:
        raise FailedFetchException("Failed to Fetch %s and didn't have it cached" % use_url)


def short_name(x, 
               short_names = {"Line": "SLM",
                              "701": "CT1",
                              "747": "CT2S",
                              "748": "CT2N",
                              "708" : "CT3",
                              "CT2-South": "CT2S",
                              "CT2-North": "CT2N",
                              }
               ):
    """ For some bus routes the route name has numbers where short
    names are usually used.  For example, 701 for CT1.  Here we
    correct for this. """

    x = str(x).split()[-1]
    return short_names.get(x,x)



class Vehicle(object):

    """ Represents a bus or a subway car

    Knows where it was at one time (t, lat, lon) and where it ought to be
    at another time (pred_t, pred_lat, pred_lon)

    """

    def __init__(self, t, lat, lon, id, dirTag, type, heading=0, preds=None, upcoming_stops=None):
        """ use either make_subway or make_bus instead """

        self.t = t

        # location at time t
        self.lat, self.lon = float(lat), float(lon)

        # predicted location at time pred_t
        if preds is None: # if we don't have predictions yet, just use the current info
            preds = self.t, self.lat, self.lon
        self.pred_t, self.pred_lat, self.pred_lon = preds

        self.id = id
        self.heading = int(heading)
        self.dirTag = dirTag

        if upcoming_stops is None:
            upcoming_stops = []
        self.upcoming_stops = upcoming_stops

        self.type = type

    @staticmethod
    def make_bus(xml_vehicle):
        def ga(a):
            return xml_vehicle.getAttribute(a)

        return Vehicle(t=time.time() - int(ga("secsSinceReport")),
                       lat=ga("lat"),
                       lon=ga("lon"),
                       id=ga("id"),
                       dirTag=ga("dirTag"),
                       heading=ga("heading"),
                       type="bus")

    @staticmethod
    def make_subway(trip, line,
                    wait_i, stop_i, direction_i,
                    wait_j, stop_j, direction_j,
                    upcoming):
        lat, lon = SubStop.get_for(stop_i).loc()
        pred_lat, pred_lon = SubStop.get_for(stop_j).loc()
        now = time.time()
        t = now+wait_i
        pred_t = now+wait_j

        return Vehicle(t=t, lat=lat, lon=lon,
                       preds=(pred_t, pred_lat, pred_lon),
                       id=trip,
                       dirTag=direction_i,
                       upcoming_stops=upcoming,
                       type="subway")

    @property
    def age(self):
        return int(time.time() - self.t + .5)

    @property
    def predAge(self):
        return int(time.time() - self.pred_t + .5)

    @property
    def round_heading(self):
        """ heading needs to be divisible by 3 in order to use the
        current bus icons we're using
        """
        return (int(int(self.heading)/3)*3)

    def time_to_min(self, t):
        """ convert a time to a number of minutes in the future """
        if t-time.time() < 0:
            return -1
        return int((t-time.time())/60)

    def sendable(self, upcoming=False):
        """ a dictionary representing this bus
        
        if upcoming, include stop predictions
        """

        tr = {
            "lat_i": self.pred_lat,
            "lon_i": self.pred_lon,
            "lat_j": self.lat,
            "lon_j": self.lon,
            "id": self.id,
            "dir": self.dirTag,
            "age_i": self.predAge,
            "age_j": self.age,
            "rhead": self.round_heading,
            }

        if upcoming:
            tr["up"] = {}

            if self.type == "bus":

                prev = -100
                for (t,s,d) in sorted(self.upcoming_stops):
                    nt = self.time_to_min(t)

                    # don't predict the past
                    if nt < 0:
                        continue                    

                    # predictions for when the bus turns around or
                    # otherwise changes route shouldn't be displayed
                    if d != self.dirTag:
                        break                

                    if (nt < 200 # we only have icons for the next 200 min
                        and
                        nt-prev >= 2 # don't show bus stops closer together than 2min
                        ):

                        tr["up"][s] = nt #, int(t-time.time())
                        prev = nt

            else: # subway
                for t,s in self.upcoming_stops:
                    if t > 0:
                        tr["up"][s] = int(t/60)

        return tr


class SubStop(object):
    """ a subway stop. """


    def __init__(self, strstop):
        self.route, self.stop,_,_,_,_,_,self.branch,_,_,_,self.stop_desc,_,self.lat,self.lon = strstop.strip().split(',')
        self.lat, self.lon = float(self.lat), float(self.lon)

    @staticmethod
    def get_for(stop):
        """ get the SubStop object for a stop """

        subpaths = request_subpaths()
        for route, substops in subpaths.items():
            for substop in substops:
                if substop.stop == stop:
                    return substop
        raise InvalidStopException("unknown stop %s" % stop)

    def arrivals(self):
        """
        Determine all predicted arrivals here.
        
        Returns [(wait (4: in minutes),
                  route (Red Line),
                  headsign (stop description of trips's last stop: Ashmont / Braintree)), ...]
        
        Sorted, with earlier arrivals sooner in the list.      
        """

        trips = []

        for trip, stop_info in request_subways_literal(self.route).items():
            for wait, stop, direction in stop_info:
                if wait < 0:
                    continue

                if stop == self.stop:
                    _, last_stop, _ = stop_info[-1]
                    headsign = SubStop.get_for(last_stop).stop_desc.replace(" Station","")
                    trips.append((wait/60, self.route + " Line", headsign))

        return trips

    def loc(self):
        return self.lat, self.lon

    def ashmont_stop(self):
        return self.branch == "Ashmont"




def request_subpaths(routes_cache={}):
    """ like request_paths but for subways, all routes at once (cached forever) """

    if not routes_cache:
        for x in get_text(SUBWAY_KEY,refresh=60*60*12)[0].split("\n"):
            if x.startswith("Line,"):
                continue
            try:
                substop = SubStop(x)
            except ValueError:
                continue

            if substop.route not in routes_cache:
                routes_cache[substop.route] = []

            routes_cache[substop.route].append(substop)

    return routes_cache



def request_paths(route_num, path_cache={}):
    # path cache shared between calls
    # never updates path cache
    # returns: directions, stops

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

        path_cache[route_num] = directions, stops

    return path_cache[route_num]

def distance(x1,y1,x2,y2):
    """ technically, euclidian distance is wrong when used on lat/lon.
    For an area as small as the boston area it should be pretty good,
    though """

    return (x1-x2)*(x1-x2) + (y1-y2)*(y1-y2)

def request_predictions(route_num, bus_hash):
    directions, stops = request_paths(route_num)

    vehicle_predictions = {} # bus_id -> best_time
    full_vehicle_predictions = {} # bus_id -> [(stop_id, time)]

    def updatePredictions(xmldoc, doc_age):
        for predictions in xmldoc.getElementsByTagName("predictions"):
            stop = stops[predictions.getAttribute("stopTag")]
            for prediction in predictions.getElementsByTagName("prediction"):
                seconds = 60*int(prediction.getAttribute("minutes")) - doc_age
                vehicle = prediction.getAttribute("vehicle")
                if vehicle not in bus_hash:
                    continue

                if vehicle not in full_vehicle_predictions:
                    full_vehicle_predictions[vehicle] = []
                full_vehicle_predictions[vehicle].append((stop.tag, prediction.getAttribute("dirTag"), seconds))

                if seconds < 60*2:
                    continue

                if vehicle not in vehicle_predictions or seconds < vehicle_predictions[vehicle][0]:
                    vehicle_predictions[vehicle] = seconds, stop.lat, stop.lon
                elif seconds == vehicle_predictions[vehicle][0]:
                    c_lat = bus_hash[vehicle].lat
                    c_lon = bus_hash[vehicle].lon

                    o_seconds, o_lat, o_lon = vehicle_predictions[vehicle]

                    if distance(c_lat, c_lon, stop.lat, stop.lon) < distance(c_lat, c_lon, o_lat, o_lon):
                        vehicle_predictions[vehicle] = seconds, stop.lat, stop.lon

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

    return vehicle_predictions, full_vehicle_predictions


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
        bus = Vehicle.make_bus(vehicle)
        bus_hash[bus.id] = Vehicle.make_bus(vehicle)

    return bus_hash


def update_predictions(route_num, bus_hash):
    def to_time(secs):
        return int(time.time()+secs)

    vehicle_predictions, full_vehicle_predictions = request_predictions(route_num, bus_hash)
    for bus_id, prediction in vehicle_predictions.items():
        secs, stop_lat, stop_lon = prediction
        bus_hash[bus_id].pred_t = to_time(secs)
        bus_hash[bus_id].pred_lat = stop_lat
        bus_hash[bus_id].pred_lon = stop_lon
        bus_hash[bus_id].upcoming_stops = [(to_time(secs), s, dt) 
                                           for (s, dt, secs) 
                                           in full_vehicle_predictions[bus_id]]


def allRoutes():
    use_url = BUS_FEED + "&".join(("command=routeList",
                                       "a=mbta"))

    try:
        xmldoc, doc_age = get_xml(use_url)
    except FailedFetchException:
        logging.warning('allRoutes: failed url: %s' % use_url)
        return []


    allr = []
    allr.extend([("Red", "Red Line"),
                 ("Orange","Orange Line"),
                 ("Blue","Blue Line")])

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


    def for_bus(self,route):
        directions, stops = request_paths(route)

        #path_structure = [[{"lat": point.lat, "lon": point.lon} for point in path] for path in paths]

        direction_structure = {}
        for direction in directions.values():
            direction_structure[direction.tag] = [[{"lat": stop.lat, "lon": stop.lon} for stop in direction.stops]]

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
                p = SubStop.get_for(stop).arrivals()
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
                tag = predictions.getAttribute("routeTag")
                for direction in predictions.getElementsByTagName("direction"):
                    title = direction.getAttribute("title")
                    for prediction in direction.getElementsByTagName("prediction"):
                        minutes = int(prediction.getAttribute("minutes")) - int(doc_age/60)
                        if minutes < 0:
                            continue
                        p.append((minutes,route,title,tag))

        p.sort()
        self.response.out.write(json.dumps(["none" if not p else "ok", p,stop]))


class Routes(webapp.RequestHandler):
    def get(self):
        self.response.out.write(json.dumps(allRoutes()))

def request_subways_literal(line):
    """ request current subway info, don't do much processing """

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
        now = datetime.datetime.now(tz_boston)
        now_local = now.strftime("%H:%M:%S")
        t_now = to_sec(now_local, "NA")

        #if (now.month > 3 or (now.month == 3 and now.day >= 2)) and now.month < 11:
        #    # DST
        #    t_now += 60*60
        
        t_now += 60*60 # DST

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
        if SubStop.get_for(stop).ashmont_stop():
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

        subways[trip] = Vehicle.make_subway(trip, route,
                                            wait_i, stop_i, direction_i,
                                            wait_j, stop_j, direction_j,
                                            [(wait_n, stop_n) for (wait_n, stop_n, dir_n) in stop_info])
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
        bus_id = cgi.escape(self.request.get('bus_id'))

        now = time.time()
        if now - self.timestamp(route) > self.max_refresh or is_subway(route):
            """ refresh buses from server every Nsec; subway caching is done in get_text """

            if is_subway(route):
                subways = request_subways(route)
                self.cache[route] = now, subways
            else:
                buses = request_buses(route)
                self.cache[route] = now, buses
                update_predictions(route, buses)

        if bus_id:
            self.response.out.write(json.dumps([self.buses(route)[bus_id].sendable(upcoming=True)]))
        else:
            self.response.out.write(json.dumps(
                    [bus.sendable() for bus in self.buses(route).values()]))

class Subways(webapp.RequestHandler):
    def get(self):
        initial_zoom, initial_lat, initial_lon, should_recenter = interpret_loc_info(
            self.request)
        template_values = {"routes": ["Red", "Orange", "Blue"],
                           "buses": True,
                           "stops": True,
                           "shading": False,
                           "est": True,
                           "snap": False,
                           "initial_zoom": initial_zoom,
                           "initial_lat": initial_lat,
                           "initial_lon": initial_lon,
                           "should_recenter": should_recenter
                           }
        path = os.path.join(os.path.dirname(__file__), 'index.html')
        self.response.out.write(template.render(path, template_values))

def interpret_loc_info(request):
    initial_zoom, initial_lat, initial_lon = 11, 42.3, -71.1
    should_recenter = "true"

    if request.get("ll"):
        try:
            lat, lon = request.get("ll").split(",")
            lat, lon = float(lat), float(lon)
            initial_lat, initial_lon = lat, lon
            should_recenter = "false"
        except Exception:
            pass

    if request.get("z"):
        try:
            initial_zoom = int(request.get("z"))
        except Exception:
            pass

    return initial_zoom, initial_lat, initial_lon, should_recenter

class RoutesInView(webapp.RequestHandler):
    def get(self):

        try:
            north = float(self.request.get("north"))
            east = float(self.request.get("east"))
            south = float(self.request.get("south"))
            west = float(self.request.get("west"))
        except ValueError:
            self.response.out.write(json.dumps([]))
            return

        def isin(lat, lon):
            return south < lat < north and west < lon < east

        routes = set()

        for route, stop, lat, lon in route_table.table:
            if isin(lat,lon):
                routes.add(route)

        self.response.out.write(json.dumps(list(sorted(routes))))

class Intro(webapp.RequestHandler):
    def get(self):
        path = os.path.join(os.path.dirname(__file__), 'intro.html')
        self.response.out.write(template.render(path, {}))

class MainPage(webapp.RequestHandler):

    def get(self):
        buses = self.request.get('buses').lower() != "false"
        stops = self.request.get('stops').lower() != "false"
        shading = self.request.get('shading').lower() == "true"

        snap = self.request.get('snap').lower() != "false"
        est = self.request.get('est').lower() != "false"

        initial_zoom, initial_lat, initial_lon, should_recenter = interpret_loc_info(
            self.request)
        routes = []
        routes_req = cgi.escape(self.request.get('routes'))
        if routes_req:
            routes = routes_req.split(",")

        for route in self.request.get_all("route"):
            routes.append(cgi.escape(route))

        template_values = {"shading": shading,
                           "snap": snap,
                           "est": est,
                           "stops": stops,
                           "buses": buses,
                           "routes": routes,
                           "initial_lat": initial_lat,
                           "initial_lon": initial_lon,
                           "initial_zoom": initial_zoom,
                           "should_recenter": should_recenter}
        
        path = os.path.join(os.path.dirname(__file__), 'index.html')

        self.response.out.write(template.render(path, template_values))

application = webapp.WSGIApplication([('/', MainPage),
                                      ('/intro', Intro),
                                      ('/subways', Subways),
                                      ('/Paths', Paths),
                                      ('/RoutesInView', RoutesInView),
                                      ('/Buses', Buses),
                                      ('/Routes', Routes),
                                      ('/Arrivals', Arrivals),
                                     ], debug=True)

def main():
    run_wsgi_app(application)

if __name__ == "__main__":
    main()
