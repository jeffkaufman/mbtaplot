from __future__ import with_statement
import os
import urllib2
import time
import xml.dom.minidom as minidom
from google.appengine.ext.webapp import template
from google.appengine.ext import webapp
from google.appengine.ext.webapp.util import run_wsgi_app
from xml.sax.saxutils import escape

maps_key='ABQIAAAA5EZP5qCoaub5KG1QSmw1KhQQgZrvOX3gye-SKpRLyNsCw8EkTRTsLF90-utHavKb_PVOhaSa31S44Q'

BUS_FEED="http://webservices.nextbus.com/service/publicXMLFeed?"

class Bus(object):
    def __init__(self, xml_vehicle):
        for attribute in ("dirTag", "heading", "id", "lat", "lon", "routeTag", "secsSinceReport"):
            setattr(self, attribute, xml_vehicle.getAttribute(attribute))

    @property
    def round_heading(self):
        return (int(int(self.heading)/3)*3)%120

class BusRoute(object):
    def __init__(self, route_num):
        self.route_num = route_num

    @property
    def paths(self):
        use_url = BUS_FEED + "&".join(("command=routeConfig",
                                       "a=mbta",
                                       "r=%s" % self.route_num
                                       ))
        usock = urllib2.urlopen(use_url)
        xmldoc = minidom.parse(usock)
        usock.close()

        return [Path(p) for p in xmldoc.getElementsByTagName("path")]

    @property
    def busses(self):        
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

class Point(object):
    def __init__(self, xml_point):
        self.lat = xml_point.getAttribute("lat")
        self.lon = xml_point.getAttribute("lon")

class Path(object):
    def __init__(self, xml_path):
        self.points = [Point(p) for p in xml_path.getElementsByTagName("point")]
    def __getitem__(self, x):
        return self.points[x]

class MainPage(webapp.RequestHandler):
    def get(self):
        route = BusRoute("77")
        template_values = {"busses": route.busses,
                           "paths": route.paths}
        
        path = os.path.join(os.path.dirname(__file__), 'index.html')
        self.response.out.write(template.render(path, template_values))

application = webapp.WSGIApplication(
                                     [('/', MainPage),
                                     ],
                                     debug=True)

def main():
    run_wsgi_app(application)

if __name__ == "__main__":
    main()
