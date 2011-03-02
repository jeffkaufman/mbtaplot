curl 'http://webservices.nextbus.com/service/publicXMLFeed?command=routeList&a=mbta' | \
      grep "route tag" | awk -F\" '{print $2}' | while read route
do
  curl 'http://webservices.nextbus.com/service/publicXMLFeed?command=routeConfig&a=mba&r='${route} | \
       grep "<stop tag" | grep lat | grep lon | while read line
  do 
    echo "s $route $line"; 
  done

  curl 'http://webservices.nextbus.com/service/publicXMLFeed?command=routeConfig&a=mbta&r='${route} | \
       grep "<stop tag" | grep -v lat | grep -v lon | while read line
  do 
    echo "d $route $line"; 
  done

done > ugly_raw_route_table.txt

curl http://developer.mbta.com/RT_Archive/RealTimeHeavyRailKeys.csv | \
     grep -v ^Line | awk '-F,' '{print $1,$2,$14,$15}' > ugly_subway_route_table.txt

python process_route_table.py ugly_raw_route_table.txt ugly_subway_route_table.txt > route_table.py
